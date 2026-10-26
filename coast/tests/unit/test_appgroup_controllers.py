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

"""Appgroup and environment API behavior."""

from unittest import mock

from coast.common import exception
from coast.common import states
from coast.conductor import manager as conductor_manager
from coast.conductor import stack_teardown
from coast.drivers import base as driver_base
from coast.drivers import noop
from coast import objects
from coast.tests import conductor
from coast.tests.unit import test_controllers


PUBLIC = {'id', 'project_id', 'name', 'description', 'status',
          'created_at', 'updated_at', 'links'}


class AppgroupApiTest(test_controllers.ControllerFixture):
    def roles(self, *names):
        return dict(self.headers, **{'X-Roles': ','.join(names)})

    def test_collection_sort_errors_name_value_and_allowed_choices(self):
        appgroup = objects.Appgroup(
            context=self.ctx, stack_id=self.env.id, name='website',
            status=states.ACTIVE, task_state=states.NONE)
        appgroup.create()
        common = ('created_at', 'description', 'id', 'name', 'project_id',
                  'status', 'updated_at')
        for path, parent_field in (
                (self.path + '/appgroups', 'stack_id'),
                ('/v1/appgroups/' + appgroup.id + '/environments',
                 'appgroup_id')):
            choices = ', '.join(sorted(common + (parent_field,)))
            for query, expected in (
                    ('sort_key=bogus',
                     "Invalid sort key 'bogus'. Use one of: %s." % choices),
                    ('sort_dir=sideways',
                     "Invalid sort direction 'sideways'. "
                     "Use one of: asc, desc.")):
                with self.subTest(path=path, query=query):
                    response = self.request('get', path + '?' + query,
                                            status=400)
                    self.assertEqual(expected, response.json['faultstring'])

    def test_crud_persists_pending_without_engine_call(self):
        appgroups = '/v1/stacks/' + self.env.id + '/appgroups'
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        accepted = 'coast.common.notifications.accepted'
        with mock.patch(cast) as rpc, mock.patch(accepted) as note:
            created = self.request('post', appgroups, {'appgroup': {
                'name': 'website', 'description': 'sites'}}, status=202)
            body = created.json['appgroup']
            self.assertEqual(PUBLIC | {'stack_id'}, set(body))
            self.assertEqual('PENDING', body['status'])
            self.assertEqual(self.env.id, body['stack_id'])
            self.assertEqual('project', body['project_id'])
            self.assertTrue(body['links'][0]['href'].endswith(
                '/v1/appgroups/' + body['id']))
            row = objects.Appgroup.get_by_id(self.ctx, body['id'])
            self.assertEqual(states.PENDING, row.status)
            self.assertEqual(states.NONE, row.task_state)
            self.assertIsNone(row.engine_ref)
            listed = self.request('get', appgroups)
            self.assertEqual(body['id'],
                             listed.json['appgroups'][0]['id'])
            item = '/v1/appgroups/' + body['id']
            self.assertEqual('website', self.request(
                'get', item).json['appgroup']['name'])
            renamed = self.request('patch', item, {'appgroup': {
                'name': 'portal', 'description': None}})
            self.assertEqual(200, renamed.status_int)
            self.assertEqual('portal', renamed.json['appgroup']['name'])
            self.assertIsNone(renamed.json['appgroup']['description'])
            self.request('post', appgroups, {'appgroup': {
                'name': 'portal'}}, status=409)
            other = objects.Stack(
                context=self.ctx, name='second', plan='small', driver='noop',
                engine_version='test', status=states.ACTIVE,
                task_state=states.NONE)
            other.create()
            other_body = self.request(
                'post', '/v1/stacks/' + other.id + '/appgroups',
                {'appgroup': {'name': 'portal'}}, status=202)
            self.assertNotEqual(body['id'],
                                other_body.json['appgroup']['id'])
            environments = item + '/environments'
            created_env = self.request('post', environments, {'environment': {
                'name': 'canary', 'description': 'live'}}, status=202)
            env_body = created_env.json['environment']
            self.assertEqual(PUBLIC | {'appgroup_id'}, set(env_body))
            self.assertEqual('PENDING', env_body['status'])
            self.assertEqual(body['id'], env_body['appgroup_id'])
            self.assertNotIn('task_state', env_body)
            self.assertNotIn('engine_ref', env_body)
            env_row = objects.Environment.get_by_id(self.ctx, env_body['id'])
            self.assertEqual(states.NONE, env_row.task_state)
            env_item = '/v1/environments/' + env_body['id']
            self.assertTrue(env_body['links'][0]['href'].endswith(env_item))
            patched = self.request('patch', env_item, {'environment': {
                'name': 'staging'}})
            self.assertEqual('staging', patched.json['environment']['name'])
            self.request('post', environments, {'environment': {
                'name': 'staging'}}, status=409)
            second_app = self.request('post', appgroups, {'appgroup': {
                'name': 'blog'}}, status=202).json['appgroup']
            self.request(
                'post', '/v1/appgroups/' + second_app['id'] +
                '/environments', {'environment': {'name': 'staging'}},
                status=202)
            admin = self.roles('admin')
            self.assertNotIn('task_state', self.request(
                'get', item, headers=admin).json['appgroup'])
            self.assertNotIn('engine_ref', self.request(
                'get', env_item, headers=admin).json['environment'])
            self.request('delete', env_item, status=409)
            self.assertEqual(states.PENDING, objects.Environment.get_by_id(
                self.ctx, env_body['id']).status)
            self.request('delete', item, status=409)
            self.assertEqual(states.PENDING, objects.Appgroup.get_by_id(
                self.ctx, body['id']).status)
            self.assertEqual(states.NONE, objects.Environment.get_by_id(
                self.ctx, env_body['id']).task_state)
            self.assertEqual(states.NONE, objects.Appgroup.get_by_id(
                self.ctx, body['id']).task_state)
            self.request('get', '/v1/appgroups', status=404)
            self.request('post', '/v1/appgroups',
                         {'appgroup': {'name': 'nope'}}, status=404)
            self.request('get', appgroups + '/' + body['id'], status=404)
            self.request('get', environments + '/' + env_body['id'],
                         status=404)
            self.assertEqual(
                ['create_appgroup', 'create_appgroup',
                 'create_app_environment', 'create_appgroup',
                 'create_app_environment'],
                [item.args[1] for item in rpc.call_args_list])
            self.assertFalse(note.called)

    def test_conductor_syncs_pending_to_active(self):
        self.config.config(enabled_drivers=['noop'],
                           group='controller_worker')
        self.env.engine_version = 'noop-1.0'
        self.env.save()
        mgr = conductor_manager.Manager(self.conf)
        appgroups = '/v1/stacks/' + self.env.id + '/appgroups'
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        with mock.patch(cast):
            created = self.request('post', appgroups, {'appgroup': {
                'name': 'website'}}, status=202).json['appgroup']
            self.assertEqual('PENDING', created['status'])
            env_body = self.request(
                'post', '/v1/appgroups/' + created['id'] +
                '/environments', {'environment': {'name': 'live'}},
                status=202).json['environment']
        app = objects.Appgroup.get_by_id(self.ctx, created['id'])
        mgr.execute(self.ctx, 'create_appgroup', app)
        app = objects.Appgroup.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ACTIVE, app.status)
        self.assertEqual('noop-project-' + app.id, app.engine_ref)
        env = objects.Environment.get_by_id(self.ctx, env_body['id'])
        mgr.execute(self.ctx, 'create_app_environment', env)
        env = objects.Environment.get_by_id(self.ctx, env_body['id'])
        self.assertEqual(states.ACTIVE, env.status)
        self.assertEqual('noop-environment-' + env.id, env.engine_ref)
        mgr.execute(self.ctx, 'create_appgroup', app)
        self.assertEqual(states.ACTIVE, objects.Appgroup.get_by_id(
            self.ctx, app.id).status)
        with mock.patch(cast):
            failed = self.request('post', appgroups, {'appgroup': {
                'name': 'broken'}}, status=202).json['appgroup']
        row = objects.Appgroup.get_by_id(self.ctx, failed['id'])
        with mock.patch.object(
                noop.NoopDriver, 'ensure_project',
                side_effect=driver_base.DriverError('down')):
            mgr.execute(self.ctx, 'create_appgroup', row)
        row = objects.Appgroup.get_by_id(self.ctx, failed['id'])
        self.assertEqual(states.ERROR, row.status)
        self.assertIsNone(row.engine_ref)

    def test_policy_roles_and_other_project(self):
        appgroups = '/v1/stacks/' + self.env.id + '/appgroups'
        created = self.request('post', appgroups, {'appgroup': {
            'name': 'website'}}, status=202).json['appgroup']
        item = '/v1/appgroups/' + created['id']
        environments = item + '/environments'
        created_env = self.request('post', environments, {'environment': {
            'name': 'canary'}}, status=202).json['environment']
        env_item = '/v1/environments/' + created_env['id']
        reader = self.roles('reader')
        member = self.roles('member')
        manager = self.roles('manager')
        for method, path, body in (
                ('get', appgroups, None),
                ('get', item, None),
                ('get', environments, None),
                ('get', env_item, None)):
            self.request(method, path, body, headers=reader)
        for method, path, body in (
                ('post', appgroups, {'appgroup': {'name': 'reader'}}),
                ('patch', item, {'appgroup': {'name': 'reader'}}),
                ('delete', item, None),
                ('post', environments, {'environment': {'name': 'reader'}}),
                ('patch', env_item, {'environment': {'name': 'reader'}}),
                ('delete', env_item, None)):
            self.request(method, path, body, headers=reader, status=403)
        self.request('patch', item, {'appgroup': {'description': 'member'}},
                     headers=member)
        self.request('patch', env_item, {'environment': {'name': 'member'}},
                     headers=member)
        for method, path, body in (
                ('post', appgroups, {'appgroup': {'name': 'member'}}),
                ('delete', item, None),
                ('post', environments, {'environment': {'name': 'member'}}),
                ('delete', env_item, None)):
            self.request(method, path, body, headers=member, status=403)
        self.request(
            'post', appgroups, {'appgroup': {'name': 'manager'}},
            headers=manager, status=202)
        self.request('patch', item, {'appgroup': {'name': 'manager'}},
                     headers=manager, status=403)
        self.request('post', environments,
                     {'environment': {'name': 'manager'}},
                     headers=manager, status=202)
        self.request('patch', env_item, {'environment': {'name': 'no'}},
                     headers=manager, status=403)
        self.request('delete', env_item, headers=manager, status=409)
        self.request('delete', item, headers=manager, status=409)
        stranger = self.roles('unrelated')
        self.request('get', item, headers=stranger, status=403)
        self.request('get', item, headers={}, status=401)
        other = dict(self.headers, **{'X-Project-Id': 'other'})
        for method, path, body in (
                ('get', appgroups, None),
                ('post', appgroups, {'appgroup': {'name': 'other'}}),
                ('get', item, None),
                ('patch', item, {'appgroup': {'name': 'other'}}),
                ('delete', item, None),
                ('get', environments, None),
                ('post', environments, {'environment': {'name': 'other'}}),
                ('get', env_item, None),
                ('delete', env_item, None)):
            self.request(method, path, body, headers=other, status=404)

    def test_validation(self):
        appgroups = '/v1/stacks/' + self.env.id + '/appgroups'
        for body in ({}, {'appgroup': {}},
                     {'appgroup': {'name': ''}},
                     {'appgroup': {'name': 'ok', 'extra': 1}},
                     {'appgroup': {'name': 'ok', 'description': 'x' * 256}},
                     {'appgroup': {'description': 'only'}},
                     {'environment': {'name': 'wrong-envelope'}}):
            self.request('post', appgroups, body, status=400)
        self.client.post(appgroups, '{bad json',
                         content_type='application/json',
                         headers=self.headers, status=400)
        created = self.request('post', appgroups, {'appgroup': {
            'name': 'website', 'description': 'sites'}}, status=202)
        app = created.json['appgroup']
        item = '/v1/appgroups/' + app['id']
        environments = item + '/environments'
        self.request('post', environments, {'environment': {}}, status=400)
        self.request('post', environments, {'environment': {
            'name': 'production', 'plan': 'small'}}, status=400)
        env = self.request('post', environments, {'environment': {
            'name': 'canary'}}, status=202).json['environment']
        env_item = '/v1/environments/' + env['id']
        self.request('patch', item, {'appgroup': {'status': 'ACTIVE'}},
                     status=400)
        self.request('patch', env_item, {'environment': {'stack_id': 'x'}},
                     status=400)
        self.request('delete', item, {'unknown': True}, status=400)
        self.request('delete', env_item, {'environment': {}}, status=400)
        for path in (appgroups, item, environments, env_item):
            self.request('get', path + '?fields=task_state', status=400)
            self.request('get', path + '?fields=engine_ref', status=400)
        for path in (appgroups, environments):
            self.request('get', path + '?limit=bad', status=400)
            self.request('get', path + '?sort_key=engine_ref', status=400)
        self.request('get', appgroups + '?all_projects=true', status=403)
        selected = self.request('get', item + '?fields=id,name')
        self.assertEqual({'id', 'name', 'links'},
                         set(selected.json['appgroup']))
        self.request('post', appgroups, {'appgroup': {
            'name': 'blog'}}, status=202)
        page = self.request('get', appgroups + '?limit=1&sort_key=name')
        self.assertEqual('blog', page.json['appgroups'][0]['name'])
        nxt = self.request('get', page.json['links'][0]['href'])
        self.assertEqual('website', nxt.json['appgroups'][0]['name'])
        self.assertEqual([], nxt.json['links'])
        self.request('get', appgroups + '?name=missing')
        other = self.request('post', appgroups, {'appgroup': {
            'name': 'other'}}, status=202).json['appgroup']
        other_env = self.request(
            'post', '/v1/appgroups/' + other['id'] + '/environments',
            {'environment': {'name': 'other'}}, status=202).json['environment']
        self.request('get', environments + '?marker=' + other_env['id'],
                     status=404)
        missing = '/v1/appgroups/missing/environments'
        self.request('get', missing, status=404)
        self.request('post', missing, {'environment': {'name': 'no'}},
                     status=404)

    def _noop_manager(self):
        self.config.config(enabled_drivers=['noop'],
                           group='controller_worker')
        self.env.engine_version = 'noop-1.0'
        self.env.save()
        return conductor_manager.Manager(
            self.conf, notifier=conductor.RecordingNotifier())

    def _post_appgroup(self, name, keep_default=False):
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        with mock.patch(cast):
            body = self.request(
                'post', '/v1/stacks/' + self.env.id + '/appgroups',
                {'appgroup': {'name': name}}, status=202).json[
                    'appgroup']
        if not keep_default:
            # Older flows test one environment at a time; drop Q147's.
            for env in self._environments(body['id']):
                env.destroy()
        return body

    def _environments(self, appgroup_id):
        return list(objects.EnvironmentList.get_all(
            self.ctx, filters={'appgroup_id': appgroup_id}))

    def test_cannot_add_appgroup_after_stack_delete_claim(self):
        self.env.status = states.DELETING
        self.env.save()
        self.env.acquire_task(states.DELETING)
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast') as cast:
            self.request('post', '/v1/stacks/' + self.env.id + '/appgroups',
                         {'appgroup': {'name': 'late'}}, status=409)
        cast.assert_not_called()
        self.assertFalse(objects.AppgroupList.get_all(
            self.ctx, filters={'stack_id': self.env.id}))

    def test_create_adds_production_environment(self):
        mgr = self._noop_manager()
        created = self._post_appgroup('website', keep_default=True)
        envs = self._environments(created['id'])
        self.assertEqual(['production'], [e.name for e in envs])
        self.assertEqual(states.PENDING, envs[0].status)
        listed = self.request(
            'get', '/v1/appgroups/' + created['id'] + '/environments')
        self.assertEqual(['production'], [
            e['name'] for e in listed.json['environments']])
        appgroup = objects.Appgroup.get_by_id(self.ctx, created['id'])
        mgr.execute(self.ctx, 'create_appgroup', appgroup)
        self.assertEqual(states.ACTIVE, objects.Appgroup.get_by_id(
            self.ctx, created['id']).status)
        env = objects.Environment.get_by_id(self.ctx, envs[0].id)
        self.assertEqual(states.ACTIVE, env.status)
        self.assertIsNotNone(env.engine_ref)
        # A second production environment is a name conflict.
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        with mock.patch(cast):
            self.request(
                'post', '/v1/appgroups/' + created['id'] + '/environments',
                {'environment': {'name': 'production'}}, status=409)

    def test_failed_appgroup_marks_default_environment_error(self):
        mgr = self._noop_manager()
        created = self._post_appgroup('website', keep_default=True)
        appgroup = objects.Appgroup.get_by_id(self.ctx, created['id'])
        with mock.patch.object(
                noop.NoopDriver, 'ensure_project',
                side_effect=driver_base.DriverError('boom')):
            mgr.execute(self.ctx, 'create_appgroup', appgroup)
        self.assertEqual(states.ERROR, objects.Appgroup.get_by_id(
            self.ctx, created['id']).status)
        self.assertEqual([states.ERROR], [
            e.status for e in self._environments(created['id'])])

    def _post_environment(self, appgroup_id, name='live'):
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        with mock.patch(cast):
            return self.request(
                'post', '/v1/appgroups/' + appgroup_id + '/environments',
                {'environment': {'name': name}}, status=202).json[
                    'environment']

    def _activate(self, appgroup_id, environment_id=None, app_ref='proj',
                  env_ref='env'):
        appgroup = objects.Appgroup.get_by_id(self.ctx, appgroup_id)
        appgroup.status = states.ACTIVE
        appgroup.engine_ref = app_ref
        appgroup.save()
        environment = None
        if environment_id:
            environment = objects.Environment.get_by_id(
                self.ctx, environment_id)
            environment.status = states.ACTIVE
            environment.engine_ref = env_ref
            environment.save()
        return appgroup, environment

    def test_delete_rejects_pending_deleting_stack_and_children(self):
        created = self._post_appgroup('website')
        item = '/v1/appgroups/' + created['id']
        env = self._post_environment(created['id'])
        env_item = '/v1/environments/' + env['id']
        blocked = self._post_appgroup('blocked')
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        accepted = 'coast.common.notifications.accepted'
        with mock.patch(cast) as rpc, mock.patch(accepted) as note:
            self.request('delete', env_item, status=409)
            self.request('delete', item, status=409)
            self._activate(blocked['id'])
            row = objects.Appgroup.get_by_id(self.ctx, blocked['id'])
            row.status = states.DELETING
            row.save()
            self.request('delete', '/v1/appgroups/' + blocked['id'],
                         status=409)
            self._activate(created['id'], env['id'])
            refusal = self.request('delete', item, status=409)
            self.assertIn('Delete the remaining environments first.',
                          refusal.json['faultstring'])
            source = objects.Source(context=self.ctx, type='git')
            source.create()
            service = objects.Service(
                context=self.ctx, stack_id=self.env.id,
                environment_id=env['id'], name='web', source_id=source.id,
                ports='80', replicas=1, auto_deploy=False,
                status=states.ACTIVE, task_state=states.NONE)
            service.create()
            refusal = self.request('delete', env_item, status=409)
            self.assertIn('Delete the remaining apps first.',
                          refusal.json['faultstring'])
            self.assertFalse(rpc.called)
            self.assertFalse(note.called)
            service.destroy()
            deleted = self.request('delete', env_item, status=202)
            other = self._post_appgroup('other')
            self._activate(other['id'])
            self.env.status = states.ERROR
            self.env.save()
            self.request('delete', '/v1/appgroups/' + other['id'],
                         status=202)
        self.assertEqual(
            'delete_app_environment', rpc.call_args_list[0][0][1])
        self.assertEqual('environment', note.call_args_list[0][0][3])
        self.assertEqual('DELETING', deleted.json['environment']['status'])
        self.assertEqual(states.ACTIVE, objects.Appgroup.get_by_id(
            self.ctx, created['id']).status)
        self.assertEqual(states.DELETING, objects.Appgroup.get_by_id(
            self.ctx, other['id']).status)
        self.assertEqual(states.DELETING, objects.Environment.get_by_id(
            self.ctx, env['id']).status)
        self.assertEqual(states.DELETING, objects.Appgroup.get_by_id(
            self.ctx, blocked['id']).status)

    def test_names_reusable_after_conductor_delete(self):
        mgr = self._noop_manager()
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        groups = '/v1/stacks/' + self.env.id + '/appgroups'
        with mock.patch(cast):
            created = self._post_appgroup('reusable')
            mgr.execute(self.ctx, 'create_appgroup',
                        objects.Appgroup.get_by_id(self.ctx, created['id']))
            environments = '/v1/appgroups/' + created['id'] + '/environments'
            body = {'environment': {'name': 'reusable'}}
            for attempt in range(2):
                env = self.request('post', environments, body,
                                   status=202).json['environment']
                mgr.execute(self.ctx, 'create_app_environment',
                            objects.Environment.get_by_id(self.ctx, env['id']))
                self.request('post', environments, body, status=409)
                path = '/v1/environments/' + env['id']
                self.request('delete', path, status=202)
                conflict = self.request('post', environments, body, status=409)
                self.assertIn('Deletion is still in progress',
                              conflict.json['faultstring'])
                mgr.execute(self.ctx, 'delete_app_environment',
                            objects.Environment.get_by_id(self.ctx, env['id']))
                self.request('get', path, status=404)
            path = '/v1/appgroups/' + created['id']
            body = {'appgroup': {'name': 'reusable'}}
            self.request('post', groups, body, status=409)
            self.request('delete', path, status=202)
            conflict = self.request('post', groups, body, status=409)
            self.assertIn('Deletion is still in progress',
                          conflict.json['faultstring'])
            mgr.execute(self.ctx, 'delete_appgroup',
                        objects.Appgroup.get_by_id(self.ctx, created['id']))
            self.request('get', path, status=404)
            replacement = self.request('post', groups, body,
                                       status=202).json['appgroup']
            self.assertNotEqual(created['id'], replacement['id'])

    def test_conductor_delete_removes_appgroup_and_environment(self):
        mgr = self._noop_manager()
        created = self._post_appgroup('website')
        env = self._post_environment(created['id'])
        appgroup = objects.Appgroup.get_by_id(self.ctx, created['id'])
        mgr.execute(self.ctx, 'create_appgroup', appgroup)
        environment = objects.Environment.get_by_id(self.ctx, env['id'])
        mgr.execute(self.ctx, 'create_app_environment', environment)
        accepted = 'coast.common.notifications.accepted'
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        with mock.patch(cast), mock.patch(accepted) as note:
            removed = self.request(
                'delete', '/v1/environments/' + env['id'], status=202)
        self.assertEqual('DELETING', removed.json['environment']['status'])
        self.assertEqual('environment', note.call_args[0][3])
        environment = objects.Environment.get_by_id(self.ctx, env['id'])
        driver = mgr.drivers['noop']
        with mock.patch.object(
                driver, 'delete_environment',
                wraps=driver.delete_environment) as drop:
            mgr.execute(self.ctx, 'delete_app_environment', environment)
        self.assertEqual('https://example.com/engine',
                         drop.call_args[0][0].base_url)
        self.assertEqual(env['id'], drop.call_args[0][3].id)
        gone = objects.Environment.get_by_id(
            self.ctx, env['id'], show_deleted=True)
        self.assertEqual(states.DELETED, gone.status)
        self.assertIsNotNone(gone.deleted_at)
        self.assertEqual(states.NONE, gone.task_state)
        self.request('get', '/v1/environments/' + env['id'], status=404)
        with mock.patch(cast), mock.patch(accepted) as note:
            removed = self.request(
                'delete', '/v1/appgroups/' + created['id'], status=202)
        self.assertIsNone(note.call_args[0][3])
        appgroup = objects.Appgroup.get_by_id(self.ctx, created['id'])
        with mock.patch.object(
                driver, 'delete_project', wraps=driver.delete_project) as drop:
            mgr.execute(self.ctx, 'delete_appgroup', appgroup)
        self.assertEqual(created['id'], drop.call_args[0][2].id)
        gone = objects.Appgroup.get_by_id(
            self.ctx, created['id'], show_deleted=True)
        self.assertEqual(states.DELETED, gone.status)
        self.assertIsNotNone(gone.deleted_at)
        self.assertEqual(states.NONE, gone.task_state)
        self.assertEqual([
            'coast.environment.delete.start',
            'coast.environment.delete.end',
            'coast.appgroup.delete.start',
            'coast.appgroup.delete.end'],
            [event for event, body in mgr.notifier.events])
        self.assertEqual(states.DELETED, mgr.notifier.events[-1][1]['status'])

    def test_engine_cleanup_preserves_reachable_active_errors(self):
        mgr = self._noop_manager()
        for error in (driver_base.EngineAuthError,
                      driver_base.EngineConflict, driver_base.DriverError):
            cleanup = mock.Mock(side_effect=error('private detail'))
            self.assertRaises(error, mgr._delete_engine_cleanup,
                              self.env, cleanup)
            cleanup.assert_called_once_with()

    def test_engine_cleanup_skips_error_stack_and_unreachable_engine(self):
        mgr = self._noop_manager()
        cleanup = mock.Mock(side_effect=driver_base.EngineUnreachable(
            'private detail'))
        with mock.patch('coast.conductor.manager.LOG') as log:
            mgr._delete_engine_cleanup(self.env, cleanup)
            cleanup.assert_called_once_with()
            self.env.status = states.ERROR
            cleanup.reset_mock()
            mgr._delete_engine_cleanup(self.env, cleanup)
            cleanup.assert_not_called()
        self.assertEqual(2, log.warning.call_count)
        self.assertNotIn('private detail', str(log.warning.call_args_list))

    def test_child_cleanup_after_failed_stack_teardown(self):
        mgr = self._noop_manager()
        created = self._post_appgroup('website')
        env = self._post_environment(created['id'])
        self._activate(created['id'], env['id'])
        source = objects.Source(context=self.ctx, type='image', image='web:1')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.env.id,
            environment_id=env['id'], name='web', source_id=source.id,
            ports='80', replicas=1, auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE)
        service.create()
        self.env.status = states.DELETING
        self.env.save()
        self.env.acquire_task(states.DELETING)
        try:
            raise ValueError('cleanup interrupted')
        except ValueError as exc:
            stack_teardown._fail(self.ctx, self.env, exc)
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        with mock.patch(cast):
            self.request('delete', '/v1/services/' + service.id, status=202)
        service = objects.Service.get_by_id(self.ctx, service.id)
        with mock.patch.object(
                mgr, '_driver', side_effect=driver_base.DriverError(
                    'Engine endpoint is not configured')) as driver:
            mgr.execute(self.ctx, 'delete_service', service)
        driver.assert_not_called()
        self.assertRaises(exception.NotFound, objects.Service.get_by_id,
                          self.ctx, service.id)
        for path, cls, identifier, operation in (
                ('/v1/environments/', objects.Environment, env['id'],
                 'delete_app_environment'),
                ('/v1/appgroups/', objects.Appgroup, created['id'],
                 'delete_appgroup')):
            with mock.patch(cast):
                self.request('delete', path + identifier, status=202)
            current = cls.get_by_id(self.ctx, identifier)
            mgr.execute(self.ctx, operation, current)
            self.assertRaises(exception.NotFound, cls.get_by_id,
                              self.ctx, identifier)
        with mock.patch(cast):
            self.request('delete', '/v1/stacks/' + self.env.id, status=202)

    def test_child_cleanup_on_idle_degraded_stack(self):
        self._child_cleanup_on_idle_degraded_stack()

    def test_degraded_cleanup_with_unreachable_engine(self):
        self._child_cleanup_on_idle_degraded_stack(unreachable=True)

    def _child_cleanup_on_idle_degraded_stack(self, unreachable=False):
        mgr = self._noop_manager()
        created = self._post_appgroup('website')
        env = self._post_environment(created['id'])
        self._activate(created['id'], env['id'])
        source = objects.Source(context=self.ctx, type='image', image='web:1')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.env.id,
            environment_id=env['id'], name='web', source_id=source.id,
            ports='80', replicas=1, auto_deploy=False,
            status=states.ERROR, task_state=states.NONE)
        service.create()
        self.env.status = states.DEGRADED
        self.env.save()
        if unreachable:
            for owner, method in ((mgr, '_delete_service_engine'),
                                  (noop.NoopDriver, 'delete_environment'),
                                  (noop.NoopDriver, 'delete_project')):
                patcher = mock.patch.object(
                    owner, method, side_effect=driver_base.EngineUnreachable(
                        'engine unavailable'))
                patcher.start()
                self.addCleanup(patcher.stop)
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        for path, cls, identifier, operation in (
                ('/v1/services/', objects.Service, service.id,
                 'delete_service'),
                ('/v1/environments/', objects.Environment, env['id'],
                 'delete_app_environment'),
                ('/v1/appgroups/', objects.Appgroup, created['id'],
                 'delete_appgroup')):
            with mock.patch(cast):
                self.request('delete', path + identifier, status=202)
            mgr.execute(self.ctx, operation,
                        cls.get_by_id(self.ctx, identifier))
            self.assertRaises(exception.NotFound, cls.get_by_id,
                              self.ctx, identifier)
        with mock.patch(cast):
            self.request('delete', '/v1/stacks/' + self.env.id, status=202)

    def test_child_delete_blocks_real_stack_operation(self):
        created = self._post_appgroup('website')
        env = self._post_environment(created['id'])
        self._activate(created['id'], env['id'])
        self.env.acquire_task(states.FAILING_OVER)
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast') as cast:
            for status in (states.ACTIVE, states.DEGRADED):
                self.env.status = status
                self.env.save()
                self.request('delete', '/v1/environments/' + env['id'],
                             status=409)
                self.request('delete', '/v1/appgroups/' + created['id'],
                             status=409)
        cast.assert_not_called()

    def test_conductor_delete_skips_null_ref_and_accepts_not_found(self):
        mgr = self._noop_manager()
        created = self._post_appgroup('website')
        self._activate(created['id'], app_ref=None)
        appgroup = objects.Appgroup.get_by_id(self.ctx, created['id'])
        self.assertIsNone(appgroup.engine_ref)
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        with mock.patch(cast):
            self.request('delete', '/v1/appgroups/' + created['id'],
                         status=202)
        with mock.patch.object(noop.NoopDriver, 'delete_project') as drop:
            mgr.execute(self.ctx, 'delete_appgroup', appgroup)
        drop.assert_not_called()
        gone = objects.Appgroup.get_by_id(
            self.ctx, created['id'], show_deleted=True)
        self.assertEqual(states.DELETED, gone.status)
        missing = self._post_appgroup('blog')
        env = self._post_environment(missing['id'])
        self._activate(missing['id'], env['id'], app_ref='proj-ref',
                       env_ref='env-ref')
        with mock.patch(cast):
            self.request('delete', '/v1/environments/' + env['id'],
                         status=202)
        environment = objects.Environment.get_by_id(self.ctx, env['id'])
        with mock.patch.object(
                noop.NoopDriver, 'delete_environment',
                side_effect=driver_base.EngineNotFound('missing-engine')):
            mgr.execute(self.ctx, 'delete_app_environment', environment)
        gone = objects.Environment.get_by_id(
            self.ctx, env['id'], show_deleted=True)
        self.assertEqual(states.DELETED, gone.status)
        self.assertNotIn('missing-engine', str(mgr.notifier.events))

    def test_conductor_delete_failure_sets_error_and_retries(self):
        mgr = self._noop_manager()
        created = self._post_appgroup('website')
        self._activate(created['id'], app_ref='proj-ref')
        cast = 'coast.conductor.rpcapi.ConductorAPI.cast'
        with mock.patch(cast):
            self.request('delete', '/v1/appgroups/' + created['id'],
                         status=202)
        appgroup = objects.Appgroup.get_by_id(self.ctx, created['id'])
        with mock.patch.object(
                noop.NoopDriver, 'delete_project',
                side_effect=driver_base.DriverError('secret-token')):
            with mock.patch('coast.conductor.manager.LOG') as log:
                mgr.execute(self.ctx, 'delete_appgroup', appgroup)
        self.assertNotIn('secret-token', str(log.warning.call_args_list))
        failed = objects.Appgroup.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ERROR, failed.status)
        self.assertIsNone(failed.deleted_at)
        self.assertEqual(states.NONE, failed.task_state)
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': created['id'], 'action': 'delete'})
        self.assertEqual(conductor_manager.CLEANUP_FAILED, events[0].message)
        self.assertEqual('failure', events[0].outcome)
        self.assertNotIn('secret-token', events[0].message)
        self.assertNotIn('secret-token', str(mgr.notifier.events))
        self.assertIn('coast.appgroup.delete.error',
                      [event for event, body in mgr.notifier.events])
        mgr.execute(self.ctx, 'delete_appgroup', failed)
        self.assertEqual(states.ERROR, objects.Appgroup.get_by_id(
            self.ctx, created['id']).status)
        with mock.patch(cast):
            self.request('delete', '/v1/appgroups/' + created['id'],
                         status=202)
        failed = objects.Appgroup.get_by_id(self.ctx, created['id'])
        mgr.execute(self.ctx, 'delete_appgroup', failed)
        gone = objects.Appgroup.get_by_id(
            self.ctx, created['id'], show_deleted=True)
        self.assertEqual(states.DELETED, gone.status)
        self.assertIsNotNone(gone.deleted_at)
