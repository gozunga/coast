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

from types import SimpleNamespace
from unittest import mock

from coast.api.v1 import ingress_controllers as ingress

from coast.common import exception
from coast.common import states
from coast.db import api as db
from coast.network import stack as network_stack
from coast import objects
from coast.tests.unit import test_controllers


class IngressRulesTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.25'
        self.rules_path = self.path + '/ingress_rules'

    def create(self, values=None, status=201, headers=None):
        return self.client.post_json(
            self.rules_path, {'ingress_rule': values or {
                'protocol': 'tcp', 'port_range_min': 8080}},
            headers=headers or self.headers, status=status)

    def test_crud_duplicate_and_managed(self):
        row = self.create().json['ingress_rule']
        self.assertFalse(row['managed'])
        self.assertEqual(8080, row['port_range_max'])
        self.create(status=409)
        listed = self.client.get(self.rules_path, headers=self.headers).json
        self.assertIn(row, listed['ingress_rules'])
        self.client.delete(self.rules_path + '/managed-http',
                           headers=self.headers, status=409)
        self.client.delete(self.rules_path + '/' + row['id'],
                           headers=self.headers, status=204)
        self.client.delete(self.rules_path + '/' + row['id'],
                           headers=self.headers, status=404)
        self.create({'protocol': 'tcp', 'port_range_min': 80}, status=409)

    def test_persisted_rules_feed_network_and_scoped_reads(self):
        self.create()
        network = network_stack.EnvironmentNetwork(
            self.conf, connection=mock.Mock())
        stored = network.customer_rules(self.ctx.project_id, self.env.id)
        self.assertEqual(1, len(stored))
        self.assertEqual([], network.customer_rules('other', self.env.id))
        self.assertEqual([dict(
            direction='ingress', ether_type='IPv4', protocol='tcp',
            remote_ip_prefix='0.0.0.0/0', port_range_min=8080,
            port_range_max=8080)], network_stack.customer_ingress(stored))

    def test_rules_ignore_api_page_limit(self):
        self.config.config(group='api', max_limit=1)
        self.config.config(group='controller_worker',
                           ingress_rules_per_stack=3)
        rows = [self.create({'protocol': 'tcp', 'port_range_min': port}).json[
            'ingress_rule'] for port in (8080, 8081, 8082)]
        listed = self.client.get(self.rules_path, headers=self.headers).json[
            'ingress_rules']
        self.assertCountEqual(rows, [r for r in listed if not r['managed']])
        self.create({'protocol': 'tcp', 'port_range_min': 8082}, status=409)
        self.create({'protocol': 'icmp'}, status=403)
        self.client.delete(self.rules_path + '/' + rows[-1]['id'],
                           headers=self.headers, status=204)
        self.create({'protocol': 'icmp'})
        self.assertEqual(3, len(db.ingress_rules.list_for_stack(
            self.ctx, self.env.id)))

    def test_direct_rule_lookup_checks_project_and_stack(self):
        row = self.create().json['ingress_rule']
        other = objects.Stack(context=self.ctx, name='other', plan='small',
                              driver='noop', engine_version='test',
                              status=states.ACTIVE, task_state=states.NONE)
        other.create()
        self.client.delete('/v1/stacks/' + other.id + '/ingress_rules/' +
                           row['id'], headers=self.headers, status=404)
        self.assertRaises(exception.NotFound, db.ingress_rules.get_for_stack,
                          self.ctx, other.id, row['id'])
        with mock.patch.object(self.ctx, 'project_id', 'other-project'):
            self.assertEqual([], db.ingress_rules.list_for_stack(
                self.ctx, self.env.id))
            self.assertRaises(exception.NotFound,
                              db.ingress_rules.get_for_stack,
                              self.ctx, self.env.id, row['id'])

    def test_network_sync_keeps_rules_beyond_api_page_limit(self):
        rows = [self.create({'protocol': 'tcp', 'port_range_min': port}).json[
            'ingress_rule'] for port in (8080, 8081, 8082)]
        self.config.config(group='api', max_limit=1)
        network = network_stack.EnvironmentNetwork(
            self.conf, connection=mock.Mock())
        stored = network.customer_rules(self.ctx.project_id, self.env.id)
        self.assertEqual(3, len(stored))
        desired = network_stack.rules(
            self.conf.controller_worker, '192.0.2.0/24', '192.0.2.2',
            False, 'control-group', ingress_rules=stored)['worker']
        current = [SimpleNamespace(id=row['id'], **rule)
                   for row, rule in zip(
                       rows, network_stack.customer_ingress(stored))]
        current.append(SimpleNamespace(
            id='unknown', direction='ingress', ether_type='IPv4',
            protocol='tcp', remote_ip_prefix='0.0.0.0/0',
            port_range_min=9000, port_range_max=9000))
        network.network.security_group_rules.return_value = current
        network._sync_rules(SimpleNamespace(id='worker'), desired, [])
        network.network.delete_security_group_rule.assert_called_once_with(
            'unknown')

    def test_managed_ssh_normalized_duplicate_and_busy_stack(self):
        self.server.role = 'control'
        self.server.private_ip = '192.0.2.2'
        self.server.save()
        rows = self.client.get(self.rules_path, headers=self.headers).json[
            'ingress_rules']
        ssh = next(row for row in rows if row['id'] == 'managed-ssh')
        self.assertTrue(ssh['managed'])
        self.assertEqual('192.0.2.2/32', ssh['source'])
        self.create({'protocol': 'tcp', 'port_range_min': 22,
                     'source': '192.0.2.2/32'}, status=409)
        self.client.delete(self.rules_path + '/managed-ssh',
                           headers=self.headers, status=409)
        self.create({'protocol': 'icmp', 'source': '192.0.2.3/24'})
        self.create({'protocol': 'icmp', 'source': '192.0.2.0/24',
                     'description': 'Same rule'}, status=409)
        self.env.acquire_task(states.DELETING)
        self.create(status=409)

    def test_validation(self):
        for value in (
                {'protocol': 'tcp'},
                {'protocol': 'icmp', 'port_range_min': 1},
                {'protocol': 'tcp', 'port_range_min': 5, 'port_range_max': 4},
                {'protocol': 'udp', 'port_range_min': 0},
                {'protocol': 'udp', 'port_range_min': 65536},
                {'protocol': 'icmp', 'source': '::/0'},
                {'protocol': 'icmp', 'source': 'invalid'},
                {'protocol': 'ipv6-icmp'},
                *[{'protocol': 'icmp', key: value} for key, value in (
                    ('direction', 'egress'), ('ether_type', 'IPv6'),
                    ('remote_group_id', 'group'),
                    ('remote_address_group_id', 'group'))]):
            self.create(value, status=400)
        self.create({'protocol': 'icmp'})
        self.create({'protocol': 'udp', 'port_range_min': 100,
                     'port_range_max': 200, 'source': '192.0.2.5/24'})
        self.assertRaises(exception.Invalid, ingress.validate,
                          {'protocol': 'icmp', 'source': '::/0'})

    def test_scope_policy_gate_and_limit(self):
        row = self.create().json['ingress_rule']
        other = dict(self.headers, **{'X-Project-Id': 'other'})
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        for headers, status in (
                (other, 404),
                (dict(self.headers, **{'X-Roles': 'unprivileged'}), 403),
                (dict(self.headers, **{'X-User-Id': ''}), 401),
                (dict(self.headers, **{
                    'OpenStack-API-Version': 'app-hosting 1.24'}), 404)):
            self.client.get(self.rules_path, headers=headers, status=status)
            self.create(headers=headers, status=status)
            self.client.delete(self.rules_path + '/' + row['id'],
                               headers=headers, status=status)
        self.client.get(self.rules_path, headers=reader)
        self.create(headers=reader, status=403)
        self.client.delete(self.rules_path + '/' + row['id'],
                           headers=reader, status=403)
        self.config.config(group='controller_worker',
                           ingress_rules_per_stack=1)
        self.create({'protocol': 'icmp'}, status=403)
        stack = self.client.get(self.path, headers=self.headers).json['stack']
        self.assertNotIn('sg_mgmt_id', stack)
        self.assertNotIn('sg_ingress_id', stack)
