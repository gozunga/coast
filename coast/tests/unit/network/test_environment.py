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

import ipaddress
from unittest import mock

from openstack import exceptions
from openstack.network.v2 import network as sdk_network
from openstack.network.v2 import router as sdk_router
from openstack.network.v2 import security_group
from openstack.network.v2 import security_group_rule
from openstack.network.v2 import subnet as sdk_subnet

from coast.conductor import controller
from coast.network import stack
from coast.tests import base


class EnvironmentNetworkTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(
            group='controller_worker', external_network_id='external',
            env_cidr_pool='192.0.2.0/24', mgmt_sg_id='control',
            mgmt_net_id='mgmt',
            network_delete_retry_interval=0)
        self.connection = mock.Mock()
        self.proxy = self.connection.network
        self.module = stack.EnvironmentNetwork(
            self.conf, connection=self.connection)
        patcher = mock.patch.object(self.module, 'customer_rules',
                                    return_value=[])
        self.custom = patcher.start()
        self.addCleanup(patcher.stop)
        self.tags = self.module._tags('tenant', 'env')
        self.resources = {}
        for kind, cls in (('network', sdk_network.Network),
                          ('subnet', sdk_subnet.Subnet),
                          ('router', sdk_router.Router),
                          ('security_group', security_group.SecurityGroup)):
            def listing(kind=kind, **query):
                if kind == 'subnet' and query.get('network_id') == 'mgmt':
                    return [sdk_subnet.Subnet(
                        network_id='mgmt', ip_version=4,
                        cidr='198.51.100.0/24')]
                return [resource['value']
                        for resource in self.resources.values()
                        if resource['kind'] == kind and
                        resource['value'].name == query.get('name')]

            def create(kind=kind, cls=cls, **attrs):
                if 'tags' in attrs:
                    raise RuntimeError("Attribute 'tags' not allowed in POST")
                value = cls(id=attrs['name'] + '-' + kind, **attrs)
                self.resources[value.id] = dict(kind=kind, value=value)
                return value
            getattr(self.proxy, kind + 's').side_effect = listing
            getattr(self.proxy, 'create_' + kind).side_effect = create

        def apply_tags(resource, tags):
            resource.tags = list(tags)
            return resource

        self.proxy.set_tags.side_effect = apply_tags
        self.proxy.ports.return_value = []
        self.rule_store = {}

        def rule_list(**query):
            return [rule for rule in self.rule_store.values()
                    if all(rule.get(key) == value
                           for key, value in query.items())]

        def rule_create(**attrs):
            if 'tags' in attrs:
                raise RuntimeError("Attribute 'tags' not allowed in POST")
            id = 'rule-' + str(
                self.proxy.create_security_group_rule.call_count)
            rule = security_group_rule.SecurityGroupRule(id=id, **attrs)
            self.rule_store[id] = rule
            return rule

        self.address_groups = {}

        def group_list(**query):
            return [group for group in self.address_groups.values()
                    if group.name == query.get('name')]

        def group_create(**attrs):
            group = mock.Mock(id=attrs['name'] + '-ag', **attrs)
            group.name = attrs['name']
            self.address_groups[group.id] = group
            return group

        def group_add(group, addresses):
            group.addresses = sorted(set(group.addresses) | set(addresses))
            return group

        def group_remove(group, addresses):
            group.addresses = sorted(set(group.addresses) - set(addresses))
            return group

        self.proxy.address_groups.side_effect = group_list
        self.proxy.create_address_group.side_effect = group_create
        self.proxy.add_addresses_to_address_group.side_effect = group_add
        self.proxy.remove_addresses_from_address_group.side_effect = (
            group_remove)
        self.proxy.delete_address_group.side_effect = (
            lambda group: self.address_groups.pop(group.id, None))
        self.proxy.security_group_rules.side_effect = rule_list
        self.proxy.create_security_group_rule.side_effect = rule_create
        self.proxy.delete_security_group_rule.side_effect = (
            lambda id: self.rule_store.pop(id, None))

    def test_customer_rules_and_management_repair(self):
        self.config.config(group='controller_worker',
                           admin_ssh_key_name='operator-key')
        self.custom.return_value = [dict(protocol='tcp', source='0.0.0.0/0',
                                         port_range_min=8080,
                                         port_range_max=8080)]
        created = self.module.create('tenant', 'env', '192.0.2.10')
        worker = created['security_groups']['worker']
        peer = created['security_groups']['agent-mgmt']
        custom = next(r for r in self.rule_store.values()
                      if r.security_group_id == worker and
                      r.port_range_min == 8080)
        unknown = self.proxy.create_security_group_rule(
            security_group_id=worker, direction='ingress', ether_type='IPv4',
            protocol='tcp', remote_ip_prefix='0.0.0.0/0',
            port_range_min=9000, port_range_max=9000)
        for id, rule in list(self.rule_store.items()):
            if rule.security_group_id == peer or (
                    rule.security_group_id == 'control' and
                    rule.remote_group_id == peer):
                del self.rule_store[id]
        foreign = self.proxy.create_security_group_rule(
            security_group_id='control', remote_group_id='other-stack',
            direction='ingress', protocol='udp', ether_type='IPv4',
            port_range_min=5565, port_range_max=5565)
        guest_bad = self.proxy.create_security_group_rule(
            security_group_id=peer, direction='ingress', protocol='tcp',
            ether_type='IPv4', remote_ip_prefix='0.0.0.0/0',
            port_range_min=22, port_range_max=22)
        bad = self.proxy.create_security_group_rule(
            security_group_id='control', remote_group_id=peer,
            direction='ingress', protocol='tcp', ether_type='IPv4',
            port_range_min=22, port_range_max=22)
        self.module.reconcile('tenant', 'env', '192.0.2.10')
        self.assertIn(custom.id, self.rule_store)
        self.assertIn(foreign.id, self.rule_store)
        self.assertNotIn(unknown.id, self.rule_store)
        self.assertNotIn(bad.id, self.rule_store)
        self.assertNotIn(guest_bad.id, self.rule_store)
        self.assertEqual(2, sum(r.security_group_id == peer
                                for r in self.rule_store.values()))
        self.assertEqual(2, sum(r.security_group_id == 'control' and
                                r.remote_group_id == peer
                                for r in self.rule_store.values()))
        self.module.create('tenant', 'env', '192.0.2.10')
        self.assertIn(custom.id, self.rule_store)
        self.custom.return_value = []
        self.module.reconcile('tenant', 'env', '192.0.2.10')
        self.assertNotIn(custom.id, self.rule_store)

    def test_create_and_resume(self):
        first = self.module.create('tenant', 'env', '192.0.2.10')
        subnet_id = first['subnet_id']
        self.proxy.ports.return_value = [
            mock.Mock(fixed_ips=[dict(subnet_id=subnet_id)])]
        rule_count = len(self.rule_store)
        second = self.module.create('tenant', 'env', '192.0.2.10')
        self.assertEqual(first, second)
        self.assertEqual(rule_count, len(self.rule_store))
        description = stack.rule_description(
            self.conf.resource_name_prefix, self.tags)
        for rule in self.rule_store.values():
            self.assertEqual([], rule.tags)
            self.assertEqual(description, rule.description)
        for call in self.proxy.create_security_group_rule.call_args_list:
            self.assertNotIn('tags', call.kwargs)
        self.assertEqual(7, len(self.resources))
        self.assertEqual(7, self.proxy.set_tags.call_count)
        for name in ('create_network', 'create_subnet', 'create_router',
                     'create_security_group'):
            for call in getattr(self.proxy, name).call_args_list:
                self.assertNotIn('tags', call.kwargs)
        for resource in self.resources.values():
            self.assertEqual(self.tags, resource['value'].tags)
        self.assertEqual(1, self.proxy.add_interface_to_router.call_count)
        self.assertTrue(self.proxy.create_network.call_args.kwargs[
            'is_port_security_enabled'])
        self.assertEqual({'network_id': 'external'},
                         self.proxy.create_router.call_args.kwargs[
                             'external_gateway_info'])

    def test_allocation_pool_and_controller_address(self):
        for prefix, end in ((24, '192.0.2.254'), (28, '192.0.2.14')):
            with self.subTest(prefix=prefix):
                self.resources.clear()
                self.config.config(group='controller_worker',
                                   env_subnet_prefix=prefix)
                fixed_ip = controller.controller_ip(self.conf)
                self.assertEqual('192.0.2.2', fixed_ip)
                result = self.module.create('tenant', 'env', fixed_ip)
                subnet = self.resources[result['subnet_id']]['value']
                self.assertEqual(
                    [{'start': '192.0.2.10', 'end': end}],
                    subnet.allocation_pools)
                self.assertTrue(subnet.is_dhcp_enabled)
                self.assertEqual(result, self.module.create(
                    'tenant', 'env', fixed_ip))

    def test_small_subnets_rejected_before_cloud_calls(self):
        for prefix in (29, 30):
            with self.subTest(prefix=prefix):
                self.config.config(group='controller_worker',
                                   env_subnet_prefix=prefix)
                self.assertRaises(ValueError, controller.controller_ip,
                                  self.conf)
                self.assertRaises(ValueError, self.module.create,
                                  'tenant', 'env', '192.0.2.2')
        self.assertEqual({}, self.resources)
        self.proxy.create_network.assert_not_called()

    def test_changed_allocation_pool_fails_closed(self):
        result = self.module.create('tenant', 'env', '192.0.2.2')
        subnet = self.resources[result['subnet_id']]['value']
        subnet.allocation_pools = [
            {'start': '192.0.2.2', 'end': '192.0.2.254'}]
        self.assertRaises(ValueError, self.module.create,
                          'tenant', 'env', '192.0.2.2')
        self.assertEqual(1, self.proxy.create_subnet.call_count)

    def test_custom_prefix_create_resume_and_cleanup(self):
        self.config.config(resource_name_prefix='coast-test')
        first = self.module.create('tenant', 'env', '192.0.2.10')
        self.assertEqual(first, self.module.create(
            'tenant', 'env', '192.0.2.10'))
        self.assertEqual(
            {'coast-test-env-env', 'coast-test-env-env-ctrl',
             'coast-test-env-env-worker', 'coast-test-env-env-build',
             'coast-test-env-env-agent-mgmt'},
            {item['value'].name for item in self.resources.values()})
        for item in self.resources.values():
            self.assertEqual(self.tags, item['value'].tags)
        deleted = []
        for kind in ('network', 'subnet', 'router', 'security_group'):
            getattr(self.proxy, 'delete_' + kind).side_effect = (
                lambda resource: deleted.append(resource.id))
        self.module.delete('tenant', 'env')
        self.assertEqual(set(self.resources), set(deleted))

    def test_name_without_ownership_is_not_adopted(self):
        other = sdk_network.Network(
            id='other', name='coast-env-env', tags=['coast'])
        self.resources['other'] = dict(kind='network', value=other)
        result = self.module.create('tenant', 'env', '192.0.2.10')
        self.assertNotEqual('other', result['network_id'])
        self.module.delete('tenant', 'env')
        self.assertNotIn(mock.call(other),
                         self.proxy.delete_network.call_args_list)

    def test_reconcile_repairs_existing_rules_and_preserves_management(self):
        created = self.module.create('tenant', 'env', '192.0.2.10')
        group = created['security_groups']['worker']
        old = self.proxy.create_security_group_rule(
            security_group_id=group, direction='egress', ether_type='IPv4',
            remote_ip_prefix='0.0.0.0/0', protocol='tcp',
            port_range_min=1, port_range_max=65535)
        management = {r.id for r in self.rule_store.values()
                      if r.security_group_id in (
                          created['security_groups']['agent-mgmt'], 'control')}
        self.config.config(group='controller_worker',
                           control_plane_cidrs=['10.0.0.0/8'])
        self.module.reconcile('tenant', 'env', '192.0.2.10')
        self.assertNotIn(old.id, self.rule_store)
        for rule in self.rule_store.values():
            if (rule.security_group_id == group and
                    rule.direction == 'egress' and rule.remote_ip_prefix):
                self.assertFalse(ipaddress.ip_network(
                    rule.remote_ip_prefix).overlaps(
                        ipaddress.ip_network('10.0.0.0/8')))
        (egress,) = self.address_groups.values()
        for value in egress.addresses:
            self.assertFalse(ipaddress.ip_network(value).overlaps(
                ipaddress.ip_network('10.0.0.0/8')))
        self.assertTrue(management.issubset(self.rule_store))
        calls = self.proxy.create_security_group_rule.call_count
        self.module.reconcile('tenant', 'env', '192.0.2.10')
        self.assertEqual(
            calls, self.proxy.create_security_group_rule.call_count)
        self.assertEqual(1, self.proxy.create_network.call_count)

    def test_full_port_range_matches_neutron_normalized_rule(self):
        group = security_group.SecurityGroup(id='sg')
        wanted = dict(direction='egress', ether_type='IPv4', protocol='udp',
                      remote_address_group_id='ag',
                      port_range_min=1, port_range_max=65535)
        stored = security_group_rule.SecurityGroupRule(
            id='stored', direction='egress', ether_type='IPv4',
            protocol='udp', remote_address_group_id='ag')
        self.proxy.security_group_rules.side_effect = None
        self.proxy.security_group_rules.return_value = [stored]
        self.module._sync_rules(group, [wanted], self.tags)
        self.proxy.create_security_group_rule.assert_not_called()
        self.proxy.delete_security_group_rule.assert_not_called()

    def _sync_concurrent_rules(self, peers):
        if peers:
            self.module.repair_peers('peer', self.tags)
        else:
            self.module._sync_rules(
                security_group.SecurityGroup(id='sg'),
                [dict(direction='egress', ether_type='IPv4', protocol='udp',
                      remote_address_group_id='ag',
                      port_range_min=1, port_range_max=65535)], self.tags)

    def test_concurrent_create_duplicate_is_present(self):
        for peers in (False, True):
            with self.subTest(peers=peers):
                self.rule_store.clear()
                self.proxy.reset_mock()

                def concurrent_create(**attrs):
                    # Simulate another apply winning the race, including
                    # Neutron's normalization of a full TCP/UDP port range.
                    if attrs.get('port_range_min') == 1:
                        attrs.pop('port_range_min')
                        attrs.pop('port_range_max')
                    rule = security_group_rule.SecurityGroupRule(
                        id=str(len(self.rule_store)), **attrs)
                    self.rule_store[rule.id] = rule
                    raise exceptions.ConflictException('duplicate',
                                                       http_status=409)

                self.proxy.create_security_group_rule.side_effect = (
                    concurrent_create)
                self._sync_concurrent_rules(peers)
                creates = 2 if peers else 1
                self.assertEqual(creates, len(self.rule_store))
                self.assertEqual(
                    1 + creates, self.proxy.security_group_rules.call_count)
                self.proxy.delete_security_group_rule.assert_not_called()

    def test_concurrent_create_unrelated_conflict_is_reraised(self):
        conflict = exceptions.ConflictException('conflict', http_status=409)
        for peers in (False, True):
            with self.subTest(peers=peers):
                self.proxy.reset_mock()
                self.proxy.create_security_group_rule.side_effect = conflict
                # A different rule appearing is insufficient confirmation.
                self.proxy.security_group_rules.side_effect = None
                self.proxy.security_group_rules.return_value = [
                    security_group_rule.SecurityGroupRule(
                        id='other', direction='ingress', ether_type='IPv4',
                        protocol='tcp', port_range_min=22, port_range_max=22)]
                caught = self.assertRaises(
                    exceptions.ConflictException,
                    self._sync_concurrent_rules, peers)
                self.assertIs(conflict, caught)
                self.assertEqual(2, self.proxy.security_group_rules.call_count)
                self.proxy.create_security_group_rule.assert_called_once()
                self.proxy.delete_security_group_rule.assert_not_called()

    def test_concurrent_delete_missing_rule_is_tolerated(self):
        for peers in (False, True):
            with self.subTest(peers=peers):
                self.rule_store.clear()
                self.proxy.reset_mock()
                self.rule_store['stale'] = (
                    security_group_rule.SecurityGroupRule(
                        id='stale', security_group_id=(
                            'control' if peers else 'sg'),
                        remote_group_id='peer', direction='egress',
                        ether_type='IPv6'))
                self.proxy.delete_security_group_rule.side_effect = (
                    exceptions.NotFoundException('gone', http_status=404))
                self._sync_concurrent_rules(peers)
                self.proxy.delete_security_group_rule.assert_called_once_with(
                    'stale')
                calls = [call[0] for call in self.proxy.mock_calls]
                self.assertLess(
                    max(i for i, name in enumerate(calls)
                        if name == 'create_security_group_rule'),
                    calls.index('delete_security_group_rule'))

    def test_rules_remove_default_egress_and_resume(self):
        group = security_group.SecurityGroup(id='sg')
        wanted = dict(direction='ingress', ether_type='IPv4',
                      protocol='tcp', remote_ip_prefix='192.0.2.10/32',
                      port_range_min=22, port_range_max=22)
        default = security_group_rule.SecurityGroupRule(
            id='default', direction='egress', ether_type='IPv6')
        good = security_group_rule.SecurityGroupRule(id='good', **wanted)
        self.proxy.security_group_rules.side_effect = None
        self.proxy.security_group_rules.return_value = [default, good]
        self.module._sync_rules(group, [wanted], self.tags)
        self.proxy.delete_security_group_rule.assert_called_once_with(
            'default')
        self.proxy.create_security_group_rule.assert_not_called()

    def test_reverse_cleanup_and_retry(self):
        self.module.create('tenant', 'env', '192.0.2.10')
        self.proxy.reset_mock()
        self.proxy.delete_router.side_effect = [
            exceptions.ConflictException('busy', http_status=409), None]
        self.module.delete('tenant', 'env')
        names = [call[0] for call in self.proxy.mock_calls
                 if call[0].startswith('delete_') or
                 call[0] == 'remove_interface_from_router']
        self.assertEqual(
            ['delete_security_group_rule'] * 2 +
            ['delete_security_group'] * 4 + ['delete_address_group'] +
            ['remove_interface_from_router', 'delete_router',
             'delete_router', 'delete_subnet', 'delete_network'], names)

    def test_delete_missing_interface(self):
        self.module.create('tenant', 'env', '192.0.2.10')
        self.proxy.remove_interface_from_router.side_effect = (
            exceptions.ResourceNotFound('gone'))
        self.module.delete('tenant', 'env')
        self.proxy.delete_network.assert_called_once()

    def test_retry_exhaustion_stops_cleanup(self):
        self.module.create('tenant', 'env', '192.0.2.10')
        self.proxy.delete_router.side_effect = (
            exceptions.ConflictException('busy', http_status=409))
        self.assertRaises(exceptions.ConflictException, self.module.delete,
                          'tenant', 'env')
        self.assertEqual(3, self.proxy.delete_router.call_count)
        self.proxy.delete_subnet.assert_not_called()

    def test_invalid_input_has_no_cloud_side_effects(self):
        for ip in ('198.51.100.10', 'not-an-ip'):
            self.assertRaises(ValueError, self.module.create,
                              'tenant', 'env', ip)
        self.proxy.create_network.assert_not_called()
        self.proxy.subnets.assert_not_called()

    def test_partial_create_resumes(self):
        self.proxy.create_subnet.side_effect = RuntimeError('interrupted')
        self.assertRaises(RuntimeError, self.module.create,
                          'tenant', 'env', '192.0.2.10')
        self.assertEqual(1, len(self.resources))
        self.proxy.create_subnet.side_effect = (
            lambda **attrs: sdk_subnet.Subnet(id='subnet', **attrs))
        self.module.create('tenant', 'env', '192.0.2.10')
        self.assertEqual(1, self.proxy.create_network.call_count)

    def test_changed_cidr_fails_closed(self):
        self.module.create('tenant', 'env', '192.0.2.10')
        self.config.config(group='controller_worker',
                           env_subnet_prefix=25)
        self.assertRaises(ValueError, self.module.create,
                          'tenant', 'env', '192.0.2.10')

    def test_non_retryable_error_stops_cleanup(self):
        self.module.create('tenant', 'env', '192.0.2.10')
        self.proxy.delete_router.side_effect = exceptions.HttpException(
            'forbidden', http_status=403)
        self.assertRaises(exceptions.HttpException, self.module.delete,
                          'tenant', 'env')
        self.assertEqual(1, self.proxy.delete_router.call_count)
        self.proxy.delete_subnet.assert_not_called()

    def test_factory_uses_neutron(self):
        with mock.patch.object(stack.clients, 'get_connection',
                               return_value=self.connection) as factory:
            stack.EnvironmentNetwork(self.conf)
        factory.assert_called_once_with('neutron', conf=self.conf)


