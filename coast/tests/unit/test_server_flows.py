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

"""VM add/remove state, transport and cleanup regression tests."""

import base64
import copy
import json
from types import SimpleNamespace
from unittest import mock

import fixtures
from openstack import exceptions as sdk_exceptions

from coast.common import context
from coast.common import exception
from coast.common import naming
from coast.common import quota
from coast.common import states
from coast.conductor import controller
from coast.conductor import rpcapi
from coast.conductor import server_add
from coast.conductor import server_remove
from coast.conductor import worker
from coast.db import api
from coast.db import models
from coast.drivers import base as driver_base
from coast.health_manager import stale
from coast import objects
from coast.tests.unit import test_stack_create


class ServerFlowTest(test_stack_create.StackCreateTest):
    def create_network(self, project, stack_id, address,
                       has_build_servers=False):
        result = super().create_network(project, stack_id, address)
        result['security_groups']['build'] = 'build-sg'
        return result

    def accepted(self, role='worker', kind='vm'):
        self.config.config(image_tag_worker='worker-pin',
                           external_network_id='external',
                           data_volume_size_gb=10,
                           group='controller_worker')
        self.config.config(definitions=json.dumps([dict(
            name='worker-plan', kind='vm', roles=['worker', 'build'],
            flavor='flavor')]), group='plans')
        self.proxies['glance'].images.return_value[0].tags = ['worker-pin']
        self.proxies['glance'].images.return_value[0].properties = {
            'coast_role': 'worker'}
        self.stack = objects.Stack(
            context=self.ctx, name='stack', plan='control',
            driver='coolify_v4', engine_version='4.4.3',
            status=states.ACTIVE, task_state=states.NONE,
            network_id='net', sg_mgmt_id='agent-sg',
            sg_ingress_id='worker-sg', ssh_key_ref='ssh-ref',
            ssh_public_key='ssh-ed25519 public', heartbeat_key_ref='hmac-ref')
        self.stack.create()
        self.payloads.update({'ssh-ref': 'private-sensitive',
                              'hmac-ref': 'a' * 64})
        self.endpoint = driver_base.EngineEndpoint(
            'https://example.com/engine', '', '', '', 'token-sensitive')
        self.patch(self.manager.__class__, '_endpoint',
                   return_value=self.endpoint)
        self.manager.drivers['coolify_v4'] = self.driver
        self.driver.register_server.return_value = (
            driver_base.DriverResult(engine_ref='worker-engine'))
        for method in ('register_server', 'unregister_server'):
            getattr(self.driver, method).side_effect = (
                lambda *args: self.assert_no_transaction() or
                driver_base.DriverResult(engine_ref='worker-engine'))
        self.floating = {}
        self.fip = mock.Mock()
        self.fip.ensure.side_effect = self.ensure_fip
        self.fip.delete.side_effect = self.delete_fip
        self.patch(server_add.fip, 'FloatingIP', return_value=self.fip)
        if kind == 'baremetal':
            quota.update(context.RequestContext(project_id='project',
                                                is_admin=True),
                         'project', {'baremetal_servers': 1})
        self.server = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='worker',
            kind=kind, role=role, status=states.PENDING,
            task_state=states.NONE)
        with api.writer.using(self.ctx):
            self.reservation = quota.deltas('server', self.server)
            quota.reserve(self.ctx, **self.reservation)
            naming.assign_server_hostname(
                self.ctx, self.stack, self.server, conf=self.conf)
            self.server.create()
            self.server.acquire_task(states.PROVISIONING)
        self.revision = rpcapi.revision(self.server)

    def ensure_fip(self, project, stack, server, port):
        self.assert_no_transaction()
        result = SimpleNamespace(id='fip-id',
                                 floating_ip_address='203.0.113.10')
        self.floating[server] = result
        return result

    def delete_fip(self, project, stack, server):
        self.assert_no_transaction()
        self.floating.pop(server, None)

    def execute(self):
        self.manager.execute(
            self.ctx, 'create_server', self.server,
            options={'plan': 'worker-plan'}, reservation=self.reservation,
            revision=self.revision)
        return objects.Server.get_by_id(self.ctx, self.server.id)

    def remove(self):
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.acquire_task(states.DELETING)
        self.server.status = states.DELETING
        self.server.save()
        self.manager.execute(self.ctx, 'delete_server', self.server,
                             revision=rpcapi.revision(self.server))

    def set_stack_status(self, status, fault=None):
        with api.writer.using(self.ctx) as session:
            session.query(models.Stack).filter_by(id=self.stack.id).update({
                'status': status, 'fault': fault})

    def mark_degraded(self, fault):
        self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.stack.status = states.DEGRADED
        self.stack.fault = fault
        self.stack.save()

    def build_mode_targets(self):
        worker = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='existing-worker',
            kind='vm', role='worker', status=states.ACTIVE,
            task_state=states.NONE, engine_ref='existing-worker',
            compute_id='existing-nova')
        worker.create()
        services = []
        for kind in ('git', 'image'):
            source = objects.Source(context=self.ctx, type=kind)
            source.create()
            service = objects.Service(
                context=self.ctx, stack_id=self.stack.id, name=kind,
                source_id=source.id, server_id=worker.id, ports='8080',
                replicas=1, auto_deploy=False, status=states.ACTIVE,
                task_state=states.NONE,
                engine_ref='existing-' + kind)
            service.create()
            services.append(service)
        self.mode = False

        def switch(ep, stack, workers, targets, enabled):
            self.assert_no_transaction()
            self.assertEqual([worker.id], [row.id for row in workers])
            self.assertEqual([services[0].id], [row.id for row in targets])
            self.mode = enabled
        self.driver.set_build_server_mode.side_effect = switch

    def other_build(self, usable=True):
        other = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='other-build',
            kind='vm', role='build', status=states.ACTIVE,
            task_state=states.NONE, engine_ref='other-engine',
            compute_id='other-nova')
        other.create()
        self.driver.get_server_status.return_value = (
            driver_base.ObservedStatus(status=states.ACTIVE if usable else
                                       states.UNHEALTHY))

    def test_first_usable_build_switches_existing_git_only(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.assertEqual(states.ACTIVE, self.execute().status)
        self.assertTrue(self.mode)
        self.execute()  # duplicate receipt cannot repeat the operation
        self.assertEqual(1, self.driver.set_build_server_mode.call_count)

    def test_second_usable_build_does_not_switch(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.other_build()
        self.assertEqual(states.ACTIVE, self.execute().status)
        self.assertFalse(self.mode)
        self.driver.set_build_server_mode.assert_not_called()

    def test_unhealthy_other_build_does_not_block_first_usable_switch(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.other_build(usable=False)
        self.assertEqual(states.ACTIVE, self.execute().status)
        self.assertTrue(self.mode)

    def test_remove_refuses_other_stack_statuses(self):
        self.accepted()
        self.execute()
        refused = sorted(set(states.TRANSITIONS['stack']) - {
            states.ACTIVE, states.DEGRADED})
        for status in refused:
            self.set_stack_status(status)
            self.remove()
            server = objects.Server.get_by_id(self.ctx, self.server.id)
            self.assertEqual(states.ERROR, server.status)
            self.assertEqual(states.NONE, server.task_state)
            stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            self.assertEqual(status, stack.status)
            self.assertEqual(states.NONE, stack.task_state)
        self.assertTrue(self.cloud_resources['server'])

    def test_degraded_remove_returns_stack_to_active(self):
        self.accepted()
        self.execute()
        self.mark_degraded(stale.HEALTH_FAULT)
        self.remove()
        self.assertRaises(
            exception.NotFound, objects.Server.get_by_id, self.ctx,
            self.server.id)
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual(states.ACTIVE, stack.status)
        self.assertIsNone(stack.fault)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertFalse(self.cloud_resources['server'])

    def test_degraded_remove_keeps_stack_when_a_peer_is_unhealthy(self):
        self.accepted()
        self.execute()
        peer = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='peer',
            kind='vm', role='worker', status=states.UNHEALTHY,
            task_state=states.NONE)
        peer.create()
        self.mark_degraded(stale.HEALTH_FAULT)
        self.remove()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual(states.DEGRADED, stack.status)
        self.assertEqual(stale.HEALTH_FAULT, stack.fault)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual(states.UNHEALTHY, objects.Server.get_by_id(
            self.ctx, peer.id).status)

    def test_degraded_remove_keeps_an_unrelated_fault(self):
        self.accepted()
        self.execute()
        self.mark_degraded('Operator stop.')
        self.remove()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual(states.DEGRADED, stack.status)
        self.assertEqual('Operator stop.', stack.fault)
        self.assertEqual(states.NONE, stack.task_state)

    def test_missing_guest_and_ports_still_remove(self):
        self.accepted()
        self.execute()
        # Nova already deleted the guest. The fake store must match that.
        self.cloud_resources['server'].clear()
        self.proxies['nova'].get_server.side_effect = (
            sdk_exceptions.ResourceNotFound)
        self.proxies['neutron'].delete_port.side_effect = (
            sdk_exceptions.ResourceNotFound)
        self.remove()
        self.assertRaises(
            exception.NotFound, objects.Server.get_by_id, self.ctx,
            self.server.id)
        self.proxies['nova'].delete_server.assert_not_called()
        self.assertEqual(2, self.proxies['neutron'].delete_port.call_count)
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual(states.ACTIVE, stack.status)
        self.assertEqual(states.NONE, stack.task_state)

    def test_last_remove_switches_before_deregistration(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.execute()

        def deregister(*args):
            self.assertFalse(self.mode)
            self.assertTrue(self.cloud_resources['server'])
        self.driver.unregister_server.side_effect = deregister
        self.remove()
        self.assertFalse(self.mode)
        self.assertFalse(self.cloud_resources['server'])

    def test_non_last_remove_does_not_switch(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.execute()
        self.other_build(usable=False)
        self.driver.set_build_server_mode.reset_mock()
        self.remove()
        self.assertTrue(self.mode)
        self.driver.set_build_server_mode.assert_not_called()

    def test_add_patch_failure_retains_guest_references(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.driver.set_build_server_mode.side_effect = RuntimeError('secret')
        row = self.execute()
        self.assertEqual(states.ERROR, row.status)
        self.assertTrue(row.engine_ref)
        self.assertTrue(row.compute_id)
        self.assertFalse(self.mode)
        self.assertTrue(self.cloud_resources['server'])
        self.assertEqual(2, len(self.cloud_resources['volume']))
        self.assertEqual(2, len(self.cloud_resources['port']))
        self.assertEqual(0, self.counters().in_use_build_servers)
        self.driver.unregister_server.assert_not_called()
        events = objects.EventList.get_all(self.ctx)
        self.assertTrue(any(row.resource_id == self.server.id and
                            row.action == 'create' and row.outcome == 'failure'
                            for row in events))

    def test_remove_patch_failure_retains_guest_and_can_retry(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.execute()
        switch = self.driver.set_build_server_mode.side_effect
        self.driver.set_build_server_mode.side_effect = RuntimeError('secret')
        self.remove()
        row = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, row.status)
        self.assertTrue(row.engine_ref)
        self.assertTrue(row.compute_id)
        self.assertTrue(self.mode)
        self.assertEqual(1, self.counters().in_use_build_servers)
        self.driver.unregister_server.assert_not_called()
        self.driver.set_build_server_mode.side_effect = switch
        self.remove()
        self.assertFalse(self.mode)
        self.assertFalse(self.cloud_resources['server'])

    def test_failed_deregistration_restores_build_mode(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.execute()
        self.driver.unregister_server.side_effect = RuntimeError('secret')
        self.remove()
        self.assertTrue(self.mode)
        row = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, row.status)
        self.assertTrue(row.engine_ref)
        self.assertTrue(row.compute_id)

    def test_failed_nova_delete_does_not_restore_deregistered_builder(self):
        self.accepted(role='build')
        self.build_mode_targets()
        self.execute()
        self.proxies['nova'].delete_server.side_effect = RuntimeError('secret')
        self.remove()
        self.assertFalse(self.mode)
        row = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertIsNone(row.engine_ref)
        self.assertTrue(row.compute_id)

    def test_failed_add_completion_restores_mode_before_guest_cleanup(self):
        self.accepted(role='build')
        self.build_mode_targets()
        original = server_add.AddServer.save

        def fail(flow, resource, **values):
            if values.get('status') == states.ACTIVE:
                raise RuntimeError('secret')
            return original(flow, resource, **values)

        with mock.patch.object(server_add.AddServer, 'save', fail):
            self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(self.mode)
        self.assertFalse(self.cloud_resources['server'])
        row = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertIsNone(row.engine_ref)
        self.assertIsNone(row.compute_id)

    def test_failed_add_mode_revert_keeps_guest(self):
        self.accepted(role='build')
        self.build_mode_targets()
        switch = self.driver.set_build_server_mode.side_effect

        def fail_reverse(*args):
            if not args[-1]:
                raise RuntimeError('secret')
            switch(*args)
        self.driver.set_build_server_mode.side_effect = fail_reverse
        with mock.patch.object(server_add.AddServer, 'complete',
                               side_effect=RuntimeError('secret')):
            row = self.execute()
        self.assertEqual(states.ERROR, row.status)
        self.assertTrue(self.mode)
        self.assertTrue(row.compute_id)
        self.assertTrue(row.engine_ref)
        self.assertTrue(self.cloud_resources['server'])
        self.driver.unregister_server.assert_not_called()

    def test_admin_key_on_added_build(self):
        self.config.config(group='controller_worker',
                           admin_ssh_key_name='operator-key')
        self.accepted(role='build')
        server = self.execute()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual('operator-key',
                         self.cloud_resources['server'][
                             server.compute_id].key_name)
        self.assertEqual('agent-sg', self.cloud_resources['port'][
            self.proxies['nova'].create_server.call_args.kwargs[
                'networks'][0]['port']].security_group_ids[0])

    def test_config_drive_separates_admin_and_engine_keys(self):
        for role in ('worker', 'build'):
            for engine_user in ('root', 'engine'):
                for admin_name in ('', 'operator-key'):
                    with self.subTest(role=role, engine_user=engine_user,
                                      admin_name=admin_name):
                        self.config.config(group='controller_worker',
                                           admin_ssh_key_name=admin_name)
                        self.config.config(group='coolify',
                                           ssh_user=engine_user)
                        cloud = worker.WorkerCloud(self.conf, SimpleNamespace(
                            id='stack', project_id='project',
                            ssh_public_key='ssh-ed25519 engine-public'))
                        server = SimpleNamespace(
                            hostname='coast-shop-5bad06-worker-1',
                            id='server', mgmt_ip='192.0.2.10', role=role)
                        data, serial, expires = cloud.config_drive(
                            server, [], 'heartbeat')
                        payload = json.loads(base64.b64decode(data).decode().
                                             split('\n', 1)[1])
                        self.assertFalse(payload['allow_public_ssh_keys'])
                        self.assertTrue(payload['disable_root'])
                        self.assertNotIn('ssh_authorized_keys', payload)
                        admin, engine = payload['users']
                        self.assertEqual('default', admin['name'])
                        self.assertEqual(
                            ['ssh-ed25519 admin-public'] if admin_name else [],
                            admin.get('ssh_authorized_keys', []))
                        self.assertEqual(engine_user, engine['name'])
                        self.assertEqual(['ssh-ed25519 engine-public'],
                                         engine['ssh_authorized_keys'])
                        root = next(f for f in payload['write_files'] if
                                    f['path'] == '/root/.ssh/authorized_keys')
                        self.assertEqual(
                            'ssh-ed25519 engine-public\n'
                            if engine_user == 'root' else '', root['content'])
                        self.assertEqual('0600', root['permissions'])
                        self.assertEqual('root:root', root['owner'])

    def test_worker_and_build_link_either_disk_bus(self):
        volume_id = '1831dfbb-951f-4e73-b54d-c493a64a1a32'
        volume = SimpleNamespace(
            cinder_volume_id=volume_id, purpose='data', mount_path='/data')
        for role in ('worker', 'build'):
            with self.subTest(role=role):
                cloud = worker.WorkerCloud(self.conf, SimpleNamespace(
                    id='stack', project_id='project',
                    ssh_public_key='ssh-ed25519 engine-public'))
                server = SimpleNamespace(
                    hostname='coast-shop-5bad06-worker-1',
                    id='server', mgmt_ip='192.0.2.10', role=role)
                data, _serial, _expires = cloud.config_drive(
                    server, [volume], 'heartbeat')
                payload = json.loads(base64.b64decode(data).decode().split(
                    '\n', 1)[1])
                alias = controller.disk_alias(volume_id)
                self.assertEqual([alias], [
                    item['device'] for item in payload['fs_setup']])
                self.assertEqual(alias, payload['mounts'][0][0])
                self.assertEqual('/data', payload['mounts'][0][1])
                script = payload['bootcmd'][0]
                for path in controller.disk_candidates(volume_id):
                    self.assertIn(path, script)
                self.assertIn('role=' + role + '\n',
                              payload['write_files'][0]['content'])

    def test_missing_admin_public_key_fails_before_boot(self):
        self.config.config(group='controller_worker',
                           admin_ssh_key_name='operator-key')
        self.proxies['nova'].get_keypair.return_value.public_key = ''
        self.accepted()
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(self.cloud_resources['server'])

    def test_custom_prefix_add_and_remove(self):
        self.config.config(resource_name_prefix='operator')
        self.accepted()
        self.assertEqual(states.ACTIVE, self.execute().status)
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertTrue(server.hostname.startswith('operator-'))
        self.assertEqual(server.hostname,
                         self.cloud_resources['server'][
                             server.compute_id].name)
        self.remove()
        self.assertFalse(self.cloud_resources['server'])
        self.assertFalse(self.cloud_resources['port'])
        self.assertEqual(1, len(self.cloud_resources['volume']))
        retained = next(iter(self.cloud_resources['volume'].values()))
        self.assertEqual('operator-' + server.id + '-data', retained.name)

    def test_build_probe_url_is_rendered(self):
        self.config.config(egress_check_url='https://registry.example.com/v2/',
                           group='controller_worker')
        self.accepted(role='build')
        server = self.execute()
        self.assertEqual(states.ACTIVE, server.status)
        nova = self.cloud_resources['server'][server.compute_id]
        payload = json.loads(base64.b64decode(nova.user_data).decode().split(
            '\n', 1)[1])
        configuration = payload['write_files'][0]['content']
        self.assertIn('role=build\n', configuration)
        self.assertIn('egress_check_url=https://registry.example.com/v2/\n',
                      configuration)

    def test_worker_egress_error_preserves_safe_fault(self):
        self.accepted()
        self.agent.wait_health.side_effect = (
            exception.GuestEgressError('secret'))
        server = self.execute()
        self.assertEqual(states.ERROR, server.status)
        self.assertEqual('no internet egress via stack router', server.fault)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.floating)
        self.assertEqual(0, self.counters().in_use_volume_gb)

    def test_add_and_remove_timeline_detail(self):
        self.accepted()
        server = self.execute()
        self.assertEqual(states.ACTIVE, server.status)
        self.remove()
        for action in ('create', 'delete'):
            rows = objects.EventList.get_all(self.ctx, filters={
                'stack_id': self.stack.id, 'resource_type': 'server',
                'resource_id': server.id, 'action': action,
                'outcome': 'success'})
            self.assertEqual(1, len(rows))
            self.assertEqual(server.name, rows[0].detail['server_name'])
            self.assertEqual(server.role, rows[0].detail['role'])
            self.assertEqual(server.kind, rows[0].detail['kind'])
            self.assertEqual(server.flavor_id, rows[0].detail['flavor_id'])
            self.assertEqual(server.image_id, rows[0].detail['image_id'])
            if action == 'create':
                self.assertEqual('worker-plan', rows[0].detail['plan'])

    def test_add_and_remove_retains_data_and_releases_only_docker(self):
        self.config.config(egress_check_url='https://registry.example.com/v2/',
                           group='controller_worker')
        self.accepted()
        server = self.execute()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual('worker-engine', server.engine_ref)
        self.assertEqual('203.0.113.10', server.floating_ip)
        self.assertEqual(30, self.counters().in_use_volume_gb)
        nova = self.cloud_resources['server'][server.compute_id]
        data = json.loads(base64.b64decode(nova.user_data).decode().
                          split('\n', 1)[1])
        self.assertIn('role=worker', data['write_files'][0]['content'])
        self.assertIn('egress_check_url=https://registry.example.com/v2/\n',
                      data['write_files'][0]['content'])
        self.assertEqual('root', data['users'][1]['name'])
        self.assertEqual(['ssh-ed25519 public'],
                         data['users'][1]['ssh_authorized_keys'])
        self.assertEqual(2, len(data['mounts']))
        self.assertEqual(2, len(nova.networks))
        self.assertNotIn('private-sensitive', str(nova))
        self.assertEqual('private-sensitive',
                         self.driver.register_server.call_args.args[-1])
        before = copy.deepcopy(self.cloud_resources)
        self.execute()
        self.assertEqual(before, self.cloud_resources)
        self.remove()
        with self.assertRaises(exception.NotFound):
            objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertFalse(self.cloud_resources['server'])
        self.assertFalse(self.cloud_resources['port'])
        self.assertFalse(self.floating)
        volumes = objects.VolumeList.get_all(self.ctx)
        self.assertEqual(1, len(volumes))
        self.assertEqual('data', volumes[0].purpose)
        self.assertIsNone(volumes[0].server_id)
        self.assertEqual(states.AVAILABLE, volumes[0].status)
        self.assertEqual(10, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(2, len(self.payloads))
        self.assertNotIn('sensitive', str(self.notifier.mock_calls))
        self.manager.execute(self.ctx, 'delete_server', self.server,
                             revision=rpcapi.revision(self.server))

    def test_build_has_no_ingress(self):
        self.accepted(role='build')
        server = self.execute()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertIsNone(server.floating_ip)
        self.assertEqual(1, self.counters().in_use_build_servers)
        self.assertFalse(self.floating)
        env_port = next(port for port in self.cloud_resources['port'].values()
                        if port.name.endswith('-env'))
        self.assertEqual(['build-sg'], env_port.security_group_ids)

    def test_volume_quota_exhaustion_reverts_before_boot(self):
        self.accepted()
        quota.update(context.RequestContext(project_id='project',
                                            is_admin=True),
                     'project', {'volume_gb': 10})
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().reserved_volume_gb)

    def test_rpc_forwards_only_server_plan(self):
        self.accepted()
        rpc = rpcapi.ConductorAPI()
        rpc._client = mock.Mock()
        rpc.cast(self.ctx, 'create_server', self.server,
                 {'plan': 'worker-plan', 'password': 'sensitive'})
        wire = rpc._client.prepare.return_value.cast.call_args.kwargs
        self.assertEqual({'plan': 'worker-plan'}, wire['options'])
        self.assertEqual(self.revision, wire['revision'])

    def test_stale_revision_and_building_duplicate_do_not_start(self):
        self.accepted()
        self.server.status = states.BUILDING
        self.server.save()
        self.execute()
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertEqual(1, self.counters().reserved_servers)

    def test_lost_cinder_reply_is_adopted_for_cleanup(self):
        self.accepted()
        original = self.proxies['cinder'].create_volume.side_effect

        def lost(**values):
            original(**values)
            raise RuntimeError('sensitive')

        self.proxies['cinder'].create_volume.side_effect = lost
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_validation_timeout_reverts_guest_and_dependencies(self):
        self.accepted()
        self.driver.register_server.side_effect = driver_base.DriverError(
            'Coolify server validation timed out')
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.floating)
        self.assertIsNone(
            objects.Server.get_by_id(self.ctx, self.server.id).engine_ref)

    def test_uncertain_register_failed_revert_keeps_all_dependencies(self):
        self.accepted()
        self.driver.register_server.side_effect = RuntimeError('sensitive')
        self.driver.unregister_server.side_effect = RuntimeError('sensitive')
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertTrue(self.cloud_resources['server'])
        self.assertTrue(self.floating)
        self.assertEqual(2, len(self.cloud_resources['volume']))
        self.assertEqual(2, len(self.cloud_resources['port']))

    def test_prepare_failure_logs_safe_cause(self):
        self.accepted()
        logs = self.useFixture(fixtures.FakeLogger())
        secret = 'password=secret'
        with mock.patch.object(server_add.AddServer, 'prepare',
                               side_effect=Exception(secret)):
            server_add.AddServer(self.manager).execute(
                self.ctx, self.server, {}, self.reservation, self.revision)
        row = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual(server_add.FAULT, row.fault)
        self.assertNotIn(secret, logs.output)
        self.assertIn('builtins.Exception', logs.output)
        self.assertIn(self.server.id, logs.output)
        self.assertIn(self.stack.id, logs.output)

    def test_wrong_image_fails_before_cloud_create(self):
        self.accepted()
        self.proxies['glance'].images.return_value[0].owner = 'foreign'
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_build_removal_preserves_unhealthy_build_registry_rules(self):
        self.accepted(role='build')
        self.execute()
        other = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='other-build',
            kind='vm', role='build', status=states.UNHEALTHY,
            task_state=states.NONE, compute_id='other-nova')
        other.create()
        self.remove()
        self.assertTrue(self.network.create.call_args.kwargs[
            'has_build_servers'])
        self.assertEqual(0, self.counters().in_use_build_servers)

    def test_last_build_removal_disables_registry_rules(self):
        self.accepted(role='build')
        self.execute()
        self.remove()
        self.assertFalse(self.network.create.call_args.kwargs[
            'has_build_servers'])
        self.assertFalse(self.cloud_resources['server'])

    def test_retained_volume_wait_failure_keeps_checkpoint_for_retry(self):
        self.accepted()
        self.execute()
        with mock.patch.object(server_remove.worker.WorkerCloud, 'wait_volume',
                               side_effect=RuntimeError('sensitive')):
            self.remove()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, server.status)
        volumes = objects.VolumeList.get_all(self.ctx)
        data = next(volume for volume in volumes if volume.purpose == 'data')
        self.assertEqual(server.id, data.server_id)
        self.assertTrue(data.cinder_volume_id)
        self.remove()
        data = objects.Volume.get_by_id(self.ctx, data.id)
        self.assertIsNone(data.server_id)

    def test_failed_final_add_commit_reverts_guest(self):
        self.accepted()
        original = server_add.AddServer.save

        def fail(flow, resource, **values):
            if values.get('status') == states.ACTIVE:
                raise RuntimeError('sensitive')
            return original(flow, resource, **values)

        with mock.patch.object(server_add.AddServer, 'save', fail):
            self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.floating)
        self.assertEqual(0, self.counters().in_use_volume_gb)

    def test_remove_failed_guest_delete_keeps_dependents(self):
        self.accepted()
        self.execute()
        self.proxies['nova'].delete_server.side_effect = (
            RuntimeError('sensitive'))
        self.remove()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, server.status)
        self.assertTrue(server.compute_id)
        self.assertTrue(self.floating)
        self.assertEqual(2, len(self.cloud_resources['port']))
        self.assertEqual(2, len(self.cloud_resources['volume']))

    def test_volume_mount_blocks_deregistration(self):
        self.accepted()
        self.execute()
        with mock.patch.object(objects.AppVolumeMountList, 'get_all',
                               return_value=[object()]):
            self.remove()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, server.status)
        self.assertEqual('worker-engine', server.engine_ref)
        self.assertTrue(self.cloud_resources['server'])

    def test_missing_plan_fails_before_ports(self):
        self.accepted()
        self.config.config(definitions='[]', group='plans')
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_lost_floating_ip_reply_is_removed(self):
        self.accepted()

        def lost(*args):
            self.ensure_fip(*args)
            raise RuntimeError('sensitive')

        self.fip.ensure.side_effect = lost
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(self.floating)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_wrong_project_cannot_start(self):
        self.accepted()
        with self.assertRaises(exception.NotFound):
            self.manager.execute(context.RequestContext(project_id='other'),
                                 'create_server', self.server)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_baremetal_fails_closed(self):
        self.accepted(kind='baremetal')
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertEqual(0, self.counters().in_use_baremetal_servers)
        self.assertFalse(any(self.cloud_resources.values()))

    def fail_step(self, name):
        self.accepted()
        original = getattr(server_add.AddServer, name)

        def fail(flow):
            original(flow)
            raise RuntimeError('private-sensitive')

        with mock.patch.object(server_add.AddServer, name, fail):
            server = self.execute()
        self.assertEqual(states.ERROR, server.status)
        self.assertEqual(states.NONE, server.task_state)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.floating)
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(2, len(self.payloads))
        self.assertNotIn('sensitive', server.fault)
        before = copy.deepcopy(self.cloud_resources)
        self.execute()
        self.assertEqual(before, self.cloud_resources)

    def test_failed_guest_delete_preserves_dependencies(self):
        self.accepted()
        self.agent.wait_health.side_effect = RuntimeError('sensitive')
        self.proxies['nova'].delete_server.side_effect = (
            RuntimeError('sensitive'))
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertTrue(self.cloud_resources['server'])
        self.assertEqual(2, len(self.cloud_resources['volume']))
        self.assertEqual(2, len(self.cloud_resources['port']))

    def test_failed_deregister_preserves_guest_and_ingress(self):
        self.accepted()
        self.execute()
        self.driver.unregister_server.side_effect = RuntimeError('sensitive')
        self.remove()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, server.status)
        self.assertTrue(server.compute_id)
        self.assertTrue(server.engine_ref)
        self.assertTrue(self.floating)
        self.assertEqual(2, len(self.cloud_resources['volume']))

    def test_placed_service_blocks_remove(self):
        self.accepted()
        self.execute()
        original = objects.ServiceList.get_all
        with mock.patch.object(objects.ServiceList, 'get_all',
                               side_effect=lambda *a, **kw: [object()] if
                               kw.get('filters', {}).get('server_id') else
                               original(*a, **kw)):
            self.remove()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, server.status)
        self.assertTrue(server.engine_ref)
        self.assertTrue(server.compute_id)

    def test_lost_nova_reply_is_adopted_for_cleanup(self):
        self.accepted()
        original = self.proxies['nova'].create_server.side_effect

        def lost(**values):
            original(**values)
            raise RuntimeError('sensitive')

        self.proxies['nova'].create_server.side_effect = lost
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_remove_failure_can_be_retried_without_recreating_guest(self):
        self.accepted()
        self.execute()
        with mock.patch.object(server_remove.RemoveServer, 'ports',
                               side_effect=RuntimeError('sensitive')):
            self.remove()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, server.status)
        self.assertIsNone(server.compute_id)
        self.assertFalse(self.floating)
        self.remove()
        self.assertEqual(1, len(self.cloud_resources['volume']))


def failure_test(name):
    def test(self):
        self.fail_step(name)
    return test


for _step in ('network', 'data_volume', 'docker_volume', 'boot', 'active',
              'health', 'floating', 'register'):
    setattr(ServerFlowTest, 'test_revert_after_' + _step, failure_test(_step))

# Reuse the cloud fixture, not the controller-only test cases.
for _name in dir(test_stack_create.StackCreateTest):
    if _name.startswith('test_') and _name not in ServerFlowTest.__dict__:
        setattr(ServerFlowTest, _name, None)
