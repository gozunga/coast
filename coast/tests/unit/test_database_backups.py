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

import base64
import datetime
import gzip
import io
from types import SimpleNamespace
from unittest import mock

from oslo_utils import timeutils

from coast.agent import backup as wire
from coast.api import handlers
from coast.common import context
from coast.common import database_backups as common
from coast.common import engine_versions
from coast.common import exception
from coast.common import public_text
from coast.common import quota
from coast.common import states
from coast.conductor import backup_storage
from coast.conductor import backups
from coast.conductor import manager
from coast.conductor import stack_teardown
from coast.drivers import noop
from coast import objects
from coast.tests import app_api


class BackupAPITest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.service.engine_ref = 'service-engine-uuid'
        self.service.template_id = 'postgresql'
        self.service.kind = 'service'
        self.service.save()
        self.server.mgmt_ip = '192.0.2.10'
        self.server.save()
        self.available = mock.patch.object(backup_storage, 'proxy_for')
        self.available.start()
        self.addCleanup(self.available.stop)
        self.policy_path = self.path + '/backup_policy'
        self.backups_path = self.path + '/backups'

    def call(self, method, path, body=None, status=200, **kwargs):
        return self.request(method, path, body, status=status,
                            version='1.20', **kwargs)

    def set_policy(self, **values):
        return self.call('put', self.policy_path, {'backup_policy': dict(
            engine='postgres', container='postgresql', **values)})

    def create(self):
        self.set_policy()
        result = self.call('post', self.backups_path, {}, status=202)
        return objects.Backup.get_by_id(self.ctx, result.json['backup']['id'])

    def activate(self):
        row = self.create()
        job = next(common.rows(objects.BackupJobList, self.ctx,
                               backup_id=row.id))
        common.change(self.ctx, job, states.BUILDING, 'backup')
        common.change(self.ctx, job, states.ACTIVE, 'backup')
        self.service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.service.release_task(states.RECONCILING)
        common.change(self.ctx, row, states.BUILDING, 'create',
                      started_at=timeutils.utcnow())
        ref = self.store.put(self.ctx, base64.b64encode(b'x' * 32).decode())
        common.change(self.ctx, row, states.ACTIVE, 'create',
                      size_bytes=42, engine_version='16.2', key_ref=ref,
                      segments=[dict(size_bytes=42, sha256='a' * 64)])
        return row

    def test_source_type_preserves_database_backup_selection(self):
        if self.service.kind != 'database':
            self.service.template_id = None
            self.service.save()
            source = objects.Source.get_by_id(self.ctx, self.service.source_id)
            source.type = 'compose'
            source.save()
        row = self.create()
        self.assertEqual('postgres', row.engine)
        self.assertEqual(self.service.engine_ref if self.service.kind ==
                         'database' else 'postgresql', row.container)

    def test_policy_defaults_schedule_off_and_update(self):
        result = self.call('get', self.policy_path).json['backup_policy']
        self.assertEqual(dict(common.POLICY_DEFAULTS, engine=None,
                              container=None), result)
        result = self.set_policy().json['backup_policy']
        self.assertFalse(result['enabled'])
        self.assertEqual(14, result['keep_daily'])
        self.assertEqual(8, result['keep_weekly'])
        self.call('put', self.policy_path,
                  {'backup_policy': {'enabled': True, 'hour': 3}})
        row = common.policy_for(self.ctx, self.service.id)
        self.assertTrue(row.enabled)
        self.assertEqual(3, row.hour)
        self.assertEqual('postgres', row.engine)

    def test_create_is_durable_and_busy_conflicts(self):
        row = self.create()
        self.assertEqual(states.PENDING, row.status)
        self.assertEqual('manual', row.kind)
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.call('post', self.backups_path, {}, status=409)
        self.call('put', self.policy_path,
                  {'backup_policy': {'enabled': True}}, status=409)
        listed = self.call('get', self.backups_path).json['backups']
        self.assertEqual([row.id], [item['id'] for item in listed])
        self.assertNotIn('key_ref', str(listed))
        self.assertNotIn('segments', str(listed))

    def test_isolation_roles_and_microversions_on_all_routes(self):
        row = self.activate()
        path = '/v1/backups/' + row.id
        operations = [('get', self.policy_path, None),
                      ('put', self.policy_path, {'backup_policy': {}}),
                      ('get', self.backups_path, None),
                      ('post', self.backups_path, {}),
                      ('get', path, None), ('delete', path, None),
                      ('get', path + '/download', None),
                      ('post', path + '/restore', {})]
        for method, url, body in operations:
            self.call(method, url, body, status=401, headers={})
            self.call(method, url, body, status=404, headers=dict(
                self.headers, **{'X-Project-Id': 'foreign'}))
            self.request(method, url, body, status=404, version='1.18')
            read = method == 'get' and not url.endswith('/download')
            self.call(method, url, body, status=200 if read else 403,
                      headers=dict(self.headers, **{'X-Roles': 'reader'}))
        self.call('get', path, headers=dict(
            self.headers, **{'X-Project-Id': 'admin-project',
                             'X-Roles': 'admin'}))
        self.call('put', self.policy_path,
                  {'backup_policy': {'enabled': False}}, headers=dict(
                      self.headers, **{'X-Project-Id': 'admin-project',
                                       'X-Roles': 'admin'}))

    def test_backup_and_restore_errors_never_name_engine(self):
        # Stored errors keep driver detail for operators and admins.
        # Project reads get the public wording, without engine names.
        leak = 'Coolify exec failed via Traefik password=private-value'
        row = self.activate()
        self.call('post', '/v1/backups/' + row.id + '/restore', {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202)
        row.error = leak
        row.save()
        for job in common.rows(objects.BackupJobList, self.ctx,
                               backup_id=row.id):
            job.error = leak
            job.save()
        path = '/v1/backups/' + row.id
        for roles in ('reader', 'reader,member,manager'):
            headers = dict(self.headers, **{'X-Roles': roles})
            listed = self.call('get', self.backups_path, headers=headers)
            shown = self.call('get', path, headers=headers)
            for response in (listed, shown):
                self.assertFalse(
                    public_text.contains_engine_name(response.body),
                    response.text)
                self.assertNotIn(b'private-value', response.body)
            self.assertEqual(public_text.scrub(leak),
                             listed.json['backups'][0]['error'])
            backup = shown.json['backup']
            self.assertEqual(public_text.scrub(leak), backup['error'])
            self.assertEqual(public_text.scrub(leak),
                             backup['restores'][0]['error'])
        admin = self.call('get', path, headers=dict(
            self.headers, **{'X-Project-Id': 'admin-project',
                             'X-Roles': 'admin'})).json['backup']
        self.assertEqual(leak, admin['error'])
        self.assertEqual(leak, admin['restores'][0]['error'])

    def test_schema_no_storage_and_missing_policy(self):
        self.call('post', self.backups_path, {}, status=409)
        for values in ({}, {'engine': 'redis', 'container': 'redis'},
                       {'engine': 'postgres', 'container': '../bad'},
                       {'engine': 'postgres', 'container': 'db', 'hour': 24},
                       {'engine': 'postgres', 'container': 'db',
                        'keep_daily': -1}):
            self.call('put', self.policy_path, {'backup_policy': values},
                      status=400)
        self.set_policy()
        self.service.template_id = None
        self.service.save()
        self.call('put', self.policy_path, {'backup_policy': {}}, status=409)
        self.service.template_id = 'postgresql'
        self.service.save()
        for body in ({'container': 'db'}, {'backup': {'kind': 'safety'}}):
            self.call('post', self.backups_path, body, status=400)
        with mock.patch.object(backup_storage, 'proxy_for',
                               side_effect=backup_storage.StorageUnavailable):
            result = self.call('post', self.backups_path, {}, status=409)
            self.assertIn('Object storage not available', result.text)
            self.call('put', self.policy_path,
                      {'backup_policy': {'enabled': True}}, status=409)
        self.assertEqual([], list(common.rows(objects.BackupList, self.ctx)))

    def test_overwrite_requires_typed_name_and_queues_safety(self):
        row = self.activate()
        url = '/v1/backups/' + row.id + '/restore'
        for body in ({'mode': 'overwrite'},
                     {'mode': 'overwrite', 'confirmation': 'wrong'},
                     {'mode': 'overwrite', 'confirmation': self.service.name,
                      'name': 'other'}, {'mode': 'bad'},
                     {'mode': 'new_copy', 'name': 'INVALID_NAME'}):
            self.call('post', url, body, status=400)
        result = self.call('post', url, {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202)
        self.assertEqual(self.service.id, result.json['restore']['service_id'])
        self.assertEqual('overwrite', result.json['restore']['operation'])
        self.assertEqual(states.PENDING, result.json['restore']['status'])
        shown = self.call('get', '/v1/backups/' + row.id).json['backup']
        self.assertEqual(1, len(shown['restores']))
        self.assertEqual(states.PENDING, shown['restores'][0]['status'])

    def test_new_copy_uses_normal_create_and_fresh_template(self):
        row = self.activate()
        url = '/v1/backups/' + row.id + '/restore'
        self.call('post', url, {}, status=400)
        with mock.patch('coast.api.v1.template_controllers.get_template',
                        return_value={'ports': []}), mock.patch(
                            'coast.common.storage.create_template_storages'), \
                mock.patch('coast.api.handlers.Handler.create_service'):
            result = self.call('post', url, {'name': 'recovered'}, status=202)
        target = objects.Service.get_by_id(
            self.ctx, result.json['restore']['service_id'])
        self.assertNotEqual(self.service.id, target.id)
        self.assertEqual('recovered', target.name)
        self.assertEqual(self.service.template_id, target.template_id)
        self.assertEqual(self.service.server_id, target.server_id)
        self.assertEqual(states.PROVISIONING, target.task_state)
        self.assertNotEqual(self.service.source_id, target.source_id)
        self.call('delete', '/v1/backups/' + row.id, status=409)

    def test_delete_is_queued_and_tombstone_hidden(self):
        row = self.activate()
        url = '/v1/backups/' + row.id
        self.call('delete', url, status=202)
        with mock.patch.object(backup_storage, 'SwiftStorage'):
            job = next(job for job in common.rows(
                objects.BackupJobList, self.ctx, backup_id=row.id)
                if job.operation == 'delete')
            backups.run_job(self.conf, self.ctx, job.id)
        self.call('get', url, status=404)
        self.assertEqual(
            [], self.call('get', self.backups_path).json['backups'])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_download_returns_authenticated_compressed_plaintext(self):
        row = self.activate()
        data = b'database dump contents' * 100
        encrypted = b''.join(wire.encrypt(io.BytesIO(data), b'x' * 32,
                                          row.engine, row.engine_version))
        with mock.patch.object(backup_storage, 'SwiftStorage'), mock.patch(
                'coast.api.v1.backup_controllers.download_chunks',
                return_value=iter([encrypted])):
            response = self.call('get', '/v1/backups/' + row.id + '/download')
        self.assertEqual(data, gzip.decompress(response.body))
        self.assertEqual('no-store', response.headers['Cache-Control'])
        self.assertNotIn('key', str(response.headers).lower())
        corrupt = bytearray(encrypted)
        corrupt[80] ^= 1
        with mock.patch.object(backup_storage, 'SwiftStorage'), mock.patch(
                'coast.api.v1.backup_controllers.download_chunks',
                return_value=iter([bytes(corrupt)])):
            self.call('get', '/v1/backups/' + row.id + '/download', status=500)


class BackupConductorTest(BackupAPITest):
    def receipt(self, **changes):
        return dict(size_bytes=42, sha256='a' * 64,
                    duration_seconds=1.2, engine_version='16.2',
                    segments=[dict(size_bytes=42, sha256='a' * 64)], **changes)

    def test_capture_dispatch_and_receipt(self):
        row = self.create()
        job = next(common.rows(objects.BackupJobList, self.ctx,
                               backup_id=row.id))
        with mock.patch.object(backup_storage, 'SwiftStorage') as storage, \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.return_value = self.receipt()
            storage.return_value.urls.return_value = [
                'https://example.com/put']
            backups.run_job(self.conf, self.ctx, job.id)
            command = agent.return_value.request.call_args.args[2]
        row = objects.Backup.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ACTIVE, row.status)
        self.assertEqual(42, row.size_bytes)
        self.assertEqual('16.2', row.engine_version)
        self.assertEqual(self.service.engine_ref, command['service_id'])
        self.assertEqual('postgresql', command['container'])
        self.assertEqual('service', command['resource_type'])
        self.assertEqual(32, len(base64.b64decode(command['key'])))
        self.assertEqual(['https://example.com/put'], command['upload_urls'])
        self.assertNotIn(command['key'], str(row.obj_to_primitive()))
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def compose_app(self):
        self.service.kind = 'app'
        self.service.template_id = None
        self.service.save()
        source = objects.Source.get_by_id(self.ctx, self.service.source_id)
        source.type = 'compose'
        source.save()

    def test_compose_app_capture_dispatch(self):
        self.compose_app()
        self.test_capture_dispatch_and_receipt()

    def test_compose_app_restore_dispatch(self):
        self.compose_app()
        row = self.activate()
        response = self.call('post', '/v1/backups/' + row.id + '/restore', {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202)
        with mock.patch.object(backup_storage, 'SwiftStorage'), \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.return_value = self.receipt()
            backups.run_job(self.conf, self.ctx,
                            response.json['restore']['id'])
        calls = agent.return_value.request.call_args_list
        self.assertEqual(['/v1/backup_database', '/v1/restore_database'],
                         [call.args[1] for call in calls])
        for call in calls:
            self.assertEqual('service', call.args[2]['resource_type'])
            self.assertEqual('postgresql', call.args[2]['container'])
        self.assertEqual(states.ACTIVE, objects.BackupJob.get_by_id(
            self.ctx, response.json['restore']['id']).status)

    def test_invalid_receipt_fails_without_publishing_and_releases_lock(self):
        row = self.create()
        job = next(common.rows(objects.BackupJobList, self.ctx,
                               backup_id=row.id))
        with mock.patch.object(backup_storage, 'SwiftStorage') as storage, \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.return_value = dict(
                self.receipt(), size_bytes=43)
            backups.run_job(self.conf, self.ctx, job.id)
            self.assertFalse(storage.return_value.complete.called)
        row = objects.Backup.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual(backups.FAULT, row.error)
        self.assertEqual(states.ERROR, objects.BackupJob.get_by_id(
            self.ctx, job.id).status)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_safety_backup_before_restore_and_kept_on_failure(self):
        row = self.activate()
        result = self.call('post', '/v1/backups/' + row.id + '/restore', {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202)
        job_id = result.json['restore']['id']
        commands = []

        def request(method, path, body):
            commands.append(path)
            if path.endswith('backup_database'):
                return self.receipt()
            raise RuntimeError('secret database password and URL')

        with mock.patch.object(backup_storage, 'SwiftStorage'), \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.side_effect = request
            backups.run_job(self.conf, self.ctx, job_id)
        self.assertEqual(['/v1/backup_database', '/v1/restore_database'],
                         commands)
        job = objects.BackupJob.get_by_id(self.ctx, job_id)
        self.assertEqual(states.ERROR, job.status)
        self.assertEqual(backups.FAULT, job.error)
        safety = objects.Backup.get_by_id(self.ctx, job.safety_backup_id)
        self.assertEqual('safety', safety.kind)
        self.assertEqual(states.ACTIVE, safety.status)
        self.assertEqual(states.ACTIVE, objects.Backup.get_by_id(
            self.ctx, row.id).status)

    def test_failed_safety_prevents_restore(self):
        row = self.activate()
        result = self.call('post', '/v1/backups/' + row.id + '/restore', {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202)
        with mock.patch.object(backup_storage, 'SwiftStorage'), \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.return_value = {}
            backups.run_job(self.conf, self.ctx, result.json['restore']['id'])
            calls = agent.return_value.request.call_args_list
            self.assertEqual(['/v1/backup_database'], [
                call.args[1] for call in calls])
        self.assertEqual(1, sum(1 for backup in common.rows(
            objects.BackupList, self.ctx, kind='safety')
            if backup.status == states.ERROR))

    def test_scheduler_is_once_per_day_and_disabled_by_default(self):
        self.set_policy()
        now = datetime.datetime(2026, 10, 19, 3)
        policy = common.policy_for(self.ctx, self.service.id)
        self.assertIsNone(common.due(policy, now))
        policy.enabled = True
        policy.save()
        scheduled = common.due(policy, now)
        self.assertEqual(now.replace(hour=2), scheduled)
        self.assertIsNone(common.due(policy, now.replace(hour=1)))
        common.submit(self.ctx, self.service, 'scheduled', scheduled)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        service.release_task(states.RECONCILING)
        self.assertRaises(exception.Conflict, common.submit, self.ctx,
                          service, 'scheduled', scheduled)
        self.assertEqual(1, len(list(common.rows(
            objects.BackupList, self.ctx))))
        policy = common.policy_for(self.ctx, self.service.id)
        self.assertIsNone(common.due(policy, now))

    def test_retention_keeps_unique_daily_weekly_manual_and_safety(self):
        start = datetime.datetime(2026, 10, 19, 2)
        rows = [SimpleNamespace(
            id=str(i), kind='scheduled', status=states.ACTIVE,
            started_at=start - datetime.timedelta(days=i)) for i in range(30)]
        rows += [SimpleNamespace(id='manual', kind='manual',
                                 status=states.ACTIVE, started_at=start),
                 SimpleNamespace(id='safety', kind='safety',
                                 status=states.ACTIVE, started_at=start)]
        policy = SimpleNamespace(keep_daily=2, keep_weekly=2)
        expired = {row.id for row in common.expired(rows, policy)}
        self.assertNotIn('0', expired)
        self.assertNotIn('1', expired)
        self.assertNotIn('manual', expired)
        self.assertNotIn('safety', expired)
        self.assertIn('29', expired)
        policy.keep_daily = policy.keep_weekly = 0
        self.assertEqual(30, len(common.expired(rows, policy)))

    def test_stale_backup_fails_closed_without_repeating_agent_command(self):
        row = self.create()
        job = next(common.rows(objects.BackupJobList, self.ctx,
                               backup_id=row.id))
        common.change(self.ctx, row, states.BUILDING, 'create')
        common.change(self.ctx, job, states.BUILDING, 'backup',
                      started_at=timeutils.utcnow() -
                      datetime.timedelta(days=1))
        with mock.patch.object(backups.agent_client, 'AgentClient') as agent:
            backups.recover(self.conf, self.ctx, job)
            self.assertFalse(agent.called)
        self.assertEqual(states.ERROR, objects.Backup.get_by_id(
            self.ctx, row.id).status)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_service_delete_removes_objects_keys_and_records(self):
        row = self.activate()
        policy = common.policy_for(self.ctx, self.service.id)
        policy.key_ref = row.key_ref
        policy.save()
        with mock.patch.object(backup_storage, 'SwiftStorage'):
            backups.drop_service(self.conf, self.ctx, self.service)
        self.assertEqual([], list(common.rows(objects.BackupList, self.ctx)))
        self.assertIsNone(common.policy_for(self.ctx, self.service.id))
        self.assertEqual({}, self.store.values)
        self.assertEqual([], list(common.rows(
            objects.BackupJobList, self.ctx)))

    def test_cross_project_parent_reference_rejected(self):
        foreign = context.RequestContext(project_id='other')
        self.assertRaises(exception.NotFound, objects.Backup(
            context=foreign, service_id=self.service.id, engine='postgres',
            container='db', kind='manual', status=states.PENDING).create)

    def test_receipt_validation_rejects_untrusted_worker_values(self):
        for values in ({'sha256': 'bad'}, {'size_bytes': True},
                       {'duration_seconds': float('nan')},
                       {'engine_version': 'credential=secret'},
                       {'segments': []},
                       {'segments': [dict(size_bytes=-1, sha256='a' * 64)]}):
            self.assertRaises(ValueError, backups._receipt,
                              dict(self.receipt(), **values), self.conf)

    def test_retention_never_deletes_a_restore_source(self):
        row = self.activate()
        row.kind = 'scheduled'
        row.save()
        policy = common.policy_for(self.ctx, self.service.id)
        policy.keep_daily = policy.keep_weekly = 0
        policy.save()
        self.call('post', '/v1/backups/' + row.id + '/restore', {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202)
        # Model a queued new-copy restore: the source is otherwise idle.
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        service.release_task(states.RECONCILING)
        self.assertRaises(exception.Conflict, backups.schedule,
                          self.conf, self.ctx, policy.id)
        self.assertEqual(states.ACTIVE, objects.Backup.get_by_id(
            self.ctx, row.id).status)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)

    def test_old_executor_cannot_release_lock_after_recovery(self):
        row = self.create()
        job = next(common.rows(objects.BackupJobList, self.ctx,
                               backup_id=row.id))
        common.change(self.ctx, job, states.BUILDING, 'backup',
                      started_at=timeutils.utcnow() -
                      datetime.timedelta(days=1))
        backups.recover(self.conf, self.ctx, job)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        service.acquire_task(states.RECONCILING)
        backups.finish_job(self.ctx, job, states.ACTIVE)
        self.assertEqual(states.ERROR, objects.BackupJob.get_by_id(
            self.ctx, job.id).status)
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, service.id).task_state)

    def test_account_signer_reuses_owned_db_reference(self):
        first_ref, first_key = backup_storage.account_key(self.ctx)
        second_ref, second_key = backup_storage.account_key(self.ctx)
        self.assertEqual(first_ref, second_ref)
        self.assertEqual(first_key, second_key)
        self.assertEqual(1, len(self.store.values))
        self.assertEqual(1, len(list(common.rows(
            objects.BackupAccountList, self.ctx))))

    def test_new_copy_restores_into_fresh_engine_reference(self):
        row = self.activate()
        with mock.patch('coast.api.v1.template_controllers.get_template',
                        return_value={'ports': []}), mock.patch(
                            'coast.common.storage.create_template_storages'), \
                mock.patch('coast.api.handlers.Handler.create_service'):
            result = self.call('post', '/v1/backups/' + row.id + '/restore',
                               {'name': 'recovered'}, status=202)
        job = objects.BackupJob.get_by_id(
            self.ctx, result.json['restore']['id'])
        # Provisioning has not completed: no restore may run yet.
        with mock.patch.object(backups.agent_client, 'AgentClient') as agent:
            backups.run_job(self.conf, self.ctx, job.id)
            self.assertFalse(agent.called)
        target = objects.Service.get_by_id(self.ctx, job.service_id)
        target.release_task(states.PROVISIONING)
        target.engine_ref = 'fresh-engine-uuid'
        target.status = states.ACTIVE
        target.save()
        with mock.patch.object(backup_storage, 'SwiftStorage'), \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.return_value = {'duration_seconds': 1}
            backups.run_job(self.conf, self.ctx, job.id)
            command = agent.return_value.request.call_args.args[2]
        self.assertEqual('fresh-engine-uuid', command['service_id'])
        self.assertEqual('postgresql', command['container'])
        self.assertEqual('service', command['resource_type'])
        self.assertEqual(states.ACTIVE, objects.BackupJob.get_by_id(
            self.ctx, job.id).status)
        self.assertEqual(1, len(list(common.rows(
            objects.BackupList, self.ctx))))

    def test_new_copy_preserves_service_quota(self):
        row = self.activate()
        self.config.config(apps=0, group='quotas')
        with mock.patch('coast.api.v1.template_controllers.get_template',
                        return_value={'ports': []}):
            self.call('post', '/v1/backups/' + row.id + '/restore',
                      {'name': 'recovered'}, status=403)
        self.assertEqual(1, len(list(common.rows(
            objects.ServiceList, self.ctx))))


class StandaloneBackupTest(BackupAPITest):
    """Exercise the same durable paths against a standalone database row."""

    def setUp(self):
        super().setUp()
        self.service.kind = 'database'
        self.service.template_id = None
        self.service.engine = next(iter(engine_versions.DATABASES))
        info = engine_versions.DATABASES[self.service.engine]
        self.service.engine_version = info['default_version']
        self.service.db_name = 'app'
        self.service.db_username = 'app'
        self.service.password_ref = self.store.put(self.ctx, 'original')
        self.service.save()
        self.policy_path = ('/v1/databases/' + self.service.id +
                            '/backup_policy')
        self.backups_path = '/v1/databases/' + self.service.id + '/backups'

    def call(self, method, path, body=None, status=200, **kwargs):
        return self.request(method, path, body, status=status,
                            version='1.29', **kwargs)

    def set_policy(self, **values):
        return self.call('put', self.policy_path, {'backup_policy': values})

    def test_policy_defaults_schedule_off_and_update(self):
        self.set_policy(enabled=True, hour=3)
        policy = common.policy_for(self.ctx, self.service.id)
        self.assertEqual(common.selection(self.service)['engine'],
                         policy.engine)
        self.assertEqual(self.service.engine_ref, policy.container)
        self.assertTrue(policy.enabled)
        self.assertEqual(3, policy.hour)

    def test_schema_no_storage_and_missing_policy(self):
        for values in ({'engine': self.service.engine}, {'container': 'db'},
                       {'hour': 24}, {'unknown': True}):
            self.call('put', self.policy_path, {'backup_policy': values},
                      status=400)
        for engine, info in engine_versions.DATABASES.items():
            if info['backups']:
                continue
            self.service.engine = engine
            self.service.save()
            for method, path in (('get', self.policy_path),
                                 ('put', self.policy_path),
                                 ('get', self.backups_path),
                                 ('post', self.backups_path)):
                result = self.call(method, path, status=409)
                self.assertIn('Backups are not available for this engine.',
                              result.text)

    def test_new_copy_uses_normal_create_and_fresh_template(self):
        row = self.activate()
        with mock.patch('coast.api.handlers.Handler.create_service'):
            response = self.call('post', '/v1/backups/' + row.id + '/restore',
                                 {'name': 'recovered'}, status=202)
        target = objects.Service.get_by_id(
            self.ctx, response.json['restore']['service_id'])
        for field in ('engine', 'engine_version', 'server_id',
                      'environment_id', 'db_name', 'db_username'):
            self.assertEqual(getattr(self.service, field),
                             getattr(target, field))
        self.assertEqual('database', target.kind)
        self.assertFalse(target.public_enabled)
        self.assertNotEqual(self.service.password_ref, target.password_ref)
        self.assertNotEqual('original', self.store.get(
            self.ctx, target.password_ref))

    def test_overwrite_captures_safety_before_restore(self):
        row = self.activate()
        response = self.call('post', '/v1/backups/' + row.id + '/restore', {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202)
        job_id = response.json['restore']['id']
        commands = []

        def request(method, path, body):
            commands.append(path)
            self.assertEqual('database', body['resource_type'])
            self.assertEqual(self.service.engine_ref, body['container'])
            if path == '/v1/backup_database':
                return dict(size_bytes=42, sha256='a' * 64,
                            duration_seconds=1,
                            engine_version=self.service.engine_version,
                            segments=[dict(size_bytes=42, sha256='a' * 64)])
            job = objects.BackupJob.get_by_id(self.ctx, job_id)
            safety = objects.Backup.get_by_id(self.ctx, job.safety_backup_id)
            self.assertEqual(states.ACTIVE, safety.status)
            return {'duration_seconds': 1}

        with mock.patch.object(backup_storage, 'SwiftStorage'), \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.side_effect = request
            backups.run_job(self.conf, self.ctx, job_id)
        self.assertEqual(['/v1/backup_database', '/v1/restore_database'],
                         commands)
        job = objects.BackupJob.get_by_id(self.ctx, job_id)
        self.assertEqual(states.ACTIVE, job.status)
        safety = objects.Backup.get_by_id(self.ctx, job.safety_backup_id)
        self.assertEqual('safety', safety.kind)
        self.assertEqual(self.service.id, safety.service_id)
        self.assertEqual(states.ACTIVE, safety.status)

    def test_overwrite_failed_safety_capture_prevents_restore(self):
        row = self.activate()
        response = self.call('post', '/v1/backups/' + row.id + '/restore', {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202)
        job_id = response.json['restore']['id']
        with mock.patch.object(backup_storage, 'SwiftStorage'), \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.side_effect = RuntimeError(
                'Safety capture failed')
            backups.run_job(self.conf, self.ctx, job_id)
        calls = agent.return_value.request.call_args_list
        self.assertEqual(['/v1/backup_database'], [
            call.args[1] for call in calls])
        self.assertEqual('database', calls[0].args[2]['resource_type'])
        job = objects.BackupJob.get_by_id(self.ctx, job_id)
        self.assertEqual(states.ERROR, job.status)
        safety = objects.Backup.get_by_id(self.ctx, job.safety_backup_id)
        self.assertEqual(states.ERROR, safety.status)
        self.assertEqual(states.ACTIVE, objects.Backup.get_by_id(
            self.ctx, row.id).status)

    def test_supported_catalog_engines_capture_schedule_and_cleanup(self):
        for engine, info in engine_versions.DATABASES.items():
            if not info['backups']:
                continue
            self.service = objects.Service.get_by_id(self.ctx, self.service.id)
            self.service.engine = engine
            self.service.engine_version = info['default_version']
            self.service.save()
            self.set_policy(enabled=True)
            policy = common.policy_for(self.ctx, self.service.id)
            scheduled = common.due(policy, datetime.datetime(2026, 10, 23, 3))
            row = common.submit(self.ctx, self.service, 'scheduled', scheduled)
            job = next(common.rows(objects.BackupJobList, self.ctx,
                                   backup_id=row.id))
            receipt = dict(size_bytes=42, sha256='a' * 64,
                           duration_seconds=1,
                           engine_version=info['default_version'] + '.0',
                           segments=[dict(size_bytes=42, sha256='a' * 64)])
            with mock.patch.object(backup_storage, 'SwiftStorage'), \
                    mock.patch.object(backups.agent_client,
                                      'AgentClient') as agent:
                agent.return_value.request.return_value = receipt
                backups.run_job(self.conf, self.ctx, job.id)
                command = agent.return_value.request.call_args.args[2]
                self.assertEqual('database', command['resource_type'])
                self.assertEqual(self.service.engine_ref, command['container'])
                self.assertEqual(common.selection(self.service)['engine'],
                                 command['engine'])
                self.assertEqual(states.ACTIVE, objects.Backup.get_by_id(
                    self.ctx, row.id).status)
                backups.drop_service(self.conf, self.ctx, self.service)
            self.assertIsNone(common.policy_for(self.ctx, self.service.id))
            self.assertEqual([], list(common.rows(
                objects.BackupList, self.ctx, service_id=self.service.id)))

    def _backup_storage_for_delete(self, row):
        policy = common.policy_for(self.ctx, self.service.id)
        policy.key_ref = row.key_ref
        policy.save()
        self.assertIn(row.key_ref, self.store.values)
        patcher = mock.patch.object(self.store, 'delete',
                                    wraps=self.store.delete)
        self.secret_delete = patcher.start()
        self.addCleanup(patcher.stop)
        proxy = mock.Mock()
        proxy.get_endpoint.return_value = (
            'https://objects.example.com/v1/AUTH_p')
        names = [row.id + '/manifest', row.id + '/000000',
                 row.id + '/000001']  # Include an uncertain upload segment.
        proxy.objects.return_value = [SimpleNamespace(name=name)
                                      for name in names]
        return proxy, names

    def _assert_backup_storage_deleted(self, row, proxy, names):
        container = 'coast-backups-' + self.service.id
        proxy.objects.assert_called_once_with(container, prefix=row.id + '/')
        self.assertEqual([mock.call(name, container=container,
                                    ignore_missing=True) for name in names],
                         proxy.delete_object.call_args_list)
        proxy.delete_container.assert_called_once_with(
            container, ignore_missing=True)
        self.secret_delete.assert_any_call(mock.ANY, row.key_ref)
        self.assertNotIn(row.key_ref, self.store.values)
        self.assertIsNone(common.policy_for(self.ctx, self.service.id))
        for cls in (objects.BackupList, objects.BackupJobList):
            self.assertEqual([], list(common.rows(
                cls, self.ctx, service_id=self.service.id)))

    def test_database_delete_removes_backup_objects_container_and_key(self):
        row = self.activate()
        proxy, names = self._backup_storage_for_delete(row)
        self.stack.engine_version = (
            noop.NoopDriver.SUPPORTED_ENGINE_VERSIONS[0])
        self.stack.save()
        endpoint = manager.Manager(self.conf)
        self.addCleanup(endpoint.close)
        with mock.patch.object(handlers.Handler, 'delete_service'):
            self.call('delete', '/v1/databases/' + self.service.id, status=202)
        current = objects.Service.get_by_id(self.ctx, self.service.id)
        with mock.patch.object(backup_storage, 'proxy_for',
                               return_value=proxy):
            endpoint.execute(self.ctx, 'delete_service', current, {})
        self.call('get', '/v1/databases/' + self.service.id, status=404)
        self._assert_backup_storage_deleted(row, proxy, names)
        self.assertNotIn(current.password_ref, self.store.values)

    def test_stack_delete_removes_backup_objects_container_and_key(self):
        row = self.activate()
        proxy, names = self._backup_storage_for_delete(row)
        self.config.config(environments=10, servers=10, apps=10, domains=10,
                           group='quotas')
        held = quota.reserve(
            self.ctx, environments=1, servers=1, apps=1, domains=1)
        quota.commit(self.ctx, held)
        self.stack.driver = 'coolify_v4'
        self.stack.engine_token_ref = self.store.put(self.ctx, 'test-token')
        self.stack.save()
        endpoint = manager.Manager(self.conf)
        self.addCleanup(endpoint.close)
        block = mock.Mock()
        block.snapshots.return_value = []
        block.volumes.return_value = []
        clients = SimpleNamespace(
            manager=endpoint, options={}, notifier=mock.Mock(),
            driver=mock.Mock(), endpoint=object(), block=block,
            cloud=mock.Mock(), dns=mock.Mock(), floating=mock.Mock(),
            network=mock.Mock(), secrets=self.store)
        # Keep workload rows to exercise the entire teardown cleanup. As in
        # the teardown flow tests, bypass only the app-group admission guard.
        with mock.patch.object(stack_teardown.stack_delete, 'require_empty'), \
                mock.patch.object(stack_teardown, 'Clients',
                                  return_value=clients), \
                mock.patch.object(backup_storage, 'proxy_for',
                                  return_value=proxy):
            with mock.patch.object(handlers.Handler, 'delete_stack'):
                self.call('delete', '/v1/stacks/' + self.stack.id, status=202)
            current = objects.Stack.get_by_id(self.ctx, self.stack.id)
            endpoint.execute(self.ctx, 'delete_stack', current, {})
        deleted = objects.Stack.get_by_id(
            self.ctx, self.stack.id, show_deleted=True)
        self.assertEqual(states.DELETED, deleted.status, deleted.fault)
        self._assert_backup_storage_deleted(row, proxy, names)
        self.assertNotIn(self.service.password_ref, self.store.values)
        clients.driver.delete_app.assert_called_once()
        self.assertEqual(self.service.id,
                         clients.driver.delete_app.call_args.args[-1].id)

    def test_database_microversion_on_nested_and_shared_routes(self):
        row = self.activate()
        for path in (self.policy_path, self.backups_path,
                     '/v1/backups/' + row.id):
            self.request('get', path, version='1.28', status=404)

    def test_agent_dispatch_uses_database_ownership_and_copy_container(self):
        row = self.activate()
        with mock.patch('coast.api.handlers.Handler.create_service'):
            response = self.call('post', '/v1/backups/' + row.id + '/restore',
                                 {'name': 'recovered'}, status=202)
        job = objects.BackupJob.get_by_id(
            self.ctx, response.json['restore']['id'])
        target = objects.Service.get_by_id(self.ctx, job.service_id)
        target.release_task(states.PROVISIONING)
        target.engine_ref = 'copy-engine-uuid'
        target.status = states.ACTIVE
        target.save()
        with mock.patch.object(backup_storage, 'SwiftStorage'), \
                mock.patch.object(backups.agent_client,
                                  'AgentClient') as agent:
            agent.return_value.request.return_value = {'duration_seconds': 1}
            backups.run_job(self.conf, self.ctx, job.id)
        command = agent.return_value.request.call_args.args[2]
        self.assertEqual('database', command['resource_type'])
        self.assertEqual(target.engine_ref, command['container'])
        self.assertEqual(target.engine_ref, command['target'])
        self.assertEqual(states.ACTIVE, objects.BackupJob.get_by_id(
            self.ctx, job.id).status)