class RulesTest(base.TestCase):
    def test_subtraction_ipv4_ipv6_and_empty(self):
        self.assertEqual(['0.0.0.0/0'],
                         stack.subtract_cidrs(['0.0.0.0/0'], []))
        self.assertEqual(['2001:db8::/65'], stack.subtract_cidrs(
            ['2001:db8::/64'], ['2001:db8:0:0:8000::/65', '10.0.0.0/8']))
        self.assertEqual([], stack.subtract_cidrs(
            ['192.0.2.0/24'], ['192.0.0.0/16']))
        self.assertEqual(['192.0.2.128/25'], stack.subtract_cidrs(
            ['192.0.2.0/24'], ['192.0.2.0/26', '192.0.2.0/25']))
        self.assertRaises(ValueError, stack.subtract_cidrs,
                          ['0.0.0.0/0'], ['invalid'])

    def test_all_env_egress_excludes_protected_ranges(self):
        self.config.config(group='controller_worker',
                           control_plane_cidrs=['10.0.0.0/8', '2001:db8::/32'],
                           dns_servers=['10.0.0.53', '192.0.2.53'],
                           ntp_servers=['10.0.0.123'])
        options = self.conf.controller_worker
        result = stack.rules(options, '192.0.2.0/24', '192.0.2.10', True,
                             'control', ['198.51.100.0/24'], 'ag')
        excluded = ['10.0.0.0/8', '198.51.100.0/24', '169.254.169.254/32']
        allowed = stack.allowed_destinations(options, ['198.51.100.0/24'])
        # The allow list is compact and never overlaps protected ranges.
        self.assertLess(len(allowed), 80)
        for value in allowed:
            for blocked in excluded:
                self.assertFalse(ipaddress.ip_network(value).overlaps(
                    ipaddress.ip_network(blocked)))
        self.assertTrue(any(ipaddress.ip_address('203.0.113.1') in
                            ipaddress.ip_network(value) for value in allowed))
        for role in ('ctrl', 'worker', 'build'):
            for rule in result[role]:
                self.assertEqual('IPv4', rule['ether_type'])
                if rule['direction'] != 'egress':
                    continue
                if 'remote_address_group_id' in rule:
                    self.assertEqual('ag', rule['remote_address_group_id'])
                    continue
                remote = ipaddress.ip_network(rule['remote_ip_prefix'])
                for value in excluded:
                    self.assertFalse(remote.overlaps(
                        ipaddress.ip_network(value)))
        # Internet egress is one rule per port range, not one per prefix.
        worker = [r for r in result['worker'] if r['direction'] == 'egress'
                  and r.get('remote_address_group_id') == 'ag']
        self.assertEqual(6, len(worker))
        dns = {r.get('remote_ip_prefix') for r in result['worker']
               if r.get('port_range_min') == 53}
        self.assertEqual({'192.0.2.53/32'}, dns)

    def test_rule_description_keeps_prefix_inside_neutron_limit(self):
        tags = ['coast', 'project_id=tenant', 'stack_id=env']
        self.assertEqual(
            'coast-test coast project_id=tenant stack_id=env',
            stack.rule_description('coast-test', tags))
        self.assertEqual('x' * 255, stack.rule_description('x' * 300, tags))
        self.assertEqual('coast', stack.rule_description('coast', []))

    def test_port_ranges(self):
        self.assertEqual([(1, 24), (26, 464), (466, 586), (588, 65535)],
                         stack.port_ranges(['25', '465', '587']))
        self.assertEqual([(2, 65534)],
                         stack.port_ranges(['1', '65535', '1']))
        self.assertEqual([(1, 24), (28, 65535)],
                         stack.port_ranges(['25', '26', '27', '26']))
        self.assertEqual([(1, 65535)], stack.port_ranges([]))
        self.assertRaises(ValueError, stack.port_ranges, ['0'])

    def test_merge_overlapping_and_adjacent_egress_ranges(self):
        def rule(protocol, remote, low, high):
            return dict(direction='egress', ether_type='IPv4',
                        protocol=protocol, remote_ip_prefix=remote,
                        port_range_min=low, port_range_max=high)

        tcp = rule('tcp', '0.0.0.0/0', 1, 100)
        udp = rule('udp', '0.0.0.0/0', 50, 60)
        dns = rule('tcp', '192.0.2.53/32', 53, 53)
        self.assertEqual([tcp, udp, dns], stack.merge_egress_ranges([
            rule('tcp', '0.0.0.0/0', 40, 100),
            rule('tcp', '0.0.0.0/0', 1, 20),
            rule('tcp', '0.0.0.0/0', 21, 50), udp, dns]))

    def test_role_rules(self):
        self.config.config(group='controller_worker',
                           control_plane_cidrs=['10.0.0.0/8'],
                           dns_servers=['10.0.0.53'],
                           ntp_servers=['10.0.0.123'])
        result = stack.rules(self.conf.controller_worker,
                             '192.0.2.0/24', '192.0.2.10', True,
                             'control')
        for role in ('ctrl', 'build'):
            dhcp = [r for r in result[role]
                    if r['direction'] == 'egress' and
                    r['protocol'] == 'udp' and
                    r['port_range_min'] <= 67 <= r['port_range_max']]
            self.assertTrue(any(ipaddress.ip_address('255.255.255.255') in
                                ipaddress.ip_network(r['remote_ip_prefix'])
                                for r in dhcp))
        mgmt = result['agent-mgmt']
        self.assertEqual(2, len(mgmt))
        self.assertEqual({'control'}, {r['remote_group_id'] for r in mgmt})
        for role in ('worker', 'build'):
            ssh = [r for r in result[role]
                   if r['direction'] == 'ingress' and
                   r['port_range_min'] == 22]
            self.assertEqual(['192.0.2.10/32'],
                             [r['remote_ip_prefix'] for r in ssh])
        self.assertTrue(any(r.get('port_range_min') == 5000
                            for r in result['ctrl']))
        echo = [r for r in result['worker'] if r['protocol'] == 'icmp']
        self.assertTrue(echo)
        self.assertEqual({(8, 0)}, {(r['port_range_min'], r['port_range_max'])
                                    for r in echo})
        for r in result['worker']:
            if r['direction'] == 'egress' and r['protocol'] == 'tcp':
                self.assertFalse(any(r['port_range_min'] <= port <=
                                     r['port_range_max']
                                     for port in (25, 465, 587)))
        without_build = stack.rules(
            self.conf.controller_worker, '192.0.2.0/24',
            '192.0.2.10', False, 'control')
        self.assertFalse(any(r.get('port_range_min') == 5000
                             for r in without_build['ctrl']))

    def test_exact_default_role_allow_lists(self):
        result = stack.rules(
            self.conf.controller_worker, '192.0.2.0/24',
            '192.0.2.10', True, 'control')

        def normalized(role):
            return {(r['direction'], r['protocol'],
                     r.get('remote_ip_prefix') or r.get('remote_group_id'),
                     r['port_range_min'], r['port_range_max'])
                    for r in result[role]}

        internet = '0.0.0.0/0'
        dns_ntp = {('egress', 'tcp', internet, 53, 53),
                   ('egress', 'udp', internet, 53, 53),
                   ('egress', 'udp', internet, 123, 123),
                   ('egress', 'udp', internet, 67, 67)}
        web = {('egress', 'tcp', internet, 80, 80),
               ('egress', 'tcp', internet, 443, 443)}
        self.assertRulesEqual(
            dns_ntp | web | {('egress', 'tcp', '192.0.2.0/24', 22, 22),
                             ('ingress', 'tcp', '192.0.2.0/24', 5000, 5000)},
            normalized('ctrl'))
        self.assertRulesEqual(
            dns_ntp | web |
            {('ingress', 'tcp', '192.0.2.10/32', 22, 22),
             ('egress', 'tcp', '192.0.2.10/32', 5000, 5000),
             ('egress', 'tcp', internet, 9418, 9418),
             ('egress', 'tcp', internet, 22, 22)}, normalized('build'))
        self.assertRulesEqual(
            {('ingress', 'tcp', '192.0.2.10/32', 22, 22),
             ('ingress', 'tcp', '0.0.0.0/0', 80, 80),
             ('ingress', 'tcp', '0.0.0.0/0', 443, 443),
             ('egress', 'tcp', internet, 1, 24),
             ('egress', 'tcp', internet, 26, 464),
             ('egress', 'tcp', internet, 466, 586),
             ('egress', 'tcp', internet, 588, 65535),
             ('egress', 'udp', internet, 1, 65535),
             ('egress', 'icmp', internet, 8, 0)}, normalized('worker'))
        self.assertRulesEqual(
            {('ingress', 'tcp', 'control', 9443, 9443),
             ('egress', 'udp', 'control', 5565, 5565)},
            normalized('agent-mgmt'))

    def test_heartbeat_port_is_only_on_the_management_group(self):
        result = stack.rules(
            self.conf.controller_worker, '192.0.2.0/24',
            '192.0.2.10', True, 'control',
            management_cidrs=['192.0.2.128/25'])
        hits = [role for role, rules in result.items()
                if any(rule.get('port_range_min') == 5565 for rule in rules)]
        self.assertEqual(['agent-mgmt'], hits)
        for role in ('ctrl', 'worker', 'build'):
            for rule in result[role]:
                self.assertNotEqual('control', rule.get('remote_group_id'))

    def assertRulesEqual(self, expected, actual):
        self.assertEqual(set(expected), actual)
