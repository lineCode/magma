"""
Copyright (c) 2018-present, Facebook, Inc.
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. An additional grant
of patent rights can be found in the PATENTS file in the same directory.
"""

import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock

import warnings
from lte.protos.mconfig.mconfigs_pb2 import PipelineD
from magma.pipelined.app.meter import MeterController
from magma.pipelined.bridge_util import BridgeTools
from magma.pipelined.tests.app.packet_builder import IPPacketBuilder
from magma.pipelined.tests.app.packet_injector import ScapyPacketInjector
from magma.pipelined.tests.app.start_pipelined import PipelinedController, \
    TestSetup
from magma.pipelined.tests.app.subscriber import SubContextConfig
from magma.pipelined.tests.app.table_isolation import RyuDirectTableIsolator, \
    RyuForwardFlowArgsBuilder
from magma.pipelined.tests.pipelined_test_util import create_service_manager, \
    start_ryu_app_thread, stop_ryu_app_thread, wait_after_send
from ryu.lib import hub


@unittest.skip("Temporarily disabled T33621957")
class MeterStatsTest(unittest.TestCase):
    BRIDGE = 'testing_br'
    MAC_DEST = "5e:cc:cc:b1:49:4b"

    @classmethod
    def setUpClass(cls):
        super(MeterStatsTest, cls).setUpClass()
        warnings.simplefilter('ignore')
        service_manager = create_service_manager([PipelineD.METERING])
        cls._tbl_num = service_manager.get_table_num(MeterController.APP_NAME)

        meter_ref = Future()
        testing_controller_reference = Future()
        meter_stat_ref = Future()

        def mock_thread_safe(cmd, body):
            cmd(body)
        loop_mock = MagicMock()
        loop_mock.call_soon_threadsafe = mock_thread_safe

        test_setup = TestSetup(
            apps=[PipelinedController.Meter,
                  PipelinedController.Testing,
                  PipelinedController.MeterStats],
            references={
                PipelinedController.Meter: meter_ref,
                PipelinedController.Testing: testing_controller_reference,
                PipelinedController.MeterStats: meter_stat_ref
            },
            config={
                'bridge_name': cls.BRIDGE,
                'bridge_ip_address': '192.168.128.1',
                'meter': {'poll_interval': -1, 'enabled': True},
            },
            mconfig={},
            loop=loop_mock,
            service_manager=service_manager,
            integ_test=False,
            rpc_stubs={'metering_cloud': MagicMock()}
        )
        BridgeTools.create_bridge(cls.BRIDGE, cls.BRIDGE)

        cls.thread = start_ryu_app_thread(test_setup)

        cls.meter_controller = meter_ref.result()
        cls.stats_controller = meter_stat_ref.result()
        cls.testing_controller = testing_controller_reference.result()

        cls.stats_controller._sync_stats = MagicMock()

    @classmethod
    def tearDownClass(cls):
        stop_ryu_app_thread(cls.thread)
        BridgeTools.destroy_bridge(cls.BRIDGE)

    def test_meter_stats(self):
        """
        Test metering stats by sending uplink and downlink packets from 2
        subscribers and making sure the correct statistics are sent to the cloud
        """
        # Set up subscribers
        sub1 = SubContextConfig('IMSI001010000000013', '192.168.128.74',
                                self._tbl_num)
        sub2 = SubContextConfig('IMSI001010000000014', '192.168.128.75',
                                self._tbl_num)

        isolator1 = RyuDirectTableIsolator(
            RyuForwardFlowArgsBuilder.from_subscriber(sub1)
                                     .build_requests(),
            self.testing_controller,
        )
        isolator2 = RyuDirectTableIsolator(
            RyuForwardFlowArgsBuilder.from_subscriber(sub2)
                                     .build_requests(),
            self.testing_controller,
        )

        # Set up packets
        pkt_sender = ScapyPacketInjector(self.BRIDGE)
        pkt_count = 4  # send each packet 4 times
        # sub1: 2 uplink pkts, 1 downlink
        # sub2: 1 uplink pkt
        packets = [
            self._make_default_pkt('45.10.0.1', sub1.ip),
            self._make_default_pkt('45.10.0.2', sub1.ip),
            self._make_default_pkt(sub1.ip, '45.10.0.1'),
            self._make_default_pkt('45.10.0.3', sub2.ip),
        ]
        pkt_size = len(packets[0])

        # Send packets through pipeline and wait
        with isolator1, isolator2:
            for pkt in packets:
                pkt_sender.send(pkt, pkt_count)
            wait_after_send(self.testing_controller)
            hub.sleep(3)

        # manually run stats update
        for _, datapath in self.stats_controller.dpset.get_all():
            self.stats_controller._poll_stats(datapath)

        # retrieve the latest statistics
        stats_queue = hub.Queue()

        def mock_sync_stats(*args, **kwargs):
            stats_queue.put(args[0])
        self.stats_controller._sync_stats = MagicMock(
            side_effect=mock_sync_stats,
        )
        stats_dict = stats_queue.get(block=True, timeout=5)

        self.assertTrue(sub1.imsi in stats_dict)
        self.assertTrue(sub2.imsi in stats_dict)
        # sub1: 2 uplink * 4 packets each - 1 packet in = 3
        #       1 downlink * 4 packets each - 1 packet in = 7
        self._assert_stats_match(stats_dict[sub1.imsi], 7, 3, pkt_size)
        # sub2: 1 uplink * 4 packets each - 1 packet in = 3
        self._assert_stats_match(stats_dict[sub2.imsi], 3, 0, pkt_size)

    def _make_default_pkt(self, dst, src):
        return IPPacketBuilder()\
            .set_ip_layer(dst, src)\
            .set_ether_layer(self.MAC_DEST, "00:00:00:00:00:00")\
            .build()

    def _assert_stats_match(self, stats, pkts_uplink, pkts_downlink, pkt_size):
        self.assertEqual(stats.pkts_tx, pkts_uplink)
        self.assertEqual(stats.pkts_rx, pkts_downlink)
        self.assertEqual(stats.bytes_tx, pkts_uplink * pkt_size)
        self.assertEqual(stats.bytes_rx, pkts_downlink * pkt_size)


if __name__ == "__main__":
    unittest.main()
