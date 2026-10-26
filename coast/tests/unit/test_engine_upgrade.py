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

"""Rebuild fencing, durable rollback, and image admission regressions."""

import datetime
import time
from types import SimpleNamespace
from unittest import mock

import fixtures

from coast.api import secret_store
from coast.common import context
from coast.common import exception
from coast.common import states
from coast.conductor import engine_upgrade
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.conductor import secret_retry
from coast.db import base_models
from coast.drivers import base as driver_base
from coast import objects
from coast.tests import base


class UpgradeTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(project_id='project')
        self.config.config(enabled_drivers=['coolify_v4'],
                           group='controller_worker')
        self.stack = objects.Stack(
            context=self.ctx, name='stack', driver='coolify_v4',
            plan='small', engine_version='4.4.3', status=states.ACTIVE,
            task_state=states.NONE, heartbeat_key_ref='heartbeat',
            engine_token_ref='token')
        self.stack.create()
        self.server = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='controller',
            role='control', kind='vm', status=states.ACTIVE,
            task_state=states.NONE, image_id='old-image', compute_id='old-vm',
            flavor_id='flavor', mgmt_ip='192.0.2.2', private_ip='198.51.100.2',
            agent_cert_serial='old-cert')
        self.server.create()
        self.volumes = []
        for purpose, mount in zip(engine_upgrade.PURPOSES, (
                '/data', '/var/lib/docker', '/var/lib/registry')):
            volume = objects.Volume(
                context=self.ctx, stack_id=self.stack.id,
                server_id=self.server.id, name=purpose, purpose=purpose,
                mount_path=mount, size_gb=10, status=states.IN_USE,
                cinder_volume_id=purpose + '-disk')
            volume.create()
            self.volumes.append(volume)
        self.driver = mock.Mock(SUPPORTED_ENGINE_VERSIONS=['4.4.3', '4.4.4'])
        self.driver.get_engine_version.return_value = '4.4.3'
        self.driver.get_server_status.return_value = (
            driver_base.ObservedStatus(states.ACTIVE))
        self.driver.get_app_status.return_value = driver_base.ObservedStatus(
            states.ACTIVE)
        self.owner = manager.Manager(self.conf, notifier=mock.Mock())
        self.owner.drivers['coolify_v4'] = self.driver
        self.patch(manager.Manager, '_endpoint', return_value=mock.Mock())
        self.patch(secret_store, 'call', return_value='heartbeat-value')
        self.cloud = mock.Mock(tags=[
            'coast', 'project_id=project', 'environment_id=' + self.stack.id])
        self.block = mock.Mock()
        self.cloud.proxy.return_value = self.block
        self.metadata = {'coast': 'true', 'project_id': 'project',
                         'environment_id': self.stack.id}
        self.block.get_volume.side_effect = lambda identifier: SimpleNamespace(
            id=identifier, metadata=self.metadata,
            attachments=[{'server_id': objects.Server.get_by_id(
                self.ctx, self.server.id).compute_id}])
        self.block.snapshots.return_value = []
        self.snapshots = {}
        self.block.create_snapshot.side_effect = self.snapshot
        self.block.get_snapshot.side_effect = lambda identifier: (
            self.snapshots[identifier])
        self.block.create_volume.side_effect = lambda **kw: SimpleNamespace(
            id=kw['name'] + '-disk')
        self.cloud.config_drive.return_value = (
            'config-drive', 'new-cert', datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(days=30))
        self.cloud.boot.side_effect = self.boot
        self.cloud.replacement.return_value = SimpleNamespace(id='new-vm')
        self.patch(engine_upgrade, 'RebuildCloud', return_value=self.cloud)
        self.agent = mock.Mock()
        self.agent.request.side_effect = self.agent_request
        self.patch(engine_upgrade.agent_client, 'AgentClient',
                   return_value=self.agent)
        self.patch(engine_upgrade.controller.ControllerCloud, 'select_image',
                   side_effect=self.image)
        self.patch(engine_upgrade.loader, 'load_driver',
                   return_value=self.driver)

    def patch(self, owner, name, **kwargs):
        patcher = mock.patch.object(owner, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def image(self, role, selection):
        return SimpleNamespace(id=selection, properties={
            'coast_engine': 'coolify',
            'coast_engine_version': ('4.4.4' if selection == 'new-image'
                                     else '4.4.3')})

    def snapshot(self, **kwargs):
        identifier = '%s-snapshot-%s' % (kwargs['volume_id'],
                                         len(self.snapshots))
        snap = SimpleNamespace(id=identifier, status='available',
                               metadata=kwargs['metadata'])
        self.snapshots[identifier] = snap
        return snap

    def boot(self, *args, **kwargs):
        self.driver.get_engine_version.return_value = (
            '4.4.4' if args[0].image_id == 'new-image' else '4.4.3')
        return SimpleNamespace(id='new-vm')

    def agent_request(self, method, path, body=None):
        if path.endswith('/quiesce'):
            return {'status': 'quiesced', 'deadline': time.time() + 600}
        if path == '/health':
            return {'status': 'ok', 'egress': {'status': 'ok'}}
        return {'status': 'resumed'}

    def accept(self, rollback=False):
        self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        admission_status = self.stack.status
        task = states.ROLLING_BACK if rollback else states.UPGRADING
        self.stack.acquire_task(task)
        if not rollback:
            retained = (self.stack.upgrade_checkpoint or {}).get(
                'retained_snapshot_ids', [])
            self.stack.upgrade_checkpoint = {
                'image_id': getattr(self, 'target_image', 'new-image'),
                'retained_snapshot_ids': retained}
        checkpoint = dict(self.stack.upgrade_checkpoint or {})
        checkpoint['admission_status'] = admission_status
        self.stack.upgrade_checkpoint = checkpoint
        self.stack.status = self.stack.task_state
        self.stack.save()
        return self.stack

    def execute(self, rollback=False):
        self.accept(rollback)
        self.flow = engine_upgrade.Upgrade(self.owner)
        self.flow.execute(self.ctx, self.stack,
                          revision=rpcapi.revision(self.stack),
                          rollback=rollback)
        return objects.Stack.get_by_id(self.ctx, self.stack.id)

    def test_secret_outage_is_transient_and_upgrade_can_be_retried(self):
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                result = self.execute()
            self.assertEqual(states.ACTIVE, result.status)
            self.assertEqual(states.NONE, result.task_state)
            self.assertIsNone(result.fault)
            self.cloud.stopped.assert_not_called()
            self.assertEqual(states.ACTIVE, objects.Server.get_by_id(
                self.ctx, self.server.id).status)
            clock.return_value = 111
            self.assertEqual(states.ACTIVE, self.execute().status)
            self.cloud.boot.assert_called_once()

    def test_rollback_secret_outage_defers_and_recovers(self):
        self.execute()
        self.cloud.stopped.reset_mock()
        self.cloud.boot.reset_mock()
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                result = self.execute(rollback=True)
            self.assertEqual(states.ACTIVE, result.status)
            self.assertEqual(states.NONE, result.task_state)
            self.assertIsNone(result.fault)
            self.cloud.stopped.assert_not_called()
            self.cloud.boot.assert_not_called()
            self.assertEqual(states.ACTIVE, objects.Server.get_by_id(
                self.ctx, self.server.id).status)
            clock.return_value = 111
            self.assertEqual(states.ACTIVE, self.execute(rollback=True).status)
            self.cloud.boot.assert_called_once()

    def test_error_rollback_secret_outage_preserves_status_and_recovers(self):
        self.execute()
        self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.stack.status = states.ERROR
        self.stack.fault = engine_upgrade.FAILED
        self.stack.save()
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.ERROR
        self.server.fault = engine_upgrade.FAILED
        self.server.save()
        self.cloud.stopped.reset_mock()
        self.cloud.boot.reset_mock()
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                result = self.execute(rollback=True)
            self.assertEqual(
                (states.ERROR, states.NONE, engine_upgrade.FAILED),
                (result.status, result.task_state, result.fault))
            server = objects.Server.get_by_id(self.ctx, self.server.id)
            self.assertEqual(
                (states.ERROR, states.NONE, engine_upgrade.FAILED),
                (server.status, server.task_state, server.fault))
            self.cloud.stopped.assert_not_called()
            self.cloud.boot.assert_not_called()
            clock.return_value = 111
            result = self.execute(rollback=True)
            self.assertEqual((states.ACTIVE, states.NONE),
                             (result.status, result.task_state))
            server = objects.Server.get_by_id(self.ctx, self.server.id)
            self.assertEqual((states.ACTIVE, states.NONE),
                             (server.status, server.task_state))
            self.cloud.boot.assert_called_once()

    def test_secret_outage_after_fencing_keeps_failure_revert_behavior(self):
        self.patch(engine_upgrade.Upgrade, 'engine',
                   side_effect=exception.SecretStoreUnavailable())
        result = self.execute()
        self.assertEqual(states.ERROR, result.status)
        self.assertEqual(engine_upgrade.FAILED, result.fault)
        self.assertEqual(states.NONE, result.task_state)
        self.assertEqual('old-vm',
                         self.cloud.stopped.call_args_list[0].args[0])
        self.cloud.boot.assert_called_once()
        self.assertEqual(states.ERROR, objects.Server.get_by_id(
            self.ctx, self.server.id).status)

    def test_upgrade_preserves_storage_and_records_verified_version(self):
        result = self.execute()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(('4.4.4', states.ACTIVE, states.NONE), (
            result.engine_version, result.status, result.task_state))
        self.assertEqual('old-image', result.previous_image_id)
        self.assertEqual('4.4.3', result.previous_engine_version)
        self.assertEqual(set(engine_upgrade.PURPOSES),
                         set(result.upgrade_snapshots))
        self.assertEqual(('new-image', 'new-vm'), (server.image_id,
                                                   server.compute_id))
        self.assertEqual([v.cinder_volume_id for v in self.volumes], [
            objects.Volume.get_by_id(self.ctx, v.id).cinder_volume_id
            for v in self.volumes])
        self.assertIsNone(result.upgrade_checkpoint)
        timeline = objects.EventList.get_all(self.ctx, filters={
            'stack_id': result.id, 'resource_type': 'stack',
            'action': 'upgrade'})
        self.assertEqual(1, len(timeline))
        self.assertEqual({
            'from_engine_version': '4.4.3', 'to_engine_version': '4.4.4',
            'image_id': 'new-image', 'server_id': self.server.id,
            'old_compute_id': 'old-vm',
            'new_compute_id': 'new-vm'}, timeline[0].detail)
        events = [call.args[1]
                  for call in self.owner.notifier.info.call_args_list
                  if call.args[1].startswith('coast.environment.')]
        self.assertEqual(['coast.environment.upgrade.start',
                          'coast.environment.upgrade.end'], events)
        self.cloud.stopped.assert_called_once_with(
            'old-vm', receipt=self.flow.receipt)
        # Failover helpers must fence/detach before the replacement boots.
        methods = [call[0] for call in self.cloud.mock_calls]
        self.assertLess(methods.index('stopped'), methods.index('detach'))
        self.assertLess(methods.index('detach'), methods.index('boot'))
        self.assertLess(methods.index('boot'), methods.index('delete_guest'))

    def test_all_snapshots_available_before_old_guest_is_stopped(self):
        order = []
        create = self.block.create_snapshot.side_effect
        self.block.create_snapshot.side_effect = lambda **kw: (
            order.append(('snapshot', kw['volume_id'])) or create(**kw))
        self.block.wait_for_status.side_effect = lambda snap, **kw: (
            order.append(('available', snap.id)))
        self.cloud.stopped.side_effect = (
            lambda identifier, **kwargs: order.append(('stopped', identifier)))
        self.cloud.detach.side_effect = lambda *args: order.append(
            ('detach', None))
        self.cloud.boot.side_effect = lambda *args, **kwargs: (
            order.append(('boot', None)) or self.boot(*args))
        self.execute()
        kinds = [kind for kind, _ in order]
        first_change = kinds.index('stopped')
        self.assertEqual(['snapshot', 'available'] * 3,
                         kinds[:first_change])
        self.assertEqual({v.cinder_volume_id for v in self.volumes},
                         {value for kind, value in order
                          if kind == 'snapshot'})
        self.assertLess(first_change, kinds.index('detach'))
        self.assertLess(kinds.index('detach'), kinds.index('boot'))

    def test_snapshot_failure_resumes_old_guest_and_keeps_snapshot(self):
        self.block.wait_for_status.side_effect = RuntimeError('failed')
        result = self.execute()
        self.assertEqual((states.ACTIVE, states.NONE, '4.4.3'), (
            result.status, result.task_state, result.engine_version))
        self.assertEqual(1, len(result.upgrade_checkpoint['snapshots']))
        self.assertFalse(self.cloud.boot.called)
        self.assertFalse(self.block.delete_snapshot.called)
        self.assertIn('/v1/engine/resume', [
            call.args[1] for call in self.agent.request.call_args_list])

    def test_boot_failure_never_moves_migrated_disks_back(self):
        self.cloud.boot.side_effect = RuntimeError('lost create reply')
        result = self.execute()
        self.assertEqual((states.ERROR, '4.4.3', states.NONE), (
            result.status, result.engine_version, result.task_state))
        self.assertTrue(result.upgrade_checkpoint['boot_attempted'])
        self.assertFalse(self.cloud.restore.called)
        self.assertEqual(3, len(result.upgrade_checkpoint['snapshots']))
        self.assertEqual('old-image', objects.Server.get_by_id(
            self.ctx, self.server.id).image_id)

    def test_version_failure_preserves_point_and_refuses_moveback(self):
        def wrong_version(*args, **kwargs):
            self.driver.get_engine_version.return_value = '4.4.3'
            return SimpleNamespace(id='new-vm')
        self.cloud.boot.side_effect = wrong_version
        self.config.config(deploy_timeout=1, group='conductor')
        self.patch(engine_upgrade.Upgrade, 'pause',
                   side_effect=exception.Conflict())
        result = self.execute()
        self.assertEqual(states.ERROR, result.status)
        self.assertEqual('4.4.3', result.engine_version)
        self.assertFalse(self.cloud.restore.called)
        self.assertFalse(self.cloud.delete_guest.called)

    def test_rollback_clones_all_snapshots_without_overwriting_disks(self):
        result = self.execute()
        originals = {v.cinder_volume_id for v in self.volumes}
        result = self.execute(rollback=True)
        self.assertEqual((states.ACTIVE, states.NONE, '4.4.3'), (
            result.status, result.task_state, result.engine_version))
        self.assertEqual('old-image', objects.Server.get_by_id(
            self.ctx, self.server.id).image_id)
        timeline = objects.EventList.get_all(self.ctx, filters={
            'stack_id': result.id, 'resource_type': 'stack',
            'action': 'upgrade_rollback'})
        self.assertEqual(1, len(timeline))
        self.assertEqual({
            'from_engine_version': '4.4.4', 'to_engine_version': '4.4.3',
            'image_id': 'old-image', 'server_id': self.server.id,
            'old_compute_id': 'new-vm',
            'new_compute_id': 'new-vm'}, timeline[0].detail)

        current = {objects.Volume.get_by_id(self.ctx, v.id).cinder_volume_id
                   for v in self.volumes}
        self.assertEqual(3, len(current))
        self.assertFalse(current & originals)
        self.assertFalse(self.cloud.delete_volume.called)
        self.assertEqual(3, len(result.upgrade_snapshots))

    def test_duplicate_cast_does_not_rebuild_twice(self):
        self.accept()
        resource = self.stack.obj_clone()
        revision = rpcapi.revision(resource)
        engine_upgrade.Upgrade(self.owner).execute(
            self.ctx, resource, revision=revision)
        before = self.cloud.boot.call_count
        engine_upgrade.Upgrade(self.owner).execute(
            self.ctx, resource, revision=revision)
        self.assertEqual(before, self.cloud.boot.call_count)

    def test_refused_conductor_image_does_not_change_cloud_resources(self):
        self.driver.SUPPORTED_ENGINE_VERSIONS = ['4.4.3']
        result = self.execute()
        self.assertEqual((states.ACTIVE, states.NONE, '4.4.3'), (
            result.status, result.task_state, result.engine_version))
        self.assertEqual(engine_upgrade.REFUSED, result.fault)
        self.assertFalse(self.cloud.boot.called)
        self.assertFalse(self.block.create_snapshot.called)

    def test_same_version_requires_later_image_creation_time(self):
        images = {
            'new-image': SimpleNamespace(
                id='new-image', created_at='2026-10-19T12:00:00Z',
                properties={'coast_engine': 'coolify',
                            'coast_engine_version': '4.4.3'}),
            'old-image': SimpleNamespace(
                id='old-image', created_at='2026-10-15T12:00:00Z',
                properties={'coast_engine': 'coolify',
                            'coast_engine_version': '4.4.3'})}
        with mock.patch.object(
                engine_upgrade.controller.ControllerCloud, 'select_image',
                side_effect=lambda role, selection: images[selection]):
            self.assertEqual('4.4.3', engine_upgrade.eligible(
                self.conf, self.stack, 'new-image', ['4.4.3']))
            for stamp in (None, 'invalid', images['old-image'].created_at,
                          '2026-10-14T12:00:00Z'):
                images['new-image'].created_at = stamp
                self.assertRaises(exception.Invalid, engine_upgrade.eligible,
                                  self.conf, self.stack, 'new-image',
                                  ['4.4.3'])
            self.assertRaises(exception.Invalid, engine_upgrade.eligible,
                              self.conf, self.stack, 'old-image', ['4.4.3'])

    def test_image_checks_fail_closed(self):
        for value in ('4.4.3', '4.4.2', 'latest', '4.4.4-beta'):
            with self.subTest(version=value):
                with mock.patch.object(
                    engine_upgrade.controller.ControllerCloud, 'select_image',
                    return_value=SimpleNamespace(id='image', properties={
                        'coast_engine': 'coolify',
                        'coast_engine_version': value})):
                    self.assertRaises(exception.Invalid,
                                      engine_upgrade.eligible,
                                      self.conf, self.stack, 'image',
                                      ['4.4.4'])
        with mock.patch.object(engine_upgrade.controller.ControllerCloud,
                               'select_image', side_effect=ValueError()):
            self.assertRaises(exception.Invalid, engine_upgrade.eligible,
                              self.conf, self.stack, 'unpublished', ['4.4.4'])

    def test_restart_releases_child_locks_and_keeps_snapshot_references(self):
        self.cloud.boot.side_effect = RuntimeError('lost reply')
        self.execute()
        self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        references = self.stack.upgrade_checkpoint['snapshots']
        self.stack.acquire_task(states.UPGRADING)
        self.stack.conductor_host = self.conf.host
        self.stack.save()
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.acquire_task(states.UPGRADING)
        self.server.save()
        self.owner.recover_stacks()
        result = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual((states.ERROR, states.NONE), (result.status,
                                                       result.task_state))
        self.assertEqual(references, result.upgrade_checkpoint['snapshots'])
        self.assertEqual(states.NONE, objects.Server.get_by_id(
            self.ctx, self.server.id).task_state)

    def workload(self, status=states.ACTIVE):
        # These unit cases isolate controller workload recovery. Serial
        # worker replacement is covered by the API/RPC functional tests.
        self.patch(engine_upgrade.Upgrade, 'rolling_targets')
        self.patch(engine_upgrade.Upgrade, 'roll_servers')
        worker = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='worker',
            role='worker', kind='vm', status=states.ACTIVE,
            task_state=states.NONE, engine_ref='worker-ref')
        worker.create()
        source = objects.Source(context=self.ctx, type='git',
                                repo_url='https://example.com/repo.git',
                                branch='main')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack.id, server_id=worker.id,
            name='service', source_id=source.id, ports='8080', replicas=1,
            auto_deploy=False, status=status, task_state=states.NONE,
            engine_ref='service-ref')
        service.create()
        deployment = objects.Deployment(
            context=self.ctx, service_id=service.id, server_id=worker.id,
            status=states.ACTIVE, source_ref='main', resolved_ref='a' * 40,
            trigger='api', spec={'source': {'type': 'git',
                                            'repo_url':
                                            'https://example.com/repo.git',
                                            'branch': 'main'},
                                 'env': {}})
        deployment.create()
        service.current_deployment_id = deployment.id
        service.save()
        return service, deployment

    def test_healthy_services_keep_engine_refs_and_current_deployment(self):
        service, deployment = self.workload()
        result = self.execute()
        service = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(states.ACTIVE, result.status)
        self.assertEqual(('service-ref', deployment.id, states.NONE), (
            service.engine_ref, service.current_deployment_id,
            service.task_state))
        self.assertFalse(self.driver.deploy_app.called)
        self.assertFalse(self.driver.register_server.called)

    def test_stopped_service_stays_stopped_and_is_not_redeployed(self):
        service, deployment = self.workload(states.STOPPED)
        self.driver.get_app_status.return_value = (
            driver_base.ObservedStatus(states.STOPPED))
        result = self.execute()
        service = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(states.ACTIVE, result.status)
        self.assertEqual((states.STOPPED, deployment.id), (
            service.status, service.current_deployment_id))
        self.assertFalse(self.driver.deploy_app.called)

    def test_missing_service_redeploys_the_pinned_current_release(self):
        service, previous = self.workload()
        self.driver.get_app_status.side_effect = [
            driver_base.ObservedStatus(states.STOPPED),
            driver_base.ObservedStatus(states.ACTIVE)]
        self.driver.deploy_app.return_value = (
            driver_base.DriverResult('new-deployment'))
        self.driver.get_deployment_status.return_value = (
            driver_base.ObservedStatus(states.ACTIVE, resolved_ref='a' * 40))
        result = self.execute()
        service = objects.Service.get_by_id(self.ctx, service.id)
        current = objects.Deployment.get_by_id(self.ctx,
                                               service.current_deployment_id)
        self.assertEqual(states.ACTIVE, result.status)
        self.assertNotEqual(previous.id, current.id)
        self.assertEqual('a' * 40, current.source_ref)
        self.assertEqual('a' * 40, current.resolved_ref)
        self.assertEqual(states.SUPERSEDED, objects.Deployment.get_by_id(
            self.ctx, previous.id).status)
        self.assertEqual('service-ref', service.engine_ref)

    def test_busy_deployment_refuses_before_any_cloud_change(self):
        service, deployment = self.workload()
        other = objects.Deployment(context=self.ctx, service_id=service.id,
                                   server_id=service.server_id,
                                   status=states.BUILDING,
                                   trigger='api', source_ref='main', spec={})
        other.create()
        result = self.execute()
        self.assertEqual((states.ACTIVE, states.NONE), (result.status,
                                                        result.task_state))
        self.assertFalse(self.block.create_snapshot.called)
        self.assertFalse(self.cloud.boot.called)

    def test_success_replaces_point_before_snapshot_cleanup(self):
        result = self.execute()
        old = dict(result.upgrade_snapshots)
        self.driver.SUPPORTED_ENGINE_VERSIONS += ['4.4.5']
        self.target_image = 'newest-image'
        self.patch(engine_upgrade.controller.ControllerCloud, 'select_image',
                   side_effect=lambda role, image: SimpleNamespace(
                       id=image, properties={
                           'coast_engine': 'coolify',
                           'coast_engine_version': (
                               '4.4.5' if image == 'newest-image'
                               else '4.4.4')}))

        def boot(*args, **kwargs):
            self.driver.get_engine_version.return_value = '4.4.5'
            return SimpleNamespace(id='newer-vm')
        self.cloud.boot.side_effect = boot

        def delete(identifier, **kwargs):
            current = objects.Stack.get_by_id(self.ctx, self.stack.id)
            self.assertEqual('4.4.5', current.engine_version)
            self.assertNotIn(identifier, current.upgrade_snapshots.values())
        self.block.delete_snapshot.side_effect = delete
        result = self.execute()
        self.assertEqual('4.4.5', result.engine_version)
        self.assertEqual('4.4.4', result.previous_engine_version)
        self.assertFalse(set(old.values()) &
                         set(result.upgrade_snapshots.values()))
        self.assertEqual(set(old.values()), {
            call.args[0]
            for call in self.block.delete_snapshot.call_args_list})

    def test_failed_successor_keeps_previous_successful_rollback_point(self):
        result = self.execute()
        old = dict(result.upgrade_snapshots)
        self.driver.SUPPORTED_ENGINE_VERSIONS += ['4.4.5']
        self.target_image = 'newest-image'
        self.patch(engine_upgrade.controller.ControllerCloud, 'select_image',
                   side_effect=lambda role, image: SimpleNamespace(
                       id=image, properties={
                           'coast_engine': 'coolify',
                           'coast_engine_version': (
                               '4.4.5' if image == 'newest-image'
                               else '4.4.4')}))
        self.cloud.boot.side_effect = RuntimeError('uncertain boot')
        result = self.execute()
        self.assertEqual(states.ERROR, result.status)
        self.assertEqual(old, result.upgrade_snapshots)
        self.assertEqual('4.4.3', result.previous_engine_version)
        self.assertEqual('4.4.4', engine_upgrade.rollback_point(result)[1])
        self.assertFalse(self.block.delete_snapshot.called)

    def test_preboot_failure_restores_original_identity_and_attachments(self):
        self.cloud.config_drive.side_effect = RuntimeError('unavailable')
        result = self.execute()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual((states.ACTIVE, states.NONE), (result.status,
                                                        result.task_state))
        self.assertEqual(('old-vm', 'old-image', 'old-cert'), (
            server.compute_id, server.image_id, server.agent_cert_serial))
        self.assertEqual('old-vm', self.cloud.restore.call_args.args[0])
        self.assertFalse(self.cloud.boot.called)

    def test_failed_compensation_does_not_report_active(self):
        self.cloud.config_drive.side_effect = RuntimeError('unavailable')
        self.cloud.restore.side_effect = RuntimeError('old guest cannot boot')
        result = self.execute()
        self.assertEqual(states.ERROR, result.status)
        self.assertEqual(states.ERROR, objects.Server.get_by_id(
            self.ctx, self.server.id).status)

    def test_conductor_rejects_mutated_previous_image_before_snapshot(self):
        self.patch(engine_upgrade.controller.ControllerCloud, 'select_image',
                   side_effect=lambda role, image: SimpleNamespace(
                       id=image, properties={
                           'coast_engine': 'coolify',
                           'coast_engine_version': '4.4.4'}))
        result = self.execute()
        self.assertEqual((states.ACTIVE, states.NONE), (result.status,
                                                        result.task_state))
        self.assertEqual('4.4.3', result.engine_version)
        self.assertFalse(self.block.create_snapshot.called)

    def test_replay_during_claim_does_not_start_another_rebuild(self):
        self.accept()
        resource = self.stack.obj_clone()
        revision = rpcapi.revision(resource)
        prepare = engine_upgrade.Upgrade.prepare

        def overlapping(flow):
            engine_upgrade.Upgrade(self.owner).execute(
                self.ctx, resource, revision=revision)
            prepare(flow)
        with mock.patch.object(engine_upgrade.Upgrade, 'prepare', overlapping):
            engine_upgrade.Upgrade(self.owner).execute(
                self.ctx, resource, revision=revision)
        self.assertEqual(1, self.cloud.boot.call_count)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def test_expired_snapshot_lease_resumes_without_booting(self):
        self.agent.request.side_effect = lambda method, path, body=None: {
            'status': 'quiesced' if path.endswith('/quiesce') else 'resumed',
            'deadline': time.time() - 1}
        result = self.execute()
        self.assertEqual((states.ACTIVE, states.NONE), (result.status,
                                                        result.task_state))
        self.assertFalse(self.cloud.boot.called)
        self.assertFalse(self.block.delete_snapshot.called)

    def test_rollback_failure_keeps_its_point_for_another_attempt(self):
        self.cloud.boot.side_effect = lambda *args, **kwargs: (
            SimpleNamespace(id='new-vm'))
        self.patch(engine_upgrade.Upgrade, 'pause',
                   side_effect=exception.Conflict())
        result = self.execute()
        self.assertEqual(states.ERROR, result.status)
        self.assertIsNone(result.upgrade_snapshots)
        snapshots = dict(result.upgrade_checkpoint['snapshots'])
        self.cloud.boot.side_effect = self.boot
        self.cloud.wait_volume.side_effect = RuntimeError('clone failed')
        result = self.execute(rollback=True)
        self.assertEqual(states.ERROR, result.status)
        self.assertEqual(snapshots, engine_upgrade.rollback_point(result)[2])
        self.cloud.wait_volume.side_effect = None
        result = self.execute(rollback=True)
        self.assertEqual((states.ACTIVE, '4.4.3'), (
            result.status, result.engine_version))

    def test_busy_services_cannot_hide_behind_public_pagination_limit(self):
        service, deployment = self.workload()
        other = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            server_id=service.server_id, name='other',
            source_id=service.source_id,
            ports='8080', replicas=1, auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='other-ref')
        other.create()
        other.acquire_task(states.DEPLOYING)
        self.config.config(max_limit=1, group='api')
        result = self.execute()
        self.assertEqual((states.ACTIVE, states.NONE), (result.status,
                                                        result.task_state))
        self.assertFalse(self.cloud.boot.called)
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, other.id).task_state)

    def test_stale_completion_cannot_release_another_attempts_locks(self):
        self.accept()
        revision = rpcapi.revision(self.stack)
        flow = engine_upgrade.Upgrade(self.owner)
        with mock.patch.object(engine_upgrade.Upgrade, 'prepare',
                               side_effect=exception.Conflict()):
            flow.execute(self.ctx, self.stack, revision=revision)
        current = objects.Stack.get_by_id(self.ctx, self.stack.id)
        current.acquire_task(states.UPGRADING)
        current.conductor_host = self.conf.host
        current.upgrade_checkpoint = {'operation_id': 'another-attempt'}
        current.save()
        self.assertFalse(flow.finish(False))
        current = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual(states.UPGRADING, current.task_state)
        self.assertEqual('another-attempt', current.upgrade_checkpoint[
            'operation_id'])

    def test_controller_status_notifications_follow_committed_states(self):
        result = self.execute()
        self.assertEqual(states.ACTIVE, result.status)
        server_events = [call.args[1:] for call in
                         self.owner.notifier.info.call_args_list
                         if call.args[1].startswith('coast.server.')]
        self.assertEqual(['coast.server.update.start',
                          'coast.server.update.end'], [
            event[0] for event in server_events])
        self.assertEqual([states.FAILOVER, states.ACTIVE], [
            event[1]['status'] for event in server_events])

    def test_restart_upgrade_error_notification_reports_error_state(self):
        self.cloud.boot.side_effect = RuntimeError('lost reply')
        self.execute()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        stack.acquire_task(states.UPGRADING)
        stack.conductor_host = self.conf.host
        stack.save()
        self.owner.notifier.reset_mock()
        self.owner.recover_stacks()
        errors = [call.args[2] for call in
                  self.owner.notifier.info.call_args_list
                  if call.args[1] == 'coast.environment.upgrade.error']
        self.assertEqual([states.ERROR], [event['status'] for event in errors])
        timeline = objects.EventList.get_all(self.ctx, filters={
            'stack_id': stack.id, 'resource_type': 'stack',
            'action': 'upgrade', 'outcome': 'failure'})
        self.assertEqual(2, len(timeline))
        for event in timeline:
            self.assertEqual(self.server.id, event.detail['server_id'])
            self.assertEqual('old-vm', event.detail['old_compute_id'])
            self.assertIsNone(event.detail['new_compute_id'])

    def test_rollback_preserves_prior_point_until_upgrade_success(self):
        first = self.execute()
        original = dict(first.upgrade_snapshots)
        self.driver.SUPPORTED_ENGINE_VERSIONS += ['4.4.5']
        self.target_image = 'newest-image'
        self.patch(engine_upgrade.controller.ControllerCloud, 'select_image',
                   side_effect=lambda role, image: SimpleNamespace(
                       id=image, properties={
                           'coast_engine': 'coolify',
                           'coast_engine_version': (
                               '4.4.5' if image == 'newest-image'
                               else '4.4.4')}))
        self.cloud.boot.side_effect = RuntimeError('uncertain boot')
        failed = self.execute()
        pending = set(failed.upgrade_checkpoint['snapshots'].values())
        self.cloud.boot.side_effect = self.boot
        restored = self.execute(rollback=True)
        self.assertEqual((states.ACTIVE, '4.4.4'), (
            restored.status, restored.engine_version))
        self.assertEqual('4.4.3', restored.previous_engine_version)
        self.assertEqual(original, restored.upgrade_snapshots)
        self.assertEqual(pending, set(restored.upgrade_checkpoint[
            'retained_snapshot_ids']))
        self.assertFalse(self.block.delete_snapshot.called)

        def boot(*args, **kwargs):
            self.driver.get_engine_version.return_value = '4.4.5'
            return SimpleNamespace(id='newest-vm')
        self.cloud.boot.side_effect = boot
        final = self.execute()
        self.assertEqual('4.4.5', final.engine_version)
        self.assertIsNone(final.upgrade_checkpoint)
        self.assertEqual(set(original.values()) | pending, {
            call.args[0]
            for call in self.block.delete_snapshot.call_args_list})
