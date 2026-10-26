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

"""Controller rebuild admission, image refusal and retained storage."""

from concurrent import futures
from contextlib import closing
import datetime
import sqlite3
import threading
from types import SimpleNamespace
from unittest import mock

import fixtures
from oslo_db import exception as db_exception
from oslo_db.sqlalchemy import enginefacade
from oslo_utils import timeutils

from coast.api import secret_store
from coast.common import context
from coast.common import exception
from coast.common import states
from coast.conductor import backups
from coast.conductor import controller
from coast.conductor import controller_failover
from coast.conductor import deployments
from coast.conductor import engine_upgrade
from coast.conductor.engine_upgrade import RebuildCloud
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.conductor import secret_retry
from coast.conductor import server_failover
from coast.db import api
from coast.db import base_models
from coast.drivers import base as driver_base
from coast.health_manager import failover
from coast.health_manager import stale
from coast import objects
from coast.tests import base


class ControllerFailoverTest(base.TestCase):
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
            hostname='coast-stack-5bad06-controller',
            role='control', kind='vm', status=states.ACTIVE,
            task_state=states.NONE, image_id='old-image', compute_id='old-vm',
            flavor_id='flavor', mgmt_ip='192.0.2.2', private_ip='198.51.100.2',
            floating_ip_id='fip-id', agent_cert_serial='old-cert')
        self.server.create()
        self.volumes = []
        for purpose, mount in controller_failover.MOUNTS.items():
            volume = objects.Volume(
                context=self.ctx, stack_id=self.stack.id,
                server_id=self.server.id, name=purpose, purpose=purpose,
                mount_path=mount, size_gb=10, status=states.IN_USE,
                cinder_volume_id=purpose + '-disk')
            volume.create()
            self.volumes.append(volume)
        self.driver = mock.Mock(SUPPORTED_ENGINE_VERSIONS=['4.4.3'])
        self.driver.get_engine_version.return_value = '4.4.3'
        self.driver.get_server_status.side_effect = (
            lambda endpoint, stack, server: driver_base.ObservedStatus(
                server.status))
        self.driver.get_app_status.return_value = driver_base.ObservedStatus(
            states.ACTIVE)
        self.manager = manager.Manager(self.conf, notifier=mock.Mock())
        self.manager.drivers['coolify_v4'] = self.driver
        self.patch(manager.Manager, '_endpoint', return_value=mock.Mock())
        self.patch(secret_store, 'call', return_value='heartbeat-value')
        self.patch(server_failover.time, 'sleep', return_value=None)
        self.order = []
        self.cloud = mock.Mock(tags=[
            'coast', 'project_id=project',
            'environment_id=' + self.stack.id])
        self.metadata = {'coast': 'true', 'project_id': 'project',
                         'environment_id': self.stack.id}
        self.ports = [SimpleNamespace(id='mgmt-port'),
                      SimpleNamespace(id='env-port')]
        self.cloud.retained_ports.return_value = self.ports
        self.cloud.proxy.return_value.get_volume.side_effect = (
            lambda identifier: SimpleNamespace(
                id=identifier, metadata=self.metadata,
                attachments=[{'server_id': 'old-vm'}]))
        self.cloud.proxy.return_value.get_ip.return_value = SimpleNamespace(
            port_id='env-port')
        self.cloud.config_drive.return_value = (
            'config-drive', 'new-cert', datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(days=30))
        self.cloud.boot.side_effect = self._boot
        self.cloud.stopped.side_effect = (
            lambda identifier, **kwargs: self.order.append(
                ('stop', identifier)))
        self.cloud.detach.side_effect = lambda identifier, ports, volumes: (
            self.order.append(('detach', identifier)))
        self.cloud.delete_guest.side_effect = (
            lambda identifier: self.order.append(('delete', identifier)))
        self.cloud.restore.side_effect = lambda *args: self.order.append(
            ('restore', args[0]))
        self.cloud.replacement.return_value = SimpleNamespace(id='new-vm')
        self.patch(engine_upgrade, 'RebuildCloud', return_value=self.cloud)
        self.agent = mock.Mock()
        self.agent_paths = []
        self.agent.request.side_effect = self._agent
        self.patch(controller_failover.agent_client, 'AgentClient',
                   return_value=self.agent)
        self.patch(engine_upgrade.controller.ControllerCloud, 'select_image',
                   side_effect=self._image)
        self.patch(engine_upgrade.loader, 'load_driver',
                   return_value=self.driver)
        self.version = '4.4.3'
        self.missing = False
        self.glance_down = False

    def test_secret_outage_defers_and_recovers(self):
        self.stale()
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                server = self.replace()
                self.assertEqual(states.UNHEALTHY, server.status)
                self.assertEqual(states.NONE, server.task_state)
                self.assertEqual('old-vm', server.compute_id)
                self.assertEqual([], self.order)
                self.assertEqual(states.UNHEALTHY, self.replace().status)
            clock.return_value = 111
            self.assertEqual(states.ACTIVE, self.replace().status)
            self.assertEqual('new-vm', objects.Server.get_by_id(
                self.ctx, self.server.id).compute_id)

    def test_secret_outage_releases_an_explicit_api_claim(self):
        self.admit_admin()
        with mock.patch.object(secret_store, 'call',
                               side_effect=exception.SecretStoreUnavailable()):
            server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual(states.NONE, server.task_state)
        self.assertEqual([], self.order)

    def test_explicit_unhealthy_outage_preserves_status_and_recovers(self):
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.UNHEALTHY
        self.server.fault = 'Existing server failure.'
        self.server.save()
        self.admit_admin()
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                result = self.replace()
            self.assertEqual(
                (states.UNHEALTHY, states.NONE, 'Existing server failure.'),
                (result.status, result.task_state, result.fault))
            self.assertEqual([], self.order)
            clock.return_value = 111
            self.admit_admin()
            result = self.replace()
            self.assertEqual((states.ACTIVE, states.NONE, None),
                             (result.status, result.task_state, result.fault))

    def test_explicit_error_outage_preserves_status_and_recovers(self):
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.ERROR
        self.server.fault = 'Existing server failure.'
        self.server.save()
        self.admit_admin()
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                result = self.replace()
            self.assertEqual(
                (states.ERROR, states.NONE, 'Existing server failure.'),
                (result.status, result.task_state, result.fault))
            self.assertEqual([], self.order)
            clock.return_value = 111
            self.admit_admin()
            result = self.replace()
            self.assertEqual((states.ACTIVE, states.NONE, None),
                             (result.status, result.task_state, result.fault))

    def test_engine_token_outage_does_not_claim_or_fence(self):
        self.stale()
        with mock.patch.object(self.manager.__class__, '_endpoint',
                               side_effect=exception.SecretStoreUnavailable()):
            self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assertEqual([], self.order)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def patch(self, owner, name, **kwargs):
        patcher = mock.patch.object(owner, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _image(self, role, selection):
        if self.glance_down:
            raise RuntimeError('glance down')
        if self.missing:
            raise ValueError('missing')
        return SimpleNamespace(id=selection, properties={
            'coast_engine': 'coolify', 'coast_role': 'controller',
            'coast_engine_version': self.version})

    def _agent(self, method, path, body=None):
        self.agent_paths.append(path)
        return {'status': 'ok', 'egress': {'status': 'ok'}}

    def _boot(self, guest, ports, volumes, data, attempt=None):
        self.assertEqual(self.server.id, guest.id)
        cloud = controller.ControllerCloud(self.conf, self.stack)
        nova = mock.Mock()
        with mock.patch.object(cloud, 'proxy', return_value=nova):
            cloud.boot(guest, ports, volumes, data, attempt=attempt)
        tags = nova.create_server.call_args.kwargs['tags']
        self.assertIn('server_id=' + self.server.id, tags)
        self.assertIn('attempt=' + attempt, tags)
        self.order.append(('boot', guest.id, guest.image_id))
        self.guest = guest
        self.assertEqual(self.server.hostname, guest.hostname)
        self.assertIsNotNone(attempt)
        return SimpleNamespace(id='new-vm')

    def resize(self, fail=False):
        source = objects.Source(context=self.ctx, type='image',
                                image='example.com/app:stable')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='service', source_id=source.id,
            ports='8080', replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE, engine_ref='app-ref')
        service.create()
        checks = []

        def assert_original_selection():
            self.assertEqual('flavor', objects.Server.get_by_id(
                self.ctx, self.server.id).flavor_id)
            self.assertEqual('small', objects.Stack.get_by_id(
                self.ctx, self.stack.id).plan)

        def health(*args, **kwargs):
            assert_original_selection()
            checks.append('health')
            if fail:
                raise RuntimeError('health failed')
            return self._agent(*args, **kwargs)

        def workload(*args, **kwargs):
            assert_original_selection()
            self.assertEqual(['health'], checks)
            checks.append('workload')
            return driver_base.ObservedStatus(states.ACTIVE)

        self.agent.request.side_effect = health
        self.driver.get_app_status.side_effect = workload
        self.stack.acquire_task(states.FAILING_OVER)
        self.admit_admin()
        self.manager.execute(self.ctx, 'resize_server', self.server,
                             options={'targets': {self.server.id: {
                                 'target_flavor': 'larger-flavor',
                                 'target_plan': 'larger'}}},
                             revision=rpcapi.revision(self.server))
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual('flavor' if fail else 'larger-flavor',
                         server.flavor_id)
        self.assertEqual('small' if fail else 'larger', stack.plan)
        self.assertEqual(['health'] if fail else ['health', 'workload'],
                         checks)
        self.assertEqual('fip-id', server.floating_ip_id)
        self.assertEqual('4.4.3', stack.engine_version)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual('larger-flavor', self.guest.flavor_id)
        for volume in self.volumes:
            self.assertEqual(volume.cinder_volume_id, objects.Volume.get_by_id(
                self.ctx, volume.id).cinder_volume_id)
        if fail:
            self.assertEqual('old-vm', server.compute_id)
            self.assertIn(('restore', 'old-vm'), self.order)

    def test_resize_secret_outage_preserves_server_and_recovers(self):
        self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.stack.status = states.ACTIVE
        self.stack.save()
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.ACTIVE
        self.server.fault = 'Existing server fault.'
        self.server.save()
        old_id, old_flavor = self.server.compute_id, self.server.flavor_id
        before = list(self.order)
        options = {'admission_status': states.ACTIVE, 'targets': {
            self.server.id: {'target_flavor': 'larger-flavor',
                             'target_plan': 'larger'}}}

        def execute():
            stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            stack.acquire_task(states.FAILING_OVER)
            server = objects.Server.get_by_id(self.ctx, self.server.id)
            server.acquire_task(states.FAILING_OVER)
            server.status = states.FAILOVER
            server.save()
            self.manager.execute(self.ctx, 'resize_server', server,
                                 options=options,
                                 revision=rpcapi.revision(server))
            return objects.Server.get_by_id(self.ctx, self.server.id)

        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                server = execute()
            self.assertEqual((states.ACTIVE, states.NONE, old_id, old_flavor,
                              'Existing server fault.'),
                             (server.status, server.task_state,
                              server.compute_id, server.flavor_id,
                              server.fault))
            self.assertEqual(before, self.order)
            self.assertEqual(states.NONE, objects.Stack.get_by_id(
                self.ctx, self.stack.id).task_state)
            clock.return_value = 111
            server = execute()
            self.assertEqual((states.ACTIVE, states.NONE, 'larger-flavor'),
                             (server.status, server.task_state,
                              server.flavor_id))
            self.assertNotEqual(old_id, server.compute_id)

    def test_resize_controller_commits_plan_after_health(self):
        self.resize()

    def test_resize_controller_failure_retains_flavor_plan_and_disks(self):
        self.resize(fail=True)

    def stale(self):
        now = timeutils.utcnow()
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.UNHEALTHY
        self.server.last_heartbeat_at = now - datetime.timedelta(seconds=300)
        self.server.save()
        with api.writer.using(self.ctx):
            failover.record(self.ctx, self.server, states.UNHEALTHY,
                            now - datetime.timedelta(seconds=121))
        self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.stack.status = states.DEGRADED
        self.stack.fault = stale.HEALTH_FAULT
        self.stack.save()

    def replace(self):
        current = objects.Server.get_by_id(self.ctx, self.server.id)
        self.manager.execute(self.ctx, 'failover_server', current,
                             options={'admission_status': getattr(
                                 self, 'admission_status', None)},
                             revision=rpcapi.revision(current))
        return objects.Server.get_by_id(self.ctx, self.server.id)

    def admit_admin(self):
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.admission_status = self.server.status
        self.server.acquire_task(states.FAILING_OVER)
        self.server.status = states.FAILOVER
        self.server.save()

    def image_events(self):
        return [event for event in objects.EventList.get_all(
            self.ctx, filters={'resource_id': self.server.id,
                               'action': 'failover'})
                if event.message == controller_failover.IMAGE_FAULT]

    def refusal_logs(self):
        return self.useFixture(fixtures.FakeLogger(
            format='%(levelname)s %(message)s'))

    def assert_refused(self, logs, reason):
        expected = ('WARNING Failover: stack_id=%s server_id=%s '
                    'event=refused reason=%s' % (
                        self.stack.id, self.server.id, reason))
        self.assertEqual(1, logs.output.count(expected), logs.output)

    def test_log_start_and_end(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.stale()
        old_id = self.server.compute_id
        result = self.replace()
        self.assertEqual(1, logs.output.count('event=start'))
        self.assertEqual(1, logs.output.count('event=end'))
        self.assertIn('outcome=success', logs.output)
        self.assertIn('duration=', logs.output)
        self.assertIn('old_compute_id=%s' % old_id, logs.output)
        self.assertIn('new_compute_id=%s' % result.compute_id, logs.output)

    def test_recorded_image_rebuild_keeps_identity_and_storage(self):
        self.stale()
        receipt = rpcapi.receipt_id(
            'failover_server', self.server, rpcapi.revision(self.server))
        self.assertIs(RebuildCloud.config_drive,
                      controller.ControllerCloud.config_drive)
        server = self.replace()
        self.cloud.stopped.assert_called_once_with(
            'old-vm', receipt=receipt)
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual(states.NONE, server.task_state)
        self.assertEqual('new-vm', server.compute_id)
        timeline = objects.EventList.get_all(self.ctx, filters={
            'resource_id': server.id, 'action': 'failover'})
        event = next(row for row in timeline if row.outcome == 'success')
        self.assertEqual(server.compute_id, event.detail['new_compute_id'])
        self.assertTrue(event.detail['old_compute_id'])
        self.assertEqual(server.name, event.detail['server_name'])

        self.assertEqual('old-image', server.image_id)
        self.assertEqual('fip-id', server.floating_ip_id)
        self.assertIsNone(server.fault)
        self.assertEqual([volume.cinder_volume_id for volume in self.volumes],
                         [objects.Volume.get_by_id(
                             self.ctx, volume.id).cinder_volume_id
                          for volume in self.volumes])
        self.assertEqual('4.4.3', objects.Stack.get_by_id(
            self.ctx, self.stack.id).engine_version)
        self.assertIsNone(objects.Stack.get_by_id(
            self.ctx, self.stack.id).upgrade_checkpoint)
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual((states.ACTIVE, states.NONE, None),
                         (stack.status, stack.task_state, stack.fault))
        self.assertEqual(('stop', 'detach', 'boot', 'delete'),
                         tuple(name for name, *_rest in self.order))
        self.assertEqual(self.guest.image_id, 'old-image')
        self.assertEqual(self.server.id, self.guest.id)
        self.driver.register_server.assert_not_called()
        self.assertEqual(['/health'], self.agent_paths)
        self.assertNotIn('sensitive', str(self.manager.notifier.mock_calls))

    def workload(self, status=states.ERROR, task=states.NONE, age=3600,
                 deployment_status=None):
        source = objects.Source(context=self.ctx, type='image',
                                image='example.com/app:stable')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='service', source_id=source.id,
            ports='8080', replicas=1, auto_deploy=False,
            status=status, task_state=states.NONE, engine_ref='app-ref')
        service.create()
        if task != states.NONE:
            service.acquire_task(task)
            with api.writer.using(self.ctx) as session:
                session.query(service.repository.model).filter_by(
                    id=service.id).update({
                        'task_started_at': timeutils.utcnow() -
                        datetime.timedelta(seconds=age)})
        deployment = None
        if deployment_status:
            deployment = objects.Deployment(
                context=self.ctx, service_id=service.id,
                server_id=self.server.id, status=deployment_status,
                trigger='api', source_ref='stable', spec={})
            deployment.create()
            with api.writer.using(self.ctx) as session:
                session.query(deployment.repository.model).filter_by(
                    id=deployment.id).update({
                        'created_at': timeutils.utcnow() -
                        datetime.timedelta(seconds=age)})
        return service, deployment

    def test_abandoned_workload_does_not_block_controller(self):
        for admin, status, task, pending in (
                (False, states.ERROR, states.DEPLOYING, states.DEPLOYING),
                (True, states.STOPPED, states.RECONCILING, states.PENDING),
                (True, states.ERROR, states.NONE, states.BUILDING)):
            with self.subTest(admin=admin, pending=pending):
                self.stale()
                if admin:
                    self.admit_admin()
                service, deployment = self.workload(status, task,
                                                    deployment_status=pending)
                self.driver.get_app_status.return_value = (
                    driver_base.ObservedStatus(states.STOPPED))

                def healthy(method, path, body=None):
                    self.assertEqual(pending, objects.Deployment.get_by_id(
                        self.ctx, deployment.id).status)
                    # A delayed deployment recovery cannot write after the
                    # failover task supersedes its expired claim.
                    deployments.Recovery(self.manager).reconcile(
                        self.ctx, deployment.id)
                    return self._agent(method, path, body)

                self.agent.request.side_effect = healthy
                server = self.replace()
                self.assertEqual('new-vm', server.compute_id)
                self.assertEqual(states.ACTIVE, server.status)
                current = objects.Service.get_by_id(self.ctx, service.id)
                self.assertEqual(states.NONE, current.task_state)
                self.assertEqual(states.STOPPED, current.status)
                self.assertEqual(states.FAILED, objects.Deployment.get_by_id(
                    self.ctx, deployment.id).status)
                self.assertEqual(states.ACTIVE, objects.Stack.get_by_id(
                    self.ctx, self.stack.id).status)
                service.destroy()
                self.server.compute_id = 'old-vm'
                self.server.save()

    def test_live_workload_still_blocks_controller(self):
        self.stale()
        service, deployment = self.workload(
            task=states.DEPLOYING, age=0,
            deployment_status=states.DEPLOYING)
        self.assertEqual('old-vm', self.replace().compute_id)
        self.assertEqual([], self.order)
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
            self.ctx, deployment.id).status)

    def test_paused_enqueue_fences_failover_even_after_expiry(self):
        self._paused_deployment_writer('enqueue')

    def test_paused_cancel_fences_failover_even_after_expiry(self):
        self._paused_deployment_writer('cancel')

    def test_paused_rollback_fences_failover_even_after_expiry(self):
        self._paused_deployment_writer('rollback')

    def test_paused_settings_sync_fences_failover(self):
        self._paused_deployment_writer('settings')

    def test_paused_domain_publish_fences_failover(self):
        self._paused_deployment_writer('domain')

    def test_paused_backup_write_fences_failover(self):
        self._paused_deployment_writer('backup')

    def test_paused_storage_apply_fences_failover(self):
        self._paused_deployment_writer('mounts')

    def test_paused_restore_write_fences_failover(self):
        self._paused_deployment_writer('restore')

    def _paused_workload(self, operation):
        backup = job = None
        self.stale()
        service, deployment = self.workload(
            task=states.DEPLOYING, age=0,
            deployment_status=states.DEPLOYING)
        deployment.started_at = timeutils.utcnow().replace(microsecond=0)
        deployment.fault = deployments.ENQUEUE
        if operation == 'cancel':
            deployment.status = states.CANCELLED
            deployment.engine_ref = 'deployment-ref'
        elif operation == 'rollback':
            target = objects.Deployment(
                context=self.ctx, service_id=service.id,
                server_id=self.server.id, status=states.ACTIVE,
                trigger='api', source_ref='stable', spec={})
            target.create()
            with api.writer.using(self.ctx) as session:
                session.query(deployment.repository.model).filter_by(
                    id=deployment.id).update({
                        'rollback_of': target.id, 'number': target.number + 1})
            deployment = objects.Deployment.get_by_id(self.ctx, deployment.id)
            deployment.started_at = timeutils.utcnow().replace(microsecond=0)
            deployment.fault = deployments.ENQUEUE
        deployment.save()
        if operation in ('settings', 'domain', 'mounts', 'backup', 'restore'):
            self.stack.status = states.ACTIVE
            self.stack.save()
            service = objects.Service.get_by_id(self.ctx, service.id)
            service.release_task(states.DEPLOYING)
            service.status = states.ACTIVE
            group = objects.Appgroup(
                context=self.ctx, stack_id=self.stack.id, name='apps',
                status=states.ACTIVE)
            group.create()
            environment = objects.Environment(
                context=self.ctx, appgroup_id=group.id, name='apps',
                status=states.ACTIVE)
            environment.create()
            with api.writer.using(self.ctx) as session:
                session.query(service.repository.model).filter_by(
                    id=service.id).update({'environment_id': environment.id,
                                           'status': states.ACTIVE})
            service = objects.Service.get_by_id(self.ctx, service.id)
            deployment.status = states.FAILED
            deployment.save()
        if operation in ('backup', 'restore'):
            self.patch(backups.common.notifications, 'emit')
            service.acquire_task(states.RECONCILING)
            backup = objects.Backup(
                context=self.ctx, service_id=service.id, kind='manual',
                status=states.BUILDING, engine='postgresql', container='db')
            backup.create()
            job = objects.BackupJob(
                context=self.ctx, backup_id=backup.id, service_id=service.id,
                operation='backup', status=states.BUILDING)
            job.create()
        return service, deployment, backup, job

    def _paused_deployment_writer(self, operation):
        service, deployment, backup, job = self._paused_workload(operation)
        # Independent connections and real SQLite write locks, rather than
        # the shared in-memory connection used by the other unit tests.
        path = self.useFixture(fixtures.TempDir()).path + '/coast.sqlite'
        connection = self.db.engine.raw_connection()
        with closing(sqlite3.connect(path)) as target:
            connection.driver_connection.backup(target)
        connection.close()
        facade = enginefacade.transaction_context()
        facade.configure(connection='sqlite:///' + path, sqlite_fk=True)
        self.addCleanup(facade.writer.get_engine().dispose)
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(facade, name)))

        paused = threading.Event()
        resume = threading.Event()
        attempting = threading.Event()
        finishing = threading.Event()
        finish = threading.Event()
        admitted = threading.Event()
        recovery = deployments.Recovery(self.manager)

        def poll(ctx, deployment_id):
            self.assertRaises(db_exception.NoEngineContextEstablished,
                              getattr, ctx, 'session')

        if operation != 'cancel':
            self.patch(recovery, 'reconcile', side_effect=poll)

        def before_write(*args):
            paused.set()
            self.assertTrue(resume.wait(120))

        self.patch(recovery.owner, '_push_compose', side_effect=before_write)
        self.patch(recovery.owner, '_variables', return_value={})
        self.patch(recovery.owner, '_mounts', return_value=[])
        self.patch(recovery.owner, '_apply_file_mounts')

        def engine_write(*args):
            if operation == 'cancel':
                before_write()
            self.order.append(('engine-write', service.id))

        self.driver.set_env.side_effect = engine_write
        self.driver.cancel_deployment.side_effect = engine_write
        self.driver.rollback.return_value = driver_base.DriverResult(
            engine_ref='deployment-ref')
        self.driver.deploy_app.return_value = driver_base.DriverResult(
            engine_ref='deployment-ref')

        def deferred_write(*args, **kwargs):
            before_write()
            self.order.append(('engine-write', service.id))

        loop = self.manager.service_reconciler
        self.patch(self.manager, '_variables', return_value={})
        self.patch(self.manager, '_fqdns', return_value=[])
        self.patch(loop, 'capture_logs', return_value=(None, []))
        self.patch(loop, 'prepare_mounts', return_value=deferred_write)
        self.driver.compare_settings.return_value = {
            'env': False, 'domains': False}
        if operation == 'domain':
            self.patch(manager.managed_dns, 'ensure_default')
            self.patch(self.manager, '_computed_host',
                       return_value='app.example.com')
            self.patch(self.manager, '_engine_domains', return_value=(
                mock.Mock(), [], ['app.example.com']))
            self.patch(self.manager, '_domains_differ', return_value=True)
            self.driver.set_domains.side_effect = deferred_write
        if operation == 'mounts':
            self.patch(self.manager, '_mounts', return_value=[])
            self.driver.set_volume_mounts.side_effect = deferred_write
        if operation in ('backup', 'restore'):
            self.patch(backups.backup_storage, 'SwiftStorage')
            self.patch(backups, '_server', return_value=self.server)
            self.patch(backups, '_receipt', return_value=[])

            def agent_write(method, path, body=None):
                if path in ('/v1/backup_database', '/v1/restore_database'):
                    deferred_write()
                    return {'duration_seconds': 0, 'size_bytes': 1,
                            'sha256': 'a' * 64,
                            'engine_version': self.stack.engine_version}
                return self._agent(method, path, body)

            self.agent.request.side_effect = agent_write
        self._observe_paused_failover(
            service, job, attempting, admitted, finishing, finish)

        def enqueue():
            ctx = context.RequestContext(project_id=self.ctx.project_id)
            recovery.owner._pending = []
            recovery.owner._seen = set()
            self._run_paused_writer(
                ctx, operation, service, deployment, backup, job, recovery)

        def replace():
            ctx = context.RequestContext(project_id=self.ctx.project_id)
            current = objects.Server.get_by_id(ctx, self.server.id)
            self.manager.execute(ctx, 'failover_server', current,
                                 revision=rpcapi.revision(current))

        with futures.ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(enqueue)
            try:
                self.assertTrue(paused.wait(120))
                later = timeutils.utcnow() + datetime.timedelta(hours=2)
                with mock.patch.object(timeutils, 'utcnow',
                                       return_value=later):
                    replacement = pool.submit(replace)
                    self.assertTrue(attempting.wait(120))
                    self.assertFalse(admitted.is_set())
                    self.assertEqual([], self.order)
                    resume.set()
                    if operation in ('backup', 'restore'):
                        self.assertTrue(finishing.wait(120))
                        replacement.result(timeout=120)
                        self.assertFalse(admitted.is_set())
                        self.assertEqual(states.BUILDING,
                                         objects.BackupJob.get_by_id(
                                             self.ctx, job.id).status)
                        finish.set()
                        writer.result(timeout=120)
                        replacement = pool.submit(replace)
                    writer.result(timeout=120)
                    replacement.result(timeout=120)
            finally:
                resume.set()
                finish.set()
        self.assertTrue(admitted.is_set())
        self.assertEqual(('engine-write', service.id), self.order[0])
        self.assertEqual(states.ACTIVE, objects.Server.get_by_id(
            self.ctx, self.server.id).status)
        self.assertEqual(
            states.CANCELLED if operation == 'cancel' else states.FAILED,
            objects.Deployment.get_by_id(self.ctx, deployment.id).status)

    def _observe_paused_failover(self, service, job, attempting, admitted,
                                 finishing, finish):
        admit = controller_failover.ControllerFailover._admit

        def admitting(flow):
            # SQLite serializes writers at the project sentinel, before
            # the service query. Signal at admission entry on either backend.
            attempting.set()
            return admit(flow)

        self.patch(controller_failover.ControllerFailover, '_admit',
                   new=admitting)
        if job is not None:
            finish_job = backups.finish_job

            def completing(ctx, job, outcome):
                # The engine fence ends before the durable job completes.
                # Force admission into that gap: live jobs must still block.
                finishing.set()
                self.assertTrue(finish.wait(120))
                return finish_job(ctx, job, outcome)

            self.patch(backups, 'finish_job', new=completing)
        claim = controller_failover.ControllerFailover._claim

        def claimed(flow):
            self.assertEqual(('engine-write', service.id), self.order[0])
            if job is not None:
                self.assertEqual(states.ACTIVE, objects.BackupJob.get_by_id(
                    flow.ctx, job.id).status)
            admitted.set()
            return claim(flow)

        self.patch(controller_failover.ControllerFailover, '_claim',
                   new=claimed)

    def _run_paused_writer(self, ctx, operation, service, deployment,
                           backup, job, recovery):
        if operation == 'mounts':
            self.manager.apply_mounts(ctx, service.id)
        elif operation == 'settings':
            self.manager.service_reconciler.run(
                objects.Service.get_by_id(ctx, service.id))
        elif operation == 'domain':
            self.manager._refresh_one(ctx, service, False)
        elif operation in ('backup', 'restore'):
            current = objects.Service.get_by_id(ctx, service.id)
            saved = objects.Backup.get_by_id(ctx, backup.id)
            if operation == 'backup':
                policy = SimpleNamespace(key_ref='key-ref')
                backups.capture(
                    self.conf, ctx, current, policy, saved, job)
            else:
                saved.status = states.ACTIVE
                saved.segments = []
                saved.key_ref = 'key-ref'
                saved.save()
                backups.restore(self.conf, ctx, job, saved, current)
            backups.finish_job(ctx, job, states.ACTIVE)
        elif operation == 'cancel':
            recovery.reconcile(ctx, deployment.id)
        else:
            recovery.enqueue(ctx, deployment)

    def test_admission_locks_services_before_infrastructure(self):
        self.stale()
        self.workload()
        order = []
        original = controller_failover.lock_workloads
        stack_get = api.stacks._get

        def workloads(ctx, stack_id):
            order.append('services')
            return original(ctx, stack_id)

        def stack(ctx, identifier, *args, **kwargs):
            if kwargs.get('lock'):
                order.append('stack')
            return stack_get(ctx, identifier, *args, **kwargs)

        with mock.patch.object(controller_failover, 'lock_workloads',
                               side_effect=workloads), \
                mock.patch.object(api.stacks, '_get', side_effect=stack):
            self.replace()
        self.assertEqual('services', order[0])
        self.assertIn('stack', order)

    def test_enqueue_locks_only_service_and_deployment(self):
        _, deployment, _, _ = self._paused_workload('enqueue')
        recovery = deployments.Recovery(self.manager)
        self.patch(recovery, 'reconcile')
        self.patch(recovery.owner, '_push_compose')
        self.patch(recovery.owner, '_variables', return_value={})
        self.patch(recovery.owner, '_mounts', return_value=[])
        self.patch(recovery.owner, '_apply_file_mounts')
        self.driver.deploy_app.return_value = driver_base.DriverResult(
            engine_ref='deployment-ref')
        tables = []
        original = api._lock_query

        def lock(session, query, model):
            tables.append(model.__tablename__)
            return original(session, query, model)

        with mock.patch.object(api, '_lock_project') as project, \
                mock.patch.object(api, '_lock_query', side_effect=lock):
            recovery.enqueue(self.ctx, deployment)
        project.assert_not_called()
        self.assertEqual('services', tables[0])
        self.assertEqual({'services', 'deployments'}, set(tables))
        self.driver.deploy_app.assert_called_once()

    def test_delayed_cancel_aborts_after_failover_reclaims_claim(self):
        self.stale()
        service, deployment = self.workload(
            task=states.DEPLOYING, deployment_status=states.DEPLOYING)
        deployment.status = states.CANCELLED
        deployment.started_at = (timeutils.utcnow() -
                                 datetime.timedelta(hours=2))
        deployment.engine_ref = 'deployment-ref'
        deployment.save()
        self.replace()
        recovery = deployments.Recovery(self.manager)
        self.assertFalse(recovery.cancel_engine(
            self.ctx, deployment, self.driver, mock.Mock()))
        self.driver.cancel_deployment.assert_not_called()

    def test_delayed_settings_write_aborts_after_failover(self):
        self.stale()
        service, _ = self.workload(task=states.RECONCILING)
        service = objects.Service.get_by_id(self.ctx, service.id)
        self.replace()
        with self.assertRaises(exception.Conflict):
            with self.manager._service_write(self.ctx, service):
                self.fail('A reclaimed writer must never reach the engine')

    def test_recovered_backup_job_cannot_use_a_new_job_claim(self):
        service, _ = self.workload(task=states.RECONCILING)
        backup = objects.Backup(
            context=self.ctx, service_id=service.id, kind='manual',
            status=states.ACTIVE, engine='postgresql', container='db')
        backup.create()
        old = objects.BackupJob(
            context=self.ctx, backup_id=backup.id, service_id=service.id,
            operation='overwrite', status=states.ERROR)
        old.create()
        objects.BackupJob(
            context=self.ctx, backup_id=backup.id, service_id=service.id,
            operation='overwrite', status=states.BUILDING).create()
        with self.assertRaises(exception.Conflict):
            with backups.engine_write(self.ctx, service, old):
                self.fail('A retired job must never reach the agent')

    def test_refused_admission_warns_with_workload_reason(self):
        logs = self.refusal_logs()
        self.stale()
        self.workload(task=states.DEPLOYING, age=0)
        for _ in range(2):
            self.assertEqual('old-vm', self.replace().compute_id)
        self.assert_refused(logs, 'services_busy')
        self.assertEqual(1, logs.output.count('event=refused'))

    def test_fresh_task_protects_old_deployment(self):
        logs = self.refusal_logs()
        self.stale()
        service, deployment = self.workload(
            task=states.DEPLOYING, deployment_status=states.DEPLOYING)
        with api.writer.using(self.ctx) as session:
            session.query(service.repository.model).filter_by(
                id=service.id).update({'task_started_at': timeutils.utcnow()})
        self.assertEqual('old-vm', self.replace().compute_id)
        self.assertEqual([], self.order)
        self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
            self.ctx, deployment.id).status)
        self.assert_refused(logs, 'services_busy')

    def test_stale_app_read_failure_keeps_controller_healthy(self):
        self.stale()
        service, deployment = self.workload(
            task=states.DEPLOYING, deployment_status=states.DEPLOYING)
        self.driver.get_app_status.side_effect = exception.Conflict()
        self.assertEqual(states.ACTIVE, self.replace().status)
        self.assertEqual(states.FAILED, objects.Deployment.get_by_id(
            self.ctx, deployment.id).status)
        current = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertEqual(states.ACTIVE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).status)

    def test_fresh_task_without_deployment_blocks_controller(self):
        logs = self.refusal_logs()
        self.stale()
        service, _ = self.workload(task=states.RECONCILING, age=0)
        self.assertEqual('old-vm', self.replace().compute_id)
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.assert_refused(logs, 'services_busy')

    def test_recent_deployment_start_protects_old_request(self):
        logs = self.refusal_logs()
        self.stale()
        _, deployment = self.workload(
            deployment_status=states.DEPLOYING)
        deployment.started_at = timeutils.utcnow()
        deployment.save()
        self.assertEqual('old-vm', self.replace().compute_id)
        self.assertEqual([], self.order)
        self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
            self.ctx, deployment.id).status)
        self.assert_refused(logs, 'services_busy')

    def test_longer_operation_budget_protects_claim(self):
        self.config.config(start_timeout=7200, group='conductor')
        logs = self.refusal_logs()
        self.stale()
        service, _ = self.workload(task=states.RECONCILING, age=3600)
        self.assertEqual('old-vm', self.replace().compute_id)
        self.assertEqual([], self.order)
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.assert_refused(logs, 'services_busy')

    def test_live_workload_preserves_other_expired_claims(self):
        logs = self.refusal_logs()
        self.stale()
        abandoned, _ = self.workload(task=states.RECONCILING)
        abandoned.name = 'abandoned-app'
        abandoned.save()
        live, _ = self.workload(task=states.DEPLOYING, age=0)
        self.assertEqual('old-vm', self.replace().compute_id)
        self.assertEqual([], self.order)
        for service in (abandoned, live):
            self.assertEqual(service.task_state, objects.Service.get_by_id(
                self.ctx, service.id).task_state)
        self.assert_refused(logs, 'services_busy')

    def test_fresh_deployment_without_task_blocks_controller(self):
        logs = self.refusal_logs()
        self.stale()
        self.workload(age=0, deployment_status=states.PENDING)
        self.assertEqual('old-vm', self.replace().compute_id)
        self.assertEqual([], self.order)
        self.assert_refused(logs, 'services_busy')

    def test_expired_task_without_deployment_is_reclaimed(self):
        self.stale()
        service, _ = self.workload(task=states.RECONCILING)
        self.assertEqual('new-vm', self.replace().compute_id)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)

    def test_live_backup_is_not_reclaimed_with_expired_task(self):
        logs = self.refusal_logs()
        self.stale()
        service, _ = self.workload(task=states.RECONCILING)
        backup = objects.Backup(
            context=self.ctx, service_id=service.id, kind='manual',
            status=states.BUILDING, engine='postgresql', container='db')
        backup.create()
        objects.BackupJob(
            context=self.ctx, backup_id=backup.id, service_id=service.id,
            operation='backup', status=states.BUILDING).create()
        self.assertEqual('old-vm', self.replace().compute_id)
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.assert_refused(logs, 'services_busy')

    def test_upgrade_still_refuses_expired_workload(self):
        self.workload(task=states.DEPLOYING,
                      deployment_status=states.DEPLOYING)
        with api.writer.using(self.ctx):
            self.assertRaises(exception.Conflict, engine_upgrade.idle,
                              self.ctx, self.stack)

    def test_failed_controller_health_does_not_reconcile_apps(self):
        self.stale()
        service, deployment = self.workload(
            task=states.DEPLOYING, deployment_status=states.DEPLOYING)
        self.cloud.wait_server.side_effect = exception.Conflict()
        self.assertEqual(states.ERROR, self.replace().status)
        self.driver.get_app_status.assert_not_called()
        self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
            self.ctx, deployment.id).status)
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, service.id).status)

    def test_idle_error_app_observation_is_saved(self):
        self.stale()
        service, _ = self.workload()
        self.assertEqual('new-vm', self.replace().compute_id)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, service.id).status)

    def test_missing_image_stays_unhealthy_until_it_returns(self):
        logs = self.refusal_logs()
        self.stale()
        self.missing = True
        server = self.replace()
        self.assertEqual(states.UNHEALTHY, server.status)
        self.assertEqual(states.NONE, server.task_state)
        self.assertEqual([], self.order)
        self.assertEqual(1, len(self.image_events()))
        self.assertNotIn('old-image', self.image_events()[0].message)
        again = self.replace()
        self.assertEqual(states.UNHEALTHY, again.status)
        self.assertEqual(1, len(self.image_events()))
        self.assert_refused(logs, 'image_unavailable')
        self.missing = False
        rebuilt = self.replace()
        self.assertEqual(states.ACTIVE, rebuilt.status)
        self.assertEqual('new-vm', rebuilt.compute_id)
        self.assertEqual('4.4.3', objects.Stack.get_by_id(
            self.ctx, self.stack.id).engine_version)

    def test_version_mismatch_refuses_without_a_boot(self):
        self.stale()
        self.version = '4.4.4'
        server = self.replace()
        self.assertEqual(states.UNHEALTHY, server.status)
        self.assertEqual([], self.order)
        self.assertEqual(1, len(self.image_events()))

    def test_glance_outage_is_not_an_image_refusal(self):
        logs = self.refusal_logs()
        self.stale()
        self.glance_down = True
        self.assertRaises(RuntimeError, self.replace)
        self.assert_refused(logs, 'image_lookup_failed')
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.UNHEALTHY, server.status)
        self.assertEqual([], self.image_events())
        self.assertEqual([], self.order)

    def test_ensure_image_conflict_does_not_reveal_the_id(self):
        self.missing = True
        error = self.assertRaises(
            exception.Conflict, controller_failover.ensure_image,
            self.ctx, self.stack, self.server, self.conf)
        self.assertNotIn('old-image', str(error))
        self.assertEqual(states.ACTIVE, objects.Server.get_by_id(
            self.ctx, self.server.id).status)
        self.assertEqual(1, len(self.image_events()))

    def test_worker_status_mismatch_keeps_the_rebuilt_controller(self):
        self.stale()
        worker = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='worker',
            role='worker', kind='vm', status=states.UNHEALTHY,
            task_state=states.NONE, compute_id='worker-vm')
        worker.create()
        self.driver.get_server_status.side_effect = (
            lambda endpoint, stack, server: driver_base.ObservedStatus(
                states.ACTIVE))
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual('new-vm', server.compute_id)
        self.assertNotIn(('restore', 'old-vm'), self.order)
        self.driver.get_server_status.assert_not_called()
        self.assertEqual(states.DEGRADED, objects.Stack.get_by_id(
            self.ctx, self.stack.id).status)

    def test_failed_worker_does_not_block_controller_recovery(self):
        self.stale()
        objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='worker',
            role='worker', kind='vm', status=states.ERROR,
            task_state=states.NONE, compute_id='worker-vm').create()
        self.driver.get_server_status.side_effect = (
            driver_base.EngineUnreachable('worker unavailable'))
        self.admit_admin()
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual('new-vm', server.compute_id)
        self.assertEqual(states.NONE, server.task_state)
        self.assertNotIn(('restore', 'old-vm'), self.order)
        self.assertEqual(states.DEGRADED, objects.Stack.get_by_id(
            self.ctx, self.stack.id).status)

    def test_lost_replacement_keeps_the_old_vm_stopped(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.stale()
        self.cloud.wait_server.side_effect = exception.Conflict()
        self.cloud.replacement.return_value = None
        server = self.replace()
        self.assertEqual(states.ERROR, server.status)
        self.assertNotIn('restore', [name for name, *_rest in self.order])
        self.assertNotIn('delete', [name for name, *_rest in self.order])
        self.assertIn(('stop', 'old-vm'), self.order)
        self.assertIn('event=start', logs.output)
        self.assertIn('event=end', logs.output)
        self.assertIn('outcome=failure', logs.output)

    def test_missing_appgroup_container_redeploys_once(self):
        self.stale()
        source = objects.Source(context=self.ctx, type='image',
                                image='example.com/app:stable')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='service', source_id=source.id,
            ports='8080', replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE, engine_ref='app-ref')
        service.create()
        target = objects.Deployment(
            context=self.ctx, service_id=service.id, server_id=self.server.id,
            status=states.ACTIVE, trigger='api', source_ref='stable',
            spec={'source': {'type': 'image'}, 'env': {}})
        target.create()
        service.current_deployment_id = target.id
        service.save()
        self.driver.get_app_status.side_effect = [
            driver_base.ObservedStatus(states.STOPPED),
            driver_base.ObservedStatus(states.ACTIVE)]
        self.driver.deploy_app.return_value = driver_base.DriverResult(
            'deploy')
        self.driver.get_deployment_status.return_value = (
            driver_base.ObservedStatus(states.ACTIVE))
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual(1, self.driver.deploy_app.call_count)
        current = objects.Service.get_by_id(self.ctx, service.id)
        self.assertNotEqual(target.id, current.current_deployment_id)
        self.assertEqual(states.SUPERSEDED, objects.Deployment.get_by_id(
            self.ctx, target.id).status)

    def test_missing_template_does_not_undo_controller_recovery(self):
        self.stale()
        source = objects.Source(context=self.ctx, type='image')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='service', source_id=source.id,
            template_id='postgresql', ports='8080', replicas=1,
            auto_deploy=False, status=states.ACTIVE, task_state=states.NONE,
            engine_ref='app-ref')
        service.create()
        self.driver.get_app_status.return_value = driver_base.ObservedStatus(
            states.STOPPED)
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.driver.deploy_app.assert_not_called()
        self.assertNotIn(('restore', 'old-vm'), self.order)

    def test_unreachable_workload_does_not_undo_controller_recovery(self):
        self.stale()
        source = objects.Source(context=self.ctx, type='image')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='service', source_id=source.id,
            ports='8080', replicas=1, auto_deploy=False,
            status=states.ERROR, task_state=states.NONE, engine_ref='app-ref')
        service.create()
        self.driver.get_app_status.side_effect = (
            driver_base.EngineUnreachable('tenant workload unavailable'))
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual('new-vm', server.compute_id)
        current = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertNotIn(('restore', 'old-vm'), self.order)

    def test_another_unhealthy_server_keeps_the_stack_degraded(self):
        self.stale()
        objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='worker',
            role='worker', kind='vm', status=states.UNHEALTHY,
            task_state=states.NONE).create()
        server = self.replace()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual(states.DEGRADED, stack.status)
        self.assertEqual(stale.HEALTH_FAULT, stack.fault)

    def test_unrelated_stack_fault_stays(self):
        self.stale()
        self.stack.fault = 'maintenance'
        self.stack.save()
        self.replace()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual(states.DEGRADED, stack.status)
        self.assertEqual('maintenance', stack.fault)

    def test_admin_failover_does_not_wait_for_grace(self):
        self.server.last_heartbeat_at = timeutils.utcnow()
        self.server.save()
        self.admit_admin()
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual('new-vm', server.compute_id)
        self.assertEqual([], self.image_events())

    def test_cap_includes_another_controller_but_not_self(self):
        logs = self.refusal_logs()
        self.stale()
        self.config.config(failover_max_concurrent=1, group='health_manager')
        other_ctx = context.RequestContext(project_id='other')
        other_stack = objects.Stack(
            context=other_ctx, name='other', plan='small', driver='noop',
            engine_version='noop', status=states.ACTIVE,
            task_state=states.NONE)
        other_stack.create()
        other = objects.Server(
            context=other_ctx, stack_id=other_stack.id, name='busy',
            role='control', kind='vm', status=states.FAILOVER,
            task_state=states.FAILING_OVER)
        other.create()
        server = self.replace()
        self.assertEqual(states.UNHEALTHY, server.status)
        self.assertEqual([], self.order)
        self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assert_refused(logs, 'global_capacity')
        other.release_task(states.FAILING_OVER)
        self.admit_admin()
        rebuilt = self.replace()
        self.assertEqual(states.ACTIVE, rebuilt.status)
        self.assertIn('reason=global_capacity', logs.output)

    def test_busy_stack_service_and_peer_skip_before_boot(self):
        logs = self.refusal_logs()
        self.stale()
        self.stack.acquire_task(states.UPGRADING)
        self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assertEqual([], self.order)
        self.stack.release_task(states.UPGRADING)
        source = objects.Source(context=self.ctx, type='image')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='service', source_id=source.id,
            ports='8080', replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE)
        service.create()
        service.acquire_task(states.DEPLOYING)
        self.assertEqual(states.UNHEALTHY, self.replace().status)
        service.release_task(states.DEPLOYING)
        peer = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='worker',
            role='worker', kind='vm', status=states.ACTIVE,
            task_state=states.NONE)
        peer.create()
        peer.acquire_task(states.RECONCILING)
        self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assertEqual([], self.order)
        peer.release_task(states.RECONCILING)
        self.admit_admin()
        self.stack.acquire_task(states.UPGRADING)
        released = self.replace()
        self.assertEqual(states.ACTIVE, released.status)
        self.assertEqual(states.NONE, released.task_state)
        self.assertEqual([], self.order)
        refused = [event for event in objects.EventList.get_all(
            self.ctx, filters={'resource_id': self.server.id,
                               'action': 'failover'})
                   if event.message == controller_failover.REFUSED]
        self.assertEqual(1, len(refused))
        self.assert_refused(logs, 'stack_busy')
        self.assert_refused(logs, 'services_busy')

    def test_disabled_driver_and_wrong_role_warn(self):
        self.stale()
        logs = self.refusal_logs()
        flow = controller_failover.ControllerFailover(self.manager)
        self.stack.driver = 'noop'
        self.stack.save()
        for _ in range(2):
            flow.execute(self.ctx, self.server)
        self.assert_refused(logs, 'driver_disabled')
        self.server.role = 'build'
        for _ in range(2):
            flow.execute(self.ctx, self.server)
        self.assert_refused(logs, 'unsupported_resource')

    def test_claim_conflict_warns_after_rollback_and_releases_admin(self):
        logs = self.refusal_logs()
        self.stale()
        with mock.patch.object(controller_failover.ControllerFailover,
                               '_claim', side_effect=exception.Conflict()):
            for _ in range(2):
                self.assertEqual(states.UNHEALTHY, self.replace().status)
            self.admit_admin()
            server = self.replace()
            self.assertEqual(states.ACTIVE, server.status)
            self.assertEqual(states.NONE, server.task_state)
            self.replace()
        self.assert_refused(logs, 'claim_conflict')
        self.assertEqual([], self.order)

    def test_not_due_and_missing_compute_warn_in_real_admission(self):
        logs = self.refusal_logs()
        self.replace()
        self.assert_refused(logs, 'not_due')
        self.stale()
        self.server.compute_id = None
        self.server.save()
        self.replace()
        self.replace()
        self.assert_refused(logs, 'server_unavailable')
        self.assertEqual([], self.order)

    def test_duplicate_receipt_does_not_restore_or_boot(self):
        logs = self.refusal_logs()
        self.stale()
        current = objects.Server.get_by_id(self.ctx, self.server.id)
        receipt = rpcapi.receipt_id(
            'failover_server', current, rpcapi.revision(current))
        objects.Event(
            context=self.ctx, id=receipt, stack_id=self.stack.id,
            resource_type='server', resource_id=self.server.id,
            action='failover', outcome='start').create()
        server = self.replace()
        self.assertEqual(states.UNHEALTHY, server.status)
        self.assertEqual('old-vm', server.compute_id)
        self.assertEqual([], self.order)
        self.assert_refused(logs, 'stale_request')

    def test_restart_fails_a_controller_attempt_and_keeps_a_stray_task(self):
        self.stack.acquire_task(states.FAILING_OVER)
        self.stack.conductor_host = self.conf.host
        self.stack.save()
        self.server.acquire_task(states.FAILING_OVER)
        self.server.status = states.FAILOVER
        self.server.save()
        self.manager.recover_stacks()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, server.status)
        self.assertEqual(states.NONE, server.task_state)
        self.assertEqual(server_failover.CONTROLLER_FAULT, server.fault)
        self.assertEqual([], self.order)
        # One stack has one control server. The stray task is on another.
        other = objects.Stack(
            context=self.ctx, name='other', driver='coolify_v4',
            plan='small', engine_version='4.4.3', status=states.ACTIVE,
            task_state=states.NONE, heartbeat_key_ref='heartbeat',
            engine_token_ref='token')
        other.create()
        stray = objects.Server(
            context=self.ctx, stack_id=other.id, name='other',
            role='control', kind='vm', status=states.ACTIVE,
            task_state=states.NONE, compute_id='kept')
        stray.create()
        other.acquire_task(states.FAILING_OVER)
        other.conductor_host = self.conf.host
        other.save()
        stray.acquire_task(states.FAILING_OVER)
        self.manager.recover_stacks()
        kept = objects.Server.get_by_id(self.ctx, stray.id)
        self.assertEqual(states.ACTIVE, kept.status)
        self.assertEqual(states.FAILING_OVER, kept.task_state)
        self.assertEqual('kept', kept.compute_id)

    def test_baremetal_admin_is_error_without_a_rebuild(self):
        logs = self.refusal_logs()
        self.server.kind = 'baremetal'
        self.server.save()
        self.admit_admin()
        server = self.replace()
        self.assertEqual(states.ERROR, server.status)
        self.assertEqual(states.NONE, server.task_state)
        self.assertEqual(failover.BAREMETAL_FAULT, server.fault)
        self.assert_refused(logs, 'bare_metal')
        self.assertEqual([], self.order)
        self.driver.register_server.assert_not_called()

    def test_post_claim_image_loss_returns_active(self):
        logs = self.refusal_logs()
        self.stale()
        original = self._image
        calls = {'count': 0}

        def vanish(role, selection):
            calls['count'] += 1
            if calls['count'] > 1:
                raise ValueError('gone')
            return original(role, selection)

        self.patch(engine_upgrade.controller.ControllerCloud, 'select_image',
                   side_effect=vanish)
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual(states.NONE, server.task_state)
        self.assertIsNone(server.fault)
        self.assertEqual('old-vm', server.compute_id)
        self.assertEqual([], self.order)
        self.assertEqual(1, len(self.image_events()))
        self.assert_refused(logs, 'image_unavailable')
