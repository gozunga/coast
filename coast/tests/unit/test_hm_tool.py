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

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack import exceptions

from coast.tests import base


SPEC = importlib.util.spec_from_file_location(
    'hm_tool', Path(__file__).resolve().parents[3] / 'tools/coast-hm0.py')
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


class FakeNetwork:
    def __init__(self):
        self.rows = {kind: [] for kind in
                     ('network', 'subnet', 'security_group', 'port')}
        self.rules = []
        self.deleted_rules = []
        self.created = []

    def __getattr__(self, name):
        if name.endswith('s') and name[:-1] in self.rows:
            return lambda **filters: [row for row in self.rows[name[:-1]]
                                      if row.name == filters['name']]
        if name.startswith('create_') and name[7:] in self.rows:
            kind = name[7:]

            def create(**attrs):
                self.created.append(dict(attrs))
                if 'tags' in attrs:
                    raise RuntimeError("Attribute 'tags' not allowed in POST")
                row = SimpleNamespace(id=kind + '-id', tags=[], **attrs)
                if kind == 'port':
                    row.mac_address = 'fa:16:3e:00:00:01'
                self.rows[kind].append(row)
                return row
            return create
        raise AttributeError(name)

    def set_tags(self, resource, tags):
        resource.tags = list(tags)
        return resource

    def _delete(self, kind, resource, ignore_missing=True):
        identifier = resource if isinstance(resource, str) else resource.id
        self.rows[kind] = [row for row in self.rows[kind]
                           if row.id != identifier]

    def delete_network(self, resource, ignore_missing=True):
        self._delete('network', resource, ignore_missing)

    def delete_subnet(self, resource, ignore_missing=True):
        self._delete('subnet', resource, ignore_missing)

    def delete_security_group(self, resource, ignore_missing=True):
        self._delete('security_group', resource, ignore_missing)

    def delete_port(self, resource, ignore_missing=True):
        self._delete('port', resource, ignore_missing)

    def security_group_rules(self, **filters):
        return list(self.rules)

    def delete_security_group_rule(self, rule_id):
        self.deleted_rules.append(rule_id)
        self.rules = [rule for rule in self.rules if rule.id != rule_id]


class PreparationTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.network = FakeNetwork()
        self.conn = SimpleNamespace(network=self.network,
                                    current_project_id='service-project')

    def prepare(self):
        return TOOL.prepare(self.conn, '192.0.2.0/24', '192.0.2.20',
                            'controller.example.com', conf=self.conf)

    def test_resumable_shared_resources_and_distinct_host_ports(self):
        first = self.prepare()
        self.assertEqual(first, self.prepare())
        TOOL.prepare(self.conn, '192.0.2.0/24', '192.0.2.21',
                     'controller2.example.com', conf=self.conf)
        self.assertEqual(2, len(self.network.rows['port']))
        self.assertIsNone(self.network.rows['subnet'][0].gateway_ip)
        self.assertFalse(self.network.rows['subnet'][0].is_dhcp_enabled)
        for rows in self.network.rows.values():
            for row in rows:
                self.assertEqual({'coast', 'project_id=service-project',
                                  'stack_id=control-plane'},
                                 set(row.tags))
        self.assertEqual('192.0.2.20/24', first['address'])
        self.assertFalse(self.network.rows['port'][0].device_owner.startswith(
            'network:'))
        self.assertTrue(self.network.rows['port'][0].is_port_security_enabled)
        self.assertEqual(['security_group-id'],
                         self.network.rows['port'][0].security_group_ids)
        self.assertTrue(self.network.created)
        self.assertTrue(all('tags' not in attrs
                            for attrs in self.network.created))

    def test_prefix_preserves_defaults_and_separates_resources(self):
        first = self.prepare()
        expected = {'network': 'coast-mgmt-net',
                    'subnet': 'coast-mgmt-subnet',
                    'security_group': 'coast-ctl-mgmt',
                    'port': 'coast-hm0-controller.example.com'}
        for kind, name in expected.items():
            self.assertEqual(name, self.network.rows[kind][0].name)
        self.config.config(resource_name_prefix='test-prefix')
        self.prepare()
        self.assertEqual(self.prepare(), self.prepare())
        for kind, name in expected.items():
            rows = self.network.rows[kind]
            self.assertEqual(2, len(rows))
            self.assertEqual('test-prefix' + name[len('coast'):], rows[1].name)
        self.config.config(resource_name_prefix='coast')
        self.assertEqual(first, self.prepare())

    def test_foreign_duplicate_and_drift_refused(self):
        self.prepare()
        net = self.network.rows['network'][0]
        net.tags = []
        self.assertRaises(ValueError, self.prepare)
        net.tags = ['coast', 'project_id=service-project',
                    'stack_id=control-plane']
        net.is_shared = True
        self.assertRaises(ValueError, self.prepare)
        self.network.rows['network'].append(net)
        self.assertRaises(ValueError, self.prepare)

    def test_cidr_address_and_project_required(self):
        for address in ('192.0.2.0', '192.0.2.255', '198.51.100.1'):
            self.assertRaises(ValueError, TOOL.prepare, self.conn,
                              '192.0.2.0/24', address,
                              'controller.example.com', conf=self.conf)
        self.conn.current_project_id = None
        self.assertRaises(ValueError, self.prepare)
        self.assertEqual([], self.network.rows['network'])

    def test_tag_failure_deletes_new_network(self):
        def fail(resource, tags):
            raise RuntimeError('tag failed')

        self.network.set_tags = fail
        self.assertRaises(RuntimeError, self.prepare)
        self.assertEqual([], self.network.rows['network'])
        self.assertNotIn('tags', self.network.created[0])

    def _run_main(self, exc):
        stderr = io.StringIO()
        argv = ['coast-hm0', 'prepare', '--cidr', '192.0.2.0/24',
                '--address', '192.0.2.10', '--host', 'host.example.com']
        with mock.patch.object(TOOL.config, 'parse_args'), \
                mock.patch.object(TOOL.clients, 'get_connection'), \
                mock.patch.object(TOOL, 'prepare', side_effect=exc), \
                mock.patch.object(TOOL.sys, 'argv', argv), \
                mock.patch.object(TOOL.sys, 'stderr', stderr):
            code = TOOL.main()
        return code, stderr.getvalue()

    def test_failure_reports_status_and_neutron_message(self):
        secret = 'super-secret-value'
        exc = exceptions.BadRequestException(
            message='https://user:%s@cloud.example/v2.0' % secret,
            details="Attribute 'tags' not allowed in POST")
        exc.status_code = 400
        code, text = self._run_main(exc)
        self.assertEqual(1, code)
        self.assertIn(
            "BadRequestException HTTP 400 "
            "Attribute 'tags' not allowed in POST", text)
        self.assertNotIn(secret, text)
        self.assertNotIn('192.0.2.0/24', text)

    def test_failure_omits_config_text(self):
        secret = 'super-secret-value'
        exc = exceptions.HttpException(message='cidr=192.0.2.0/24')
        exc.status_code = 403
        exc.details = 'password=%s' % secret
        code, text = self._run_main(exc)
        self.assertEqual(1, code)
        self.assertIn('HttpException HTTP 403', text)
        self.assertNotIn(secret, text)
        self.assertNotIn('192.0.2.0', text)
        self.assertNotIn('password', text)

    def test_default_egress_removed_and_peer_rules_preserved(self):
        default = SimpleNamespace(id='default', direction='egress',
                                  protocol=None, remote_group_id=None,
                                  remote_address_group_id=None,
                                  remote_ip_prefix='::/0')
        peer = SimpleNamespace(id='peer', direction='ingress', protocol='udp',
                               remote_group_id='env-agent-sg',
                               remote_address_group_id=None,
                               remote_ip_prefix=None, ether_type='IPv4',
                               port_range_min=5565, port_range_max=5565)
        self.network.rules = [default, peer]
        self.prepare()
        self.assertEqual(['default'], self.network.deleted_rules)
        self.assertEqual([peer], self.network.rules)
        peer.port_range_max = 5566
        self.assertRaises(ValueError, self.prepare)
        self.assertEqual([], self.network.rows['port'][1:])


