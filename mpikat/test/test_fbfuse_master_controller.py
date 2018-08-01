"""
Copyright (c) 2018 Ewan Barr <ebarr@mpifr-bonn.mpg.de>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import unittest
import mock
import signal
import logging
import time
import sys
import importlib
import re
import ipaddress
from urllib2 import urlopen, URLError
from StringIO import StringIO
from tornado.ioloop import IOLoop
from tornado.gen import coroutine, Return, sleep
from tornado.testing import AsyncTestCase, gen_test
from katpoint import Antenna, Target
from katcp import AsyncReply
from katcp.testutils import mock_req, handle_mock_req
import mpikat
from mpikat import (
    FbfMasterController,
    FbfProductController,
    FbfWorkerWrapper
    )
from mpikat.katportalclient_wrapper import KatportalClientWrapper
from mpikat.test.utils import MockFbfConfigurationAuthority
from mpikat.ip_manager import ContiguousIpRange, ip_range_from_stream

root_logger = logging.getLogger('')
root_logger.setLevel(logging.CRITICAL)


def type_converter(value):
    try: return int(value)
    except: pass
    try: return float(value)
    except: pass
    return value

class MockKatportalClientWrapper(mock.Mock):
    @coroutine
    def get_observer_string(self, antenna):
        if re.match("^[mM][0-9]{3}$", antenna):
            raise Return("{}, -30:42:39.8, 21:26:38.0, 1035.0, 13.5".format(antenna))
        else:
            raise SensorNotFoundError("No antenna named {}".format(antenna))

    @coroutine
    def get_antenna_feng_id_map(self, instrument_name, antennas):
        ant_feng_map = {antenna:ii for ii,antenna in enumerate(antennas)}
        raise Return(ant_feng_map)

    @coroutine
    def get_bandwidth(self, stream):
        raise Return(856e6)

    @coroutine
    def get_cfreq(self, stream):
        raise Return(1.28e9)

    @coroutine
    def get_sideband(self, stream):
        raise Return("upper")

    @coroutine
    def gey_sync_epoch(self):
        raise Return(1532530856)

    @coroutine
    def get_itrf_reference(self):
        raise Return((5109318.841, 2006836.367, -3238921.775))


class TestFbfMasterController(AsyncTestCase):
    DEFAULT_STREAMS = ('{"cam.http": {"camdata": "http://10.8.67.235/api/client/1"}, '
        '"cbf.antenna_channelised_voltage": {"i0.antenna-channelised-voltage": '
        '"spead://239.2.1.150+15:7148"}}')
    DEFAULT_NCHANS = 4096
    DEFAULT_ANTENNAS = 'm007,m008'

    def setUp(self):
        super(TestFbfMasterController, self).setUp()
        self.server = FbfMasterController('127.0.0.1', 0, dummy=True)
        self.server._katportal_wrapper_type = MockKatportalClientWrapper
        self.server.start()

    def tearDown(self):
        super(TestFbfMasterController, self).tearDown()

    def _add_n_servers(self, n):
        base_ip = ipaddress.ip_address(u'192.168.1.150')
        for ii in range(n):
            self.server._server_pool.add(str(base_ip+ii), 5000)

    @coroutine
    def _configure_helper(self, product_name, antennas, nchans, streams_json, proxy_name):
        #Patching isn't working here for some reason (maybe pathing?), the
        #hack solution is to manually switch to the Mock for the portal
        #client. TODO: Fix the structure of the code so that this can be
        #patched properly
        #Test that a valid configure call goes through
        #mpikat.KatportalClientWrapper = MockKatportalClientWrapper
        req = mock_req('configure', product_name, antennas, nchans, streams_json, proxy_name)
        reply,informs = yield handle_mock_req(self.server, req)
        #mpikat.KatportalClientWrapper = KatportalClientWrapper
        raise Return((reply, informs))

    @coroutine
    def _get_sensor_reading(self, sensor_name):
        req = mock_req('sensor-value', sensor_name)
        reply,informs = yield handle_mock_req(self.server, req)
        self.assertTrue(reply.reply_ok(), msg=reply)
        status, value = informs[0].arguments[-2:]
        value = type_converter(value)
        raise Return((status, value))

    @coroutine
    def _check_sensor_value(self, sensor_name, expected_value, expected_status='nominal', tolerance=None):
        #Test that the products sensor has been updated
        status, value = yield self._get_sensor_reading(sensor_name)
        value = type_converter(value)
        self.assertEqual(status, expected_status)
        if not tolerance:
            self.assertEqual(value, expected_value)
        else:
            max_value = value + value*tolerance
            min_value = value - value*tolerance
            self.assertTrue((value<=max_value) and (value>=min_value))

    @coroutine
    def _check_sensor_exists(self, sensor_name):
        #Test that the products sensor has been updated
        req = mock_req('sensor-list', sensor_name)
        reply,informs = yield handle_mock_req(self.server, req)
        raise Return(reply.reply_ok())

    @coroutine
    def _send_request_expect_ok(self, request_name, *args):
        if request_name == 'configure':
            reply, informs = yield self._configure_helper(*args)
        else:
            reply,informs = yield handle_mock_req(self.server, mock_req(request_name, *args))
        self.assertTrue(reply.reply_ok(), msg=reply)
        raise Return((reply, informs))

    @coroutine
    def _send_request_expect_fail(self, request_name, *args):
        if request_name == 'configure':
            reply, informs = yield self._configure_helper(*args)
        else:
            reply,informs = yield handle_mock_req(self.server, mock_req(request_name, *args))
        self.assertFalse(reply.reply_ok(), msg=reply)
        raise Return((reply, informs))

    @gen_test
    def test_product_lookup_errors(self):
        #Test that calls that require products fail if not configured
        yield self._send_request_expect_fail('capture-start', 'test')
        yield self._send_request_expect_fail('capture-stop', 'test')
        yield self._send_request_expect_fail('provision-beams', 'test')
        yield self._send_request_expect_fail('reset-beams', 'test')
        yield self._send_request_expect_fail('deconfigure', 'test')
        yield self._send_request_expect_fail('set-default-target-configuration', 'test', '')
        yield self._send_request_expect_fail('set-default-sb-configuration', 'test', '')
        yield self._send_request_expect_fail('add-beam', 'test', '')
        yield self._send_request_expect_fail('add-tiling', 'test', '', 0, 0, 0, 0)
        yield self._send_request_expect_fail('configure-coherent-beams', 'test', 0, '', 0, 0)
        yield self._send_request_expect_fail('configure-incoherent-beam', 'test', '', 0, 0)

    @gen_test
    def test_configure_start_stop_deconfigure(self):
        #Patching isn't working here for some reason (maybe pathing?)
        #hack solution is to manually switch to the Mock for the portal
        #client. TODO: Fix the structure of the code so that this can be
        #patched properly
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        product_state_sensor = '{}.state'.format(product_name)
        yield self._send_request_expect_ok('configure', product_name, self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        yield self._check_sensor_value('products', product_name)
        yield self._check_sensor_value(product_state_sensor, FbfProductController.IDLE)
        yield self._send_request_expect_ok('provision-beams', product_name)
        # after provision beams we need to wait on the system to get into a ready state
        product = self.server._products[product_name]
        while True:
            yield sleep(0.5)
            if product.ready: break
        yield self._check_sensor_value(product_state_sensor, FbfProductController.READY)
        yield self._send_request_expect_ok('capture-start', product_name)
        yield self._check_sensor_value(product_state_sensor, FbfProductController.CAPTURING)
        yield self._send_request_expect_ok('capture-stop', product_name)
        yield self._send_request_expect_ok('deconfigure', product_name)
        self.assertEqual(self.server._products, {})
        has_sensor = yield self._check_sensor_exists(product_state_sensor)
        self.assertFalse(has_sensor)

    @gen_test
    def test_configure_same_product(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        yield self._send_request_expect_ok('configure', product_name, self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        yield self._send_request_expect_fail('configure', product_name, self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)

    @gen_test
    def test_configure_no_antennas(self):
        yield self._send_request_expect_fail('configure', 'test_product', '', self.DEFAULT_NCHANS,
            self.DEFAULT_STREAMS, 'FBFUSE_test')

    @gen_test
    def test_configure_bad_antennas(self):
        yield self._send_request_expect_fail('configure', 'test_product', 'NotAnAntenna',
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, 'FBFUSE_test')

    @gen_test
    def test_configure_bad_n_channels(self):
        yield self._send_request_expect_fail('configure', 'test_product', self.DEFAULT_ANTENNAS,
            4097, self.DEFAULT_STREAMS, 'FBFUSE_test')

    @gen_test
    def test_configure_bad_streams(self):
        yield self._send_request_expect_fail('configure', 'test_product', self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, '{}', 'FBFUSE_test')

    @gen_test
    def test_capture_start_while_stopping(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        product_state_sensor = '{}.state'.format(product_name)
        yield self._send_request_expect_ok('configure', product_name, self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        product = self.server._products[product_name]
        product._state_sensor.set_value(FbfProductController.STOPPING)
        yield self._send_request_expect_fail('capture-start', product_name)

    @gen_test
    def test_register_deregister_worker_servers(self):
        hostname = '127.0.0.1'
        port = 10000
        yield self._send_request_expect_ok('register-worker-server', hostname, port)
        server = self.server._server_pool.available()[-1]
        self.assertEqual(server.hostname, hostname)
        self.assertEqual(server.port, port)
        other = FbfWorkerWrapper(hostname, port)
        self.assertEqual(server, other)
        self.assertIn(other, self.server._server_pool.available())
        reply, informs = yield self._send_request_expect_ok('worker-server-list')
        self.assertEqual(int(reply.arguments[1]), 1)
        self.assertEqual(informs[0].arguments[0], "{} free".format(server))
        #try adding the same server again (should work)
        yield self._send_request_expect_ok('register-worker-server', hostname, port)
        yield self._send_request_expect_ok('deregister-worker-server', hostname, port)
        self.assertEqual(len(self.server._server_pool.available()), 0)

    @gen_test
    def test_deregister_allocated_worker_server(self):
        hostname, port = '127.0.0.1', 60000
        yield self._send_request_expect_ok('register-worker-server', hostname, port)
        server = self.server._server_pool.allocate(1)[0]
        yield self._send_request_expect_fail('deregister-worker-server', hostname, port)

    @gen_test
    def test_deregister_nonexistant_worker_server(self):
        hostname, port = '192.168.1.150', 60000
        yield self._send_request_expect_ok('deregister-worker-server', hostname, port)

    @gen_test
    def test_configure_coherent_beams(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        tscrunch = 6
        fscrunch = 2
        nbeams = 100
        yield self._send_request_expect_ok('configure', product_name, self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        yield self._send_request_expect_ok('configure-coherent-beams', product_name, nbeams,
            self.DEFAULT_ANTENNAS, fscrunch, tscrunch)
        yield self._check_sensor_value("{}.coherent-beam-count".format(product_name), nbeams)
        yield self._check_sensor_value("{}.coherent-beam-tscrunch".format(product_name), tscrunch)
        yield self._check_sensor_value("{}.coherent-beam-fscrunch".format(product_name), fscrunch)
        yield self._check_sensor_value("{}.coherent-beam-antennas".format(product_name), self.DEFAULT_ANTENNAS)

    @gen_test
    def test_configure_incoherent_beam(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        tscrunch = 6
        fscrunch = 2
        yield self._send_request_expect_ok('configure', product_name, self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        yield self._send_request_expect_ok('configure-incoherent-beam', product_name,
            self.DEFAULT_ANTENNAS, fscrunch, tscrunch)
        yield self._check_sensor_value("{}.incoherent-beam-tscrunch".format(product_name), tscrunch)
        yield self._check_sensor_value("{}.incoherent-beam-fscrunch".format(product_name), fscrunch)
        yield self._check_sensor_value("{}.incoherent-beam-antennas".format(product_name), self.DEFAULT_ANTENNAS)

    @gen_test
    def test_configure_coherent_beams_invalid_antennas(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        subarray_antennas = 'm007,m008,m009,m010'
        yield self._send_request_expect_ok('configure', product_name, subarray_antennas,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        #Test invalid antenna combinations
        yield self._send_request_expect_fail('configure-coherent-beams', product_name, 100,
            'm007,m008,m011', 1, 16)
        yield self._send_request_expect_fail('configure-coherent-beams', product_name, 100,
            'm007,m008,m009,m010,m011', 1, 16)
        yield self._send_request_expect_fail('configure-coherent-beams', product_name, 100,
            '', 1, 16)
        yield self._send_request_expect_fail('configure-coherent-beams', product_name, 100,
            'm007,m007,m008,m009', 1, 16)

    @gen_test
    def test_configure_incoherent_beam_invalid_antennas(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        subarray_antennas = 'm007,m008,m009,m010'
        yield self._send_request_expect_ok('configure', product_name, subarray_antennas,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        #Test invalid antenna combinations
        yield self._send_request_expect_fail('configure-incoherent-beam', product_name,
                    'm007,m008,m011', 1, 16)
        yield self._send_request_expect_fail('configure-incoherent-beam', product_name,
                    'm007,m008,m009,m010,m011', 1, 16)
        yield self._send_request_expect_fail('configure-incoherent-beam', product_name,
                    '', 1, 16)
        yield self._send_request_expect_fail('configure-incoherent-beam', product_name,
                    'm007,m007,m008,m009', 1, 16)

    @gen_test
    def test_set_configuration_authority(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        subarray_antennas = 'm007,m008,m009,m010'
        hostname, port = "127.0.0.1", 60000
        yield self._send_request_expect_ok('configure', product_name, subarray_antennas,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        yield self._send_request_expect_ok('set-configuration-authority', product_name, hostname, port)
        address = "{}:{}".format(hostname,port)
        yield self._check_sensor_value('{}.configuration-authority'.format(product_name), address)

    @gen_test
    def test_get_sb_configuration_from_ca(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        hostname = "127.0.0.1"
        sb_id = "default_subarray"
        target = 'test_target,radec,12:00:00,01:00:00'
        sb_config = {
            u'coherent-beams-nbeams':100,
            u'coherent-beams-tscrunch':22,
            u'coherent-beams-fscrunch':2,
            u'coherent-beams-antennas':'m007',
            u'coherent-beams-granularity':6,
            u'incoherent-beam-tscrunch':16,
            u'incoherent-beam-fscrunch':1,
            u'incoherent-beam-antennas':'m008'
            }
        ca_server = MockFbfConfigurationAuthority(hostname, 0)
        ca_server.start()
        ca_server.set_sb_config_return_value(proxy_name, sb_id, sb_config)
        self._add_n_servers(64)
        port = ca_server.bind_address[1]
        yield self._send_request_expect_ok('configure', product_name, self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        yield self._send_request_expect_ok('set-configuration-authority', product_name, hostname, port)
        yield self._send_request_expect_ok('provision-beams', product_name)
        product = self.server._products[product_name]
        while True:
            yield sleep(0.5)
            if product.ready: break
        # Here we need to check if the proxy sensors have been updated
        yield self._check_sensor_value("{}.coherent-beam-count".format(product_name), sb_config['coherent-beams-nbeams'], tolerance=0.05)
        yield self._check_sensor_value("{}.coherent-beam-tscrunch".format(product_name), sb_config['coherent-beams-tscrunch'])
        yield self._check_sensor_value("{}.coherent-beam-fscrunch".format(product_name), sb_config['coherent-beams-fscrunch'])
        yield self._check_sensor_value("{}.coherent-beam-antennas".format(product_name), 'm007')
        yield self._check_sensor_value("{}.incoherent-beam-tscrunch".format(product_name), sb_config['incoherent-beam-tscrunch'])
        yield self._check_sensor_value("{}.incoherent-beam-fscrunch".format(product_name), sb_config['incoherent-beam-fscrunch'])
        yield self._check_sensor_value("{}.incoherent-beam-antennas".format(product_name), 'm008')
        expected_ibc_mcast_group = ContiguousIpRange(str(self.server._ip_pool._ip_range.base_ip),
            self.server._ip_pool._ip_range.port, 1)
        yield self._check_sensor_value("{}.incoherent-beam-multicast-group".format(product_name),
            expected_ibc_mcast_group.format_katcp())
        _, ngroups = yield self._get_sensor_reading("{}.coherent-beam-ngroups".format(product_name))
        expected_cbc_mcast_groups = ContiguousIpRange(str(self.server._ip_pool._ip_range.base_ip+1),
            self.server._ip_pool._ip_range.port, ngroups)
        yield self._check_sensor_value("{}.coherent-beam-multicast-groups".format(product_name),
            expected_cbc_mcast_groups.format_katcp())
        yield self._send_request_expect_ok('capture-start', product_name)

    @gen_test
    def test_get_target_configuration_from_ca(self):
        product_name = 'test_product'
        proxy_name = 'FBFUSE_test'
        hostname = "127.0.0.1"
        sb_id = "default_subarray" #TODO replace this when the sb_id is actually provided to FBF
        targets = ['test_target0,radec,12:00:00,01:00:00',
                   'test_target1,radec,13:00:00,02:00:00']
        ca_server = MockFbfConfigurationAuthority(hostname, 0)
        ca_server.start()
        ca_server.set_sb_config_return_value(proxy_name, sb_id, {})
        ca_server.set_target_config_return_value(proxy_name, targets[0], {'beams':targets})
        port = ca_server.bind_address[1]
        self._add_n_servers(64)
        yield self._send_request_expect_ok('configure', product_name, self.DEFAULT_ANTENNAS,
            self.DEFAULT_NCHANS, self.DEFAULT_STREAMS, proxy_name)
        yield self._send_request_expect_ok('set-configuration-authority', product_name, hostname, port)
        yield self._send_request_expect_ok('provision-beams', product_name)
        product = self.server._products[product_name]
        while True:
            yield sleep(0.5)
            if product.ready: break
        yield self._send_request_expect_ok('capture-start', product_name)
        yield self._send_request_expect_ok('target-start', product_name, targets[0])
        yield self._check_sensor_value('{}.coherent-beam-cfbf00000'.format(product_name),
            Target(targets[0]).format_katcp())
        yield self._check_sensor_value('{}.coherent-beam-cfbf00001'.format(product_name),
            Target(targets[1]).format_katcp())
        new_targets = ['test_target2,radec,14:00:00,03:00:00',
                       'test_target3,radec,15:00:00,04:00:00']
        ca_server.update_target_config(proxy_name, {'beams':new_targets})
        # Need to give some time for the update callback to hit the top of the
        # event loop and change the beam configuration sensors.
        yield sleep(1)
        yield self._check_sensor_value('{}.coherent-beam-cfbf00000'.format(product_name),
            Target(new_targets[0]).format_katcp())
        yield self._check_sensor_value('{}.coherent-beam-cfbf00001'.format(product_name),
            Target(new_targets[1]).format_katcp())
        yield self._send_request_expect_ok('target-stop', product_name)
        # Put beam configuration back to original:
        ca_server.update_target_config(proxy_name, {'beams':targets})
        yield sleep(1)
        # At this point the sensor values should NOT have updated
        yield self._check_sensor_value('{}.coherent-beam-cfbf00000'.format(product_name),
            Target(new_targets[0]).format_katcp())
        yield self._check_sensor_value('{}.coherent-beam-cfbf00001'.format(product_name),
            Target(new_targets[1]).format_katcp())
        #Not start up a new target-start
        yield self._send_request_expect_ok('target-start', product_name, targets[0])
        yield self._check_sensor_value('{}.coherent-beam-cfbf00000'.format(product_name),
            Target(targets[0]).format_katcp())
        yield self._check_sensor_value('{}.coherent-beam-cfbf00001'.format(product_name),
            Target(targets[1]).format_katcp())


if __name__ == '__main__':
    unittest.main(buffer=True)
