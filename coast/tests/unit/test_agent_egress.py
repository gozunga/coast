# Copyright 2026 Gozunga and Coast contributors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""First-start route selection, bounded probes and safe cached results."""

import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
from unittest import mock

import dns.exception

from coast.agent import egress
from coast.tests import base


class EgressTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(bind_ip='192.0.2.2', request_timeout=1,
                           group='agent')
        self.route = [{'dev': 'ens4', 'gateway': '198.51.100.1'}]
        self.links = [{'ifname': 'ens4', 'address': 'fa:16:3e:00:00:02',
                       'addr_info': [{'family': 'inet',
                                      'local': '198.51.100.10',
                                      'prefixlen': 24}]}]
        self.network = {'network': {'ethernets': {
            'mgmt': {'match': {'macaddress': 'fa:16:3e:00:00:01'},
                     'dhcp4': False},
            'env': {'match': {'macaddress': 'fa:16:3e:00:00:02'},
                    'dhcp4': True}}}}

    def route_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network.yaml'
            path.write_text(json.dumps(self.network))
            with mock.patch.object(egress, 'Path', return_value=path), \
                    mock.patch.object(egress, 'ip', side_effect=[
                        self.links, self.route]):
                return egress.environment_route('192.0.2.2')

    def test_route_uses_env_mac_not_nic_order(self):
        self.links.insert(0, {'ifname': 'docker0', 'address': 'other'})
        self.assertEqual(('ens4', '198.51.100.10'), self.route_result())
        self.links[1]['ifname'] = 'ens9'
        self.route[0]['dev'] = 'ens9'
        self.assertEqual(('ens9', '198.51.100.10'), self.route_result())

    def test_address_lookup_preserves_mac_and_filters_ipv4_lease(self):
        directory = Path(__file__).parent / 'fixtures' / 'iproute2'
        outputs = {
            ('ip', '-j', 'address'): (
                directory / 'address.json').read_bytes(),
            ('ip', '-j', '-4', 'address'): (
                directory / 'address-ipv4.json').read_bytes(),
            ('ip', '-j', '-4', 'route', 'show', 'default'): (
                json.dumps(self.route).encode()),
        }

        def run(command, **kwargs):
            return mock.Mock(stdout=outputs[tuple(command)])

        path = mock.Mock()
        path.read_text.return_value = json.dumps(self.network)
        with mock.patch.object(egress, 'Path', return_value=path), \
                mock.patch.object(egress.subprocess, 'run', side_effect=run):
            self.assertEqual(('ens4', '198.51.100.10'),
                             egress.environment_route('192.0.2.2'))

    def test_missing_duplicate_or_management_default_is_rejected(self):
        for routes in ([], self.route * 2,
                       [{'dev': 'ens3', 'gateway': '192.0.2.1'}],
                       [{'dev': 'ens4'}],
                       [{'dev': 'ens4', 'gateway': '203.0.113.1'}]):
            self.route = routes
            self.assertRaises(ValueError, self.route_result)

    def test_link_local_or_management_address_is_not_env_lease(self):
        for address in ('169.254.1.2', '192.0.2.2'):
            self.links[0]['addr_info'][0]['local'] = address
            self.route[0]['gateway'] = address
            self.assertRaises(ValueError, self.route_result)

    def test_probe_urls_are_https_and_have_no_credentials(self):
        self.assertIsNone(egress.target(''))
        value = egress.target('https://registry.example.com/v2/')
        self.assertEqual('registry.example.com', value.hostname)
        for value in ('http://registry.example.com', 'https://',
                      'https://user:secret@registry.example.com',
                      'https://registry.example.com/v2/?token=secret',
                      'https://registry.example.com/#secret',
                      'https://registry.example.com:invalid',
                      'https://registry.example.com:0',
                      'https://[2001:db8::1]/',
                      'https://registry.example.com/\nsecret'):
            self.assertRaises(ValueError, egress.target, value)

    def check(self, *, dns_error=None, https_ok=True, url=''):
        self.config.config(egress_check_url=url, group='agent')
        check = egress.Check(self.conf)
        self.assertEqual({'status': 'pending'}, check.result)
        with mock.patch.object(egress, 'environment_route',
                               return_value=('ens4', '198.51.100.10')), \
                mock.patch.object(egress.dns.resolver, 'resolve',
                                  side_effect=dns_error,
                                  return_value=['203.0.113.10']) as resolve, \
                mock.patch.object(egress, 'https',
                                  return_value=https_ok) as https:
            if not url:
                https.side_effect = AssertionError('HTTPS must be skipped')
            check.run()
        return check.result, resolve.call_args

    def test_empty_url_checks_dns_but_skips_https(self):
        result, query = self.check()
        self.assertEqual({'status': 'ok'}, result)
        self.assertEqual('docker.io', query.args[0])
        self.assertEqual(5, query.kwargs['lifetime'])

    def test_url_uses_registry_dns_and_ipv4_literal_still_checks_dns(self):
        result, query = self.check(url='https://registry.example.com/v2/')
        self.assertEqual({'status': 'ok'}, result)
        self.assertEqual('registry.example.com', query.args[0])
        result, query = self.check(url='https://203.0.113.10/v2/')
        self.assertEqual({'status': 'ok'}, result)
        self.assertEqual('docker.io', query.args[0])

    def test_dns_and_https_failures_are_fixed_health_results(self):
        result, query = self.check(dns_error=dns.exception.Timeout('secret'))
        self.assertEqual({'status': 'error', 'reason': 'dns'}, result)
        result, query = self.check(url='https://registry.example.com/v2/',
                                   https_ok=False)
        self.assertEqual({'status': 'error', 'reason': 'https'}, result)
        self.assertNotIn('secret', str(result))

    def test_invalid_config_does_not_probe_or_leak(self):
        result, query = self.check(url='https://user:secret@example.com')
        self.assertEqual({'status': 'error', 'reason': 'config'}, result)
        self.assertIsNone(query)

    def test_dhcp_grace_and_deadline(self):
        for responses, clock, expected in (
                ([ValueError('secret'), ('ens4', '198.51.100.10')],
                 [0, 0], {'status': 'ok'}),
                ([ValueError('secret')], [0, 2],
                 {'status': 'error', 'reason': 'route'})):
            check = egress.Check(self.conf)
            with mock.patch.object(egress, 'environment_route',
                                   side_effect=responses), \
                    mock.patch.object(egress.time, 'monotonic',
                                      side_effect=clock), \
                    mock.patch.object(egress.time, 'sleep'), \
                    mock.patch.object(egress.dns.resolver, 'resolve',
                                      return_value=['203.0.113.10']):
                check.run()
            self.assertEqual(expected, check.result)


