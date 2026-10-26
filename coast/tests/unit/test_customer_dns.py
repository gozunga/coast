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

"""Project isolation, ownership, providers and DNS reconciliation."""

from types import SimpleNamespace
from unittest import mock

from keystoneauth1 import exceptions as ks_exceptions
from keystoneauth1 import session as ks_session
from keystoneauth1 import token_endpoint
from openstack import connection
from openstack import exceptions
import requests_mock

from coast.common import exception
from coast.network import customer_dns
from coast.network import dns_credentials
from coast.tests import base
from coast.tests import dns


class CustomerDNSTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.proxy = dns.Proxy()
        self.client = customer_dns.CustomerDNS(
            'project', self.conf, provider=dns.Provider(self.proxy))
        self.record = dict(name='web.example.com', type='A',
                           records=['203.0.113.10'])

    def test_project_filter_longest_match_and_label_boundary(self):
        self.assertEqual(['zone', 'deep'], [z.id for z in self.client.zones()])
        self.assertEqual('deep', self.client.zone('app.sd.example.com').id)
        self.assertEqual('zone', self.client.zone('example.com').id)
        for host in ('notexample.com', 'other.com'):
            self.assertRaises(customer_dns.DNSFailure, self.client.zone, host)
        self.assertRaises(customer_dns.DNSFailure, self.client.zone,
                          'web.example.com', 'foreign')

    def test_ensure_updates_owned_only_and_delete_is_idempotent(self):
        first = self.client.ensure('zone', 'stack', 'service', [self.record])
        second = self.client.ensure('zone', 'stack', 'service', [self.record])
        self.assertEqual(first, second)
        self.record['records'] = ['203.0.113.20']
        self.client.ensure('zone', 'stack', 'service', [self.record])
        self.assertEqual(['203.0.113.20'], self.proxy.items['1'].records)
        foreign = self.proxy.create_recordset(
            'zone', name='foreign.example.com.', type='A',
            records=['203.0.113.30'], description='customer', ttl=300)
        self.client.delete('stack', 'service')
        self.client.delete('stack', 'service')
        self.assertEqual([foreign.id], list(self.proxy.items))

    def test_collision_preflight_and_cname_conflict_do_not_write(self):
        self.proxy.create_recordset('zone', name='web.example.com.',
                                    type='A', records=['203.0.113.30'],
                                    description='customer', ttl=300)
        txt = dict(name='_coast-challenge.web.example.com', type='TXT',
                   records=['"token"'])
        self.assertRaises(customer_dns.DNSFailure, self.client.ensure,
                          'zone', 'stack', 'service', [txt, self.record])
        self.assertEqual(1, len(self.proxy.items))
        self.record['type'] = 'CNAME'
        self.assertRaises(customer_dns.DNSFailure, self.client.ensure,
                          'zone', 'stack', 'service', [self.record])
        self.assertEqual(1, len(self.proxy.items))

    def test_type_replacement_preserves_foreign_records(self):
        foreign = self.proxy.create_recordset(
            'zone', name=self.record['name'] + '.', type='CNAME',
            records=['customer.example.com.'], description='customer', ttl=300)
        self.assertRaises(customer_dns.DNSFailure, self.client.ensure,
                          'zone', 'stack', 'service', [self.record],
                          replace_types=True)
        self.assertEqual([foreign], list(self.proxy.items.values()))

    def test_cleanup_filters_names_and_ownership(self):
        self.client.ensure('zone', 'stack', 'service', [self.record])
        second = dict(self.record, name='two.example.com')
        self.client.ensure('zone', 'stack', 'service', [second])
        self.client.delete('stack', 'service', 'zone', ['web.example.com'])
        self.assertEqual('two.example.com.', next(
            iter(self.proxy.items.values())).name)
        self.client.delete('wrong-stack', 'service')
        self.assertEqual(1, len(self.proxy.items))

    def test_routing_ipv4_ipv6_cname_and_safe_failures(self):
        for address, kind in [('203.0.113.10', 'A'), ('2001:db8::1', 'AAAA')]:
            record = customer_dns.routing('web', address)
            self.assertEqual(kind, record['type'])
        self.assertEqual(['default.example.com.'], customer_dns.routing(
            'web', None, 'CNAME', 'default.example.com')['records'])
        self.assertRaises(customer_dns.DNSFailure, customer_dns.routing,
                          'web', None)
        for status, phrase in [(404, 'not found'), (409, 'conflict'),
                               (413, 'quota'), (403, 'credentials')]:
            error = exceptions.HttpException('secret')
            error.status_code = status
            self.assertIn(phrase, customer_dns.failure(error))
            self.assertNotIn('secret', customer_dns.failure(error))

    def test_missing_endpoint_failure_mapping(self):
        for error in (ks_exceptions.EndpointNotFound,
                      exceptions.EndpointNotFound):
            self.assertEqual('DNS zones are not available on this cloud.',
                             customer_dns.failure(error('secret')))
            with mock.patch.object(self.client.provider, 'proxy',
                                   side_effect=error('secret')):
                self.assertRaises(customer_dns.DNSUnavailable,
                                  self.client.zones)

    def test_provider_interface_and_separate_sudo_headers(self):
        conn = SimpleNamespace(dns=self.proxy)
        session = mock.Mock()
        session.get_project_id.return_value = 'project'
        self.config.config(region_name='example-region',
                           valid_interfaces=['internal'], group='designate')
        with mock.patch.object(dns_credentials.clients, 'get_connection',
                               return_value=conn), mock.patch.object(
                dns_credentials.loading,
                'load_auth_from_conf_options') as auth, \
                mock.patch.object(dns_credentials.loading,
                                  'load_session_from_conf_options',
                                  return_value=session), mock.patch.object(
                    dns_credentials.connection, 'Connection',
                    return_value=conn):
            for cls in (dns_credentials.ServiceAccountProvider,
                        dns_credentials.TrustProvider):
                provider = cls(self.conf)
                self.assertIsInstance(
                    provider, dns_credentials.CredentialProvider)
                proxy = provider.proxy('project', 'zone', 'trust')
                self.assertEqual(self.proxy.zones(), proxy.zones())
                self.assertTrue(proxy.logger.disabled)
            sudo = dns_credentials.ServiceAccountProvider(self.conf).proxy(
                'project', 'zone')
            self.assertEqual('project', sudo.additional_headers[
                'X-Auth-Sudo-Project-ID'])
            self.assertEqual({}, self.proxy.additional_headers)
            self.assertEqual('trust', auth.call_args.kwargs['trust_id'])
            self.assertIsNone(auth.call_args.kwargs['project_id'])
            session.get_project_id.return_value = 'other'
            self.assertRaises(exception.Invalid,
                              dns_credentials.TrustProvider(self.conf).proxy,
                              'project', 'zone', 'trust')
        self.assertRaises(exception.Invalid,
                          dns_credentials.TrustProvider(self.conf).proxy,
                          'project', 'zone')

    def test_linked_hostname_normalizes_label_and_respects_dns_limits(self):
        self.assertEqual('my-app-abcd.app.example.com',
                         customer_dns.default_hostname(
                             'My app', 'abcdefgh', 'app.example.com'))
        host = customer_dns.default_hostname('x' * 255, 'abcd', 'example.com')
        self.assertEqual(63, len(host.split('.')[0]))
        self.assertRaises(exception.Invalid, customer_dns.default_hostname,
                          'app', 'abcd', '.'.join(['a' * 61] * 4))

    def test_sdk_sudo_header_and_recordset_wire_contract(self):
        endpoint = 'https://dns.example.com/v2'
        auth = token_endpoint.Token(endpoint, 'test-token')
        conn = connection.Connection(
            session=ks_session.Session(auth=auth),
            dns_endpoint_override=endpoint, dns_api_version='2')
        with requests_mock.Mocker() as http:
            http.get(endpoint, json={'version': {
                'id': 'v2', 'status': 'CURRENT',
                'links': [{'rel': 'self', 'href': endpoint + '/'}]}})
            http.get(endpoint + '/zones', json={'zones': [{
                'id': 'zone', 'name': 'example.com.',
                'project_id': 'project'}]})
            path = endpoint + '/zones/zone/recordsets'
            listing = http.get(path, json={'recordsets': []})
            created = http.post(path, json=dict(
                self.record, id='record', ttl=300), status_code=202)
            with mock.patch.object(dns_credentials.clients, 'get_connection',
                                   return_value=conn):
                client = customer_dns.CustomerDNS(
                    'project', self.conf,
                    provider=dns_credentials.ServiceAccountProvider(self.conf))
                client.ensure('zone', 'stack', 'service', [self.record])
            self.assertEqual('project', listing.last_request.headers[
                'X-Auth-Sudo-Project-ID'])
            self.assertEqual('project', created.last_request.headers[
                'X-Auth-Sudo-Project-ID'])
            body = created.last_request.json()
            self.assertEqual('coast project_id=project stack_id=stack '
                             'service_id=service', body['description'])
            self.assertEqual('web.example.com.', body['name'])
            self.assertEqual(['203.0.113.10'], body['records'])
            self.assertNotIn(
                'X-Auth-Sudo-Project-ID', conn.dns.additional_headers)
