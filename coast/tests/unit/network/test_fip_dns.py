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

from unittest import mock

from openstack.dns.v2 import recordset
from openstack import exceptions
from openstack.network.v2 import floating_ip
from openstack.network.v2 import network
from openstack.network.v2 import port
from openstack.network.v2 import security_group

from coast.common import clients
from coast.network import dns
from coast.network import fip
from coast.tests import base


class FloatingIPTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(group='controller_worker',
                           external_network_id='external')
        self.connection = mock.Mock()
        self.proxy = self.connection.network
        self.module = fip.FloatingIP(self.conf, self.connection)
        self.tags = self.module._tags('tenant', 'env', 'worker')
        self.port = port.Port(id='port', network_id='network',
                              security_group_ids=['worker-sg'],
                              is_port_security_enabled=True)
        self.proxy.get_port.return_value = self.port
        self.net = network.Network(id='network', name='coast-env-env',
                                   tags=self.tags[:3])
        self.proxy.get_network.return_value = self.net
        self.group = security_group.SecurityGroup(
            id='worker-sg', name='coast-env-env-worker', tags=self.tags[:3])
        self.proxy.get_security_group.return_value = self.group
        self.store = []
        self.proxy.ips.side_effect = lambda **kw: list(self.store)

        def create(**attrs):
            if 'tags' in attrs:
                raise RuntimeError("Attribute 'tags' not allowed in POST")
            value = floating_ip.FloatingIP(
                id='fip', floating_ip_address='192.0.2.1', **attrs)
            self.store.append(value)
            return value

        def apply_tags(resource, tags):
            resource.tags = list(tags)
            return resource

        def update(ip, **attrs):
            for key, value in attrs.items():
                setattr(ip, key, value)
            return ip

        self.proxy.create_ip.side_effect = create
        self.proxy.set_tags.side_effect = apply_tags
        self.proxy.update_ip.side_effect = update
        self.proxy.delete_ip.side_effect = (
            lambda ip, **kw: self.store.remove(ip))

    def ensure(self):
        return self.module.ensure('tenant', 'env', 'worker', 'port')

    def test_custom_prefix_checks_owned_network_and_worker(self):
        self.config.config(resource_name_prefix='coast-test')
        self.assertRaises(ValueError, self.ensure)
        self.net.name = 'coast-test-env-env'
        self.assertRaises(ValueError, self.ensure)
        self.group.name = 'coast-test-env-env-worker'
        first = self.ensure()
        self.assertEqual('port', first.port_id)
        self.assertEqual(self.tags, first.tags)
        self.assertEqual(first, self.ensure())
        self.assertEqual(1, len(self.store))

    def test_allocate_resume_and_delete(self):
        first = self.ensure()
        self.assertEqual('port', first.port_id)
        self.assertEqual(self.tags, first.tags)
        self.assertNotIn('tags', self.proxy.create_ip.call_args.kwargs)
        self.proxy.set_tags.assert_called_once()
        self.assertEqual(first, self.ensure())
        self.assertEqual(1, len(self.store))
        self.module.delete('tenant', 'env', 'worker')
        self.module.delete('tenant', 'env', 'worker')
        self.assertEqual([], self.store)

    def test_tag_failure_deletes_new_floating_ip(self):
        self.proxy.set_tags.side_effect = exceptions.HttpException('tag')
        self.assertRaises(exceptions.HttpException, self.ensure)
        self.assertEqual([], self.store)
        self.proxy.delete_ip.assert_called_once()
        self.assertNotIn('tags', self.proxy.create_ip.call_args.kwargs)

    def test_adopt_after_association_failure(self):
        self.proxy.update_ip.side_effect = exceptions.HttpException()
        self.assertRaises(exceptions.HttpException, self.ensure)
        self.proxy.update_ip.side_effect = None
        self.proxy.update_ip.return_value = self.store[0]
        self.ensure()
        self.assertEqual(1, len(self.store))

    def test_other_project_and_worker_not_adopted_or_deleted(self):
        other = floating_ip.FloatingIP(
            id='other', tags=['coast', 'project_id=other',
                              'stack_id=env', 'server_id=worker'])
        sibling = floating_ip.FloatingIP(
            id='sibling', tags=self.tags[:3] + ['server_id=sibling'])
        self.store.extend([other, sibling])
        self.ensure()
        self.module.delete('tenant', 'env', 'worker')
        self.assertEqual([other, sibling], self.store)

    def test_duplicate_owned_ips_rejected(self):
        ip = self.ensure()
        self.store.append(ip)
        self.assertRaises(ValueError, self.ensure)
        self.assertRaises(ValueError, self.module.delete,
                          'tenant', 'env', 'worker')

    def test_wrong_network_or_existing_association_rejected(self):
        ip = self.ensure()
        ip.floating_network_id = 'another'
        self.assertRaises(ValueError, self.ensure)
        ip.floating_network_id = 'external'
        ip.port_id = 'another-port'
        self.assertRaises(ValueError, self.ensure)

    def test_controller_build_and_foreign_network_rejected(self):
        for role in ('ctrl', 'build'):
            self.group.name = 'coast-env-env-' + role
            self.assertRaises(ValueError, self.ensure)
        self.group.name = 'coast-env-env-worker'
        self.net.tags = ['coast']
        self.assertRaises(ValueError, self.ensure)
        self.assertEqual([], self.store)

    def test_port_security_and_external_network_required(self):
        self.port.is_port_security_enabled = False
        self.assertRaises(ValueError, self.ensure)
        self.port.is_port_security_enabled = True
        self.config.config(group='controller_worker', external_network_id=None)
        self.assertRaises(ValueError, self.ensure)

    def test_delete_missing_race_and_retry(self):
        self.ensure()
        self.proxy.delete_ip.side_effect = exceptions.HttpException()
        self.assertRaises(exceptions.HttpException, self.module.delete,
                          'tenant', 'env', 'worker')
        self.proxy.delete_ip.side_effect = exceptions.ResourceNotFound()
        self.module.delete('tenant', 'env', 'worker')

    def test_client_factory(self):
        with mock.patch.object(clients, 'get_connection',
                               return_value=self.connection) as factory:
            module = fip.FloatingIP(self.conf)
        self.assertIs(self.proxy, module.network)
        factory.assert_called_once_with('neutron', conf=self.conf)


class DNSTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(group='dns', use_designate=True,
                           designate_zone_id='zone')
        self.config.config(default_suffix='apps.example.com', group='domains')
        self.connection = mock.Mock()
        self.proxy = self.connection.dns
        self.proxy.additional_headers = {}
        self.proxy.get_zone.return_value = mock.Mock(name='unused')
        self.proxy.get_zone.return_value.name = 'example.com.'
        self.module = dns.DNS(self.conf, self.connection)
        self.store = []
        self.proxy.recordsets.side_effect = lambda *a, **kw: list(self.store)

        def create(zone, **attrs):
            item = recordset.Recordset(id='record', **attrs)
            self.store.append(item)
            return item

        def update(item, **attrs):
            attrs.pop('zone_id')
            for key, value in attrs.items():
                setattr(item, key, value)
            return item

        self.proxy.create_recordset.side_effect = create
        self.proxy.update_recordset.side_effect = update
        self.proxy.delete_recordset.side_effect = (
            lambda item, **kw: self.store.remove(item))

    def ensure(self, address='192.0.2.1', name='service-123.apps.example.com'):
        return self.module.ensure('tenant', 'env', 'service', name, address)

    def test_prefix_does_not_change_explicit_dns_names_or_ownership(self):
        self.ensure()
        self.config.config(resource_name_prefix='coast-test')
        self.ensure()
        self.assertEqual(1, len(self.store))
        self.assertEqual('service-123.apps.example.com.', self.store[0].name)
        self.assertEqual(self.module._owner('tenant', 'env', 'service'),
                         self.store[0].description)
        self.module.delete('tenant', 'env', 'service')
        self.assertEqual([], self.store)

    def test_create_resume_update_delete(self):
        required = self.ensure()
        self.assertEqual([dict(name='service-123.apps.example.com.', type='A',
                               records=['192.0.2.1'])], required)
        self.ensure()
        self.assertEqual(1, len(self.store))
        self.ensure('192.0.2.2')
        self.assertEqual(['192.0.2.2'], self.store[0].records)
        self.assertEqual(300, self.store[0].ttl)
        self.config.config(group='dns', record_ttl=600)
        self.ensure('192.0.2.2')
        self.assertEqual(600, self.store[0].ttl)
        self.module.delete('tenant', 'env', 'service')
        self.module.delete('tenant', 'env', 'service')
        self.assertEqual([], self.store)

    def test_manual_never_loads_client(self):
        self.config.config(group='dns', use_designate=False,
                           designate_zone_id=None)
        with mock.patch.object(clients, 'get_connection') as factory:
            module = dns.DNS(self.conf)
            result = module.ensure('tenant', 'env', 'service',
                                   'service.apps.example.com', '192.0.2.1')
            module.delete('tenant', 'env', 'service')
        self.assertEqual(['192.0.2.1'], result[0]['records'])
        factory.assert_not_called()

    def test_unowned_record_never_updated_or_deleted(self):
        self.ensure()
        self.store[0].description = 'coast project_id=another'
        self.assertRaises(ValueError, self.ensure, '192.0.2.2')
        self.module.delete('tenant', 'env', 'service')
        self.assertEqual(1, len(self.store))
        self.assertEqual(['192.0.2.1'], self.store[0].records)

    def test_delete_old_names_but_preserve_other_services(self):
        self.ensure()
        self.ensure(name='renamed.apps.example.com')
        self.store.append(recordset.Recordset(
            id='other', description=self.module._owner(
                'tenant', 'env', 'other')))
        self.module.delete('tenant', 'env', 'service')
        self.assertEqual(['other'], [item.id for item in self.store])

    def test_named_cleanup_preserves_custom_records_with_same_owner(self):
        self.ensure()
        self.store.append(recordset.Recordset(
            id='custom', name='shop.example.com.', type='A',
            description=self.module._owner('tenant', 'env', 'service')))
        self.module.delete('tenant', 'env', 'service',
                           names=['service-123.apps.example.com'])
        self.module.delete('tenant', 'env', 'service',
                           names=['service-123.apps.example.com'])
        self.assertEqual(['custom'], [item.id for item in self.store])

    def test_duplicate_recordsets_fail_closed(self):
        self.ensure()
        self.store.append(self.store[0])
        self.assertRaises(ValueError, self.ensure)

    def test_lost_create_response_is_adopted(self):
        create = self.proxy.create_recordset.side_effect

        def lost_response(*args, **kwargs):
            create(*args, **kwargs)
            raise exceptions.HttpException()

        self.proxy.create_recordset.side_effect = lost_response
        self.assertRaises(exceptions.HttpException, self.ensure)
        self.ensure()
        self.assertEqual(1, len(self.store))
        self.assertEqual(['192.0.2.1'], self.store[0].records)

    def test_failed_update_is_resumable(self):
        self.ensure()
        update = self.proxy.update_recordset.side_effect
        self.proxy.update_recordset.side_effect = exceptions.HttpException()
        self.assertRaises(exceptions.HttpException, self.ensure, '192.0.2.2')
        self.proxy.update_recordset.side_effect = update
        self.ensure('192.0.2.2')
        self.assertEqual(['192.0.2.2'], self.store[0].records)

    def test_missing_delete_and_failed_delete(self):
        self.ensure()
        self.proxy.delete_recordset.side_effect = exceptions.HttpException()
        self.assertRaises(exceptions.HttpException, self.module.delete,
                          'tenant', 'env', 'service')
        self.proxy.delete_recordset.side_effect = exceptions.ResourceNotFound()
        self.module.delete('tenant', 'env', 'service')

    def test_configuration_and_zone_boundary(self):
        self.config.config(group='dns', designate_zone_id=None)
        self.assertRaises(ValueError, self.ensure)
        self.config.config(group='dns', designate_zone_id='zone')
        self.proxy.get_zone.return_value.name = 'other.example.com.'
        self.assertRaises(ValueError, self.ensure)
        self.assertEqual([], self.store)

    def test_invalid_names_and_ipv6_rejected(self):
        for name in ('*.apps.example.com', 'apps.example.com',
                     'service.apps.example.com.evil',
                     '-service.apps.example.com'):
            self.assertRaises(ValueError, self.ensure, name=name)
        self.assertRaises(ValueError, self.ensure, '2001:db8::1')
        self.assertEqual([], self.store)

    def test_client_factory(self):
        with mock.patch.object(clients, 'get_connection',
                               return_value=self.connection) as factory:
            module = dns.DNS(self.conf)
            module.ensure('tenant', 'env', 'service',
                          'service.apps.example.com', '192.0.2.1')
        factory.assert_called_once_with('designate', conf=self.conf)