class Body(io.BytesIO):
    def read(self, *args):
        raise AssertionError('Probe must not read response bodies')


class ProbeSocket:
    def __init__(self, status=401, failure=None):
        self.status = status
        self.failure = failure
        self.source = None
        self.destination = None
        self.request = b''

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def bind(self, source):
        self.source = source

    def connect(self, destination):
        self.destination = destination
        if self.failure:
            raise self.failure

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.request += data

    def makefile(self, *args):
        return Body(('HTTP/1.1 %s Status\r\nContent-Length: 6\r\n'
                     'Location: https://other.example.com/\r\n'
                     'Connection: close\r\n\r\nsecret' % self.status).encode())

    def close(self):
        pass


class HTTPSProbeTest(base.TestCase):
    def probe(self, sockets, routes=None, tls_error=None):
        context = mock.Mock()
        context.wrap_socket.side_effect = (tls_error or
                                           (lambda sock, **kw: sock))
        with mock.patch.object(egress, 'ip', return_value=routes or [
                {'dev': 'ens4'}]), \
                mock.patch.object(egress.socket, 'socket',
                                  side_effect=sockets), \
                mock.patch.object(egress.socket, 'getaddrinfo',
                                  side_effect=AssertionError('Second DNS')), \
                mock.patch.object(egress.ssl, 'create_default_context',
                                  return_value=context):
            value = egress.target('https://registry.example.com/v2/')
            return egress.https(value,
                                ['203.0.113.' + str(i + 10)
                                 for i in range(len(sockets))],
                                'ens4', '198.51.100.10')

    def test_any_http_status_is_success_without_body_or_redirect(self):
        for status in (200, 301, 401, 403, 500):
            sock = ProbeSocket(status)
            self.assertTrue(self.probe([sock]))
            self.assertEqual(('198.51.100.10', 0), sock.source)
            self.assertEqual(('203.0.113.10', 443), sock.destination)
            self.assertIn(b'GET /v2/ HTTP/1.1', sock.request)
            self.assertNotIn(b'Authorization:', sock.request)
            self.assertNotIn(b'other.example.com', sock.request)

    def test_connect_and_tls_failure_do_not_prove_egress(self):
        sock = ProbeSocket(failure=socket.timeout('secret'))
        self.assertFalse(self.probe([sock]))
        result = self.probe([ProbeSocket()], tls_error=ssl.SSLError('secret'))
        self.assertFalse(result)

    def test_connect_cannot_start_http_after_deadline(self):
        sock = ProbeSocket()
        with mock.patch.object(egress.time, 'monotonic',
                               side_effect=[0, 0, 20]):
            self.assertFalse(self.probe([sock]))
        self.assertEqual(b'', sock.request)

    def test_next_dns_address_can_prove_egress(self):
        self.assertTrue(self.probe([ProbeSocket(failure=OSError('secret')),
                                   ProbeSocket(401)]))

    def test_management_target_route_is_not_env_egress(self):
        self.assertFalse(self.probe([ProbeSocket()], routes=[{'dev': 'ens3'}]))

    def test_http_headers_cannot_outlive_probe_deadline(self):
        closed = threading.Event()

        class Headers(io.BytesIO):
            def readline(self, *args):
                if not closed.wait(0.5):
                    raise AssertionError('Probe deadline did not stop headers')
                raise OSError('secret')

        class SlowSocket(ProbeSocket):
            def makefile(self, *args):
                return Headers()

            def shutdown(self, how):
                closed.set()

        # Reach header reading even if the worker is delayed before TLS.
        # Keep the real timer to verify that expiry shuts down the socket.
        with mock.patch.object(egress, 'PROBE_TIMEOUT', 0.01), \
                mock.patch.object(egress.time, 'monotonic', return_value=0):
            self.assertFalse(self.probe([SlowSocket()]))
        self.assertTrue(closed.wait(timeout=1))
