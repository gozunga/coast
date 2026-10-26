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

"""Service resource validation, isolation, state, and secret tests."""

from unittest import mock

import fixtures
from oslo_utils import timeutils

from coast.api import handlers
from coast.api import secret_store
from coast.api.v1 import service_controllers
from coast.common import context
from coast.common import exception
from coast.common import states
from coast.db import api as db_api
from coast import objects
from coast.tests import app_api


class AppControllerTest(app_api.AppAPITest):
    def assert_stack_before_service(self, method, path, body=None,
                                    status=202):
        locks = []
        stack_get = db_api.stacks._get
        cas = db_api.cas_task_state
        lock_query = db_api._lock_query

        def stack(*args, **kwargs):
            if kwargs.get('lock'):
                locks.append('stack')
            return stack_get(*args, **kwargs)

        def task(ctx, model, *args, **kwargs):
            if model is objects.Service.repository.model:
                locks.append('service')
            return cas(ctx, model, *args, **kwargs)

        def query(session, statement, model):
            if model is objects.Service.repository.model:
                locks.append('service')
            return lock_query(session, statement, model)

        with mock.patch.object(db_api.stacks, '_get', side_effect=stack), \
                mock.patch.object(db_api, 'cas_task_state',
                                  side_effect=task), \
                mock.patch.object(db_api, '_lock_query', side_effect=query):
            self.request(method, path, body, status=status)
        self.assertIn('service', locks)
        self.assertIn('stack', locks)
        self.assertLess(locks.index('stack'), locks.index('service'), locks)

    def test_rename_still_rejects_duplicate_name(self):
        sibling = objects.Service(
            context=self.ctx, stack_id=self.stack.id, name='sibling',
            source_id=self.source.id, server_id=self.server.id,
            ports='3000', auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE)
        sibling.create()
        self.request('patch', self.path,
                     {'service': {'name': sibling.name}}, status=409)
        self.assertEqual('web', objects.Service.get_by_id(
            self.ctx, self.service.id).name)

    def test_delete_locks_stack_before_service(self):
        self.assert_stack_before_service('delete', self.path)

    def test_delete_inflight_locks_stack_before_service(self):
        self.service.acquire_task(states.DEPLOYING)
        self.assert_stack_before_service('delete', self.path)

    def test_actions_lock_stack_before_service(self):
        self.assert_stack_before_service('post', self.path + '/action',
                                         {'stop': {}})

    def test_env_locks_stack_before_service(self):
        self.assert_stack_before_service('put', self.path + '/env',
                                         {'env': {'KEY': {'value': 'value'}}},
                                         status=200)

    def test_template_env_locks_stack_before_service(self):
        self.service.template_id = 'database'
        self.service.engine_ref = 'engine-app'
        self.service.save()
        self.assert_stack_before_service('patch', self.path + '/env',
                                         {'env': {'KEY': {'value': 'value'}}},
                                         status=200)

    def test_domain_delete_locks_stack_before_service(self):
        self.assert_stack_before_service('delete', self.domain_path)

    def endpoints(self):
        return [
            ('get', '/v1/services', None),
            ('post', '/v1/services', self.create_body()),
            ('get', self.path, None),
            ('patch', self.path, {'service': {'name': 'renamed'}}),
            ('delete', self.path, None),
            *[('post', self.path + '/action', {action: {}})
              for action in ('start', 'stop', 'restart')],
            ('get', self.path + '/deployments', None),
            ('post', self.path + '/deployments',
             {'deployment': {'source_ref': 'main'}}),
            ('get', self.dep_path, None),
            *[('post', self.dep_path + '/action', {action: {}})
              for action in ('rollback', 'cancel')],
            ('get', self.dep_path + '/log', None),
            ('get', self.path + '/logs', None),
            ('get', self.path + '/domains', None),
            ('post', self.path + '/domains',
             {'domain': {'fqdn': 'new.example.com'}}),
            ('get', self.domain_path, None),
            ('delete', self.domain_path, None),
            ('post', self.domain_path + '/action', {'verify': {}}),
            ('get', self.path + '/env', None),
            ('put', self.path + '/env', {'env': {}}),
            ('patch', self.path + '/env', {'env': {}}),
            ('get', '/v1/events', None)]

    def test_authentication_role_and_microversion_every_endpoint(self):
        for method, path, body in self.endpoints():
            self.request(method, path, body, headers={}, status=401)
            self.request(method, path, body, headers=dict(
                self.headers, **{'X-Roles': 'unrelated'}), status=403)
            self.client.request(path, method=method.upper(), headers=dict(
                self.headers, **{'OpenStack-API-Version': 'app-hosting 1.99'}),
                status=406)

    def test_project_isolation_every_nested_endpoint(self):
        for roles in ('reader,member', 'admin'):
            foreign = dict(self.headers, **{
                'X-Project-Id': 'other', 'X-Roles': roles})
            for method, path, body in self.endpoints():
                if path.startswith(self.path):
                    self.request(method, path, body, headers=foreign,
                                 status=404)
            for path, key in (('/v1/services', 'services'),
                              ('/v1/events', 'events')):
                self.assertEqual([], self.request(
                    'get', path, headers=foreign).json[key])
        body = self.create_body()
        self.request('post', '/v1/services', body, headers=foreign, status=404)

    def test_reader_cannot_write_or_read_env(self):
        headers = dict(self.headers, **{'X-Roles': 'reader'})
        for method, path, body in self.endpoints():
            if method != 'get' or path.endswith('/env'):
                self.request(method, path, body,
                             headers=headers, status=403)

    def test_schema_errors_all_writes(self):
        for method, path, body in self.endpoints():
            if method != 'get':
                self.request(method, path, {'unknown': 'private-value'},
                             status=400)
        for change in ({'replicas': 2}, {'memory_limit_mb': True},
                       {'cpu_limit': float('nan')},
                       {'cpu_limit': float('inf')},
                       {'cpu_limit': 10 ** 400},
                       {'deploy': 'yes'}, {'source': {'type': 'image'}},
                       {'source': {'type': 'git', 'repo_url':
                                   'https://user:password@example.com/x.git'}},
                       {'source': {'type': 'git',
                                   'repo_url': 'https://example.com/x.git',
                                   'credentials': {'type': 'deploy_key'}}}):
            body = self.create_body()
            body['service'].update(change)
            result = self.request('post', '/v1/services', body, status=400)
            self.assertNotIn('password', result.text)
        for key in ('server_id', 'stack_id', 'appgroup_id',
                    'environment_id', 'template_id', 'env', 'project_id'):
            self.request('patch', self.path, {
                         'service': {key: 'x'}}, status=400)
        self.client.post('/v1/services', '{bad json', headers=self.headers,
                         content_type='application/json', status=400)

    def test_create_public_git_deployment_and_snapshot(self):
        result = self.request('post', '/v1/services', self.create_body(
            env={'PUBLIC': {'value': 'production'},
                 'PASSWORD': {'value': 'private-value', 'secret': True}}),
            status=202).json['service']
        service = objects.Service.get_by_id(self.ctx, result['id'])
        dep = objects.Deployment.get_by_id(self.ctx, result['deployment_id'])
        self.assertEqual(states.PENDING, service.status)
        self.assertEqual(states.PROVISIONING, service.task_state)
        self.assertEqual(states.PENDING, dep.status)
        self.assertEqual(1, dep.number)
        self.assertIn('PASSWORD', dep.spec['env'])
        self.assertNotIn('private-value', str(dep.spec))
        self.assertNotIn('production', str(dep.spec))
        self.assertIsNone(result['current_deployment_id'])
        self.assertIsNone(result['default_url'])
        self.assertNotIn('engine_ref', result)
        self.assertNotIn('task_state', result)
        self.assertNotIn('credentials_ref', result['source'])
        # Delete always wins: it cancels the in-flight deployment.
        self.request('delete', '/v1/services/' + service.id, status=202)
        self.assertEqual(states.CANCELLED, objects.Deployment.get_by_id(
            self.ctx, dep.id).status)
        self.assertEqual(states.DELETING, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        body = self.create_body(deploy=False)
        body['service']['name'] = 'second'
        result = self.request(
            'post', '/v1/services', body, status=202).json['service']
        self.assertNotIn('deployment_id', result)

    def test_create_placement_and_duplicate_name(self):
        body = self.create_body()
        body['service']['name'] = 'web'
        self.request('post', '/v1/services', body, status=409)
        self.server.role = 'build'
        self.server.save()
        self.request('post', '/v1/services', self.create_body(), status=400)
        self.server.role = 'worker'
        self.server.save()
        body = self.create_body()
        body['service']['name'] = 'explicit-stack'
        body['service']['stack_id'] = self.stack.id
        self.request('post', '/v1/services', body, status=400)

    def test_create_requires_worker(self):
        for value in (None, ''):
            body = self.create_body()
            body['service']['server_id'] = value
            self.assert_parent_rejected(body, 400)
            result = self.request('post', '/v1/services', body, status=400)
            if value is None:
                self.assertEqual(
                    "server_id is required: a service must be placed on a "
                    "worker server in the app group's stack.",
                    result.json['faultstring'])
        body = self.create_body()
        del body['service']['server_id']
        self.assert_parent_rejected(body, 400)
        # The worker check runs before parent lookups (no 404 first).
        body = self.create_body()
        body['service'].update(server_id=None, appgroup_id='missing',
                               environment_id='missing')
        result = self.request('post', '/v1/services', body, status=400)
        self.assertIn('server_id is required', result.json['faultstring'])

    def test_create_parent_schema_and_read_only_template(self):
        for field in ('appgroup_id', 'environment_id'):
            for value in (None, '', 'x' * 37):
                body = self.create_body()
                body['service'][field] = value
                self.request('post', '/v1/services', body, status=400)
            body = self.create_body()
            del body['service'][field]
            self.request('post', '/v1/services', body, status=400)
        body = self.create_body()
        body['service']['template_id'] = 'wordpress-with-mariadb'
        self.request('post', '/v1/services', body, status=400)

    def assert_parent_rejected(self, body, status, headers=None):
        # Rejection must not reserve quota or write workload/secret rows.
        with self.db.engine.connect() as connection:
            tables = objects.Service.repository.model.metadata.tables.values()
            before = {table.name: list(connection.execute(
                table.select()).mappings()) for table in tables}
        secrets = dict(self.store.values)
        self.request('post', '/v1/services', body, status=status,
                     headers=headers)
        with self.db.engine.connect() as connection:
            after = {table.name: list(connection.execute(
                table.select()).mappings()) for table in tables}
        self.assertEqual(before, after)
        self.assertEqual(secrets, self.store.values)

    def test_missing_and_foreign_parent_ids(self):
        foreign_ctx = self.ctx.__class__(project_id='other')
        stack = objects.Stack(context=foreign_ctx, name='other', plan='small',
                              driver='noop', engine_version='test',
                              status=states.ACTIVE, task_state=states.NONE)
        stack.create()
        appgroup = objects.Appgroup(
            context=foreign_ctx, stack_id=stack.id, name='other',
            status=states.ACTIVE, task_state=states.NONE, engine_ref='other')
        appgroup.create()
        environment = objects.Environment(
            context=foreign_ctx, appgroup_id=appgroup.id, name='other',
            status=states.ACTIVE, task_state=states.NONE, engine_ref='other')
        environment.create()
        server = objects.Server(
            context=foreign_ctx, stack_id=stack.id, name='other', kind='vm',
            role='worker', status=states.ACTIVE, task_state=states.NONE)
        server.create()
        for source in ({'type': 'git',
                        'repo_url': 'https://example.com/x.git'},
                       'template:wordpress-with-mariadb'):
            for field, parent in (('appgroup_id', appgroup),
                                  ('environment_id', environment),
                                  ('server_id', server)):
                for parent_id in ('missing', parent.id):
                    body = self.create_body(env={
                        'PASSWORD': {'value': 'not-written', 'secret': True}})
                    body['service'].update(
                        {field: parent_id, 'source': source})
                    for role in ('member', 'admin'):
                        self.assert_parent_rejected(body, 404, dict(
                            self.headers, **{'X-Roles': role}))

    def test_same_project_parent_mismatch(self):
        other = objects.Appgroup(
            context=self.ctx, stack_id=self.stack.id, name='other',
            status=states.ACTIVE, task_state=states.NONE, engine_ref='other')
        other.create()
        stack = objects.Stack(context=self.ctx, name='other', plan='small',
                              driver='noop', engine_version='test',
                              status=states.ACTIVE, task_state=states.NONE)
        stack.create()
        server = objects.Server(
            context=self.ctx, stack_id=stack.id, name='other', kind='vm',
            role='worker', status=states.ACTIVE, task_state=states.NONE)
        server.create()
        for source in ({'type': 'git',
                        'repo_url': 'https://example.com/x.git'},
                       'template:wordpress-with-mariadb'):
            for change in ({'appgroup_id': other.id},
                           {'server_id': server.id}):
                body = self.create_body()
                body['service'].update(source=source, **change)
                self.assert_parent_rejected(body, 400)

    def test_inactive_or_unmapped_parents(self):
        for source in ({'type': 'git',
                        'repo_url': 'https://example.com/x.git'},
                       'template:wordpress-with-mariadb'):
            for parent in (self.application, self.environment):
                for status, ref in ((states.PENDING, 'ref'),
                                    (states.ERROR, 'ref'),
                                    (states.DELETING, 'ref'),
                                    (states.ACTIVE, None),
                                    (states.ACTIVE, '')):
                    parent.status = status
                    parent.engine_ref = ref
                    # Seed a parent state; do not simulate invalid transitions.
                    with db_api.writer.using(self.ctx):
                        model = parent.repository.model
                        self.ctx.session.query(model).filter_by(
                            id=parent.id).update({'status': status,
                                                 'engine_ref': ref})
                    body = self.create_body()
                    body['service']['source'] = source
                    self.assert_parent_rejected(body, 409)
                parent.status = states.ACTIVE
                parent.engine_ref = 'restored'
                with db_api.writer.using(self.ctx):
                    self.ctx.session.query(parent.repository.model).filter_by(
                        id=parent.id).update({'status': states.ACTIVE,
                                              'engine_ref': 'restored'})

    def test_parent_response_fields_and_filters(self):
        created = self.request('post', '/v1/services', self.create_body(),
                               status=202).json['service']
        row = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(self.environment.id, row.environment_id)
        path = '/v1/services/' + row.id
        for result in (created, self.request('get', path).json['service']):
            self.assertEqual(self.application.id, result['appgroup_id'])
            self.assertEqual(self.environment.id, result['environment_id'])
            self.assertIsNone(result['template_id'])
        query = ('?appgroup_id=' + self.application.id +
                 '&environment_id=' + self.environment.id +
                 '&fields=appgroup_id,environment_id,template_id')
        result = self.request('get', '/v1/services' + query).json['services']
        self.assertEqual(2, len(result))
        for item in result:
            self.assertEqual(self.application.id, item['appgroup_id'])
            self.assertEqual(self.environment.id, item['environment_id'])
            self.assertIsNone(item['template_id'])
        self.assertEqual([], self.request(
            'get', '/v1/services?appgroup_id=missing').json['services'])
        # Q148: kind is stored; 1.0 git/image rows are apps.
        self.assertEqual('app', created['kind'])
        apps = self.request('get', '/v1/services?kind=app').json['services']
        self.assertEqual(2, len(apps))
        self.assertTrue(all(item['kind'] == 'app' for item in apps))
        template = objects.Service.get_by_id(self.ctx, created['id'])
        template.template_id = 'postgresql'
        template.kind = 'service'
        template.save()
        services = self.request(
            'get', '/v1/services?kind=service').json['services']
        self.assertEqual([created['id']], [item['id'] for item in services])
        self.assertEqual('service', services[0]['kind'])
        self.assertEqual(1, len(self.request(
            'get', '/v1/services?kind=app').json['services']))
        self.request('get', '/v1/services?kind=other', status=400)
        template.template_id = None
        template.kind = 'app'
        template.save()
        self.assertEqual([], self.request(
            'get', '/v1/services?environment_id=missing').json['services'])
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        self.assertEqual([], self.request(
            'get', '/v1/services' + query, headers=foreign).json['services'])
        self.request('get', '/v1/services?sort_key=appgroup_id', status=400)
        for value in ({}, [], (), set()):
            self.assertRaises(exception.Invalid, objects.ServiceList.get_all,
                              self.ctx, filters={'appgroup_id': value})

    def test_update_service_and_frozen_deployment(self):
        self.request('post', self.path + '/deployments',
                     {'deployment': {'source_ref': 'commit'}}, status=202)
        dep = objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': self.service.id},
            sort_key='number', sort_dir='desc')[0]
        # Settings save during a deploy without the lock; deploy unaffected.
        result = self.request('patch', self.path,
                              {'service': {'ports': '8080'}})
        self.assertTrue(result.json['service']['pending_changes'])
        self.assertNotEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        conflict = self.request('post', self.path + '/deployments',
                                {'deployment': {}}, status=409)
        self.assertIn('A deployment is in progress', conflict.text)
        self.unlock()
        self.request('patch', self.path, {'service': {
            'name': 'renamed', 'ports': '8080',
            'source': {'branch': 'release', 'build_pack': 'dockerfile'}}})
        self.assertEqual('3000', objects.Deployment.get_by_id(
            self.ctx, dep.id).spec['ports'])
        source = objects.Source.get_by_id(self.ctx, self.source.id)
        self.assertEqual('release', source.branch)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        # A rename alone is not a pending deploy change on a clean service.
        row = objects.Service.get_by_id(self.ctx, self.service.id)
        row.settings_changed_at = None
        row.save()
        result = self.request('patch', self.path,
                              {'service': {'description': 'x'}})
        self.assertFalse(result.json['service']['pending_changes'])

    def _freeze_current_spec(self):
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        # Deployment specs are immutable through the API layer; write the
        # frozen spec straight to the row to model an earlier deploy.
        spec = service_controllers.snapshot(self.ctx, service)
        with db_api.writer.using(self.ctx):
            self.ctx.session.query(self.deployment.repository.model).filter_by(
                id=self.deployment.id).update({'spec': spec})
        return service

    def test_pending_changes_follow_the_frozen_spec(self):
        service = self._freeze_current_spec()
        service.settings_changed_at = timeutils.utcnow()
        service.save()
        self.assertFalse(self.request('get', self.path).json[
            'service']['pending_changes'])
        self.request('patch', self.path, {'service': {'ports': '8080'}})
        self.assertTrue(self.request('get', self.path).json[
            'service']['pending_changes'])
        self.request('patch', self.path, {'service': {'ports': '3000'}})
        self.assertFalse(self.request('get', self.path).json[
            'service']['pending_changes'])

    def test_metadata_patch_pushes_without_a_redeploy_flag(self):
        service = self._freeze_current_spec()
        service.engine_ref = 'engine-app'
        service.settings_changed_at = timeutils.utcnow()
        service.save()
        with mock.patch.object(handlers.Handler, 'rename_service') as renamed:
            result = self.request('patch', self.path, {'service': {
                'name': 'renamed', 'description': 'notes'}})
        renamed.assert_called_once()
        stored = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.RECONCILING, stored.task_state)
        self.assertEqual('renamed', stored.name)
        self.assertFalse(result.json['service']['pending_changes'])
        self.unlock()
        with db_api.writer.using(self.ctx):
            self.ctx.session.query(stored.repository.model).filter_by(
                id=stored.id).update({'task_state': states.DEPLOYING})
        with mock.patch.object(handlers.Handler, 'rename_service') as renamed:
            self.request('patch', self.path, {'service': {
                'description': 'later'}})
        renamed.assert_not_called()
        stored = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.DEPLOYING, stored.task_state)
        self.assertEqual('later', stored.description)
        self.assertFalse(self.request('get', self.path).json[
            'service']['pending_changes'])
        self.unlock()
        stored.engine_ref = 'engine-app'
        stored.save()
        with mock.patch.object(handlers.Handler, 'rename_service') as renamed:
            self.request('patch', self.path, {'service': {
                'name': 'mixed', 'ports': '8080'}})
        renamed.assert_not_called()
        stored = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.NONE, stored.task_state)
        self.assertTrue(self.request('get', self.path).json[
            'service']['pending_changes'])

    def test_default_url_uses_the_stored_host(self):
        created = self.request(
            'post', '/v1/services', self.create_body(ports='8080'),
            status=202).json['service']
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual('https://' + service.default_domain,
                         created['default_url'])
        self.assertIn('203-0-113-25', created['default_url'])
        service.default_domain = 'stored.example.com'
        service.save()
        shown = self.request('get', '/v1/services/' + service.id).json[
            'service']
        self.assertEqual('https://stored.example.com', shown['default_url'])

    def test_tls_mode_starts_at_microversion_1_2(self):
        body = {'domain': {'fqdn': 'tls.example.com', 'tls': 'none'}}
        self.request('post', self.path + '/domains', body, status=406)
        self.request('post', self.path + '/domains', body, status=406,
                     version='1.1')
        self.request('patch', self.domain_path,
                     {'domain': {'tls': 'none'}}, status=406)
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        self.request('patch', self.domain_path, {'domain': {'tls': 'none'}},
                     headers=reader, status=403)
        created = self.request('post', self.path + '/domains', body,
                               status=201, version='1.2').json['domain']
        self.assertEqual('none', created['tls'])
        updated = self.request(
            'patch', self.path + '/domains/' + created['id'],
            {'domain': {'tls': 'letsencrypt'}}, status=202,
            version='1.2').json['domain']
        self.assertEqual('letsencrypt', updated['tls'])

    def test_skip_verification_marks_the_domain_active(self):
        self.config.config(skip_verification=True, group='domains')
        created = self.request('post', self.path + '/domains', {'domain': {
            'fqdn': 'custom.example.com'}}, status=201).json['domain']
        self.assertEqual(states.ACTIVE, created['status'])
        self.assertIsNotNone(created['verified_at'])

    def test_verify_records_a_matching_txt_record(self):
        with mock.patch(
                'coast.api.v1.service_controllers.domain_check.txt_matches',
                return_value=True):
            result = self.request('post', self.domain_path + '/action',
                                  {'verify': {}}, status=202)
        self.assertEqual(states.VERIFIED, result.json['domain']['status'])

    def test_async_service_actions_and_delete(self):
        self.request('post', self.path + '/action', {'start': {}}, status=409)
        for action in ('stop', 'restart'):
            result = self.request('post', self.path + '/action',
                                  {action: {}}, status=202)
            self.assertEqual(states.ACTIVE, result.json['service']['status'])
            self.request('post', self.path + '/action',
                         {action: {}}, status=409)
            self.unlock()
        self.service.status = states.STOPPED
        self.service.save()
        result = self.request('post', self.path + '/action', {'start': {}},
                              status=202)
        self.assertEqual(states.STOPPED, result.json['service']['status'])
        self.unlock()
        self.request('delete', self.path, status=202)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.DELETING, service.status)
        self.assertIsNone(service.deleted_at)
        self.request('delete', self.path, status=409)

    def test_resolved_ref_is_read_only_and_visible(self):
        self.request('post', self.path + '/deployments',
                     {'deployment': {'resolved_ref': 'a' * 40}}, status=400)
        self.request('post', self.path + '/deployments',
                     {'deployment': {'resolved_ref': None}}, status=400)
        self.deployment.resolved_ref = 'sha256:' + 'b' * 64
        self.deployment.save()
        shown = self.request('get', self.dep_path).json['deployment']
        self.assertEqual(self.deployment.resolved_ref, shown['resolved_ref'])
        listed = self.request('get', self.path + '/deployments').json[
            'deployments']
        self.assertEqual(shown['resolved_ref'], listed[0]['resolved_ref'])

    def test_rollback_pins_resolved_ref(self):
        self.deployment.resolved_ref = 'a' * 40
        self.deployment.save()
        self.test_rollback_new_row_copies_target()

    def test_deploy_cancel_lock_and_number(self):
        result = self.request('post', self.path + '/deployments',
                              {'deployment': {'source_ref': 'release'}},
                              status=202).json['deployment']
        self.assertIsNone(result['resolved_ref'])
        self.assertEqual(2, result['number'])
        path = self.path + '/deployments/' + result['id']
        self.request('post', self.path + '/deployments',
                     {'deployment': {}},
                     status=409)
        self.request('post', self.dep_path + '/action',
                     {'cancel': {}}, status=409)
        result = self.request('post', path + '/action', {'cancel': {}},
                              status=202)
        self.assertEqual(states.CANCELLED, result.json['deployment']['status'])
        self.request('post', path + '/action', {'cancel': {}}, status=409)
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_rollback_new_row_copies_target(self):
        self.deployment.status = states.SUPERSEDED
        self.deployment.save()
        latest = objects.Deployment(
            context=self.ctx, service_id=self.service.id, spec={},
            trigger='api',
            status=states.ACTIVE)
        latest.create()
        self.service.current_deployment_id = latest.id
        self.service.save()
        result = self.request('post', self.dep_path + '/action',
                              {'rollback': {}}, status=202).json['deployment']
        row = objects.Deployment.get_by_id(self.ctx, result['id'])
        self.assertEqual(self.deployment.resolved_ref or
                         self.deployment.source_ref, row.source_ref)
        self.assertIsNone(row.resolved_ref)
        self.assertEqual(3, row.number)
        self.assertEqual('rollback', row.trigger)
        self.assertEqual(self.deployment.id, row.rollback_of)
        self.assertEqual(dict(self.deployment.spec, file_mounts=[]), row.spec)
        self.assertEqual(self.deployment.image_digest, row.image_digest)
        self.assertEqual(states.SUPERSEDED, objects.Deployment.get_by_id(
            self.ctx, self.deployment.id).status)
        self.request('post', self.dep_path + '/action', {'rollback': {}},
                     status=409)

    def test_current_and_failed_rollback_targets_conflict(self):
        self.request('post', self.dep_path + '/action', {'rollback': {}},
                     status=409)
        self.deployment.status = states.SUPERSEDED
        self.deployment.save()
        failed = objects.Deployment(
            context=self.ctx, service_id=self.service.id, spec={},
            trigger='api',
            status=states.FAILED)
        failed.create()
        self.request(
            'post', self.path + '/deployments/' + failed.id + '/action',
            {'rollback': {}}, status=409)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_domains_create_records_verify_and_deferred_delete(self):
        result = self.request('post', self.path + '/domains', {'domain': {
            'fqdn': 'new.example.com', 'auto_dns': False}}, status=201).json[
                'domain']
        self.assertEqual(states.PENDING_VERIFICATION, result['status'])
        self.assertEqual(['TXT', 'A', 'CNAME'], [
            row['type'] for row in result['dns_records']])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.request('post', self.path + '/domains', {'domain': {
            'fqdn': 'new.example.com'}}, status=409)
        result = self.request('post', self.domain_path + '/action',
                              {'verify': {}}, status=202)
        self.assertEqual(states.PENDING_VERIFICATION,
                         result.json['domain']['status'])
        self.request('delete', self.domain_path, status=409)
        self.unlock()
        self.request('delete', self.domain_path, status=202)
        self.assertIsNotNone(objects.Domain.get_by_id(
            self.ctx, self.domain.id))

    def test_domain_global_uniqueness_does_not_leak_owner(self):
        other = context.RequestContext(project_id='other')
        source = objects.Source(context=other, type='git')
        source.create()
        stack = objects.Stack(
            context=other, name='other', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        stack.create()
        service = objects.Service(context=other, name='web',
                                  stack_id=stack.id, source_id=source.id,
                                  ports='', replicas=1, auto_deploy=False,
                                  status=states.ACTIVE, task_state=states.NONE)
        service.create()
        objects.Domain(context=other, service_id=service.id,
                       fqdn='taken.example.com',
                       verification_token='other-secret', tls='letsencrypt',
                       auto_dns=False, dns_mode='manual',
                       status=states.PENDING_VERIFICATION).create()
        result = self.request('post', self.path + '/domains', {'domain': {
            'fqdn': 'taken.example.com'}}, status=409)
        self.assertNotIn('other-secret', result.text)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_env_mask_value_means_unchanged(self):
        path = self.path + '/env'
        self.request('put', path, {'env': {
            'PUBLIC': {'value': 'production'},
            'PASSWORD': {'value': 'private-value', 'secret': True}}})
        before = {row.key: (row.value_ref, row.version)
                  for row in objects.SecretList.get_all(self.ctx)}
        self.unlock()
        result = self.request('put', path, {'env': {
            'PUBLIC': {'value': '********'},
            'PASSWORD': {'value': '********', 'secret': True}}})
        self.assertEqual('production', result.json['env']['PUBLIC']['value'])
        after = {row.key: (row.value_ref, row.version)
                 for row in objects.SecretList.get_all(self.ctx)}
        self.assertEqual(before, after)
        self.unlock()
        self.request('patch', path, {'env': {
            'NEW': {'value': '********'}}}, status=400)

    def test_env_noop_save_does_not_flag_pending_changes(self):
        path = self.path + '/env'
        self.request('put', path, {'env': {
            'PUBLIC': {'value': 'production'},
            'PASSWORD': {'value': 'private-value', 'secret': True}}})
        self.unlock()
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        service.settings_changed_at = None
        service.save()
        rows = {row.key: (row.value_ref, row.version)
                for row in objects.SecretList.get_all(self.ctx)}
        reads = len(self.store.reads)
        for method, body in (
                ('put', {'PUBLIC': {'value': 'production'},
                         'PASSWORD': {'value': '********', 'secret': True}}),
                ('patch', {'PUBLIC': {'value': 'production'}}),
                ('patch', {'PASSWORD': {'value': '********'}})):
            result = self.request(method, path, {'env': body})
            self.assertNotIn('X-Coast-Redeploy-Required', result.headers)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertIsNone(service.settings_changed_at)
        self.assertEqual(states.NONE, service.task_state)
        self.assertEqual(rows, {row.key: (row.value_ref, row.version)
                                for row in objects.SecretList.get_all(
                                    self.ctx)})
        shown = self.request('get', self.path).json['service']
        self.assertFalse(shown['pending_changes'])
        # Secret values are never read to detect a no-op.
        password = [r for r in objects.SecretList.get_all(self.ctx)
                    if r.key == 'PASSWORD'][0]
        self.assertNotIn(password.value_ref, self.store.reads[reads:])
        result = self.request('patch', path, {'env': {
            'PUBLIC': {'value': 'staging'}}})
        self.assertEqual('true', result.headers['X-Coast-Redeploy-Required'])
        self.request('patch', path, {'env': {
            'PASSWORD': {'value': '********', 'secret': False}}},
            status=400)

    def test_env_replace_merge_delete_mask_and_snapshot_refs(self):
        self.config.config(max_limit=1, group='api')
        path = self.path + '/env'
        result = self.request('put', path, {'env': {
            'PUBLIC': {'value': 'production'},
            'PASSWORD': {'value': 'private-value', 'secret': True,
                         'build_time': True}}})
        self.assertEqual('true', result.headers['X-Coast-Redeploy-Required'])
        self.assertIsNone(result.json['env']['PASSWORD']['value'])
        self.assertEqual('production', result.json['env']['PUBLIC']['value'])
        result = self.request('patch', path, {'env': {
            'PASSWORD': {'value': 'new-private-value'}, 'PUBLIC': None}})
        self.assertEqual({'PASSWORD'}, set(result.json['env']))
        row = objects.SecretList.get_all(self.ctx)[0]
        self.assertTrue(row.is_secret)
        self.assertTrue(row.build_time)
        self.assertEqual(2, row.version)
        self.assertNotIn(row.value_ref, self.store.reads)
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        result = self.request('get', path, headers=admin)
        self.assertIsNone(result.json['env']['PASSWORD']['value'])
        self.request('post', self.path + '/deployments',
                     {'deployment': {}},
                     status=202)
        dep = objects.DeploymentList.get_all(
            self.ctx, sort_key='number', sort_dir='desc')[0]
        ref = dep.spec['env']['PASSWORD']['value_ref']
        self.assertEqual('new-private-value', self.store.get(self.ctx, ref))
        self.assertNotIn('new-private-value', str(dep.spec))
        self.unlock()
        self.request('put', path, {'env': {}})
        self.assertEqual('new-private-value', self.store.get(self.ctx, ref))
        self.assertEqual([], list(objects.SecretList.get_all(self.ctx)))

    def test_secret_store_failure_and_db_failure_roll_back(self):
        with mock.patch.object(self.store, 'put',
                               side_effect=exception.Invalid('sensitive')):
            self.request('put', self.path + '/env', {'env': {
                'KEY': {'value': 'sensitive'}}}, status=500)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        with mock.patch.object(db_api.secrets, 'create',
                               side_effect=exception.Invalid()):
            self.request('post', '/v1/services', self.create_body(
                env={'KEY': {'value': 'sensitive'}}), status=400)
        self.assertEqual({}, self.store.values)
        self.assertEqual(1, len(objects.ServiceList.get_all(self.ctx)))
        self.assertEqual(1, len(objects.SourceList.get_all(self.ctx)))
        with mock.patch('coast.api.secret_store.STORE',
                        secret_store.SecretStore()):
            result = self.request('put', self.path + '/env', {'env': {
                'KEY': {'value': 'sensitive'}}}, status=500)
        self.assertNotIn('sensitive', result.text)

    def test_secret_values_absent_from_database_and_logs(self):
        capture = self.useFixture(fixtures.FakeLogger())
        self.request('post', '/v1/services', self.create_body(env={
            'PASSWORD': {'value': 'never-persist-or-log', 'secret': True}}),
            status=202)
        with db_api.reader.using(self.ctx):
            tables = objects.Service.repository.model.metadata
            for table in tables.sorted_tables:
                rows = self.ctx.session.execute(table.select()).all()
                self.assertNotIn('never-persist-or-log', str(rows))
        self.assertNotIn('never-persist-or-log', capture.output)

    def test_logs_empty_payload_and_validation(self):
        self.assertEqual({'logs': {'lines': []}}, self.request(
            'get', self.path +
            '/logs?tail=200&since=2026-10-09T14:06:01Z').json)
        self.assertEqual({'log': {'deployment_id': self.deployment.id,
                                  'complete': False, 'lines': []}},
                         self.request('get', self.dep_path + '/log').json)
        for suffix in ('tail=bad', 'tail=-1', 'since=bad', 'unknown=x'):
            self.request('get', self.path + '/logs?' + suffix, status=400)
        self.request('get', self.dep_path + '/log?tail=200', status=400)

    def test_events_read_only_filters_and_pagination(self):
        for outcome in ('start', 'success'):
            objects.Event(context=self.ctx, stack_id=self.stack.id,
                          resource_type='service', resource_id=self.service.id,
                          action='service.create', outcome=outcome,
                          message='Sanitized activity').create()
        path = '/v1/events?stack_id=' + self.stack.id
        result = self.request('get', path + '&limit=1&sort_key=outcome')
        self.assertEqual(1, len(result.json['events']))
        self.assertEqual('start', result.json['events'][0]['outcome'])
        next_page = self.request('get', result.json['links'][0]['href'])
        self.assertEqual('success', next_page.json['events'][0]['outcome'])
        self.assertEqual([], next_page.json['links'])
        event_id = result.json['events'][0]['id']
        self.request('get', '/v1/events/' + event_id, status=404)
        self.request('post', '/v1/events', {'event': {}}, status=405)

    def test_pagination_fields_and_bad_filters(self):
        self.request('post', '/v1/services',
                     self.create_body(deploy=False), status=202)
        self.unlock()
        result = self.request(
            'get', '/v1/services?limit=1&sort_key=name&fields=id,name')
        self.assertEqual({'id', 'name', 'links'},
                         set(result.json['services'][0]))
        self.assertEqual('new', result.json['services'][0]['name'])
        result = self.request('get', result.json['links'][0]['href'])
        self.assertEqual('web', result.json['services'][0]['name'])
        for path in ('/v1/services', self.path, self.path + '/domains',
                     self.domain_path, self.path + '/deployments',
                     self.dep_path, '/v1/events'):
            self.request('get', path + '?fields=engine_ref', status=400)
        for query in ('limit=bad', 'limit=-1', 'sort_dir=bad', 'sort_key=spec',
                      'created_at=bad', 'replicas=bad', 'auto_deploy=bad',
                      'cpu_limit=nan', 'cpu_limit=inf',
                      'stack_id=missing&fields=unknown'):
            self.request('get', '/v1/services?' + query, status=400)
        self.request('get', '/v1/services?all_projects=true', status=403)
        self.request('get', '/v1/services?project_id=other', status=403)

    def test_same_project_wrong_service_and_marker(self):
        body = self.create_body(deploy=False)
        second = self.request('post', '/v1/services', body, status=202
                              ).json['service']['id']
        for suffix in ('/deployments/' + self.deployment.id,
                       '/domains/' + self.domain.id):
            self.request('get', '/v1/services/' + second + suffix, status=404)
            if 'domain' in suffix:
                self.request('delete', '/v1/services/' + second + suffix,
                             status=404)
            self.request('post', '/v1/services/' + second + suffix + '/action',
                         ({'verify': {}} if 'domain' in suffix
                          else {'cancel': {}}),
                         status=404)
        self.request('get', '/v1/services/' + second + '/deployments?marker=' +
                     self.deployment.id, status=404)
        self.request('get', '/v1/services/' + second + '/domains?marker=' +
                     self.domain.id, status=404)
        self.request('get', '/v1/services/missing/env', status=404)

    def test_busy_operations_do_not_change_state(self):
        self.service.acquire_task(states.RECONCILING)
        for method, path, body in self.endpoints():
            if method != 'get' and path.startswith(self.path):
                # Settings and env edits are DB-only and never wait for a
                # task (James 2026-10-15).
                settings = (method in ('patch', 'put') and
                            (path == self.path or path.endswith('/env')))
                expected = 200 if settings else 409
                self.request(method, path, body, status=expected)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        self.assertEqual(states.ACTIVE, objects.Deployment.get_by_id(
            self.ctx, self.deployment.id).status)

    def test_quota_hook_and_handler_failure_not_secret_logging(self):
        with mock.patch('coast.api.v1.stack_controllers.quota_hook',
                        side_effect=exception.OverQuota()):
            self.request('post', '/v1/services', self.create_body(),
                         status=403)
        with mock.patch.object(handlers.Handler, 'create_deployment',
                               side_effect=exception.Conflict()):
            self.request('post', self.path + '/deployments',
                         {'deployment': {}},
                         status=409)
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_env_responses_include_stored_build_time(self):
        path = self.path + '/env'
        incoming = {'env': {
            'RUNTIME': {'value': 'runtime', 'build_time': False},
            'BUILD': {'value': 'build', 'build_time': True},
            'SECRET': {'value': 'hidden', 'secret': True,
                       'build_time': False}}}
        for version in ('1.0', '1.6'):
            for method in ('put', 'get', 'patch'):
                result = self.request(
                    method, path, None if method == 'get' else incoming,
                    version=version).json['env']
                self.assertFalse(result['RUNTIME']['build_time'])
                self.assertTrue(result['BUILD']['build_time'])
                self.assertFalse(result['SECRET']['build_time'])
                self.assertIsNone(result['SECRET']['value'])
                self.assertTrue(result['SECRET']['secret'])

    def test_build_time_defaults_and_patch_preservation(self):
        path = self.path + '/env'
        for version, expected in (('1.5', False), ('1.6', True)):
            self.request('put', path, {'env': {'KEY': {'value': 'first'}}},
                         version=version)
            self.assertIs(expected, objects.SecretList.get_all(
                self.ctx)[0].build_time)
            self.request('patch', path, {'env': {'KEY': {'value': 'next'}}},
                         version='1.6')
            self.assertIs(expected, objects.SecretList.get_all(
                self.ctx)[0].build_time)
        self.request('patch', path, {'env': {
            'KEY': {'value': 'next', 'build_time': False},
            'NEW': {'value': 'new'}}}, version='1.6')
        flags = {row.key: row.build_time
                 for row in objects.SecretList.get_all(self.ctx)}
        self.assertEqual({'KEY': False, 'NEW': True}, flags)

    def test_service_create_build_time_default(self):
        for version, expected in (('1.5', False), ('1.6', True)):
            body = self.create_body()
            body['service']['name'] = 'app-' + version.replace('.', '-')
            body['service']['env'] = {'KEY': {'value': 'value'}}
            result = self.request('post', '/v1/services', body,
                                  version=version, status=202)
            rows = objects.SecretList.get_all(self.ctx, filters={
                'service_id': result.json['service']['id']})
            self.assertIs(expected, rows[0].build_time)

    def test_template_env_default_and_build_time_rejection(self):
        self.service.template_id = 'template'
        self.service.save()
        path = self.path + '/env'
        for version in ('1.0', '1.6'):
            self.request('put', path, {'env': {'KEY': {'value': 'runtime'}}},
                         version=version)
            row = objects.SecretList.get_all(self.ctx)[0]
            self.assertFalse(row.build_time)
            for method in ('put', 'patch'):
                result = self.request(method, path, {'env': {'KEY': {
                    'value': 'rejected', 'build_time': True}}},
                    version=version, status=400)
                self.assertEqual(
                    'Template service variables are runtime-only; '
                    'build_time is not supported for template services',
                    result.json['faultstring'])
                self.assertNotIn('Coolify', result.json['faultstring'])
                self.assertNotIn('Traefik', result.json['faultstring'])
                saved = objects.SecretList.get_all(self.ctx)[0]
                self.assertEqual(row.value_ref, saved.value_ref)
                self.assertFalse(saved.build_time)

    def test_put_resets_flags_and_patch_preserves_defaults(self):
        path = self.path + '/env'
        self.request('put', path, {'env': {
            'KEY': {'value': 'initial', 'secret': True, 'build_time': True}}})
        self.request('patch', path, {'env': {'KEY': {'value': 'changed'}}})
        row = objects.SecretList.get_all(self.ctx)[0]
        self.assertTrue(row.is_secret)
        self.assertTrue(row.build_time)
        result = self.request(
            'put', path, {'env': {'KEY': {'value': 'public'}}})
        self.assertEqual('public', result.json['env']['KEY']['value'])
        self.assertEqual(3, result.json['env']['KEY']['version'])
        row = objects.SecretList.get_all(self.ctx)[0]
        self.assertFalse(row.is_secret)
        self.assertFalse(row.build_time)

    def test_secret_backend_errors_never_expose_values(self):
        self.request('put', self.path + '/env', {'env': {
            'PUBLIC': {'value': 'sensitive'}}})
        for operation, method, body in (
                ('get', 'get', None),
                ('put', 'patch', {'env': {'KEY': {'value': 'sensitive'}}})):
            with mock.patch.object(
                    self.store, operation,
                    side_effect=exception.Invalid('sensitive')):
                result = self.request(method, self.path + '/env', body,
                                      status=500)
            self.assertNotIn('sensitive', result.text)

    def test_admin_lists_use_resource_owner_for_computed_fields(self):
        other = context.RequestContext(project_id='other')
        source = objects.Source(context=other, type='git',
                                repo_url='https://example.com/other.git')
        source.create()
        env = objects.Stack(
            context=other, name='other', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        env.create()
        service = objects.Service(
            context=other, name='other', stack_id=env.id,
            source_id=source.id, ports='', auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE)
        service.create()
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        result = self.request(
            'get', '/v1/services?all_projects=true', headers=admin)
        self.assertEqual(2, len(result.json['services']))
        result = self.request(
            'get', '/v1/services?project_id=other', headers=admin)
        self.assertEqual(service.id, result.json['services'][0]['id'])
        self.request('get', '/v1/services?marker=' + service.id, status=404)
        self.request('get', '/v1/services/' + service.id, headers=admin,
                     status=404)
