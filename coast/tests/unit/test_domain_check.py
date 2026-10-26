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

import ssl
from unittest import mock

import dns.exception
import dns.rdata
import dns.resolver

from coast.network import domain_check
from coast.tests import base


class DomainCheckTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.resolver = mock.Mock()
        self.fqdn = 'www.example.com'

    def record(self, kind, value):
        return dns.rdata.from_text('IN', kind, value)

    def test_txt_matches_chunks_and_multiple_records(self):
        self.resolver.resolve.return_value = [
            self.record('TXT', '"wrong"'),
            self.record('TXT', '"to" "ken"')]
        self.assertTrue(domain_check.txt_matches(
            self.fqdn, 'token', self.resolver))
        self.resolver.resolve.assert_called_once_with(
            '_coast-challenge.www.example.com', 'TXT')

    def test_txt_requires_exact_token(self):
        self.resolver.resolve.return_value = [
            self.record('TXT', '"prefix-token"')]
        self.assertFalse(domain_check.txt_matches(
            self.fqdn, 'token', self.resolver))

    def test_txt_missing_or_timeout(self):
        for error in (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer,
                      dns.exception.Timeout, dns.resolver.NoNameservers):
            with self.subTest(error=error):
                self.resolver.resolve.side_effect = error()
                self.assertFalse(domain_check.txt_matches(
                    self.fqdn, 'token', self.resolver))

    def test_cname_normalizes_case_and_dot(self):
        self.resolver.resolve.return_value = [
            self.record('CNAME', 'Web.apps.example.com.')]
        self.assertTrue(domain_check.routes_to(
            self.fqdn, 'WEB.APPS.EXAMPLE.COM', '192.0.2.1', self.resolver))

    def test_routes_accept_any_matching_address(self):
        for kind, fip, other in (
                ('A', '192.0.2.1', '192.0.2.2'),
                ('AAAA', '2001:db8::1', '2001:db8::2')):
            with self.subTest(kind=kind):
                def resolve(name, query):
                    if query == kind:
                        return [self.record(kind, other),
                                self.record(kind, fip)]
                    raise dns.resolver.NoAnswer()
                self.resolver.resolve.side_effect = resolve
                self.assertTrue(domain_check.routes_to(
                    self.fqdn, 'web.apps.example.com', fip, self.resolver))

    def test_routes_reject_wrong_cname_and_addresses(self):
        self.resolver.resolve.side_effect = [
            [self.record('CNAME', 'cdn.example.com.')],
            [self.record('A', '192.0.2.2')],
            [self.record('AAAA', '2001:db8::2')]]
        self.assertFalse(domain_check.routes_to(
            self.fqdn, 'web.apps.example.com', '192.0.2.1', self.resolver))

    def test_routes_missing_or_timeout(self):
        for error in (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer,
                      dns.exception.Timeout, dns.resolver.NoNameservers):
            with self.subTest(error=error):
                self.resolver.resolve.side_effect = error()
                self.assertFalse(domain_check.routes_to(
                    self.fqdn, 'web.apps.example.com', '192.0.2.1',
                    self.resolver))

    def test_ipv6_equivalent_notation(self):
        self.resolver.resolve.side_effect = [
            dns.resolver.NoAnswer(), dns.resolver.NoAnswer(),
            [self.record('AAAA', '2001:db8::1')]]
        self.assertTrue(domain_check.routes_to(
            self.fqdn, 'web.apps.example.com', '2001:0db8:0:0:0:0:0:1',
            self.resolver))

    @mock.patch.object(domain_check.socket, 'create_connection')
    @mock.patch.object(domain_check.ssl, 'create_default_context')
    def test_tls_handshake_and_cleanup(self, context_factory, connect):
        context = context_factory.return_value
        sock = connect.return_value.__enter__.return_value
        self.assertTrue(domain_check.tls_ready(self.fqdn, timeout=7))
        connect.assert_called_once_with((self.fqdn, 443), timeout=7)
        context.wrap_socket.assert_called_once_with(
            sock, server_hostname=self.fqdn)
        self.assertTrue(connect.return_value.__exit__.called)
        self.assertTrue(context.wrap_socket.return_value.__exit__.called)

    @mock.patch.object(domain_check.socket, 'create_connection')
    @mock.patch.object(domain_check.ssl, 'create_default_context')
    def test_tls_certificate_or_handshake_failure(self, context_factory,
                                                  connect):
        for error in (ssl.SSLCertVerificationError, ssl.SSLError):
            with self.subTest(error=error):
                context_factory.return_value.wrap_socket.side_effect = error()
                self.assertFalse(domain_check.tls_ready(self.fqdn))
                self.assertTrue(connect.return_value.__exit__.called)

    @mock.patch.object(domain_check.socket, 'create_connection')
    @mock.patch.object(domain_check.ssl, 'create_default_context')
    def test_tls_connection_failure(self, context_factory, connect):
        for error in (OSError, TimeoutError):
            with self.subTest(error=error):
                connect.side_effect = error()
                self.assertFalse(domain_check.tls_ready(self.fqdn))

    @mock.patch.object(domain_check.dns.resolver, 'Resolver')
    def test_resolver_system_configuration(self, factory):
        self.assertIs(factory.return_value, domain_check.make_resolver([]))
        factory.assert_called_once_with(configure=True)

    @mock.patch.object(domain_check.dns.resolver, 'Resolver')
    def test_resolver_explicit_nameservers(self, factory):
        nameservers = ['192.0.2.53']
        resolver = domain_check.make_resolver(nameservers)
        factory.assert_called_once_with(configure=False)
        self.assertEqual(nameservers, resolver.nameservers)
        self.assertIsNot(nameservers, resolver.nameservers)


class HttpReadyTest(base.TestCase):
    def probe(self, reply=None, error=None):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        if error:
            sock.recv.side_effect = error
        else:
            sock.recv.return_value = reply
        context = mock.MagicMock()
        context.wrap_socket.return_value = sock
        with mock.patch.object(domain_check.ssl, 'create_default_context',
                               return_value=context), \
                mock.patch.object(domain_check.socket, 'create_connection',
                                  return_value=mock.MagicMock()) as connect:
            result = domain_check.http_ready('web.example', '203.0.113.5')
        return result, connect, context, sock

    def test_status_codes(self):
        for reply, expected in ((b'HTTP/1.1 200 OK\r\n', True),
                                (b'HTTP/1.1 302 Found\r\n', True),
                                (b'HTTP/1.1 404 Not Found\r\n', True),
                                (b'HTTP/1.1 502 Bad Gateway\r\n', False),
                                (b'HTTP/1.1 503 Unavailable\r\n', False),
                                (b'garbage', False), (b'', False)):
            with self.subTest(reply=reply):
                self.assertEqual(expected, self.probe(reply)[0])
        result, connect, context, sock = self.probe(b'HTTP/1.1 200 OK\r\n')
        self.assertEqual(('203.0.113.5', 443), connect.call_args.args[0])
        self.assertEqual('web.example', context.wrap_socket.call_args.kwargs[
            'server_hostname'])
        self.assertIn(b'Host: web.example\r\n', sock.sendall.call_args.args[0])

    def test_network_errors_are_not_ready(self):
        self.assertFalse(self.probe(error=OSError())[0])
        self.assertFalse(self.probe(error=domain_check.ssl.SSLError())[0])
