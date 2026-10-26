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

import datetime

import fixtures
from oslo_versionedobjects import fixture as object_fixture

from coast.common import context
from coast.common import exception
from coast.common import states
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast import objects
from coast.objects import base as object_base
from coast.tests import base


# Changes to fields or remotable methods require a version bump.
OBJECT_HASHES = {
    'Backup': '1.0-a26ba831828123e15d4b773409d1178f',
    'BackupAccount': '1.0-6174382d855f298d5f4e23983ccf1307',
    'BackupAccountList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'BackupJob': '1.0-b60199b3d34fd4b2a160a706dd241b80',
    'BackupJobList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'BackupList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'BackupPolicy': '1.0-a950fce789a3c1c9dc5c5069bd1e8745',
    'BackupPolicyList': '1.0-9f1c4d988694411e28105fd5d4f6090f',

    'Appgroup': '1.0-272e8a040c225bc734cdcc595038f885',
    'AppgroupList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Environment': '1.0-caabc07cb45bd7846c8341bc0613d851',
    'EnvironmentList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Service': '1.18-be8ced1851710238679e6552d68b61c1',
    'FileMount': '1.0-466bd6612ee7cddfdb308d1f5939d691',
    'FileMountList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'ServiceList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'AppVolumeMount': '1.0-88f15b0fe07110922167b8498ce06f6d',
    'AppVolumeMountList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Deployment': '1.2-76a19a3405b2affc8646c3b492d7906c',
    'DeploymentList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Domain': '1.2-22805eee29c1ed0cfb09b5e2a8eabe26',
    'DomainList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Stack': '1.7-4f061f2f26e8adb6c4d2b5dfcff0b312',
    'StackList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Event': '1.1-4d7d046b1af857b170888dbcf0951224',
    'EventList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'ConsoleToken': '1.0-cdac9c9eb631dc8c51ecc35dd5b06e94',  # gitleaks:allow
    'ConsoleTokenList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'StackDeleteSnapshot': '1.1-834412ff56f7f6b7ba7eb416b7eaab00',
    'StackDeleteSnapshotList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Export': '1.0-2d8a0151a9c71f8b5fd5cc25c9beb08d',
    'ExportList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Quota': '1.1-153858f23b5559dc876e668d260fb6d7',
    'QuotaUsage': '1.1-050a7268080a895febe36e87f23df460',
    'QuotaList': '1.0-15ecf022a68ddbb8c2a6739cfc9f8f5e',
    'QuotaUsageList': '1.0-15ecf022a68ddbb8c2a6739cfc9f8f5e',
    'Secret': '1.0-f0b273037b5ab10de6d21b40777ce327',
    'SecretList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Server': '1.2-27cc4a1dca5c606c3815dea21336d4c2',
    'ServerList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Source': '1.2-1e53f5f313a4a0873dd2c3251f8820b0',
    'SourceList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Volume': '1.0-27d1c514b9f673f06a6ffb63e81fbb8a',
    'VolumeList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
    'Storage': '1.1-5e29be78cc7c57b852b1100004eb1f2b',
    'StorageList': '1.0-9f1c4d988694411e28105fd5d4f6090f',
}


RESOURCE_NAMES = (
    'Stack', 'Appgroup', 'Environment', 'Server', 'Volume', 'Source',
    'Service', 'Deployment', 'Domain', 'Secret', 'AppVolumeMount',
    'Export', 'Event', 'StackDeleteSnapshot', 'ConsoleToken', 'Storage',
    'FileMount',
    'Quota', 'QuotaUsage')


