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

"""Real stack teardown with fake cloud clients."""

import datetime
import json
from types import SimpleNamespace
from unittest import mock
import uuid

import fixtures
from openstack import exceptions as sdk_exceptions

from coast.common import context
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import controller
from coast.conductor import exports
from coast.conductor import manager
from coast.conductor import stack_teardown
from coast.conductor import volume_delete
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast.drivers import base as driver_base
from coast import objects
from coast.tests import base


def _phases(order):
    phases = []
    for name in order:
        if not phases or phases[-1] != name:
            phases.append(name)
    return phases


class _Calls(object):
    def __init__(self, owner, name):
        self.owner = owner
        self.name = name

    def delete(self, *args, **kwargs):
        if self.name == 'secret':
            self.owner.secret_refs.append(args[-1])
        self.owner.mark(self.name)

    def delete_app(self, *_args, **_kwargs):
        self.owner.mark('coolify')

    def delete_environment(self, *_args, **_kwargs):
        self.owner.mark('coolify')

    def delete_project(self, *_args, **_kwargs):
        self.owner.mark('coolify')

    def unregister_server(self, *_args, **_kwargs):
        self.owner.mark('coolify')

    def delete_stack_project(self, *_args, **_kwargs):
        self.owner.mark('coolify')

    def delete_ports(self, _server):
        self.owner.mark('ports')

    def delete_server(self, _server):
        self.owner.mark('server')

    def find_volume(self, _volume):
        self.owner.mark('volume')
        return None

    def get_volume(self, ident):
        self.owner.mark('volume')
        if self.owner.gone or self.owner.missing:
            raise sdk_exceptions.ResourceNotFound('missing')
        return SimpleNamespace(
            id=ident, status='available', attachments=[],
            metadata={'coast': 'true', 'project_id': 'project',
                      'stack_id': self.owner.stack_id})

    def snapshots(self, **kwargs):
        self.owner.mark('volume')
        volume_id = kwargs.get('volume_id')
        return [snap for snap in self.owner.snapshots.values()
                if volume_id is None or snap.volume_id == volume_id]

    def create_snapshot(self, **kwargs):
        self.owner.mark('snapshot')
        if kwargs.get('force') is not False:
            raise RuntimeError('snapshot force must be false')
        if self.owner.fail_snapshot:
            raise RuntimeError('snapshot failed')
        self.owner.snapshot_calls += 1
        ident = 'snap-%s' % self.owner.snapshot_calls
        snap = SimpleNamespace(
            id=ident, status='creating', volume_id=kwargs.get('volume_id'),
            name=kwargs.get('name'))
        self.owner.snapshots[ident] = snap
        return snap

    def get_snapshot(self, ident):
        self.owner.mark('snapshot')
        snap = self.owner.snapshots.get(ident)
        if snap is None:
            raise sdk_exceptions.ResourceNotFound('missing')
        return snap

    def wait_for_status(self, snap, **_kwargs):
        self.owner.mark('snapshot')
        if self.owner.fail_snapshot_wait:
            raise RuntimeError('snapshot wait failed')
        snap.status = 'available'
        return snap

    def volumes(self, **_kwargs):
        self.owner.mark('volume')
        return []

    def delete_volume(self, _ident, **kwargs):
        self.owner.mark('volume')
        if (kwargs.get('force') or kwargs.get('cascade') or
                kwargs.get('ignore_missing')):
            raise RuntimeError('volume delete must not force or cascade')
        self.owner.gone = True

    def wait_for_delete(self, *_args, **_kwargs):
        self.owner.mark('volume')


class FakeClients(object):
    def __init__(self, ctx, stack_id):
        self.ctx = ctx
        self.stack_id = stack_id
        self.order = []
        self.secret_refs = []
        self.missing = False
        self.fail_network = False
        self.gone = False
        self.options = {}
        self.snapshots = {}
        self.snapshot_calls = 0
        self.fail_snapshot = False
        self.fail_snapshot_wait = False
        self.notifier = mock.Mock()
        self.endpoint = object()
        self.driver = _Calls(self, 'coolify')
        self.dns = _Calls(self, 'dns')
        self.floating = _Calls(self, 'floating')
        self.cloud = _Calls(self, 'cloud')
        self.block = _Calls(self, 'volume')
        self.network = _Calls(self, 'network')
        self.secrets = _Calls(self, 'secret')

    def mark(self, name):
        self.order.append(name)
        try:
            self.ctx.session
        except Exception:
            pass
        else:
            raise RuntimeError('Cloud call ran inside a DB transaction')
        if self.missing:
            raise sdk_exceptions.ResourceNotFound('missing')
        if name == 'network' and self.fail_network:
            raise RuntimeError('boom')


class StackTeardownTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        self.useFixture(fixtures.MonkeyPatch(
            'oslo_config.cfg.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.config.config(
            environments=10, servers=10, apps=10, domains=10,
            volume_gb=1000, group='quotas')
        self.config.config(
            volume_delete_poll_interval=1, volume_delete_timeout=1,
            group='controller_worker')
        self.ctx = context.RequestContext(project_id='project')
        self.owner = manager.Manager(self.conf, notifier=mock.Mock())
        self.addCleanup(self.owner.close)
        self.env = objects.Stack(
            context=self.ctx, name='existing', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        self.env.create()
        self.server = objects.Server(
            context=self.ctx, name='worker', stack_id=self.env.id,
            kind='vm', role='worker', status=states.ACTIVE,
            task_state=states.NONE)
        self.server.create()
        self.volume = objects.Volume(
            context=self.ctx, stack_id=self.env.id, server_id=self.server.id,
            name='service-data', size_gb=10, mount_path='/data',
            purpose='data', status=states.AVAILABLE,
            cinder_volume_id='cinder-1')
        self.volume.create()
        self.source = objects.Source(
            context=self.ctx, type='image', image='app:1',
            credentials_ref='ref-source')
        self.source.create()
        self.application = objects.Appgroup(
            context=self.ctx, stack_id=self.env.id, name='shop',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='project-1')
        self.application.create()
        self.app_env = objects.Environment(
            context=self.ctx, appgroup_id=self.application.id,
            name='production', status=states.ACTIVE,
            task_state=states.NONE, engine_ref='env-1')
        self.app_env.create()
        self.service = objects.Service(
            context=self.ctx, stack_id=self.env.id,
            environment_id=self.app_env.id, name='web',
            source_id=self.source.id, server_id=self.server.id,
            ports='80', replicas=1, auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='app-1')
        self.service.create()
        self.secret = objects.Secret(
            context=self.ctx, service_id=self.service.id, key='DB',
            is_secret=True, value_ref='ref-secret', build_time=False,
            version=1)
        self.secret.create()
        self.domain = objects.Domain(
            context=self.ctx, service_id=self.service.id,
            fqdn='app.example.com', verification_token='token',
            tls='off', auto_dns=False, dns_mode='manual',
            status=states.ACTIVE)
        self.domain.create()
        objects.Deployment(
            context=self.ctx, service_id=self.service.id, spec={},
            trigger='manual', status=states.ACTIVE).create()
        objects.AppVolumeMount(
            context=self.ctx, service_id=self.service.id,
            volume_id=self.volume.id, sub_path='',
            container_path='/data', read_only=False).create()
        self.server.engine_ref = 'server-1'
        self.server.save()
        self.env.driver = 'coolify_v4'
        self.env.engine_ref = 'stack-project'
        self.env.engine_token_ref = 'ref-token'
        self.env.heartbeat_key_ref = 'ref-heart'
        self.env.engine_token_previous_ref = 'ref-backup'
        self.env.ssh_key_ref = 'ref-ssh'
        self.env.status = states.DELETING
        self.env.save()
        self.env.acquire_task(states.DELETING)
        with api.writer.using(self.ctx):
            held = dict(environments=1, servers=1, volume_gb=1, apps=1,
                        domains=1)
            quota.reserve(self.ctx, **held)
            quota.rollback(self.ctx, held)
        # Legacy cascading cleanup remains covered below at the step level.
        # New requests are checked separately before any step can run.
        self.empty_guard = mock.patch.object(
            stack_teardown.stack_delete, 'require_empty')
        self.empty_guard.start()
        self.addCleanup(self.empty_guard.stop)
        self.clients = FakeClients(self.ctx, self.env.id)
        self.clients.manager = self.owner
        patcher = mock.patch.object(
            stack_teardown, 'Clients', return_value=self.clients)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_conductor_refuses_children_before_cloud_calls(self):
        self.empty_guard.stop()
        self.delete()
        row = self.stack()
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual(states.NONE, row.task_state)
        self.assertIn('shop', row.fault)
        self.assertEqual([], self.clients.order)

    def test_servers_are_gone_before_deleting_attached_ports(self):
        attached = {self.server.id}

        def delete_server(server):
            attached.discard(server.id)

        def delete_ports(server):
            if server.id in attached:
                raise sdk_exceptions.ConflictException('Port is in use')

        with mock.patch.object(self.clients.cloud, 'delete_server',
                               side_effect=delete_server):
            with mock.patch.object(self.clients.cloud, 'delete_ports',
                                   side_effect=delete_ports):
                self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertFalse(attached)

    def test_retry_skips_already_deleted_server_and_ports(self):
        gone = set()

        def remove(kind, server):
            key = (kind, server.id)
            if key in gone:
                raise sdk_exceptions.ResourceNotFound('already removed')
            gone.add(key)

        with mock.patch.object(self.clients.cloud, 'delete_server',
                               side_effect=lambda row: remove('server', row)):
            with mock.patch.object(
                    self.clients.cloud, 'delete_ports',
                    side_effect=lambda row: remove('ports', row)):
                self.clients.fail_network = True
                self.delete()
                self.assertEqual(states.ERROR, self.stack().status)
                self.clients.fail_network = False
                self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertIn(('server', self.server.id), gone)
        self.assertIn(('ports', self.server.id), gone)

    def test_failure_masks_password_in_fault_event_and_traceback(self):
        with mock.patch.object(stack_teardown, '_floating', side_effect=(
                ValueError('cleanup failed password=private-value'))):
            with mock.patch.object(stack_teardown.LOG, 'error') as log:
                self.delete()
        self.assertNotIn('private-value', self.stack().fault)
        self.assertIn('cleanup failed', self.stack().fault)
        args = log.call_args
        self.assertNotIn('private-value', str(args.args))
        self.assertNotIn('private-value', str(args.kwargs['exc_info'][1]))
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.env.id, 'action': 'delete',
            'outcome': 'failure'})
        self.assertNotIn('private-value', events[0].message)

    def test_empty_exception_still_records_failure_type(self):
        with mock.patch.object(stack_teardown, '_floating',
                               side_effect=ValueError()):
            self.delete()
        self.assertEqual('ValueError', self.stack().fault)
        self.assertEqual(states.NONE, self.stack().task_state)

    def test_stack_tag_only_guest_blocks_teardown(self):
        self.server.hostname = None
        self.server.compute_id = 'unowned-nova-id'
        self.server.save()
        cloud = controller.ControllerCloud(self.conf, self.env)
        nova = mock.Mock()
        nova.servers.return_value = []
        guest = SimpleNamespace(id=self.server.compute_id,
                                name='unowned-worker', tags=list(cloud.tags))
        nova.get_server.return_value = guest
        with mock.patch.object(cloud, 'proxy', return_value=nova):
            with mock.patch.object(self.clients.cloud, 'delete_server',
                                   side_effect=cloud.delete_server):
                self.delete()
        row = self.stack()
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual('Guest ownership differs', row.fault)
        nova.get_server.assert_called_once_with('unowned-nova-id')
        nova.delete_server.assert_not_called()
        nova.wait_for_delete.assert_not_called()

    def test_value_error_records_message_and_traceback(self):
        with mock.patch.object(stack_teardown, '_floating',
                               side_effect=ValueError('broken allocation')):
            with mock.patch.object(stack_teardown.LOG, 'error') as log:
                self.delete()
        row = self.stack()
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual(states.NONE, row.task_state)
        self.assertEqual('broken allocation', row.fault)
        info = log.call_args.kwargs['exc_info']
        self.assertEqual('broken allocation', str(info[1]))
        self.assertIsNotNone(info[2])

    def test_delete_removes_all_ingress_rules_for_only_this_stack(self):
        other = objects.Stack(
            context=self.ctx, name='other', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        other.create()
        for stack_id in (self.env.id, other.id):
            for port in (8080, 8081, 8082):
                api.ingress_rules.create(self.ctx, dict(
                    stack_id=stack_id, protocol='tcp', source='0.0.0.0/0',
                    port_range_min=port, port_range_max=port))
        self.config.config(group='api', max_limit=1)
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertEqual([], api.ingress_rules.list_for_stack(
            self.ctx, self.env.id))
        self.assertEqual(3, len(api.ingress_rules.list_for_stack(
            self.ctx, other.id)))
        # Retrying child cleanup after soft deletion is harmless.
        stack_teardown._drop_children(self.ctx, self.stack())
        self.assertEqual(3, len(api.ingress_rules.list_for_stack(
            self.ctx, other.id)))

    def delete(self):
        self.owner.execute(
            self.ctx, 'delete_stack', self.env, options={})

    def stack(self):
        return objects.Stack.get_by_id(
            self.ctx, self.env.id, show_deleted=True)

    def test_recovery_cleanup_checks_ownership_and_retries(self):
        metadata = {'coast': 'true', 'project_id': self.env.project_id,
                    'stack_id': self.env.id}
        owned = SimpleNamespace(id='recovery', metadata=metadata,
                                attachments=[])
        foreign = SimpleNamespace(id='foreign', metadata=dict(
            metadata, project_id='another-project'), attachments=[])
        snap = SimpleNamespace(id='rollback-point', metadata=metadata)
        block = mock.Mock()
        block.volumes.return_value = [owned, foreign]
        block.snapshots.side_effect = lambda **kw: (
            [] if kw.get('volume_id') else [snap, foreign])
        block.get_snapshot.return_value = snap
        block.get_volume.return_value = owned
        clients = SimpleNamespace(block=block)
        block.delete_snapshot.side_effect = RuntimeError('busy')
        self.assertRaises(RuntimeError, stack_teardown._upgrade_storage,
                          self.ctx, self.env, clients)
        block.delete_volume.assert_not_called()
        block.delete_snapshot.side_effect = None
        stack_teardown._upgrade_storage(self.ctx, self.env, clients)
        block.delete_volume.assert_called_once_with(
            owned.id, ignore_missing=False, force=False, cascade=False)
        self.assertTrue(all(call.args[0] == snap.id for call in
                            block.delete_snapshot.call_args_list))

    def usage(self):
        with self.db.engine.connect() as connection:
            return connection.execute(
                models.QuotaUsage.__table__.select()).mappings().one()

    def test_pasted_content_and_historical_refs_are_deleted(self):
        self.service.dockerfile_ref = 'ref-dockerfile'
        self.service.compose_ref = 'ref-compose'
        self.service.save()
        objects.Deployment(
            context=self.ctx, service_id=self.service.id,
            spec={'dockerfile_ref': 'ref-old-dockerfile',
                  'compose_ref': 'ref-old-compose'},
            trigger='api', status=states.ACTIVE).create()
        self.delete()
        for reference in ('ref-dockerfile', 'ref-compose',
                          'ref-old-dockerfile', 'ref-old-compose'):
            self.assertIn(reference, self.clients.secret_refs)

    def test_happy_path_deletes_in_order_and_releases_quota(self):
        self.delete()
        row = self.stack()
        self.assertEqual(states.DELETED, row.status)
        self.assertIsNotNone(row.deleted_at)
        self.assertEqual(states.NONE, row.task_state)
        self.assertIsNone(row.fault)
        self.assertIsNone(row.engine_token_ref)
        self.assertIsNone(row.ssh_key_ref)
        timeline = objects.EventList.get_all(self.ctx, filters={
            'stack_id': row.id, 'resource_type': 'stack',
            'action': 'delete', 'outcome': 'success'})
        self.assertEqual(1, len(timeline))
        self.assertGreaterEqual(timeline[0].detail['servers'], 1)
        self.assertGreaterEqual(timeline[0].detail['volumes'], 1)

        self.assertRaises(
            exception.NotFound, objects.Volume.get_by_id, self.ctx,
            self.volume.id)
        server = objects.Server.get_by_id(
            self.ctx, self.server.id, show_deleted=True)
        self.assertEqual(states.DELETING, server.status)
        self.assertIsNotNone(server.deleted_at)
        used = self.usage()
        self.assertEqual(0, used['in_use_environments'])
        self.assertEqual(0, used['in_use_servers'])
        self.assertEqual(0, used['in_use_volume_gb'])
        self.assertEqual(0, used['in_use_apps'])
        self.assertEqual(0, used['in_use_domains'])
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        self.assertEqual(
            ['coolify', 'dns', 'floating', 'server', 'ports', 'volume',
             'network', 'secret'], _phases(self.clients.order))
        self.assertIn('ref-token', self.clients.secret_refs)
        self.assertIn('ref-secret', self.clients.secret_refs)
        self.assertIn('ref-source', self.clients.secret_refs)

    def test_delete_recounts_hard_deleted_volume_quota(self):
        for mount in objects.AppVolumeMountList.get_all(
                self.ctx, filters={'volume_id': self.volume.id}):
            mount.destroy()
        self.volume.destroy()
        extra = quota.reserve(self.ctx, volume_gb=102)
        quota.commit(self.ctx, extra)
        # 10 GiB was already in use, plus 102 GiB with no volume row.
        self.assertEqual(112, self.usage()['in_use_volume_gb'])
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertIsNone(self.stack().fault)
        used = self.usage()
        self.assertEqual(0, used['in_use_volume_gb'])
        self.assertEqual(0, used['in_use_servers'])
        self.assertEqual(0, used['in_use_environments'])
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)

    def test_delete_recounts_failed_volume_that_is_still_charged(self):
        objects.Event(
            context=self.ctx, stack_id=self.env.id, resource_type='volume',
            resource_id=self.volume.id, action='create', outcome='failure',
            message='Interrupted by conductor restart').create()
        self.assertEqual(10, self.usage()['in_use_volume_gb'])
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertEqual(0, self.usage()['in_use_servers'])
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])

    def test_missing_quota_released_receipt_prevents_completion(self):
        self.clients.fail_network = True
        self.delete()
        receipt = objects.EventList.get_all(self.ctx, filters={
            'stack_id': self.env.id,
            'action': volume_delete.RECEIPT_ACTION})[0]
        payload = json.loads(receipt.message)
        del payload['quota_released']
        receipt.message = json.dumps(payload)
        receipt.save()
        self.clients.fail_network = False
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertIsNone(self.stack().deleted_at)
        self.assertIsNotNone(self.stack().fault)
        self.assertEqual(receipt.message, objects.Event.get_by_id(
            self.ctx, receipt.id).message)
        timeline_id = str(uuid.uuid5(
            uuid.UUID(self.env.id), 'delete-timeline'))
        timeline = objects.Event.get_by_id(self.ctx, timeline_id)
        self.assertNotEqual('success', timeline.outcome)

    def _fail_stack_create(self):
        self.env.status = states.ERROR
        self.env.fault = 'Coast could not provision this environment.'
        self.env.save()
        objects.Event(
            context=self.ctx, stack_id=self.env.id,
            resource_type='environment', resource_id=self.env.id,
            action='create', outcome='failure',
            message='Coast could not provision this environment.').create()

    def _set_volume_counters(self, in_use, reserved):
        with api.writer.using(self.ctx):
            row = self.ctx.session.query(models.QuotaUsage).one()
            row.in_use_volume_gb = in_use
            row.reserved_volume_gb = reserved

    def _destroy_volume_row(self):
        for mount in objects.AppVolumeMountList.get_all(
                self.ctx, filters={'volume_id': self.volume.id}):
            mount.destroy()
        self.volume.destroy()

    def test_failed_create_removed_volumes_release_held_charge(self):
        # Health failure already removed the Cinder volume and the row.
        # A remaining reservation blocks the normal recount.
        self._fail_stack_create()
        self._destroy_volume_row()
        self._set_volume_counters(50, 1)
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertIsNone(self.stack().fault)
        used = self.usage()
        self.assertEqual(0, used['in_use_volume_gb'])
        self.assertEqual(1, used['reserved_volume_gb'])
        self.assertEqual(0, used['in_use_servers'])
        self.assertEqual(0, used['in_use_environments'])
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertEqual(1, self.usage()['reserved_volume_gb'])
        self.delete()
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertEqual(1, self.usage()['reserved_volume_gb'])

    def test_failed_create_removed_volumes_keep_other_live_charge(self):
        self._fail_stack_create()
        self._destroy_volume_row()
        other = objects.Stack(
            context=self.ctx, name='other', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        other.create()
        objects.Volume(
            context=self.ctx, stack_id=other.id, name='other-data',
            size_gb=12, mount_path='/data', purpose='data',
            status=states.AVAILABLE).create()
        self._set_volume_counters(62, 1)
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertIsNone(self.stack().fault)
        self.assertEqual(12, self.usage()['in_use_volume_gb'])
        self.assertEqual(1, self.usage()['reserved_volume_gb'])
        self.assertEqual(1, len(objects.VolumeList.get_all(self.ctx)))

    def test_failed_create_unmarked_missing_volume_releases_once(self):
        # The stack create failed. This volume has no failure event, and
        # Cinder already has no volume. Delete must release its size once.
        self._fail_stack_create()
        self.volume.cinder_volume_id = None
        self.volume.save()
        self._set_volume_counters(10, 1)
        self.delete()
        row = self.stack()
        self.assertEqual(states.DELETED, row.status, row.fault)
        self.assertRaises(
            exception.NotFound, objects.Volume.get_by_id, self.ctx,
            self.volume.id)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertEqual(1, self.usage()['reserved_volume_gb'])
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        self.delete()
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertEqual(1, self.usage()['reserved_volume_gb'])

    def test_missing_accounted_volume_still_retries_delete(self):
        self.volume.cinder_volume_id = None
        self.volume.save()
        before = self.usage()['in_use_volume_gb']
        self.delete()
        row = self.stack()
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual('Volume cleanup failed; retry delete', row.fault)
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.assertEqual(before, self.usage()['in_use_volume_gb'])

    def test_delete_with_current_deployment(self):
        deployment = objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': self.service.id})[0]
        self.service.current_deployment_id = deployment.id
        self.service.save()

        self.delete()

        self.assertEqual(states.DELETED, self.stack().status)
        service = objects.Service.get_by_id(
            self.ctx, self.service.id, show_deleted=True)
        self.assertIsNone(service.current_deployment_id)
        self.assertIsNotNone(service.deleted_at)
        deployment = objects.Deployment.get_by_id(
            self.ctx, deployment.id, show_deleted=True)
        self.assertIsNotNone(deployment.deleted_at)
        self.assertEqual(0, self.usage()['in_use_apps'])

    def test_missing_resources_are_already_gone(self):
        self.clients.missing = True
        self.delete()
        row = self.stack()
        self.assertEqual(states.DELETED, row.status)
        self.assertIsNone(row.fault)
        self.assertRaises(
            exception.NotFound, objects.Volume.get_by_id, self.ctx,
            self.volume.id)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertNotIn('secret', self.clients.order[:1])
        self.assertIn('secret', self.clients.order)

    def test_midway_failure_sets_error_and_stops(self):
        self.clients.fail_network = True
        self.delete()
        row = self.stack()
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual('boom', row.fault)
        self.assertEqual(states.NONE, row.task_state)
        self.assertNotIn('secret', self.clients.order)
        self.assertIn('network', self.clients.order)
        self.assertRaises(
            exception.NotFound, objects.Volume.get_by_id, self.ctx,
            self.volume.id)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertEqual(1, self.usage()['in_use_environments'])
        self.assertEqual('ref-token', row.engine_token_ref)

    def test_retry_from_error_completes_delete(self):
        self.clients.fail_network = True
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.clients.fail_network = False
        self.clients.order = []
        self.clients.secret_refs = []
        self.delete()
        row = self.stack()
        self.assertEqual(states.DELETED, row.status)
        self.assertIsNone(row.fault)
        self.assertIsNone(row.engine_token_ref)
        self.assertIn('secret', self.clients.order)
        used = self.usage()
        self.assertEqual(0, used['in_use_environments'])
        self.assertEqual(0, used['in_use_servers'])
        self.assertEqual(0, used['in_use_volume_gb'])

    def _retry_after_step(self, name):
        original = getattr(stack_teardown, name)

        def interrupt(*args):
            original(*args)
            raise RuntimeError('interrupted')

        with mock.patch.object(stack_teardown, name, interrupt):
            self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        if name != '_export_before_delete':
            self.clients.driver = mock.Mock()
            self.clients.driver.delete_app.side_effect = (
                driver_base.EngineUnreachable('offline'))
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertIn('ref-token', self.clients.secret_refs)

    def test_controller_gone_skips_engine_and_keeps_secret(self):
        self.server.role = 'control'
        self.server.compute_id = 'controller-id'
        self.server.save()
        nova = mock.Mock()
        nova.get_server.side_effect = sdk_exceptions.ResourceNotFound('gone')
        self.clients.cloud.proxy = mock.Mock(return_value=nova)
        self.clients.driver = mock.Mock()
        self.clients.driver.delete_app.side_effect = (
            driver_base.EngineUnreachable('offline'))
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertIn('ref-token', self.clients.secret_refs)

    def _engine_offline(self, method='delete_environment'):
        self.clients.driver = mock.Mock()
        getattr(self.clients.driver, method).side_effect = (
            driver_base.EngineUnreachable('sensitive exception detail'))

    def _skip_events(self):
        return objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.env.id, 'action': 'delete.engine.skip'})

    def test_existing_controller_unreachable_continues(self):
        self.server.role = 'control'
        self.server.compute_id = 'controller-id'
        self.server.save()
        self.clients.cloud.proxy = mock.Mock(return_value=mock.Mock())
        self._engine_offline()
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertIn('server', self.clients.order)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertEqual(0, self.usage()['in_use_environments'])
        self.assertEqual('stack-project', self.stack().engine_ref)
        events = self._skip_events()
        self.assertEqual(1, len(events))
        self.assertEqual('failure', events[0].outcome)
        self.assertEqual('Engine cleanup skipped: engine unreachable.',
                         events[0].message)
        self.assertFalse(objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.env.id, 'action': 'delete.engine',
            'outcome': 'success'}))

    def test_skip_retry_keeps_one_receipt_and_engine_references(self):
        self._engine_offline()
        self.clients.fail_network = True
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertEqual('stack-project', self.stack().engine_ref)
        self.assertEqual('ref-token', self.stack().engine_token_ref)
        self.assertEqual(1, self.usage()['in_use_environments'])
        self.delete()
        self.assertEqual(1, len(self._skip_events()))
        self.clients.fail_network = False
        self.clients.driver.delete_environment.side_effect = None
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertIsNone(self.stack().engine_ref)
        self.assertEqual(1, len(self._skip_events()))

    def test_other_engine_errors_still_block(self):
        for error in (driver_base.EngineAuthError,
                      driver_base.EngineConflict, driver_base.DriverError):
            with self.subTest(error=error):
                self.clients.driver = mock.Mock()
                self.clients.driver.delete_environment.side_effect = (
                    error('engine failure'))
                self.delete()
                self.assertEqual(states.ERROR, self.stack().status)
                self.assertNotIn('server', self.clients.order)
                self.assertEqual('ref-token', self.stack().engine_token_ref)
                self.assertFalse(self._skip_events())
                self.assertEqual(10, self.usage()['in_use_volume_gb'])

    def test_unreachable_cloud_call_is_not_skipped(self):
        self.clients.cloud.delete_server = mock.Mock(side_effect=(
            driver_base.EngineUnreachable('cloud failure')))
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertFalse(self._skip_events())
        self.assertEqual(10, self.usage()['in_use_volume_gb'])

    def test_skip_does_not_bypass_snapshot_failure(self):
        self._engine_offline()
        self.clients.options = {'snapshot_volumes': True}
        self.clients.fail_snapshot = True
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertEqual(1, len(self._skip_events()))
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.assertEqual(10, self.usage()['in_use_volume_gb'])

    def test_skip_does_not_bypass_retained_storage_dependency(self):
        self._engine_offline()
        self.clients.snapshots['retained'] = SimpleNamespace(
            id='retained', status='available', volume_id='cinder-1')
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertEqual(1, len(self._skip_events()))
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.assertEqual(10, self.usage()['in_use_volume_gb'])
        self.assertFalse(self.clients.gone)

    def test_unreachable_export_still_blocks_delete(self):
        self._arm_export(fail=driver_base.EngineUnreachable('offline'))
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertFalse(self._skip_events())
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.assertNotIn('coolify', self.clients.order)
        self.assertNotIn('server', self.clients.order)

    def test_skip_event_write_failure_blocks_cloud_teardown(self):
        self._engine_offline()
        create = objects.Event.create

        def fail_skip(event):
            if event.action == 'delete.engine.skip':
                raise RuntimeError('DB failure')
            return create(event)

        with mock.patch.object(objects.Event, 'create', fail_skip):
            self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertNotIn('server', self.clients.order)

    def test_snapshot_created_and_row_recorded(self):
        self.clients.options = {'snapshot_volumes': True}
        self.delete()
        row = self.stack()
        self.assertEqual(states.DELETED, row.status)
        self.assertIsNone(row.fault)
        recorded = list(objects.StackDeleteSnapshotList.get_all(self.ctx))
        self.assertEqual(1, len(recorded))
        self.assertEqual(self.volume.id, recorded[0].volume_id)
        self.assertEqual(self.env.id, recorded[0].stack_id)
        self.assertEqual('cinder-1', recorded[0].cinder_volume_id)
        self.assertEqual('snap-1', recorded[0].cinder_snapshot_id)
        self.assertEqual('available', recorded[0].status)
        self.assertEqual(1, self.clients.snapshot_calls)
        self.assertEqual('available', self.clients.snapshots['snap-1'].status)
        self.assertRaises(
            exception.NotFound, objects.Volume.get_by_id, self.ctx,
            self.volume.id)
        self.assertIn('snapshot', self.clients.order)
        self.assertLess(
            self.clients.order.index('snapshot'),
            self.clients.order.index('volume'))

    def test_snapshot_retry_does_not_create_a_duplicate(self):
        self.clients.options = {'snapshot_volumes': True}
        self.clients.fail_snapshot_wait = True
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertEqual('snapshot wait failed', self.stack().fault)
        recorded = list(objects.StackDeleteSnapshotList.get_all(self.ctx))
        self.assertEqual(1, len(recorded))
        self.assertEqual('creating', recorded[0].status)
        self.assertEqual('snap-1', recorded[0].cinder_snapshot_id)
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.clients.fail_snapshot_wait = False
        self.clients.order = []
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertEqual(1, self.clients.snapshot_calls)
        recorded = list(objects.StackDeleteSnapshotList.get_all(self.ctx))
        self.assertEqual(1, len(recorded))
        self.assertEqual('available', recorded[0].status)
        self.assertEqual('snap-1', recorded[0].cinder_snapshot_id)
        self.assertEqual(['snap-1'], list(self.clients.snapshots))

    def test_retention_starts_at_completion_and_survives_retry(self):
        self.clients.options = {
            'snapshot_volumes': True, 'snapshot_retention_days': 7}
        self.clients.fail_network = True
        self.delete()
        row = objects.StackDeleteSnapshotList.get_all(self.ctx)[0]
        self.assertEqual(7, row.retention_days)
        self.assertIsNone(row.expires_at)
        self.clients.options['snapshot_retention_days'] = 1
        self.clients.fail_network = False
        before = datetime.datetime.now(datetime.timezone.utc)
        self.delete()
        row = objects.StackDeleteSnapshot.get_by_id(self.ctx, row.id)
        self.assertEqual(7, row.retention_days)
        self.assertGreaterEqual(row.expires_at,
                                before + datetime.timedelta(days=7))
        expiry = row.expires_at
        self.delete()
        self.assertEqual(expiry, objects.StackDeleteSnapshot.get_by_id(
            self.ctx, row.id).expires_at)

    def test_null_retention_receipt_prevents_completion(self):
        self.clients.options = {'snapshot_volumes': True}
        self.clients.fail_network = True
        self.delete()
        row = objects.StackDeleteSnapshotList.get_all(self.ctx)[0]
        with api.writer.using(self.ctx) as session:
            session.query(models.StackDeleteSnapshot).filter_by(
                id=row.id).update({'retention_days': None})
        self.clients.fail_network = False
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertIsNone(self.stack().deleted_at)
        self.assertIsNotNone(self.stack().fault)
        receipt = objects.StackDeleteSnapshot.get_by_id(self.ctx, row.id)
        self.assertIsNone(receipt.retention_days)
        self.assertIsNone(receipt.expires_at)
        timeline_id = str(uuid.uuid5(
            uuid.UUID(self.env.id), 'delete-timeline'))
        timeline = objects.Event.get_by_id(self.ctx, timeline_id)
        self.assertNotEqual('success', timeline.outcome)

    def test_completion_failure_rolls_back_expiry(self):
        self.clients.options = {'snapshot_volumes': True}
        with mock.patch.object(stack_teardown, '_drop_children',
                               side_effect=RuntimeError('failed')):
            self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertIsNone(objects.StackDeleteSnapshotList.get_all(
            self.ctx)[0].expires_at)

    def _expired_snapshot(self):
        self.clients.options = {'snapshot_volumes': True}
        self.delete()
        row = objects.StackDeleteSnapshotList.get_all(self.ctx)[0]
        row.expires_at = datetime.datetime.now(
            datetime.timezone.utc) - datetime.timedelta(days=1)
        with api.writer.using(self.ctx) as session:
            session.query(models.StackDeleteSnapshot).filter_by(
                id=row.id).update({'expires_at': row.expires_at.replace(
                    tzinfo=None)})
            session.query(models.Stack).filter_by(id=self.env.id).update(
                {'driver': 'coolify'})
        return row

    def _housekeep(self, block):
        with mock.patch.object(stack_teardown.controller, 'ControllerCloud',
                               return_value=SimpleNamespace(
                                   proxy=lambda service: block)):
            stack_teardown.housekeep(self.owner)

    def test_expired_snapshot_deleted_only_after_confirmed_absence(self):
        row = self._expired_snapshot()
        block = mock.Mock()
        block.get_snapshot.side_effect = [
            SimpleNamespace(id=row.cinder_snapshot_id, status='available'),
            sdk_exceptions.ResourceNotFound('missing')]
        self._housekeep(block)
        self.assertRaises(exception.NotFound,
                          objects.StackDeleteSnapshot.get_by_id,
                          self.ctx, row.id)
        self._housekeep(block)
        self.assertEqual(1, block.delete_snapshot.call_count)

    def test_sweep_is_bounded_and_continues_next_run(self):
        row = self._expired_snapshot()
        with api.writer.using(self.ctx) as session:
            session.add(models.StackDeleteSnapshot(
                id='second', project_id=self.ctx.project_id,
                stack_id=self.env.id, volume_id='second-volume',
                cinder_volume_id='second-cinder', cinder_snapshot_id='second',
                status='available', retention_days=0,
                expires_at=row.expires_at.replace(tzinfo=None)))
        block = mock.Mock()
        block.get_snapshot.side_effect = sdk_exceptions.ResourceNotFound(
            'gone')
        with mock.patch.object(stack_teardown,
                               'SNAPSHOT_CLEANUP_BATCH_SIZE', 1):
            self._housekeep(block)
            self.assertEqual(1, len(
                objects.StackDeleteSnapshotList.get_all(self.ctx)))
            self._housekeep(block)
            self.assertEqual(0, len(
                objects.StackDeleteSnapshotList.get_all(self.ctx)))

    def test_snapshot_delete_failure_keeps_receipt_for_retry(self):
        row = self._expired_snapshot()
        block = mock.Mock()
        block.get_snapshot.return_value = SimpleNamespace(
            id=row.cinder_snapshot_id, status='available')
        block.delete_snapshot.side_effect = RuntimeError('secret')
        self._housekeep(block)
        objects.StackDeleteSnapshot.get_by_id(self.ctx, row.id)
        block.get_snapshot.side_effect = sdk_exceptions.ResourceNotFound(
            'gone')
        self._housekeep(block)
        self.assertRaises(exception.NotFound,
                          objects.StackDeleteSnapshot.get_by_id,
                          self.ctx, row.id)

    def test_null_and_future_expiry_are_never_deleted(self):
        row = self._expired_snapshot()
        block = mock.Mock()
        for expiry in (None, datetime.datetime.now(datetime.timezone.utc) +
                       datetime.timedelta(days=1)):
            row.expires_at = expiry
            with api.writer.using(self.ctx) as session:
                session.query(models.StackDeleteSnapshot).filter_by(
                    id=row.id).update({'expires_at': expiry.replace(
                        tzinfo=None) if expiry is not None else None})
            self._housekeep(block)
            objects.StackDeleteSnapshot.get_by_id(self.ctx, row.id)
        self.assertEqual(0, block.get_snapshot.call_count)

    def test_unconfirmed_or_in_progress_snapshot_keeps_receipt(self):
        row = self._expired_snapshot()
        block = mock.Mock()
        block.get_snapshot.return_value = SimpleNamespace(
            id=row.cinder_snapshot_id, status='deleting')
        self._housekeep(block)
        self.assertEqual(0, block.delete_snapshot.call_count)
        objects.StackDeleteSnapshot.get_by_id(self.ctx, row.id)

    def test_failed_stack_is_not_swept(self):
        row = self._expired_snapshot()
        with api.writer.using(self.ctx) as session:
            session.query(models.Stack).filter_by(id=self.env.id).update(
                {'status': states.ERROR})
        block = mock.Mock()
        self._housekeep(block)
        self.assertEqual(0, block.get_snapshot.call_count)
        objects.StackDeleteSnapshot.get_by_id(self.ctx, row.id)

    def test_snapshot_failure_sets_stack_error(self):
        self.clients.options = {'snapshot_volumes': True}
        self.clients.fail_snapshot = True
        before_volume_gb = self.usage()['in_use_volume_gb']
        self.delete()
        row = self.stack()
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual('snapshot failed', row.fault)
        self.assertEqual(states.NONE, row.task_state)
        self.assertEqual(
            [], list(objects.StackDeleteSnapshotList.get_all(self.ctx)))
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.assertNotIn('volume', self.clients.order)
        self.assertEqual(before_volume_gb, self.usage()['in_use_volume_gb'])

    def test_unrecorded_snapshot_still_blocks_volume_delete(self):
        self.clients.options = {'snapshot_volumes': True}
        self.clients.snapshots['other'] = SimpleNamespace(
            id='other', status='available', volume_id='cinder-1')
        self.delete()
        row = self.stack()
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual('Volume cleanup failed; retry delete', row.fault)
        self.assertEqual(1, self.clients.snapshot_calls)
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        recorded = list(objects.StackDeleteSnapshotList.get_all(self.ctx))
        self.assertEqual(['snap-1'], [
            item.cinder_snapshot_id for item in recorded])

    def _arm_export(self, fail=None):
        calls = []
        storage = mock.Mock()

        def archive(flow, ctx, stack, export, storage_arg):
            calls.append(export.id)
            self.clients.mark('export')
            if fail is not None:
                raise fail
            flow.owner._save(export, status=states.UPLOADING)
            export.artifacts = [{
                'volume_id': self.volume.id,
                'name': self.volume.id + '.tar.gz',
                'size_bytes': 12, 'sha256': 'a' * 64}]
            export.object_expires_at = datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(hours=1)

        self.clients.options = {'export_before_delete': True}
        self.clients.manager = self.owner
        storage_patch = mock.patch.object(
            exports, 'SwiftStorage', return_value=storage)
        create_patch = mock.patch.object(
            exports.ExportFlow, 'create', autospec=True,
            side_effect=archive)
        storage_patch.start()
        create_patch.start()
        self.addCleanup(storage_patch.stop)
        self.addCleanup(create_patch.stop)
        return calls, storage

    def test_export_runs_before_teardown(self):
        calls, storage = self._arm_export()
        self.delete()
        row = self.stack()
        self.assertEqual(states.DELETED, row.status)
        self.assertIsNone(row.fault)
        self.assertEqual(1, len(calls))
        self.assertEqual(
            stack_teardown._delete_export_id(self.env.id), calls[0])
        export = objects.Export.get_by_id(self.ctx, calls[0])
        self.assertEqual(states.ACTIVE, export.status)
        self.assertIsNone(export.fault)
        self.assertEqual([self.volume.id], export.volume_ids)
        self.assertLess(
            self.clients.order.index('export'),
            self.clients.order.index('coolify'))
        storage.delete.assert_not_called()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertEqual(states.ACTIVE, objects.Export.get_by_id(
            self.ctx, calls[0]).status)

    def test_export_retry_reuses_the_export(self):
        calls, storage = self._arm_export()
        self.clients.fail_network = True
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertEqual(1, len(calls))
        self.assertEqual(states.ACTIVE, objects.Export.get_by_id(
            self.ctx, calls[0]).status)
        self.clients.fail_network = False
        self.clients.order = []
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
        self.assertEqual(1, len(calls))
        found = list(objects.ExportList.get_all(self.ctx))
        self.assertEqual([calls[0]], [item.id for item in found])
        self.assertEqual(states.ACTIVE, found[0].status)
        storage.delete.assert_not_called()
        self.assertNotIn('export', self.clients.order)

    def test_export_failure_deletes_nothing(self):
        calls, storage = self._arm_export(fail=RuntimeError('timeout'))
        before = self.usage()['in_use_volume_gb']
        self.delete()
        row = self.stack()
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual(states.NONE, row.task_state)
        self.assertIn('Export before delete failed', row.fault)
        self.assertNotIn('timeout', row.fault)
        self.assertNotIn('coolify', self.clients.order)
        self.assertNotIn('volume', self.clients.order)
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.assertEqual(before, self.usage()['in_use_volume_gb'])
        self.assertEqual(1, len(calls))
        export = objects.Export.get_by_id(self.ctx, calls[0])
        self.assertEqual(states.ERROR, export.status)
        storage.delete.assert_not_called()
        self.clients.order = []
        self.delete()
        self.assertEqual(states.ERROR, self.stack().status)
        self.assertEqual(1, len(list(objects.ExportList.get_all(self.ctx))))
        objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.assertNotIn('coolify', self.clients.order)

    def test_engine_less_stack_skips_coolify_and_completes(self):
        self.env.engine_token_ref = None
        self.env.save()
        calls, storage = self._arm_export()
        self.delete()
        row = self.stack()
        self.assertEqual(states.DELETED, row.status)
        self.assertIsNone(row.fault)
        self.assertIsNone(row.engine_token_ref)
        self.assertNotIn('coolify', self.clients.order)
        self.assertEqual(
            ['export', 'dns', 'floating', 'server', 'ports', 'volume',
             'network', 'secret'], _phases(self.clients.order))
        self.assertEqual(
            stack_teardown._delete_export_id(self.env.id), calls[0])
        self.assertEqual(states.ACTIVE, objects.Export.get_by_id(
            self.ctx, calls[0]).status)
        storage.delete.assert_not_called()
        self.assertNotIn('ref-token', self.clients.secret_refs)
        self.assertIn('ref-heart', self.clients.secret_refs)
        self.assertIn('ref-backup', self.clients.secret_refs)
        self.assertIn('ref-ssh', self.clients.secret_refs)
        self.assertIn('ref-secret', self.clients.secret_refs)
        self.assertIn('ref-source', self.clients.secret_refs)
        self.assertRaises(
            exception.NotFound, objects.Volume.get_by_id, self.ctx,
            self.volume.id)
        server = objects.Server.get_by_id(
            self.ctx, self.server.id, show_deleted=True)
        self.assertEqual(states.DELETING, server.status)
        self.assertIsNotNone(server.deleted_at)
        used = self.usage()
        self.assertEqual(0, used['in_use_environments'])
        self.assertEqual(0, used['in_use_servers'])
        self.assertEqual(0, used['in_use_volume_gb'])
        self.assertEqual(0, used['in_use_apps'])
        self.assertEqual(0, used['in_use_domains'])

    def test_endpoint_without_token_ref_does_not_read_secrets(self):
        objects.Server(
            context=self.ctx, name='control', stack_id=self.env.id,
            kind='vm', role='control', status=states.ACTIVE,
            task_state=states.NONE, mgmt_ip='192.0.2.10').create()
        for ref in (None, ''):
            self.env.engine_token_ref = ref
            self.env.save()
            with mock.patch(
                    'coast.conductor.manager.secret_store.call') as fetch:
                error = self.assertRaises(
                    driver_base.EngineAuthError, self.owner._endpoint,
                    self.env)
            self.assertEqual(
                'Engine token reference is required', str(error))
            fetch.assert_not_called()

    def test_retry_after_export_before_delete(self):
        self._retry_after_step('_export_before_delete')

    def test_retry_after_coolify(self):
        self._retry_after_step('_coolify')

    def test_retry_after_dns(self):
        self._retry_after_step('_dns')

    def test_retry_after_floating(self):
        self._retry_after_step('_floating')

    def test_retry_after_ports(self):
        self._retry_after_step('_ports')

    def test_retry_after_servers(self):
        self._retry_after_step('_servers')

    def test_retry_after_snapshot_volumes(self):
        self._retry_after_step('_snapshot_volumes')

    def test_retry_after_volumes(self):
        self._retry_after_step('_volumes')

    def test_retry_after_network(self):
        self._retry_after_step('_network')

    def test_retry_after_secrets(self):
        self._retry_after_step('_secrets')

    def test_retry_after_finish(self):
        self.delete()
        self.delete()
        self.assertEqual(states.DELETED, self.stack().status)
