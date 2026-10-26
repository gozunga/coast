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

"""Stack rebuild actions through WSGI and serialized fake RPC."""

import datetime
import time
from types import SimpleNamespace
from unittest import mock

from openstack import exceptions as sdk_exceptions

from coast.api import secret_store
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import engine_upgrade
from coast.conductor import manager
from coast.conductor import secret_retry
from coast.conductor import server_failover
from coast.conductor import stack_teardown
from coast.drivers import base
from coast.drivers.coolify import v4
from coast import objects
from coast.tests import conductor


class UpgradeRPCTest(conductor.ConductorTest):
    def prepare(self):
        self.path = self.stack(workers=[])
        self.stack_id = self.path.rsplit('/', 1)[-1]
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        stack.driver = 'coolify_v4'
        stack.engine_version = '4.4.3'
        stack.heartbeat_key_ref = self.store.put(self.ctx, 'heartbeat')
        stack.engine_token_ref = self.store.put(self.ctx, 'token')
        stack.ssh_key_ref = self.store.put(self.ctx, 'ssh-key')
        stack.save()
        self.config.config(enabled_drivers=['noop', 'coolify_v4'],
                           image_owner_id='image-owner',
                           image_tag_controller='controller-pin',
                           group='controller_worker')
        self.controller = objects.ServerList.get_all(self.ctx, filters={
            'stack_id': stack.id, 'role': 'control'})[0]
        self.controller.compute_id = 'old-vm'
        self.controller.image_id = 'old-image'
        self.controller.flavor_id = 'flavor'
        self.controller.mgmt_ip = '192.0.2.2'
        self.controller.save()
        old_data = objects.VolumeList.get_all(self.ctx, filters={
            'stack_id': stack.id, 'purpose': 'data'})[0]
        quota.release(self.ctx, volume_gb=old_data.size_gb)
        old_data.destroy()
        quota.commit(self.ctx, quota.reserve(self.ctx, volume_gb=30))
        self.volumes = []
        for purpose, mount in zip(engine_upgrade.PURPOSES, (
                '/data', '/var/lib/docker', '/var/lib/registry')):
            volume = objects.Volume(
                context=self.ctx, stack_id=stack.id,
                server_id=self.controller.id, name=purpose, purpose=purpose,
                mount_path=mount, size_gb=10, status=states.IN_USE,
                cinder_volume_id=purpose + '-disk')
            volume.create()
            self.volumes.append(volume)
        self.driver = mock.Mock(SUPPORTED_ENGINE_VERSIONS=['4.4.3', '4.4.7'])
        self.driver.get_engine_version.return_value = '4.4.3'
        self.manager.drivers['coolify_v4'] = self.driver
        self.mock(manager.Manager, '_endpoint', return_value=mock.Mock())
        self.mock(engine_upgrade.loader, 'load_driver',
                  return_value=self.driver)
        self.cloud = mock.Mock(tags=[
            'coast', 'project_id=project', 'environment_id=' + stack.id])
        self.metadata = {'coast': 'true', 'project_id': 'project',
                         'environment_id': stack.id}
        self.block = mock.Mock()
        self.cloud.proxy.return_value = self.block
        self.block.get_volume.side_effect = lambda identifier: SimpleNamespace(
            id=identifier, metadata=self.metadata,
            attachments=[{'server_id': objects.Server.get_by_id(
                self.ctx, self.controller.id).compute_id}])
        self.block.snapshots.return_value = []
        self.block.create_snapshot.side_effect = lambda **kw: SimpleNamespace(
            id=kw['volume_id'] + '-snapshot')
        self.block.get_snapshot.side_effect = lambda identifier: (
            SimpleNamespace(id=identifier, status='available',
                            metadata=self.metadata))
        self.block.create_volume.side_effect = lambda **kw: SimpleNamespace(
            id=kw['name'] + '-disk')
        self.cloud.config_drive.return_value = (
            'config-drive', 'cert', datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(days=30))
        self.cloud.boot.side_effect = self.boot
        self.mock(engine_upgrade, 'RebuildCloud', return_value=self.cloud)
        self.agent = mock.Mock()
        self.agent.request.side_effect = lambda method, path, body=None: (
            {'status': 'quiesced', 'deadline': time.time() + 600} if
            path.endswith('/quiesce') else
            {'status': 'ok', 'egress': {'status': 'ok'}})
        self.mock(engine_upgrade.agent_client, 'AgentClient',
                  return_value=self.agent)
        self.images = [SimpleNamespace(
            id=image, name=image, status='active', owner='image-owner',
            created_at=('2026-10-19T12:00:00Z' if image == 'new-image'
                        else '2026-10-15T12:00:00Z'),
            tags=['controller-pin'], properties={
                'coast_role': 'controller', 'coast_engine': 'coolify',
                'coast_engine_version': version})
            for image, version in (('new-image', '4.4.7'),
                                   ('old-image', '4.4.3'))]
        glance = mock.Mock()
        glance.images.side_effect = lambda **kw: self.images
        self.mock(engine_upgrade.controller.ControllerCloud, 'proxy',
                  return_value=glance)
        self.body = {'upgrade': {'image_id': 'new-image'}}

    def mock(self, owner, name, **kwargs):
        patcher = mock.patch.object(owner, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def boot(self, guest, *args, **kwargs):
        self.driver.get_engine_version.return_value = (
            '4.4.7' if guest.image_id == 'new-image' else '4.4.3')
        return SimpleNamespace(id='replacement-' + guest.id)

    def rolling(self, fail=False, same_version=True, worker_version=None):
        self.prepare()
        target_version = '4.4.3' if same_version else '4.4.7'
        self.images[0].properties['coast_engine_version'] = target_version

        self.live_version = '4.4.3'

        def boot_controller(guest, *args, **kwargs):
            self.live_version = target_version
            self.driver.get_engine_version.return_value = target_version
            return SimpleNamespace(id='replacement-controller')

        self.cloud.boot.side_effect = boot_controller
        worker_image = SimpleNamespace(
            id='worker-image', name='worker-image', status='active',
            owner='image-owner', tags=['worker-pin'], properties={
                'coast_role': 'worker',
                'coast_engine_version': worker_version or target_version})
        self.images.append(worker_image)
        self.config.config(image_tag_worker='worker-pin',
                           group='controller_worker')
        self.driver.get_server_status.return_value = base.ObservedStatus(
            states.ACTIVE)
        self.driver.register_server.side_effect = (
            lambda ep, stack, server, key: base.DriverResult(
                server.engine_ref))
        self.guests = []
        for role in ('worker', 'build'):
            server = objects.Server(
                context=self.ctx, stack_id=self.stack_id, name=role,
                role=role, kind='vm', status=states.ACTIVE,
                task_state=states.NONE, compute_id=role + '-old',
                image_id='worker-old', flavor_id='flavor',
                mgmt_ip='192.0.2.10', private_ip='192.0.2.10',
                engine_ref=role + '-engine')
            server.create()
            self.guests.append(server)
            for purpose, mount in (('data', '/data'),
                                   ('docker', '/var/lib/docker')):
                objects.Volume(
                    context=self.ctx, stack_id=self.stack_id,
                    server_id=server.id, name=role + purpose, purpose=purpose,
                    mount_path=mount, size_gb=10, status=states.IN_USE,
                    cinder_volume_id=role + purpose).create()
        parents = self.service_parents(self.stack_id)
        source = objects.Source(context=self.ctx, type='image',
                                image='example.com/image:stable')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack_id,
            server_id=self.guests[0].id, name='service',
            environment_id=parents['environment_id'], source_id=source.id,
            ports='8080', replicas=1, auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='retained-service')
        service.create()
        self.driver.get_app_status.return_value = base.ObservedStatus(
            states.ACTIVE)
        cloud = mock.Mock()
        cloud.config_drive.return_value = self.cloud.config_drive.return_value

        def find_volume(volume):
            server = objects.Server.get_by_id(self.ctx, volume.server_id)
            return SimpleNamespace(
                id=volume.cinder_volume_id,
                attachments=[{'server_id': server.compute_id}])

        cloud.find_volume.side_effect = find_volume

        def boot_worker(guest, ports, volumes, data, attempt):
            self.assertIn(guest.id, [server.id for server in self.guests])
            nova = mock.Mock()
            real_cloud = engine_upgrade.controller.ControllerCloud(
                self.conf, objects.Stack.get_by_id(self.ctx, self.stack_id))
            with mock.patch.object(real_cloud, 'proxy', return_value=nova):
                real_cloud.boot(guest, ports, volumes, data, attempt=attempt)
            tags = nova.create_server.call_args.kwargs['tags']
            self.assertIn('server_id=' + guest.id, tags)
            self.assertIn('attempt=' + attempt, tags)
            return SimpleNamespace(id='replacement-' + guest.id)

        cloud.retained_ports.return_value = [SimpleNamespace(id='port')]
        cloud.boot.side_effect = boot_worker
        self.mock(server_failover.worker, 'WorkerCloud', return_value=cloud)
        completed = []

        def fence(identifier, receipt=None):
            self.assertEqual(states.UPGRADING, objects.Service.get_by_id(
                self.ctx, service.id).task_state)
            controller = objects.Server.get_by_id(self.ctx, self.controller.id)
            self.assertEqual((states.ACTIVE, 'new-image'),
                             (controller.status, controller.image_id))
            self.assertEqual(states.UPGRADING, objects.Stack.get_by_id(
                self.ctx, self.stack_id).task_state)
            for previous in completed:
                current = objects.Server.get_by_id(self.ctx, previous.id)
                self.assertEqual((states.ACTIVE, states.NONE, 'worker-image'),
                                 (current.status, current.task_state,
                                  current.image_id))
            completed.append(next(g for g in self.guests
                                  if g.compute_id == identifier))

        cloud.stopped.side_effect = fence
        if fail:
            cloud.wait_server.side_effect = RuntimeError('guest failed')
        if not same_version:
            driver = v4.CoolifyV4Driver(self.conf)
            self.manager.drivers['coolify_v4'] = driver
            self.mock(engine_upgrade.loader, 'load_driver',
                      return_value=driver)
            transport = mock.MagicMock()
            transport.__enter__.return_value = transport

            def request(method, path, **kw):
                if path == '/version':
                    return self.live_version
                if method == 'GET' and path.startswith('/servers/'):
                    return {'settings': {'is_reachable': True,
                                         'is_usable': True,
                                         'is_terminal_enabled': False}}
                if path.startswith('/applications/'):
                    return {'status': 'running:healthy'}
                return {}

            transport.request.side_effect = request
            self.mock(driver, '_client', return_value=transport)
            self.mock(manager.Manager, '_endpoint', return_value=mock.Mock(
                registry_host=None, registry_username=None,
                registry_password=None))
        self.request('post', self.path + '/action', self.body,
                     status=202, complete=True)
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        current_service = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual((states.NONE, states.ACTIVE, 'retained-service'),
                         (current_service.task_state, current_service.status,
                          current_service.engine_ref))
        if worker_version and worker_version != target_version:
            self.assertEqual((states.ACTIVE, states.NONE, '4.4.3'),
                             (stack.status, stack.task_state,
                              stack.engine_version))
            self.cloud.boot.assert_not_called()
            self.block.create_snapshot.assert_not_called()
            cloud.boot.assert_not_called()
            return
        self.assertEqual(states.ERROR if fail else states.ACTIVE, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual(target_version, stack.engine_version)
        self.assertEqual(1 if fail else 2, cloud.boot.call_count)
        for guest in self.guests:
            current = objects.Server.get_by_id(self.ctx, guest.id)
            self.assertEqual(states.NONE, current.task_state)
            if not fail:
                self.assertEqual('worker-image', current.image_id)
                self.assertEqual(states.ACTIVE, current.status)
                self.assertNotEqual(guest.compute_id, current.compute_id)
        if fail:
            self.assertEqual(1, cloud.wait_server.call_count)
            self.assertEqual(1, len(completed))
            untouched = next(g for g in self.guests if g not in completed)
            current = objects.Server.get_by_id(self.ctx, untouched.id)
            self.assertEqual(untouched.compute_id, current.compute_id)
            self.assertEqual('worker-old', current.image_id)
        self.assertEqual('replacement-controller', objects.Server.get_by_id(
            self.ctx, self.controller.id).compute_id)

    def test_same_engine_build_rolls_every_server_in_order(self):
        self.rolling()

    def test_new_engine_version_rolls_every_server_in_order(self):
        self.rolling(same_version=False)

    def test_mismatched_worker_image_refused_before_cloud_changes(self):
        self.rolling(worker_version='4.4.2')

    def test_rolling_upgrade_stops_on_first_failed_server(self):
        self.rolling(fail=True)

    def test_same_version_image_must_be_a_newer_publication(self):
        self.prepare()
        self.images[0].properties['coast_engine_version'] = '4.4.3'
        for created_at in (None, 'invalid', '2026-10-14T12:00:00Z',
                           self.images[1].created_at):
            self.images[0].created_at = created_at
            response = self.request('post', self.path + '/action', self.body,
                                    status=400)
            self.assertIn('App engine server', response.text)
            self.assertNotIn('Coolify', response.text)
        self.request('post', self.path + '/action',
                     {'upgrade': {'image_id': 'old-image'}}, status=400)
        self.assertFalse(self.cloud.boot.called)

    def test_owner_upgrade_and_rollback_roundtrip_with_hidden_metadata(self):
        self.prepare()
        driver = v4.CoolifyV4Driver(self.conf)
        self.manager.drivers['coolify_v4'] = driver
        self.mock(engine_upgrade.loader, 'load_driver', return_value=driver)
        self.cloud.retained_ports.return_value = [
            SimpleNamespace(id='management'), SimpleNamespace(id='private')]
        self.metadata['stack_id'] = self.stack_id
        self.cloud.tags.append('stack_id=' + self.stack_id)
        disks = {v.cinder_volume_id: SimpleNamespace(
            id=v.cinder_volume_id, metadata=self.metadata, attachments=[],
            status='available', source_volume_id=None) for v in self.volumes}
        snapshots = {}

        def get(resources, identifier):
            if identifier not in resources:
                raise sdk_exceptions.ResourceNotFound()
            return resources[identifier]

        def create_snapshot(**kw):
            snap = SimpleNamespace(
                id=kw['name'], volume_id=kw['volume_id'], status='available',
                metadata=kw['metadata'])
            snapshots[snap.id] = snap
            return snap

        def create_volume(**kw):
            disk = SimpleNamespace(id=kw['name'], metadata=kw['metadata'],
                                   status='available', attachments=[],
                                   source_volume_id=None)
            disks[disk.id] = disk
            return disk

        def delete_volume(identifier, **kw):
            self.assertFalse(any(s.volume_id == identifier
                                 for s in snapshots.values()))
            disks.pop(identifier)

        self.block.get_volume.side_effect = lambda ref: get(disks, ref)
        self.block.get_snapshot.side_effect = lambda ref: get(snapshots, ref)
        self.block.create_snapshot.side_effect = create_snapshot
        self.block.create_volume.side_effect = create_volume
        self.block.snapshots.side_effect = lambda **kw: [
            snap for snap in snapshots.values()
            if (not kw.get('name') or snap.id == kw['name']) and
            (not kw.get('volume_id') or snap.volume_id == kw['volume_id'])]
        self.block.volumes.side_effect = lambda: list(disks.values())
        self.block.delete_snapshot.side_effect = (
            lambda ref, **kw: snapshots.pop(ref, None))
        self.block.delete_volume.side_effect = delete_volume
        self.live_version = '4.4.3'
        transport = mock.MagicMock()
        transport.__enter__.return_value = transport
        pending_versions = []

        def request(method, path, **kw):
            if path == '/version':
                return (pending_versions.pop(0) if pending_versions else
                        self.live_version)
            if method == 'GET':
                return []
            return 'ok'

        transport.request.side_effect = request
        self.config.config(build_poll_interval=1,
                           group='controller_worker')
        self.mock(driver, '_client', return_value=transport)

        def boot(guest, *args, **kwargs):
            self.assertEqual(self.controller.id, guest.id)
            self.assertNotEqual(guest.id, kwargs['attempt'])
            nova = mock.Mock()
            cloud = engine_upgrade.controller.ControllerCloud(
                self.conf, objects.Stack.get_by_id(self.ctx, self.stack_id))
            with mock.patch.object(cloud, 'proxy', return_value=nova):
                cloud.boot(guest, *args, **kwargs)
            tags = nova.create_server.call_args.kwargs['tags']
            self.assertIn('server_id=' + self.controller.id, tags)
            self.assertIn('attempt=' + kwargs['attempt'], tags)
            pending_versions.append(self.live_version)
            self.live_version = ('4.4.7' if guest.image_id == 'new-image'
                                 else '4.4.3')
            return SimpleNamespace(id='replacement-' + guest.id)

        self.cloud.boot.side_effect = boot
        response = self.request('post', self.path + '/action', self.body,
                                status=202, complete=True)
        self.assertEqual(states.UPGRADING, response.json['stack']['status'])
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        self.assertEqual((states.ACTIVE, states.NONE, '4.4.7'), (
            stack.status, stack.task_state, stack.engine_version))
        self.assertEqual('new-image', objects.Server.get_by_id(
            self.ctx, self.controller.id).image_id)
        self.assertEqual(3, len(stack.upgrade_snapshots))
        self.request('post', self.path + '/action',
                     {'upgrade': {'image_id': 'old-image'}}, status=400)
        self.request('post', self.path + '/action', {'upgrade_rollback': {}},
                     status=202, complete=True)
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        self.assertEqual((states.ACTIVE, states.NONE, '4.4.3'), (
            stack.status, stack.task_state, stack.engine_version))
        self.assertEqual('old-image', objects.Server.get_by_id(
            self.ctx, self.controller.id).image_id)
        for headers in (self.headers, dict(self.headers,
                                           **{'X-Roles': 'admin'})):
            detail = self.request('get', self.path,
                                  headers=headers).json['stack']
            for private in ('previous_image_id', 'previous_engine_version',
                            'upgrade_snapshots', 'upgrade_checkpoint'):
                self.assertNotIn(private, detail)
        events = [event for event, payload in self.manager.notifier.events]
        self.assertIn('coast.environment.upgrade.end', events)
        self.assertIn('coast.environment.upgrade_rollback.end', events)
        self.request('post', self.path + '/action', self.body,
                     status=202, complete=True)
        self.assertEqual(6, len(disks))
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        clients = mock.Mock(manager=self.manager, block=self.block,
                            cloud=self.cloud, driver=driver, options={})
        clients.secrets = self.store
        self.cloud.delete_server.side_effect = lambda server: None
        with mock.patch.object(stack_teardown, 'Clients',
                               return_value=clients):
            self.request('delete', self.path, status=202, complete=True)
        self.request('get', self.path, status=404)
        gone = objects.Stack.get_by_id(self.ctx, stack.id, show_deleted=True)
        self.assertEqual(states.DELETED, gone.status, gone.fault)
        self.assertEqual({}, disks)
        self.assertEqual({}, snapshots)

    def test_schema_roles_and_cross_project_admission(self):
        self.prepare()
        for body in ({'upgrade': {}}, {'upgrade': {'image_id': 'new-image',
                                                   'version': '4.4.7'}},
                     {'upgrade_rollback': {'image_id': 'old-image'}},
                     dict(self.body, upgrade_rollback={})):
            self.request('post', self.path + '/action', body, status=400)
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        foreign = dict(self.headers, **{'X-Project-Id': 'other-project'})
        unauthenticated = dict(self.headers, **{'X-User-Id': ''})
        for body in (self.body, {'upgrade_rollback': {}}):
            self.request('post', self.path + '/action', body,
                         headers=reader, status=403)
            self.request('post', self.path + '/action', body,
                         headers=foreign, status=404)
            self.request('post', self.path + '/action', body,
                         headers=unauthenticated, status=401)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack_id).task_state)

    def test_catalog_checks_and_busy_stack(self):
        self.prepare()
        image = self.images[0]
        for field, value in (('status', 'queued'), ('owner', 'other-owner'),
                             ('tags', [])):
            previous = getattr(image, field)
            setattr(image, field, value)
            self.request('post', self.path + '/action', self.body, status=400)
            setattr(image, field, previous)
        for key, value in (('coast_role', 'worker'), ('coast_engine', 'other'),
                           ('coast_engine_version', '4.4.2'),
                           ('coast_engine_version', '4.4.5')):
            previous = image.properties[key]
            image.properties[key] = value
            self.request('post', self.path + '/action', self.body, status=400)
            image.properties[key] = previous
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        stack.acquire_task(states.RECONCILING)
        self.request('post', self.path + '/action', self.body, status=409)
        self.assertEqual('4.4.3', objects.Stack.get_by_id(
            self.ctx, self.stack_id).engine_version)
        self.assertFalse(self.cloud.boot.called)

    def test_admin_may_upgrade_for_another_project(self):
        self.prepare()
        admin = dict(self.headers, **{'X-Roles': 'admin',
                                      'X-Project-Id': 'operator-project'})
        self.request('post', self.path + '/action', self.body,
                     headers=admin, status=202, complete=True)
        self.assertEqual('4.4.7', objects.Stack.get_by_id(
            self.ctx, self.stack_id).engine_version)

    def test_error_rollback_outage_preserves_admission_status(self):
        self.prepare()
        self.request('post', self.path + '/action', self.body,
                     status=202, complete=True)
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        stack.status = states.ERROR
        stack.fault = engine_upgrade.FAILED
        stack.save()
        controller = objects.Server.get_by_id(self.ctx, self.controller.id)
        controller.status = states.ERROR
        controller.fault = engine_upgrade.FAILED
        controller.save()
        self.cloud.stopped.reset_mock()
        self.cloud.boot.reset_mock()
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                self.request('post', self.path + '/action',
                             {'upgrade_rollback': {}},
                             status=202, complete=True)
            for obj in (objects.Stack.get_by_id(self.ctx, self.stack_id),
                        objects.Server.get_by_id(self.ctx, controller.id)):
                self.assertEqual(
                    (states.ERROR, states.NONE, engine_upgrade.FAILED),
                    (obj.status, obj.task_state, obj.fault))
            self.cloud.stopped.assert_not_called()
            self.cloud.boot.assert_not_called()
            clock.return_value = 111
            self.request('post', self.path + '/action',
                         {'upgrade_rollback': {}}, status=202, complete=True)
            for obj in (objects.Stack.get_by_id(self.ctx, self.stack_id),
                        objects.Server.get_by_id(self.ctx, controller.id)):
                self.assertEqual((states.ACTIVE, states.NONE),
                                 (obj.status, obj.task_state))
            self.cloud.boot.assert_called_once()

    def test_rollback_requires_a_recorded_point(self):
        self.prepare()
        self.request('post', self.path + '/action', {'upgrade_rollback': {}},
                     status=409)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack_id).task_state)