class ObjectTest(base.TestCase):

    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(project_id='project')
        self.other = context.RequestContext(project_id='other')
        self.admin = context.RequestContext(project_id='project',
                                            is_admin=True)
        self.rows = self._resources(self.ctx)

    def _env(self, ctx, name='env'):
        return api.stacks.create(ctx, dict(
            name=name, driver='noop', engine_version='test', plan='test',
            status='PENDING'))

    def _server(self, ctx, env, role='worker', name='server'):
        return api.servers.create(ctx, dict(
            stack_id=env.id, name=name, kind='vm', role=role,
            status='PENDING', task_state='NONE'))

    def _service(self, ctx, env, source, name='service', server=None):
        return api.services.create(ctx, dict(
            stack_id=env.id, source_id=source.id, name=name,
            server_id=server.id if server else None, ports='8080',
            auto_deploy=False, status='PENDING', task_state='NONE'))

    def _deployment(self, ctx, service):
        return api.deployments.create(ctx, dict(
            service_id=service.id, spec={'ports': '8080'}, trigger='api',
            status='PENDING'))

    def _appgroup(self, ctx, env, name='website'):
        return api.appgroups.create(ctx, dict(
            stack_id=env.id, name=name, status='PENDING',
            task_state='NONE'))

    def _environment(self, ctx, appgroup, name='production'):
        return api.environments.create(ctx, dict(
            appgroup_id=appgroup.id, name=name, status='PENDING',
            task_state='NONE'))

    def _resources(self, ctx):
        env = self._env(ctx)
        appgroup = self._appgroup(ctx, env)
        app_environment = self._environment(ctx, appgroup)
        server = self._server(ctx, env)
        volume = api.volumes.create(ctx, dict(
            stack_id=env.id, server_id=server.id, name='data', size_gb=1,
            purpose='data', status='CREATING'))
        source = api.sources.create(ctx, dict(type='git'))
        service = self._service(ctx, env, source, server=server)
        deployment = self._deployment(ctx, service)
        domain = api.domains.create(ctx, dict(
            service_id=service.id, fqdn=ctx.project_id + '.example.com',
            verification_token='test', tls='letsencrypt', auto_dns=False,
            dns_mode='manual', status='PENDING_VERIFICATION'))
        secret = api.secrets.create(ctx, dict(
            service_id=service.id, key='KEY', is_secret=True,
            value_ref='opaque-ref',
            build_time=False, version=1))
        mount = api.service_volume_mounts.create(ctx, dict(
            service_id=service.id, volume_id=volume.id, sub_path='uploads',
            container_path='/uploads', read_only=False))
        export = api.exports.create(ctx, dict(
            stack_id=env.id, status='PENDING', volume_ids=[volume.id],
            backend='swift'))
        event = api.events.create(ctx, dict(
            stack_id=env.id, resource_type='service', resource_id=service.id,
            action='create', outcome='start'))
        delete_snapshot = api.stack_delete_snapshots.create(ctx, dict(
            stack_id=env.id, volume_id=volume.id, cinder_volume_id='cinder',
            cinder_snapshot_id='snap', status='creating'))
        storage = api.storages.create(ctx, dict(
            stack_id=env.id, name='uploads', size_gb=1,
            status='AVAILABLE', origin='api'))
        token = api.console_tokens.create(ctx, dict(
            token_hash=(ctx.project_id + ('0' * 64))[:64],
            stack_id=env.id, resource_type='app', resource_id=service.id,
            user_id='user', expires_at=datetime.datetime(2026, 1, 2)))
        file_mount = api.file_mounts.create(ctx, dict(
            service_id=service.id, container_path='/etc/site.conf',
            content_ref='opaque-ref'))
        return dict(zip((
            api.stacks, api.appgroups, api.environments, api.servers,
            api.volumes, api.sources, api.services, api.deployments,
            api.domains, api.secrets, api.service_volume_mounts,
            api.exports, api.events, api.stack_delete_snapshots,
            api.console_tokens, api.storages, api.file_mounts), (
            env, appgroup, app_environment, server, volume, source,
            service, deployment, domain, secret, mount, export, event,
            delete_snapshot, token, storage, file_mount)))

    def test_quota_1_0_primitive_omits_storage_gb(self):
        quota = objects.Quota(storage_gb=50)
        old = quota.obj_to_primitive(target_version='1.0')
        self.assertNotIn('storage_gb', old['coast_object.data'])
        self.assertEqual('1.0', old['coast_object.version'])
        usage = objects.QuotaUsage(in_use_storage_gb=1,
                                   reserved_storage_gb=2)
        old = usage.obj_to_primitive(target_version='1.0')
        self.assertNotIn('in_use_storage_gb', old['coast_object.data'])
        self.assertNotIn('reserved_storage_gb', old['coast_object.data'])

    def test_snapshot_1_0_primitive_omits_retention(self):
        snapshot = objects.StackDeleteSnapshot(
            retention_days=7, expires_at=datetime.datetime.now(
                datetime.timezone.utc))
        old = snapshot.obj_to_primitive(target_version='1.0')
        self.assertNotIn('retention_days', old['coast_object.data'])
        self.assertNotIn('expires_at', old['coast_object.data'])

    def test_stack_1_0_primitive_omits_secret_refs(self):
        stack = objects.Stack.get_by_id(self.ctx, self.rows[api.stacks].id)
        stack.heartbeat_key_ref = 'hmac-ref'
        stack.save()
        stack.registry_secret_ref = 'registry-ref'
        stack.save()
        current = stack.obj_to_primitive()
        self.assertEqual('1.7', current['coast_object.version'])
        self.assertEqual(
            'hmac-ref', current['coast_object.data']['heartbeat_key_ref'])
        self.assertEqual(
            'registry-ref',
            current['coast_object.data']['registry_secret_ref'])
        old = stack.obj_to_primitive(target_version='1.0')
        self.assertNotIn('heartbeat_key_ref', old['coast_object.data'])
        self.assertNotIn('engine_backup_ref', old['coast_object.data'])
        self.assertNotIn('registry_secret_ref', old['coast_object.data'])
        previous = stack.obj_to_primitive(target_version='1.2')
        self.assertNotIn('registry_secret_ref', previous['coast_object.data'])
        self.assertIn('conductor_host', previous['coast_object.data'])

    def test_stack_host_compatibility_and_task_release(self):
        stack = objects.Stack.get_by_id(self.ctx, self.rows[api.stacks].id)
        stack.acquire_task('PROVISIONING')
        stack.conductor_host = 'conductor-host'
        stack.save()
        for version in ('1.0', '1.1'):
            primitive = stack.obj_to_primitive(target_version=version)
            self.assertNotIn('conductor_host', primitive['coast_object.data'])
        stack.release_task('PROVISIONING')
        self.assertIsNone(stack.conductor_host)
        self.assertIsNone(objects.Stack.get_by_id(
            self.ctx, stack.id).conductor_host)

    def test_server_1_0_primitive_omits_deleted_at(self):
        server = objects.Server.get_by_id(
            self.ctx, self.rows[api.servers].id)
        current = server.obj_to_primitive()
        self.assertEqual('1.2', current['coast_object.version'])
        self.assertIn('deleted_at', current['coast_object.data'])
        old = server.obj_to_primitive(target_version='1.0')
        self.assertEqual('1.0', old['coast_object.version'])
        self.assertNotIn('deleted_at', old['coast_object.data'])

    def test_runtime_fields_and_null_port_wire_compatibility(self):
        service = objects.Service.get_by_id(
            self.ctx, self.rows[api.services].id)
        service.ports = None
        service.reconcile_fault = 'Container failed.'
        service.fault = 'The database could not be started.'
        service.dockerfile_ref = 'https://barbican.example.com/secrets/content'
        service.save()
        current = service.obj_to_primitive()['coast_object.data']
        self.assertEqual(service.fault, current['fault'])
        self.assertEqual(service.dockerfile_ref, current['dockerfile_ref'])
        self.assertNotIn('dockerfile_ref', service.obj_to_primitive(
            target_version='1.17')['coast_object.data'])
        self.assertNotIn('fault', service.obj_to_primitive(
            target_version='1.16')['coast_object.data'])
        self.assertIn('unhealthy_since', current)
        self.assertIn('running_since', current)
        previous = service.obj_to_primitive(target_version='1.8')[
            'coast_object.data']
        self.assertNotIn('unhealthy_since', previous)
        self.assertNotIn('running_since', previous)
        self.assertIsNone(current['ports'])
        self.assertEqual('Container failed.', current['reconcile_fault'])
        old = service.obj_to_primitive(target_version='1.7')[
            'coast_object.data']
        self.assertEqual('', old['ports'])
        self.assertNotIn('reconcile_fault', old)
        deployment = objects.Deployment.get_by_id(
            self.ctx, self.rows[api.deployments].id)
        deployment.runtime_log = 'Captured failure'
        deployment.save()
        self.assertEqual('Captured failure', deployment.obj_to_primitive()[
            'coast_object.data']['runtime_log'])
        self.assertNotIn('runtime_log', deployment.obj_to_primitive(
            target_version='1.1')['coast_object.data'])

    def test_source_1_1_omits_rotation_checkpoint(self):
        source = objects.Source(context=self.ctx, type='git',
                                deploy_public_key='public',
                                deploy_key_rotation={'reference': 'opaque'})
        primitive = source.obj_to_primitive(target_version='1.1')
        self.assertNotIn('deploy_key_rotation',
                         primitive['coast_object.data'])
        self.assertEqual('public', primitive['coast_object.data'][
            'deploy_public_key'])

    def test_fingerprints(self):
        checker = object_fixture.ObjectVersionChecker(
            object_base.CoastObjectRegistry.obj_classes())
        expected, actual = checker.test_hashes(OBJECT_HASHES)
        self.assertEqual(expected, actual)

    def test_deployment_resolved_ref_compatibility_and_persistence(self):
        obj = objects.Deployment.get_by_id(
            self.ctx, self.rows[api.deployments].id)
        self.assertIsNone(obj.resolved_ref)
        for reference in ('a' * 40, 'sha256:' + 'b' * 64):
            obj.resolved_ref = reference
            obj.save()
            obj = objects.Deployment.get_by_id(self.ctx, obj.id)
            self.assertEqual(reference, obj.resolved_ref)
            current = obj.obj_to_primitive()
            self.assertEqual(reference, current['coast_object.data'][
                'resolved_ref'])
            legacy = obj.obj_to_primitive(target_version='1.0')
            self.assertNotIn('resolved_ref', legacy['coast_object.data'])
            self.assertEqual('1.0', legacy['coast_object.version'])

    def test_primary_address_object_compatibility(self):
        for cls, field, old in ((objects.Service, 'default_domain_enabled',
                                 '1.13'), (objects.Domain, 'primary', '1.1')):
            row = cls.get_by_id(self.ctx, self.rows[cls.repository].id)
            primitive = row.obj_to_primitive(target_version=old)
            self.assertNotIn(field, primitive['coast_object.data'])

    def test_all_model_fields_round_trip(self):
        for name in RESOURCE_NAMES:
            cls = getattr(objects, name)
            model = getattr(models, name)
            if name in ('Quota', 'QuotaUsage'):
                with api.writer.using(self.ctx):
                    if name == 'Quota':
                        row = model(project_id=self.ctx.project_id,
                                    environments=-1)
                        self.ctx.session.add(row)
                        self.ctx.session.flush()
                    else:
                        row = self.ctx.session.query(model).one()
                    obj = cls.obj_from_db(self.ctx, row)
            else:
                row = self.rows[cls.repository]
                obj = cls.get_by_id(self.ctx, row.id)
            with self.subTest(resource=name):
                self.assertEqual(set(model.__table__.columns.keys()),
                                 set(cls.fields))
                if name == 'Service':
                    version = '1.18'
                elif name == 'Domain':
                    version = '1.2'
                elif name == 'Deployment':
                    version = '1.2'
                elif name == 'Stack':
                    version = '1.7'
                elif name == 'Server':
                    version = '1.2'
                elif name == 'Source':
                    version = '1.2'
                elif name in ('StackDeleteSnapshot',
                              'Event', 'Quota', 'QuotaUsage', 'Storage'):
                    version = '1.1'
                else:
                    version = '1.0'
                self.assertEqual(version, cls.VERSION)
                self.assertEqual(set(), obj.obj_what_changed())
                for column in model.__table__.columns:
                    actual = getattr(obj, column.name)
                    if isinstance(actual, datetime.datetime):
                        actual = actual.replace(tzinfo=None)
                    self.assertEqual(getattr(row, column.name), actual)
                serializer = object_base.CoastObjectSerializer()
                restored = serializer.deserialize_entity(
                    self.ctx, serializer.serialize_entity(self.ctx, obj))
                self.assertIsInstance(restored, cls)
                self.assertIs(self.ctx, restored._context)
                self.assertEqual(obj.obj_to_primitive(),
                                 restored.obj_to_primitive())

    def test_create_save_and_destroy_every_repository_object(self):
        # Delete leaves dependent fixtures intact for soft-deleted rows.
        for name in RESOURCE_NAMES[:-2]:
            cls = getattr(objects, name)
            row = self.rows[cls.repository]
            values = {c.name: getattr(row, c.name)
                      for c in cls.repository.model.__table__.columns
                      if c.name not in ('id', 'created_at', 'updated_at',
                                        'deleted_at', 'number')}
            if name in ('Stack', 'Appgroup', 'Environment', 'Server',
                        'Volume', 'Service', 'Storage'):
                values['name'] += '-copy'
            elif name == 'Domain':
                values['fqdn'] = 'copy.example.com'
            elif name == 'Secret':
                values['key'] += '_COPY'
            elif name in ('AppVolumeMount', 'FileMount'):
                values['container_path'] += '-copy'
            elif name == 'StackDeleteSnapshot':
                values['volume_id'] = 'copy-volume'
                values['cinder_snapshot_id'] = 'copy-snap'
            elif name == 'ConsoleToken':
                values['token_hash'] = 'c' * 64
            with self.subTest(resource=name):
                obj = cls(context=self.ctx, **values)
                obj.create()
                self.assertEqual(set(), obj.obj_what_changed())
                if name == 'StackDeleteSnapshot':
                    obj.status = 'available'
                elif 'status' in cls.fields:
                    obj.status = next(iter(sorted(states.TRANSITIONS[
                        states.kind(cls.repository.resource)][obj.status])))
                elif name == 'Source':
                    obj.branch = 'release'
                elif name == 'Secret':
                    obj.value_ref = 'new-ref'
                elif name == 'AppVolumeMount':
                    obj.read_only = True
                elif name == 'FileMount':
                    obj.content_ref = 'new-ref'
                elif name == 'ConsoleToken':
                    obj.user_id = 'copy-user'
                else:
                    obj.message = 'updated'
                obj.save()
                self.assertEqual(set(), obj.obj_what_changed())
                self.assertEqual(obj.obj_to_primitive(),
                                 cls.get_by_id(self.ctx, obj.id).
                                 obj_to_primitive())
                obj.destroy()
                self.assertRaises(exception.NotFound, cls.get_by_id,
                                  self.ctx, obj.id)
                if 'deleted_at' in cls.fields:
                    self.assertIsNotNone(obj.deleted_at)
                    self.assertIsNotNone(cls.get_by_id(
                        self.ctx, obj.id, show_deleted=True).deleted_at)

    def test_status_and_task_validation(self):
        for name in RESOURCE_NAMES:
            cls = getattr(objects, name)
            if name == 'StackDeleteSnapshot':
                obj = cls()
                obj.status = 'available'
                self.assertEqual('available', obj.status)
                continue
            if 'status' in cls.fields:
                obj = cls()
                for status in states.TRANSITIONS[states.kind(
                        cls.repository.resource)]:
                    obj.status = status
                    self.assertEqual(status, obj.status)
                for invalid in ('UNKNOWN', states.NONE, None):
                    self.assertRaises(ValueError, setattr, obj,
                                      'status', invalid)
            if 'task_state' in cls.fields:
                obj = cls()
                for task in states.TASK_STATES:
                    obj.task_state = task
                self.assertRaises(ValueError, setattr, obj, 'task_state',
                                  'UNKNOWN')

    def test_save_uses_persisted_status_not_stale_object(self):
        for name in ('Stack', 'Server', 'Volume', 'Service', 'Deployment',
                     'Domain', 'Export'):
            cls = getattr(objects, name)
            obj = cls.get_by_id(self.ctx, self.rows[cls.repository].id)
            original = obj.status
            if name == 'Domain':
                original = states.VERIFIED
            obj.status = (states.FAILED if name == 'Deployment' else
                          states.ERROR)
            obj.save()
            obj.status = original
            with self.subTest(resource=name):
                self.assertRaises(exception.InvalidStateTransition, obj.save)
                self.assertIn('status', obj.obj_what_changed())

    def test_cas_ownership_conflicts_and_pending_edits(self):
        for name in ('Stack', 'Server', 'Service'):
            cls = getattr(objects, name)
            obj = cls.get_by_id(self.ctx, self.rows[cls.repository].id)
            stale = cls.get_by_id(self.ctx, obj.id)
            obj.acquire_task(states.PROVISIONING)
            self.assertEqual(states.PROVISIONING, obj.task_state)
            self.assertEqual(set(), obj.obj_what_changed())
            self.assertRaises(exception.Conflict, stale.acquire_task,
                              states.DEPLOYING)
            self.assertRaises(exception.Conflict, obj.release_task,
                              states.DEPLOYING)
            obj.release_task(states.PROVISIONING)
            self.assertEqual(states.NONE, obj.task_state)
            obj.name = 'unsaved'
            self.assertRaises(exception.Invalid, obj.acquire_task,
                              states.PROVISIONING)
            self.assertEqual('unsaved', obj.name)
            obj.save()
            obj.task_state = states.DEPLOYING
            self.assertRaises(exception.Invalid, obj.save)

    def test_isolation_and_explicit_admin_scope(self):
        for name in RESOURCE_NAMES[:-2]:
            cls = getattr(objects, name)
            row = self.rows[cls.repository]
            list_cls = getattr(objects, name + 'List')
            with self.subTest(resource=name):
                self.assertRaises(exception.NotFound, cls.get_by_id,
                                  self.other, row.id)
                self.assertEqual([], list(list_cls.get_all(self.other)))
                foreign = cls.obj_from_db(self.other, row)
                self.assertRaises(exception.NotFound, foreign.save)
                self.assertRaises(exception.NotFound, foreign.destroy)
                self.assertRaises(exception.NotAuthorized, cls.get_by_id,
                                  self.other, row.id, all_projects=True)
                admin = context.RequestContext(project_id='other',
                                               is_admin=True)
                self.assertRaises(exception.NotFound, cls.get_by_id,
                                  admin, row.id)
                obj = cls.get_by_id(admin, row.id, all_projects=True)
                obj.save(all_projects=True)
                page = list_cls.get_all(admin, all_projects=True, limit=1,
                                        sort_key='id')
                self.assertEqual(row.id, page[0].id)
                self.assertEqual([], list(list_cls.get_all(
                    admin, all_projects=True, marker=row.id, sort_key='id')))
                serializer = object_base.CoastObjectSerializer()
                restored = serializer.deserialize_entity(
                    self.ctx, page.obj_to_primitive())
                self.assertIsInstance(restored, list_cls)
                self.assertEqual(page.obj_to_primitive(),
                                 restored.obj_to_primitive())
                if 'task_state' in cls.fields:
                    self.assertRaises(exception.NotFound,
                                      foreign.acquire_task,
                                      states.PROVISIONING)
                    obj.acquire_task(states.PROVISIONING, all_projects=True)
                    obj.release_task(states.PROVISIONING, all_projects=True)

    def test_references_are_masked_in_objects_and_lists(self):
        for name in RESOURCE_NAMES:
            cls = getattr(objects, name)
            obj = cls()
            for field in cls.fields:
                if field.endswith('_ref'):
                    setattr(obj, field, 'sensitive-' + field)
                    self.assertNotIn('sensitive-', repr(obj))
                    self.assertNotIn('sensitive-', str(obj))
                    self.assertIn('***', repr(obj))
            if name not in ('Quota', 'QuotaUsage'):
                list_cls = getattr(objects, name + 'List')
                self.assertNotIn('sensitive-', repr(list_cls(objects=[obj])))
        obj = objects.Deployment(spec={
            'env': [{'value_ref': 'nested-sensitive'}]})
        self.assertNotIn('nested-sensitive', repr(obj))
        self.assertIn('nested-sensitive', str(obj.obj_to_primitive()))
        self.assertNotIn('value', objects.Secret.fields)

    def test_nested_json_round_trip_and_change_tracking(self):
        obj = objects.Deployment.get_by_id(
            self.ctx, self.rows[api.deployments].id)
        nested = {'env': [{'name': 'KEY', 'value_ref': 'opaque-ref',
                           'version': 2}], 'limits': {'cpu': 0.5},
                  'enabled': True, 'optional': None}
        created = objects.Deployment(
            context=self.ctx, service_id=obj.service_id, spec=nested,
            trigger='api', status=states.PENDING)
        created.create()
        self.assertEqual(nested, created.spec)
        serializer = object_base.CoastObjectSerializer()
        restored = serializer.deserialize_entity(
            self.ctx, serializer.serialize_entity(self.ctx, created))
        self.assertEqual(nested, restored.spec)
        restored.spec = {'changed': True}
        self.assertRaises(exception.Invalid, restored.save)
        self.assertEqual(nested, objects.Deployment.get_by_id(
            self.ctx, created.id).spec)
        export = objects.Export.get_by_id(
            self.ctx, self.rows[api.exports].id)
        export.artifacts = [{'size': 42, 'sha256': 'digest'}]
        export.save()
        self.assertEqual(export.artifacts, objects.Export.get_by_id(
            self.ctx, export.id).artifacts)
        self.assertRaises(ValueError, objects.Deployment,
                          spec={'bad': object()})

    def test_stale_status_cannot_bypass_database_transition_check(self):
        obj = objects.Stack.get_by_id(
            self.ctx, self.rows[api.stacks].id)
        api.stacks.update(self.ctx, obj.id, {'status': states.ERROR})
        # Valid from the stale PENDING, but invalid from the persisted ERROR.
        obj.status = states.BUILDING
        self.assertRaises(exception.InvalidStateTransition, obj.save)
        persisted = objects.Stack.get_by_id(self.ctx, obj.id)
        self.assertEqual(states.ERROR, persisted.status)

    def test_quota_lists_serialize(self):
        serializer = object_base.CoastObjectSerializer()
        for name in ('Quota', 'QuotaUsage'):
            cls = getattr(objects, name)
            list_cls = getattr(objects, name + 'List')
            obj = list_cls(objects=[cls(project_id=self.ctx.project_id)])
            restored = serializer.deserialize_entity(
                self.ctx, serializer.serialize_entity(self.ctx, obj))
            self.assertEqual(self.ctx.project_id, restored[0].project_id)
            self.assertEqual(1, len(list(restored)))

    def test_in_place_json_edits_are_persisted_or_rejected(self):
        export = objects.Export.get_by_id(
            self.ctx, self.rows[api.exports].id)
        export.artifacts = [{'size': 42, 'sha256': 'digest'}]
        export.save()
        export.artifacts[0]['size'] = 43
        export.artifacts.append({'size': 7, 'sha256': 'second'})
        self.assertEqual({'artifacts'}, export.obj_what_changed())
        export.save()
        self.assertEqual(set(), export.obj_what_changed())
        self.assertEqual(export.artifacts, objects.Export.get_by_id(
            self.ctx, export.id).artifacts)
        deployment = objects.Deployment.get_by_id(
            self.ctx, self.rows[api.deployments].id)
        deployment.spec['ports'] = '9090'
        self.assertEqual({'spec'}, deployment.obj_what_changed())
        self.assertRaises(exception.Invalid, deployment.save)
        self.assertEqual({'ports': '8080'}, objects.Deployment.get_by_id(
            self.ctx, deployment.id).spec)

    def test_json_change_tracking_survives_serialization_and_clone(self):
        serializer = object_base.CoastObjectSerializer()
        export = objects.Export.get_by_id(
            self.ctx, self.rows[api.exports].id)
        export.artifacts = [{'size': 42}]
        export.save()
        restored = serializer.deserialize_entity(
            self.ctx, serializer.serialize_entity(self.ctx, export))
        restored.artifacts[0]['size'] = 43
        self.assertEqual({'artifacts'}, restored.obj_what_changed())
        cloned = restored.obj_clone()
        cloned.artifacts[0]['size'] = 44
        self.assertEqual(43, restored.artifacts[0]['size'])
        dirty = serializer.deserialize_entity(
            self.ctx, serializer.serialize_entity(self.ctx, restored))
        self.assertEqual({'artifacts'}, dirty.obj_what_changed())
        dirty.save()
        self.assertEqual(43, objects.Export.get_by_id(
            self.ctx, export.id).artifacts[0]['size'])

    def test_selective_json_reset_does_not_clear_other_edits(self):
        export = objects.Export(artifacts=[{'size': 42}], volume_ids=['one'])
        export.obj_reset_changes()
        export.artifacts[0]['size'] = 43
        export.volume_ids.append('two')
        self.assertEqual({'artifacts', 'volume_ids'},
                         export.obj_what_changed())
        export.obj_reset_changes(fields=['artifacts'])
        self.assertEqual({'volume_ids'}, export.obj_what_changed())
        export.obj_reset_changes(recursive=True)
        self.assertEqual(set(), export.obj_what_changed())

    def test_engine_rollback_metadata_roundtrip_and_compatibility(self):
        stack = objects.Stack.get_by_id(self.ctx, self.rows[api.stacks].id)
        stack.previous_image_id = 'previous-image'
        stack.previous_engine_version = '4.4.3'
        stack.upgrade_snapshots = {'data': 'snapshot-id'}
        stack.upgrade_checkpoint = {'snapshots': {'data': 'pending-snapshot'}}
        stack.save()
        loaded = objects.Stack.get_by_id(self.ctx, stack.id)
        self.assertEqual(stack.upgrade_checkpoint, loaded.upgrade_checkpoint)
        self.assertEqual(stack.upgrade_snapshots, loaded.upgrade_snapshots)
        serialized = loaded.obj_to_primitive(target_version='1.3')
        for field in ('previous_image_id', 'previous_engine_version',
                      'upgrade_snapshots', 'upgrade_checkpoint'):
            self.assertNotIn(field, serialized['coast_object.data'])
        restored = object_base.CoastObject.obj_from_primitive(
            loaded.obj_to_primitive(), self.ctx)
        self.assertEqual(loaded.upgrade_checkpoint,
                         restored.upgrade_checkpoint)

    def test_health_check_compatibility(self):
        obj = objects.Service.get_by_id(self.ctx, self.rows[api.services].id)
        self.assertEqual('http', obj.health_check)
        obj.health_check = 'none'
        obj.health_check_port = 8080
        obj.save()
        self.assertEqual('none', objects.Service.get_by_id(
            self.ctx, obj.id).health_check)
        old = obj.obj_to_primitive(target_version='1.6')['coast_object.data']
        self.assertNotIn('health_check', old)
        self.assertNotIn('health_check_port', old)