class PlumbingTest(base.TestCase):
    bridge = 'br-mgmt'
    port = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    mac = 'fa:16:3e:00:00:01'

    def setUp(self):
        super().setUp()
        self.calls = []
        self.extra_link = None
        self.existing = False
        self.owner = self.port
        self.default_route = ''
        self.fail_add_port = False
        self.actual_bridge = self.bridge

    def run_command(self, *args):
        self.calls.append(args)
        output = ''
        if args == ('ip', 'netns', 'list'):
            output = 'coast-mgmt\n'
        elif args == ('ip', '-n', 'coast-mgmt', '-j', 'link', 'show'):
            links = [{'ifname': 'lo'}]
            if self.existing:
                links.append({'ifname': 'coast-hm0'})
            if self.extra_link:
                links.append({'ifname': self.extra_link})
            output = json.dumps(links)
        elif args == ('ip', '-j', 'link', 'show'):
            output = '[{"ifname":"lo"}]'
        elif 'route' in args:
            output = self.default_route
        elif 'port-to-br' in args:
            output = self.actual_bridge
        elif 'external_ids:iface-id' in args:
            output = self.owner
        elif 'add-port' in args and self.fail_add_port:
            raise RuntimeError('OVS unavailable')
        return SimpleNamespace(stdout=output)

    def plug(self):
        with mock.patch.object(TOOL, 'run', side_effect=self.run_command):
            TOOL.plug(self.bridge, self.port, self.mac, '192.0.2.20/24')

    def test_namespace_static_address_and_no_host_routes(self):
        self.plug()
        self.assertIn(('ip', 'link', 'set', 'coast-hm0', 'netns',
                       'coast-mgmt'), self.calls)
        self.assertIn(('ip', '-n', 'coast-mgmt', 'address', 'add',
                       '192.0.2.20/24', 'dev', 'coast-hm0'), self.calls)
        self.assertFalse(any('route' in call and 'add' in call
                             for call in self.calls))
        self.assertTrue(any('net.ipv4.ip_forward=0' in call
                            for call in self.calls))

    def test_resume_owned_interface_without_creating_veth(self):
        self.existing = True
        self.plug()
        self.assertFalse(any('veth' in call for call in self.calls))
        self.actual_bridge = 'other-bridge'
        self.assertRaises(ValueError, self.plug)
        self.actual_bridge = self.bridge
        self.owner = 'other-port'
        self.assertRaises(ValueError, self.plug)

    def test_refuse_foreign_interfaces_default_route_and_bad_inputs(self):
        self.extra_link = 'eth0'
        self.assertRaises(ValueError, self.plug)
        self.extra_link = None
        self.default_route = 'default via 192.0.2.1'
        self.assertRaises(ValueError, self.plug)
        self.assertRaises(ValueError, TOOL.plug, 'bad;bridge', self.port,
                          self.mac, '192.0.2.20/24')
        self.assertRaises(ValueError, TOOL.plug, self.bridge, self.port,
                          self.mac, '0.0.0.0/24')

    def test_failed_ovs_attach_removes_partial_veth(self):
        self.fail_add_port = True
        self.assertRaises(RuntimeError, self.plug)
        self.assertIn(('ip', 'link', 'delete', 'coast-bhm0'), self.calls)
        self.assertIn(('ovs-vsctl', '--if-exists', 'del-port',
                       self.bridge, 'coast-bhm0'), self.calls)
