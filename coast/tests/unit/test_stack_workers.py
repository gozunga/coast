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

"""Initial servers reuse the separate add tasks inside the stack flow."""

import base64
import copy
import json
from types import SimpleNamespace
from unittest import mock

from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import server_add
from coast.conductor import stack_create
from coast.db import api as db_api
from coast.db import models
from coast.drivers import base as driver_base
from coast import objects
from coast.tests.unit import test_server_flows
from coast.tests.unit import test_stack_create


class StackWorkersTest(test_stack_create.StackCreateTest):
    def create_network(self, project, stack_id, address,
                       has_build_servers=False):
        result = super().create_network(project, stack_id, address)
        result['security_groups']['build'] = 'build-sg'
        return result
    ensure_fip = test_server_flows.ServerFlowTest.ensure_fip
    delete_fip = test_server_flows.ServerFlowTest.delete_fip

    def accepted(self, options=None):
        self.config.config(servers=4, group='quotas')
        self.config.config(image_tag_worker='worker-pin',
                           external_network_id='external',
                           data_volume_size_gb=10,
                           group='controller_worker')
        self.config.config(definitions=json.dumps([
            dict(name='control', kind='vm', roles=['control'],
                 flavor='flavor'),
            dict(name='worker', kind='vm', roles=['worker', 'build'],
                 flavor='worker-flavor')]), group='plans')
        image = self.proxies['glance'].images.return_value[0]
        self.proxies['glance'].images.return_value.append(SimpleNamespace(
            id='worker-image', tags=['worker-pin'], owner=image.owner,
            status='active', properties={'coast_role': 'worker'}))
        self.manager.drivers['coolify_v4'] = self.driver
        self.driver.register_server.side_effect = self.register_server
        self.floating = {}
        self.fip = mock.Mock()
        self.fip.ensure.side_effect = self.ensure_fip
        self.fip.delete.side_effect = self.delete_fip
        self.patch(server_add.fip, 'FloatingIP', return_value=self.fip)
        self.roles = []
        return super().accepted(options or dict(
            workers=[dict(kind='vm', plan='worker')], build_server=True))

    def register_server(self, endpoint, stack, server, key):
        self.assert_no_transaction()
        self.assertEqual(1, self.agent.bootstrap.call_count)
        controllers = objects.ServerList.get_all(
            self.ctx, filters={'role': 'control'})
        self.assertEqual(states.ACTIVE, controllers[0].status)
        self.assertEqual(states.BUILDING, stack.status)
        self.assertEqual(states.PROVISIONING, stack.task_state)
        self.roles.append(server.role)
        return test_stack_create.driver_base.DriverResult(
            engine_ref=server.id)

    def delete_cloud(self, kind, value):
        # Other guests can still exist when this guest's volume is removed.
        self.assert_no_transaction()
        identifier = value if isinstance(value, str) else value.id
        self.cloud_resources[kind].pop(identifier, None)

    def test_admin_key_on_all_initial_guests(self):
        self.config.config(group='controller_worker',
                           admin_ssh_key_name='operator-key')
        self.accepted()
        self.assertEqual(states.ACTIVE, self.execute().status)
        guests = list(self.cloud_resources['server'].values())
        self.assertEqual(3, len(guests))
        self.assertEqual({'operator-key'}, {g.key_name for g in guests})
        for guest in guests:
            data = base64.b64decode(guest.user_data).decode()
            payload = json.loads(data.split('\n', 1)[1])
            self.assertEqual(guest.name, payload['hostname'])
            self.assertEqual(guest.name, payload['fqdn'])
            self.assertFalse(payload['preserve_hostname'])
            if 'role=controller\n' in payload['write_files'][0]['content']:
                self.assertEqual(['default'], payload['users'])
            else:
                self.assertFalse(payload['allow_public_ssh_keys'])
                self.assertTrue(payload['disable_root'])
                self.assertEqual(
                    ['ssh-ed25519 admin-public'],
                    payload['users'][0]['ssh_authorized_keys'])
                root = next(f for f in payload['write_files'] if
                            f['path'] == '/root/.ssh/authorized_keys')
                stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
                self.assertEqual(stack.ssh_public_key + '\n', root['content'])

    def test_every_guest_keeps_existing_volume_filesystems(self):
        # A moved or retained Cinder disk must never be reformatted. Only
        # partition 'auto' with overwrite False reuses a labelled ext4.
        self.accepted(dict(workers=[dict(kind='vm', plan='worker')],
                           build_server=True))
        self.assertEqual(states.ACTIVE, self.execute().status)
        guests = list(self.cloud_resources['server'].values())
        self.assertEqual(3, len(guests))
        for guest in guests:
            data = base64.b64decode(guest.user_data).decode()
            payload = json.loads(data.split('\n', 1)[1])
            self.assertEqual(guest.name, payload['hostname'])
            self.assertEqual(guest.name, payload['fqdn'])
            self.assertFalse(payload['preserve_hostname'])
            self.assertTrue(payload['fs_setup'])
            for item in payload['fs_setup']:
                self.assertEqual(('auto', False, 'ext4'), (
                    item['partition'], item['overwrite'],
                    item['filesystem']))
                self.assertTrue(item['label'].startswith('coast-'))
                self.assertLessEqual(len(item['label']), 16)
            self.assertNotIn('disk_setup', payload)
            mapping = guest.block_device_mapping[1:]
            self.assertTrue(mapping)
            self.assertFalse(any(m['delete_on_termination']
                                 for m in mapping))

    def test_initial_build_and_workers_then_duplicate(self):
        self.accepted(dict(workers=[dict(kind='vm', plan='worker')] * 2,
                           build_server=True))
        self.assertEqual(states.ACTIVE, self.execute().status)
        self.assertEqual(['build', 'worker', 'worker'], self.roles)
        servers = objects.ServerList.get_all(self.ctx)
        self.assertEqual(4, len(servers))
        self.assertEqual(['build-1', 'controller', 'worker-1', 'worker-2'],
                         sorted(s.name for s in servers))
        self.assertEqual({states.ACTIVE}, {s.status for s in servers})
        self.assertEqual({states.NONE}, {s.task_state for s in servers})
        self.assertEqual(2, len(self.floating))
        self.assertEqual(3, self.counters().in_use_servers)
        self.assertEqual(1, self.counters().in_use_build_servers)
        self.assertEqual(140, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().reserved_servers)
        before = copy.deepcopy(self.cloud_resources)
        self.execute()
        self.assertEqual(before, self.cloud_resources)
        self.assertEqual(['build', 'worker', 'worker'], self.roles)

    def test_initial_build_egress_fault_reverts_stack(self):
        self.accepted()
        self.agent.wait_health.side_effect = [
            None, exception.GuestEgressError('secret')]
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual('no internet egress via stack router', stack.fault)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.payloads)
        self.assertFalse(self.networks)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().in_use_build_servers)
        self.assertFalse(self.roles)

    def test_worker_validation_timeout_reverts_build_and_controller(self):
        self.accepted()
        original = self.register_server

        def fail(*args):
            result = original(*args)
            if args[2].role == 'worker':
                raise driver_base.DriverError(
                    'Coolify server validation timed out')
            return result

        self.driver.register_server.side_effect = fail
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertEqual(['build', 'worker'], self.roles)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.payloads)
        self.assertFalse(self.floating)
        self.assertEqual({states.ERROR}, {
            s.status for s in objects.ServerList.get_all(self.ctx)})
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(0, self.counters().in_use_build_servers)
        self.assertEqual(0, self.counters().in_use_volume_gb)

    def test_uncertain_registration_preserves_controller_dependencies(self):
        self.accepted(dict(workers=[dict(kind='vm', plan='worker')]))
        self.driver.register_server.side_effect = RuntimeError('sensitive')
        self.driver.unregister_server.side_effect = RuntimeError('sensitive')
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertTrue(stack.network_id)
        self.assertTrue(stack.engine_token_ref)
        self.assertEqual(2, len(self.cloud_resources['server']))
        self.assertEqual(5, len(self.cloud_resources['volume']))
        self.assertEqual(4, len(self.cloud_resources['port']))
        self.assertEqual(1, len(self.floating))
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(0, self.counters().in_use_volume_gb)

    def test_no_unique_build_plan_fails_before_cloud(self):
        self.accepted()
        self.config.config(definitions='[]', group='plans')
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertEqual(0, self.counters().in_use_servers)

    def assert_names(self, prefix):
        for server in objects.ServerList.get_all(self.ctx):
            self.assertTrue(server.hostname.startswith(prefix + '-'))
            self.assertEqual(server.hostname,
                             self.cloud_resources['server'][
                                 server.compute_id].name)
            names = {port.name for port in
                     self.cloud_resources['port'].values()}
            self.assertIn(prefix + '-' + server.id + '-mgmt', names)
            self.assertIn(prefix + '-' + server.id + '-env', names)
        for volume in objects.VolumeList.get_all(self.ctx):
            expected = '%s-%s-%s' % (
                prefix, volume.server_id, volume.purpose)
            self.assertEqual(expected, volume.name)
            self.assertEqual(expected, self.cloud_resources['volume'][
                volume.cinder_volume_id].name)

    def test_default_guest_and_resource_names(self):
        self.accepted()
        self.assertEqual(states.ACTIVE, self.execute().status)
        self.assert_names('coast')

    def test_custom_prefix_creation_and_revert(self):
        self.config.config(resource_name_prefix='operator')
        self.accepted()

        names = []

        def fail(flow):
            names.extend(resource.name for resources in
                         self.cloud_resources.values()
                         for resource in resources.values())
            raise RuntimeError('sensitive')

        with mock.patch.object(stack_create.CreateStack, 'complete', fail):
            self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.payloads)
        self.assertEqual(16, len(names))
        self.assertTrue(all(name.startswith('operator-') for name in names))

    def test_custom_prefix_names(self):
        self.config.config(resource_name_prefix='operator')
        self.accepted()
        self.assertEqual(states.ACTIVE, self.execute().status)
        self.assert_names('operator')

    def test_failed_final_commit_reverts_all_servers(self):
        self.accepted()
        original = stack_create.CreateStack.save

        def fail(flow, resource, **values):
            if (resource is flow.stack and
                    values.get('status') == states.ACTIVE):
                raise RuntimeError('sensitive')
            return original(flow, resource, **values)

        with mock.patch.object(stack_create.CreateStack, 'save', fail):
            stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual(['build', 'worker'], self.roles)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.floating)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual({states.ERROR}, {
            s.status for s in objects.ServerList.get_all(self.ctx)})
        events = objects.EventList.get_all(self.ctx)
        self.assertFalse(any(event.action == 'create' and
                             event.outcome == 'success' for event in events))

    def _overquota_at_later_volume(self):
        # Control data, docker, and registry fit. The next data volume does
        # not. The create is already at zero before delete.
        self.config.config(volume_gb=50, group='quotas')
        self.accepted()
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().reserved_volume_gb)
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(0, self.counters().in_use_environments)
        volumes = objects.VolumeList.get_all(self.ctx)
        self.assertEqual(50, sum(item.size_gb for item in volumes))
        self.assertTrue(volumes)
        return stack

    def _set_volume_counters(self, in_use, reserved):
        with db_api.writer.using(self.ctx):
            row = self.ctx.session.query(models.QuotaUsage).one()
            row.in_use_volume_gb = in_use
            row.reserved_volume_gb = reserved

    def _delete_stack(self, stack):
        self.manager.execute(self.ctx, 'delete_stack', stack)
        return objects.Stack.get_by_id(
            self.ctx, stack.id, show_deleted=True)

    def _cleanup_receipts(self, stack):
        return [event for event in objects.EventList.get_all(
            self.ctx, filters={'stack_id': stack.id,
                               'action': 'delete.cleanup'})
                if event.outcome == 'success']

    def test_later_volume_overquota_then_delete_releases_once(self):
        stack = self._overquota_at_later_volume()
        deleted = self._delete_stack(stack)
        self.assertEqual(states.DELETED, deleted.status)
        self.assertIsNone(deleted.fault)
        self.assertFalse(objects.VolumeList.get_all(self.ctx))
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().reserved_volume_gb)
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(0, self.counters().in_use_environments)
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        again = self._delete_stack(stack)
        self.assertEqual(states.DELETED, again.status)
        self.assertEqual(0, self.counters().in_use_volume_gb)

    def test_delete_releases_failed_volume_quota_held_with_reservation(self):
        stack = self._overquota_at_later_volume()
        self._set_volume_counters(50, 1)
        deleted = self._delete_stack(stack)
        self.assertEqual(states.DELETED, deleted.status)
        self.assertIsNone(deleted.fault)
        self.assertFalse(objects.VolumeList.get_all(self.ctx))
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(1, self.counters().reserved_volume_gb)
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        self.assertFalse(
            quota.release_exact_failed_volume_gb(self.ctx, 50))
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(1, self.counters().reserved_volume_gb)
        receipts = self._cleanup_receipts(stack)
        self.assertTrue(receipts)
        self.assertTrue(all(
            json.loads(event.message)['quota_released'] is True
            for event in receipts))

    def test_delete_does_not_take_other_volume_quota_while_reserved(self):
        stack = self._overquota_at_later_volume()
        self._set_volume_counters(40, 1)
        deleted = self._delete_stack(stack)
        self.assertEqual(states.DELETED, deleted.status)
        self.assertIsNone(deleted.fault)
        self.assertFalse(objects.VolumeList.get_all(self.ctx))
        self.assertEqual(40, self.counters().in_use_volume_gb)
        self.assertEqual(1, self.counters().reserved_volume_gb)
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(0, self.counters().in_use_environments)
        receipts = self._cleanup_receipts(stack)
        self.assertTrue(receipts)
        self.assertTrue(all(
            json.loads(event.message)['quota_released'] is False
            for event in receipts))


# Reuse the cloud fixture, not controller-only test cases.
for _name in dir(test_stack_create.StackCreateTest):
    if _name.startswith('test_') and _name not in StackWorkersTest.__dict__:
        setattr(StackWorkersTest, _name, None)
