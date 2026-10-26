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

"""Health-manager and admin controller rebuilds through fake RPC."""

import datetime
import json
from types import SimpleNamespace
from unittest import mock

from oslo_utils import timeutils

from coast.api import secret_store
from coast.api.v1 import resize as resize_api
from coast.common import exception
from coast.common import states
from coast.conductor import controller
from coast.conductor import controller_failover
from coast.conductor import engine_upgrade
from coast.conductor import manager
from coast.conductor import secret_retry
from coast.conductor import worker
from coast.db import api as db_api
from coast.db import models
from coast.drivers import base
from coast.health_manager import failover
from coast.health_manager import stale
from coast import objects
from coast.tests import conductor


class ControllerFailoverRPCTest(conductor.ConductorTest):
    def prepare(self):
        self.path = self.stack(workers=[])
        self.stack_id = self.path.rsplit('/', 1)[-1]
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        stack.driver = 'coolify_v4'
        stack.engine_version = '4.4.3'
        stack.heartbeat_key_ref = self.store.put(self.ctx, 'heartbeat')
        stack.engine_token_ref = self.store.put(self.ctx, 'token')
        stack.save()
        self.config.config(insecure_direct_url=False, group='coolify')
        self.config.config(enabled_drivers=['noop', 'coolify_v4'],
                           group='controller_worker')
        self.controller = objects.ServerList.get_all(self.ctx, filters={
            'stack_id': stack.id, 'role': 'control'})[0]
        self.controller.compute_id = 'old-vm'
        self.controller.image_id = 'old-image'
        self.controller.flavor_id = 'flavor'
        self.controller.mgmt_ip = '192.0.2.2'
        self.controller.private_ip = '198.51.100.2'
        self.controller.floating_ip = '203.0.113.2'
        self.controller.floating_ip_id = 'fip-id'
        self.controller.save()
        old_data = objects.VolumeList.get_all(self.ctx, filters={
            'stack_id': stack.id, 'purpose': 'data'})[0]
        old_data.destroy()
        self.volumes = []
        disk_ids = {
            'data': '1831dfbb-951f-4e73-b54d-c493a64a1a32',
            'docker': '51b39d45-a8a5-43d7-b5f7-7dbf195d4014',
            'registry': '62c4ae56-b9b6-44e8-a6a8-8ecf2a6e5125'}
        for purpose, mount in controller_failover.MOUNTS.items():
            volume = objects.Volume(
                context=self.ctx, stack_id=stack.id,
                server_id=self.controller.id, name=purpose, purpose=purpose,
                mount_path=mount, size_gb=10, status=states.IN_USE,
                cinder_volume_id=disk_ids[purpose])
            volume.create()
            self.volumes.append(volume)
        self.driver = mock.Mock(SUPPORTED_ENGINE_VERSIONS=['4.4.3'])
        self.driver.get_engine_version.return_value = '4.4.3'
        self.driver.get_server_status.return_value = base.ObservedStatus(
            states.ACTIVE)
        self.driver.get_app_status.return_value = base.ObservedStatus(
            states.ACTIVE)
        self.manager.drivers['coolify_v4'] = self.driver
        self.mock(manager.Manager, '_endpoint', return_value=mock.Mock())
        self.mock(engine_upgrade.loader, 'load_driver',
                  return_value=self.driver)
        self.order = []
        self.cloud = mock.Mock(tags=[
            'coast', 'project_id=project', 'environment_id=' + stack.id])
        self.metadata = {'coast': 'true', 'project_id': 'project',
                         'environment_id': stack.id}
        self.ports = [SimpleNamespace(id='mgmt-port'),
                      SimpleNamespace(id='env-port')]
        self.cloud.retained_ports.return_value = self.ports
        self.cloud.proxy.return_value.get_volume.side_effect = (
            lambda identifier: SimpleNamespace(
                id=identifier, metadata=self.metadata,
                attachments=[{'server_id': 'old-vm'}]))
        self.cloud.proxy.return_value.get_ip.return_value = SimpleNamespace(
            port_id='env-port')
        self.cloud.config_drive.side_effect = self.config_drive
        self.cloud.boot.side_effect = self.boot
        self.cloud.stopped.side_effect = (
            lambda identifier, **kwargs: self.order.append('stop'))
        self.cloud.detach.side_effect = lambda *args: self.order.append(
            'detach')
        self.cloud.delete_guest.side_effect = lambda identifier: (
            self.order.append('delete'))
        self.mock(engine_upgrade, 'RebuildCloud', return_value=self.cloud)
        self.agent_paths = []
        agent = mock.Mock()
        agent.request.side_effect = self.agent_request
        self.mock(controller_failover.agent_client, 'AgentClient',
                  return_value=agent)
        self.missing = False
        self.mock(engine_upgrade.controller.ControllerCloud, 'select_image',
                  side_effect=self.image)
        self.admin = dict(self.headers, **{'X-Roles': 'admin'})

    def mock(self, owner, name, **kwargs):
        patcher = mock.patch.object(owner, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def image(self, role, selection):
        if self.missing:
            raise ValueError('missing')
        return SimpleNamespace(
            id=selection, status='active', properties={
                'coast_role': 'controller', 'coast_engine': 'coolify',
                'coast_engine_version': '4.4.3'})

    def config_drive(self, server, volumes, key):
        # The recorded image runs this bootcmd. It is the mount order
        # the replacement guest must use.
        script = controller.volume_link_script(volumes)
        expires = (datetime.datetime.now(datetime.timezone.utc) +
                   datetime.timedelta(days=30))
        return script, 'cert', expires

    def boot(self, guest, ports, volumes, user_data, *, attempt):
        self.order.append('boot')
        self.assertEqual('old-image', guest.image_id)
        self.assertEqual(self.controller.id, guest.id)
        self.assertEqual(self.controller.hostname, guest.hostname)
        self.assertNotEqual(guest.id, attempt)
        self.assertNotIn('systemctl stop docker.service', user_data)
        self.assertNotIn('systemctl mask docker.socket', user_data)
        self.assertNotIn('systemctl mask docker.service', user_data)
        self.assertIn('link_disk ', user_data)
        self.assertNotIn('/v1/bootstrap', user_data)
        for volume in volumes:
            self.assertIn(
                controller.disk_alias(volume.cinder_volume_id), user_data)
        return SimpleNamespace(id='new-vm')

    def agent_request(self, method, path, body=None):
        self.agent_paths.append(path)
        return {'status': 'ok', 'egress': {'status': 'ok'}}

    def assert_rebuilt(self):
        saved = objects.Server.get_by_id(self.ctx, self.controller.id)
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        self.assertEqual(states.ACTIVE, saved.status)
        self.assertEqual(states.NONE, saved.task_state)
        self.assertEqual('new-vm', saved.compute_id)
        self.assertEqual('old-image', saved.image_id)
        self.assertEqual('203.0.113.2', saved.floating_ip)
        self.assertEqual('fip-id', saved.floating_ip_id)
        self.assertEqual('192.0.2.2', saved.mgmt_ip)
        self.assertEqual('198.51.100.2', saved.private_ip)
        self.assertEqual(self.ports, self.cloud.boot.call_args.args[1])
        self.assertEqual([v.cinder_volume_id for v in self.volumes],
                         [v.cinder_volume_id for v in
                          self.cloud.boot.call_args.args[2]])
        self.assertEqual('4.4.3', stack.engine_version)
        self.assertEqual(
            [volume.cinder_volume_id for volume in self.volumes],
            [objects.Volume.get_by_id(self.ctx, volume.id).cinder_volume_id
             for volume in self.volumes])
        self.assertTrue(all(objects.Volume.get_by_id(
            self.ctx, v.id).status == states.IN_USE for v in self.volumes))
        self.assertEqual(['stop', 'detach', 'boot', 'delete'], self.order)
        self.assertEqual(['/health'], self.agent_paths)
        self.driver.register_server.assert_not_called()
        self.assertEqual(states.ACTIVE, stack.status)

    def test_error_app_failover_then_normal_tenant_delete(self):
        self.prepare()
        source = objects.Source(context=self.ctx, type='image',
                                image='example.com/app:stable')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack_id,
            server_id=self.controller.id, name='failed-app',
            source_id=source.id, ports='8080', replicas=1,
            auto_deploy=False, status=states.ERROR, task_state=states.NONE,
            engine_ref='app-ref', **self.service_parents(self.stack_id))
        service.create()
        service.acquire_task(states.DEPLOYING)
        deployment = objects.Deployment(
            context=self.ctx, service_id=service.id,
            server_id=self.controller.id, status=states.DEPLOYING,
            trigger='api', source_ref='stable', spec={})
        deployment.create()
        action = self.path + '/servers/' + self.controller.id + '/action'
        self.request('post', action, {'failover': {}}, headers=self.admin,
                     status=409)
        self.assertEqual('old-vm', objects.Server.get_by_id(
            self.ctx, self.controller.id).compute_id)
        old = timeutils.utcnow() - datetime.timedelta(hours=1)
        with db_api.writer.using(self.ctx) as session:
            session.query(models.Service).filter_by(id=service.id).update({
                'task_started_at': old})
            session.query(models.Deployment).filter_by(
                id=deployment.id).update({'created_at': old})
        self.driver.get_app_status.return_value = base.ObservedStatus(
            states.ERROR)
        self.request('post', self.path + '/servers/' + self.controller.id +
                     '/action', {'failover': {}}, headers=self.admin,
                     status=202, complete=True)
        self.assert_rebuilt()
        self.assertEqual(states.FAILED, objects.Deployment.get_by_id(
            self.ctx, deployment.id).status)
        path = '/v1/services/' + service.id
        shown = self.request('get', path).json['service']
        self.assertEqual(states.ERROR, shown['status'])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.request('delete', path, status=202, complete=True)
        self.request('get', path, status=404)

    def test_resize_controller_api_to_rpc_replacement(self):
        self.prepare()
        action = self.path + '/servers/' + self.controller.id + '/action'
        with mock.patch.object(resize_api, 'selection', return_value={
                'target_flavor': 'large', 'target_plan': 'large'}):
            self.request('post', action, {'resize': {'flavor': 'large'}},
                         status=202, complete=True,
                         headers=dict(self.headers, **{
                             'X-Roles': 'member',
                             'OpenStack-API-Version': 'app-hosting 1.26'}))
        self.assert_rebuilt()
        saved = objects.Server.get_by_id(self.ctx, self.controller.id)
        self.assertEqual('large', saved.flavor_id)
        self.assertEqual('large', objects.Stack.get_by_id(
            self.ctx, self.stack_id).plan)
        types = {name for name, _ in self.manager.notifier.events}
        self.assertIn('coast.server.resize.start', types)
        self.assertIn('coast.server.resize.end', types)

    def test_resize_controller_secret_outage_defers_and_recovers(self):
        self.prepare()
        self.controller.fault = 'Existing server fault.'
        self.controller.save()
        with mock.patch.object(resize_api, 'selection', return_value={
                'target_flavor': 'large', 'target_plan': 'large'}):
            with mock.patch.object(secret_retry.time, 'monotonic',
                                   return_value=100) as clock:
                with mock.patch.object(
                        secret_store, 'call',
                        side_effect=exception.SecretStoreUnavailable()):
                    self.resize_request(202, complete=True)
                saved = objects.Server.get_by_id(self.ctx, self.controller.id)
                self.assertEqual(
                    (states.ACTIVE, states.NONE, 'old-vm', 'flavor',
                     'Existing server fault.'),
                    (saved.status, saved.task_state, saved.compute_id,
                     saved.flavor_id, saved.fault))
                self.cloud.stopped.assert_not_called()
                self.cloud.boot.assert_not_called()
                self.assertEqual(states.NONE, objects.Stack.get_by_id(
                    self.ctx, self.stack_id).task_state)
                clock.return_value = 111
                self.resize_request(202, complete=True)
        self.assert_rebuilt()
        self.assertEqual('large', objects.Server.get_by_id(
            self.ctx, self.controller.id).flavor_id)

    def resize_catalog(self):
        self.config.config(definitions=json.dumps([dict(
            name='target-plan', flavor='target', kind='vm',
            roles=['worker', 'build', 'control'])]), group='plans')
        self.flavors = {
            'flavor': SimpleNamespace(id='flavor', name='current', vcpus=2,
                                      ram=2048, disk=10, ephemeral=0),
            'target': SimpleNamespace(id='target', name='target', vcpus=4,
                                      ram=4096, disk=10, ephemeral=0)}
        nova = self.mock(resize_api.controller.ControllerCloud, 'proxy')
        nova.return_value.find_flavor.side_effect = (
            lambda value: self.flavors[value])

    def resize_request(self, status, complete=False):
        return self.request(
            'post', self.path + '/servers/' + self.controller.id + '/action',
            {'resize': {'flavor': 'target'}}, status=status, complete=complete,
            headers=dict(self.headers, **{
                'X-Roles': 'member',
                'OpenStack-API-Version': 'app-hosting 1.26'}))

    def test_resize_smaller_and_equal_flavors_refused(self):
        self.prepare()
        self.resize_catalog()
        for cpu, ram in ((1, 1024), (2, 2048)):
            with self.subTest(cpu=cpu, ram=ram):
                self.flavors['target'].vcpus = cpu
                self.flavors['target'].ram = ram
                response = self.resize_request(400)
                self.assertIn('strictly larger flavor',
                              response.json['faultstring'])
                self.assertNotIn('coolify', response.text.lower())
                self.assertNotIn('traefik', response.text.lower())
        self.assertEqual([], self.order)

    def test_resize_busy_stack_and_server_refused(self):
        self.prepare()
        self.resize_catalog()
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        for row in (stack, self.controller):
            with self.subTest(resource=row.id):
                row.acquire_task(states.RECONCILING)
                self.resize_request(409)
                row.release_task(states.RECONCILING)
        self.assertEqual([], self.order)

    def test_resize_controller_rollback(self):
        self.prepare()
        self.resize_catalog()
        old_plan = objects.Stack.get_by_id(self.ctx, self.stack_id).plan
        self.cloud.replacement.return_value = SimpleNamespace(id='new-vm')
        self.cloud.proxy.return_value.volume_attachments.return_value = []
        self.cloud.proxy.return_value.server_interfaces.return_value = []
        restore = worker.WorkerCloud.restore
        self.cloud.restore.side_effect = lambda *args: restore(
            self.cloud, *args)
        self.cloud.wait_server.side_effect = [RuntimeError('failed'), None]
        self.resize_request(202, complete=True)
        saved = objects.Server.get_by_id(self.ctx, self.controller.id)
        self.assertEqual('old-vm', saved.compute_id)
        self.assertEqual('flavor', saved.flavor_id)
        self.assertEqual(states.ERROR, saved.status)
        self.assertTrue(saved.fault)
        self.assertEqual(states.NONE, saved.task_state)
        self.cloud.delete_guest.assert_called_once_with('new-vm')
        self.cloud.proxy.return_value.create_port.assert_not_called()
        self.cloud.proxy.return_value.create_volume.assert_not_called()
        self.cloud.proxy.return_value.delete_port.assert_not_called()
        self.cloud.proxy.return_value.delete_volume.assert_not_called()
        self.cloud.restore.assert_called_once()
        self.cloud.proxy.return_value.start_server.assert_called_once_with(
            'old-vm')
        args = self.cloud.restore.call_args.args
        self.assertEqual('old-vm', args[0])
        self.assertEqual(self.ports, args[1])
        self.assertEqual([v.cinder_volume_id for v in self.volumes],
                         [v.cinder_volume_id for v in args[2]])
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        # Conservative health rollup requires operator recovery after failure.
        self.assertEqual(states.DEGRADED, stack.status)
        self.assertTrue(stack.fault)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual(old_plan, stack.plan)
        self.cloud.boot.assert_called_once()
        self.assertEqual([mock.call('old-vm', v.cinder_volume_id) for
                          v in self.volumes], self.cloud.proxy.return_value.
                         create_volume_attachment.call_args_list)
        self.assertEqual([mock.call('old-vm', port_id=p.id)
                          for p in self.ports],
                         self.cloud.proxy.return_value.
                         create_server_interface.call_args_list)
        self.assertEqual('192.0.2.2', saved.mgmt_ip)
        self.assertEqual('198.51.100.2', saved.private_ip)
        self.assertEqual('fip-id', saved.floating_ip_id)
        self.assertEqual('203.0.113.2', saved.floating_ip)
        types = {name for name, _ in self.manager.notifier.events}
        self.assertIn('coast.server.resize.error', types)

    def test_stale_controller_rebuilds_on_the_recorded_image(self):
        self.prepare()
        now = timeutils.utcnow()
        self.controller.last_heartbeat_at = now - datetime.timedelta(
            seconds=500)
        self.controller.save()
        detector = stale.Detector(self.conf, clock=lambda: now)
        detector.started -= 61
        with mock.patch.object(failover.rpcapi, 'ConductorAPI',
                               return_value=self.rpc):
            # The episode time must be in the past. The conductor uses
            # the real clock, not this detector clock.
            now -= datetime.timedelta(seconds=121)
            detector.run()
            self.assertEqual(states.UNHEALTHY, objects.Server.get_by_id(
                self.ctx, self.controller.id).status)
            now += datetime.timedelta(seconds=121)
            detector.run()
        self.manager.done.get(timeout=10)
        self.assert_rebuilt()

    def explicit_outage(self, status):
        self.prepare()
        self.controller.status = status
        self.controller.fault = 'Existing server failure.'
        self.controller.save()
        action = self.path + '/servers/' + self.controller.id + '/action'
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                self.request('post', action, {'failover': {}},
                             headers=self.admin, status=202, complete=True)
            saved = objects.Server.get_by_id(self.ctx, self.controller.id)
            self.assertEqual((status, states.NONE, 'Existing server failure.'),
                             (saved.status, saved.task_state, saved.fault))
            self.cloud.stopped.assert_not_called()
            self.cloud.boot.assert_not_called()
            clock.return_value = 111
            self.request('post', action, {'failover': {}},
                         headers=self.admin, status=202, complete=True)
            self.assert_rebuilt()

    def test_explicit_unhealthy_outage_preserves_status_and_recovers(self):
        self.explicit_outage(states.UNHEALTHY)

    def test_explicit_error_outage_preserves_status_and_recovers(self):
        self.explicit_outage(states.ERROR)

    def admin_recovery(self, status, task=states.NONE, age=0,
                       stack_task=states.NONE, expected=202, peer=False):
        self.prepare()
        self.config.config(admin_failover_stuck_timeout=600,
                           group='health_manager')
        with db_api.writer.using(self.ctx) as session:
            session.query(models.Server).filter_by(
                id=self.controller.id).update({
                    'status': status, 'task_state': task,
                    'updated_at': timeutils.utcnow() - datetime.timedelta(
                        seconds=age)})
            session.query(models.Stack).filter_by(id=self.stack_id).update({
                'task_state': stack_task})
        if peer:
            objects.Server(
                context=self.ctx, stack_id=self.stack_id, name='build-1',
                role='build', kind='vm', status=states.FAILOVER,
                task_state=states.FAILING_OVER).create()
        action = self.path + '/servers/' + self.controller.id + '/action'
        # Observe admission before the asynchronous conductor consumes it.
        with mock.patch.object(self.handler, 'failover_server') as cast:
            self.request('post', action, {'failover': {}}, status=403)
            response = self.request('post', action, {'failover': {}},
                                    headers=self.admin, status=expected)
        saved = objects.Server.get_by_id(self.ctx, self.controller.id)
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        if expected == 202:
            self.assertEqual(
                states.FAILOVER, response.json['server']['status'])
            self.assertEqual(states.FAILOVER, saved.status)
            self.assertEqual(states.FAILING_OVER, saved.task_state)
            self.assertEqual(states.NONE, stack.task_state)
            cast.assert_called_once()
        else:
            self.assertEqual(status, saved.status)
            self.assertEqual(task, saved.task_state)
            self.assertEqual(stack_task, stack.task_state)
            cast.assert_not_called()

    def test_admin_failover_from_error(self):
        self.admin_recovery(states.ERROR)

    def test_admin_retries_stuck_failover(self):
        self.admin_recovery(states.FAILOVER, states.FAILING_OVER, age=601)

    def test_admin_releases_stuck_stack_failover(self):
        self.admin_recovery(states.FAILOVER, states.FAILING_OVER, age=601,
                            stack_task=states.FAILING_OVER)

    def test_admin_refuses_fresh_failover(self):
        self.admin_recovery(states.FAILOVER, states.FAILING_OVER,
                            stack_task=states.FAILING_OVER, expected=409)

    def test_admin_preserves_another_server_failover(self):
        self.admin_recovery(states.FAILOVER, states.FAILING_OVER, age=601,
                            stack_task=states.FAILING_OVER, expected=409,
                            peer=True)

    def test_admin_preserves_other_stack_tasks(self):
        self.admin_recovery(states.FAILOVER, states.FAILING_OVER, age=601,
                            stack_task=states.UPGRADING, expected=409)

    def test_admin_refuses_building(self):
        self.admin_recovery(states.BUILDING, expected=409)

    def test_admin_refuses_a_missing_image_then_rebuilds(self):
        self.prepare()
        action = self.path + '/servers/' + self.controller.id + '/action'
        self.request('post', action, {'failover': {}}, status=403)
        self.missing = True
        refused = self.request('post', action, {'failover': {}},
                               headers=self.admin, status=409)
        self.assertNotIn('old-image', refused.text)
        saved = objects.Server.get_by_id(self.ctx, self.controller.id)
        self.assertEqual(states.ACTIVE, saved.status)
        self.assertEqual(states.NONE, saved.task_state)
        self.assertEqual([], self.order)
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.controller.id, 'action': 'failover'})
        self.assertEqual([controller_failover.IMAGE_FAULT],
                         [event.message for event in events])
        self.missing = False
        response = self.request('post', action, {'failover': {}},
                                headers=self.admin, status=202, complete=True)
        self.assertEqual(states.FAILOVER, response.json['server']['status'])
        self.assert_rebuilt()


for _name in dir(conductor.ConductorTest):
    if (_name.startswith('test_') and
            _name not in ControllerFailoverRPCTest.__dict__):
        setattr(ControllerFailoverRPCTest, _name, None)
