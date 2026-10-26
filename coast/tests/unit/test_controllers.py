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

"""Controller behavior through the noauth WSGI pipeline and real objects."""

import json
from unittest import mock

import fixtures
from oslo_policy import policy as policy_module

from coast.api.v1 import stack_controllers
from coast.common import context
from coast.common import naming
from coast.common import states
from coast.conductor import stack_teardown
from coast.db import base_models
from coast.drivers import base as driver_base
from coast import objects
from coast.tests.unit import test_api


class ControllerFixture(test_api.APITest):
    def setUp(self):
        super().setUp()
        self.config.config(definitions=json.dumps([
            dict(name='small', kind='vm',
                 roles=['control', 'worker', 'build'], flavor='small'),
            dict(name='worker', kind='vm', roles=['worker'], flavor='worker'),
            dict(name='metal', kind='baremetal', roles=['worker'],
                 flavor='metal')]), group='plans')
        self.config.config(environments=10, servers=10, group='quotas')
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.headers = {'X-User-Id': 'user', 'X-Project-Id': 'project',
                        'X-Roles': 'reader,member,manager'}
        self.ctx = context.RequestContext(project_id='project')
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
            context=self.ctx, stack_id=self.env.id, name='service-data',
            size_gb=10, mount_path='/data', purpose='data',
            status=states.AVAILABLE, cinder_volume_id='cinder-1')
        self.volume.create()
        self.upgrade_body = {'upgrade': {'image_id': 'published'}}
        self.path = '/v1/stacks/' + self.env.id
        self.server_path = self.path + '/servers/' + self.server.id
        self.volume_path = self.path + '/volumes/' + self.volume.id

    def request(self, method, path, body=None, headers=None, status=200):
        headers = self.headers if headers is None else headers
        if body is not None:
            response = getattr(self.client, method + '_json')(
                path, body, headers=headers, status=status)
        else:
            response = getattr(self.client, method)(
                path, headers=headers, status=status)
        if status >= 400:
            self.assert_fault(response, status)
        self.assert_version(response)
        return response


class ControllerTest(ControllerFixture):
    def endpoints(self):
        return [
            ('get', '/v1/stacks', None),
            ('post', '/v1/stacks',
             {'stack': {'name': 'new', 'plan': 'small'}}),
            ('get', self.path, None),
            ('patch', self.path, {'stack': {'name': 'renamed'}}),
            ('delete', self.path, None),
            ('post', self.path + '/action', self.upgrade_body),
            ('get', self.path + '/servers', None),
            ('post', self.path + '/servers', {'server': {
                'name': 'new', 'kind': 'vm', 'role': 'worker',
                'plan': 'small'}}),
            ('get', self.server_path, None),
            ('delete', self.server_path, None),
            ('post', self.server_path + '/action', {'failover': {}}),
            ('get', self.path + '/volumes', None),
            ('post', self.path + '/volumes',
             {'volume': {'name': 'new', 'size_gb': 1}}),
            ('get', self.volume_path, None),
            ('patch', self.volume_path, {'volume': {'size_gb': 11}}),
            ('delete', self.volume_path, None),
            ('post', self.volume_path + '/action', {'snapshot': {}})]

    def test_authentication_and_wrong_role_every_endpoint(self):
        for method, path, body in self.endpoints():
            self.request(method, path, body, headers={}, status=401)
            headers = dict(self.headers, **{'X-Roles': 'unrelated'})
            self.request(method, path, body, headers=headers, status=403)

    def test_worker_credential_cannot_access_another_stack(self):
        # Guests have no Keystone password. Even a coast_agent role token
        # cannot use the tenant API, including another stack in its project.
        foreign = objects.Stack(
            context=self.ctx, name='other-stack', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        foreign.create()
        headers = dict(self.headers, **{
            'X-User-Id': self.server.id, 'X-Roles': 'coast_agent'})
        for stack_id in (self.env.id, foreign.id):
            path = '/v1/stacks/' + stack_id
            self.request('get', path, headers=headers, status=403)
            self.request('patch', path, {'stack': {'name': 'changed'}},
                         headers=headers, status=403)
            self.request('delete', path, headers=headers, status=403)
        self.assertEqual('other-stack', objects.Stack.get_by_id(
            self.ctx, foreign.id).name)

    def test_other_project_every_resource_endpoint(self):
        headers = dict(self.headers, **{'X-Project-Id': 'other'})
        for method, path, body in self.endpoints():
            if path != '/v1/stacks':
                self.request(method, path, body, headers=headers, status=404)
        result = self.request('get', '/v1/stacks', headers=headers)
        self.assertEqual([], result.json['stacks'])

    def test_reader_cannot_write_and_failover_requires_admin(self):
        for method, path, body in self.endpoints():
            if method != 'get':
                headers = dict(self.headers, **{'X-Roles': 'reader'})
                self.request(method, path, body, headers=headers, status=403)
        self.request('post', self.server_path + '/action',
                     {'failover': {}}, status=403)
        with mock.patch.object(stack_controllers.engine_upgrade, 'eligible'):
            self.request('post', self.path + '/action', self.upgrade_body,
                         status=202)

    def test_create_persists_and_passes_options(self):
        body = {'name': 'new', 'description': 'description', 'plan': 'small',
                'workers': [{'kind': 'vm', 'plan': 'worker'}],
                'build_server': False, 'data_volume_gb': 100}
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'create_stack') as handler:
            result = self.request('post', '/v1/stacks',
                                  {'stack': body}, status=202)
        self.assertNotIn('driver', result.json['stack'])
        self.assertNotRegex(result.text.lower(), 'coolify|traefik')
        row = objects.Stack.get_by_id(
            self.ctx, result.json['stack']['id'])
        self.assertEqual(states.PENDING, row.status)
        self.assertEqual(states.PROVISIONING, row.task_state)
        self.assertEqual(body, handler.call_args.args[2])
        result = self.request('post', self.path + '/servers', {'server': {
            'name': 'new', 'kind': 'vm', 'role': 'build', 'plan': 'worker'}},
            status=202)
        row = objects.Server.get_by_id(self.ctx, result.json['server']['id'])
        self.assertEqual(states.PENDING, row.status)
        self.assertEqual(states.PROVISIONING, row.task_state)
        self.assertIsNone(row.flavor_id)

    def test_unknown_fields_and_schema_errors(self):
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        for method, path, body in self.endpoints():
            if body is not None:
                self.request(method, path, {'unknown': 'value'},
                             headers=admin, status=400)
        for values in ({}, {'name': 'new', 'plan': 2},
                       {'name': 'new', 'plan': 'small', 'workers': [
                           {'kind': 'byo', 'plan': 'worker'}]},
                       {'name': 'new', 'plan': 'small',
                        'data_volume_gb': True}):
            self.request('post', '/v1/stacks',
                         {'stack': values}, status=400)
        for kind, role in (('byo', 'worker'), ('vm', 'control'),
                           ('baremetal', 'worker')):
            self.request('post', self.path + '/servers', {'server': {
                'name': 'new', 'plan': 'small', 'kind': kind, 'role': role}},
                status=400)
        self.client.post('/v1/stacks', '{bad json',
                         content_type='application/json', headers=self.headers,
                         status=400)
        self.request('delete', self.path, {'unknown': True}, status=400)
        self.request('delete', self.server_path, {'unknown': True}, status=400)

    def test_detail_visibility_and_fields(self):
        for path, key in ((self.path, 'stack'),
                          (self.server_path, 'server')):
            result = self.request('get', path).json[key]
            self.assertNotIn('task_state', result)
            self.assertNotIn('engine_ref', result)
            admin = dict(self.headers, **{'X-Roles': 'admin'})
            result = self.request('get', path, headers=admin).json[key]
            self.assertIn('task_state', result)
            self.assertIn('engine_ref', result)
            result = self.request('get', path + '?fields=id,name').json[key]
            self.assertEqual({'id', 'name', 'links'}, set(result))
        self.request('get', self.path + '?fields=engine_token_ref', status=400)

    def test_stack_delete_names_appgroups_and_does_not_cast(self):
        self.config.config(max_limit=1, group='api')
        for name in ('b', 'a'):
            objects.Appgroup(
                context=self.ctx, stack_id=self.env.id, name=name,
                status=states.ACTIVE, task_state=states.NONE).create()
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast') as cast:
            response = self.request('delete', self.path, status=409)
            admin = dict(self.headers, **{'X-Roles': 'admin'})
            self.request('delete', self.path, headers=admin, status=409)
        self.assertIn('existing still has 2 app groups: a, b. '
                      'Delete them first.', response.text)
        cast.assert_not_called()
        row = objects.Stack.get_by_id(self.ctx, self.env.id)
        self.assertEqual(states.ACTIVE, row.status)
        self.assertEqual(states.NONE, row.task_state)

    def test_empty_stack_delete_casts(self):
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast') as cast:
            self.request('delete', self.path, status=202)
        cast.assert_called_once()

    def test_deleted_and_foreign_appgroups_do_not_block_delete(self):
        for project in ('project', 'foreign'):
            ctx = context.RequestContext(project_id=project)
            stack = self.env
            if project == 'foreign':
                stack = objects.Stack(
                    context=ctx, name='other', plan='small', driver='noop',
                    engine_version='test', status=states.ACTIVE,
                    task_state=states.NONE)
                stack.create()
            app = objects.Appgroup(
                context=ctx, stack_id=stack.id, name='private',
                status=states.ACTIVE, task_state=states.NONE)
            app.create()
            if project == 'project':
                app.destroy()
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast'):
            self.request('delete', self.path, status=202)

    def test_stack_fault_visible_to_project_reader(self):
        self.env.fault = 'Volume cleanup failed; retry delete'
        self.env.save()
        result = self.request('get', self.path)
        self.assertEqual(self.env.fault, result.json['stack']['fault'])

    def test_teardown_fault_hides_engine_names_on_tenant_reads(self):
        reason = ('Coolify API returned HTTP 401 \u2014 Traefik cleanup '
                  'failed password=private-value')
        failure = driver_base.EngineAuthError(reason)
        with mock.patch.object(stack_teardown, '_teardown',
                               side_effect=failure):
            with mock.patch.object(stack_teardown.LOG, 'error') as log:
                stack_teardown.teardown(self.ctx, self.env, mock.Mock())
        expected = ('application engine API returned HTTP 401 - '
                    'application routing cleanup failed password=***')
        for roles in ('reader', 'reader,member,manager'):
            headers = dict(self.headers, **{'X-Roles': roles})
            shown = self.request('get', self.path, headers=headers)
            listed = self.request('get', '/v1/stacks', headers=headers)
            self.assertEqual(expected, shown.json['stack']['fault'])
            self.assertEqual(expected, listed.json['stacks'][0]['fault'])
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        shown = self.request('get', self.path, headers=admin)
        self.assertIn('Coolify API returned HTTP 401',
                      shown.json['stack']['fault'])
        self.assertNotIn('private-value', shown.json['stack']['fault'])
        info = log.call_args.kwargs['exc_info']
        self.assertIn('Coolify API returned HTTP 401', str(info[1]))
        self.assertNotIn('private-value', str(info[1]))
        self.assertIsNotNone(info[2])
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.env.id, 'action': 'delete',
            'outcome': 'failure'})
        self.assertEqual(expected, events[0].message)

    def test_update_and_delete(self):
        result = self.request('patch', self.path,
                              {'stack': {'description': 'changed'}})
        self.assertEqual('changed', result.json['stack']['description'])
        result = self.request('delete', self.server_path, status=202)
        self.assertEqual(states.DELETING, result.json['server']['status'])
        # The server is already DELETING. A second delete starts cleanup again.
        result = self.request('delete', self.server_path, status=202)
        self.assertEqual(states.DELETING, result.json['server']['status'])
        result = self.request('delete', self.path, status=202)
        self.assertEqual(states.DELETING, result.json['stack']['status'])
        self.request('delete', self.path, status=409)

    def test_delete_accepts_failed_and_stuck_servers(self):
        allowed = (states.ERROR, states.UNHEALTHY, states.PENDING,
                   states.BUILDING, states.FAILOVER, states.ACTIVE)
        for status in allowed:
            row = objects.Server(
                context=self.ctx, name='srv-' + status.lower(),
                stack_id=self.env.id, kind='vm', role='worker',
                status=status, task_state=states.NONE)
            row.create()
            result = self.request(
                'delete', self.path + '/servers/' + row.id, status=202)
            self.assertEqual(states.DELETING, result.json['server']['status'])
            fresh = objects.Server.get_by_id(self.ctx, row.id)
            self.assertEqual(states.DELETING, fresh.task_state)
        stuck = objects.Server(
            context=self.ctx, name='stuck', stack_id=self.env.id,
            kind='vm', role='worker', status=states.DELETING,
            task_state=states.NONE)
        stuck.create()
        stuck.acquire_task(states.DELETING)
        result = self.request(
            'delete', self.path + '/servers/' + stuck.id, status=202)
        self.assertEqual(states.DELETING, result.json['server']['status'])
        fresh = objects.Server.get_by_id(self.ctx, stuck.id)
        self.assertEqual(states.DELETING, fresh.status)
        self.assertEqual(states.DELETING, fresh.task_state)

    def test_error_control_and_placed_service_stay_refused(self):
        control = objects.Server(
            context=self.ctx, name='control', stack_id=self.env.id,
            kind='vm', role='worker', status=states.ERROR,
            task_state=states.NONE)
        control.create()
        control.role = 'control'
        control.save()
        self.request(
            'delete', self.path + '/servers/' + control.id, status=400)
        self.assertEqual(states.ERROR, objects.Server.get_by_id(
            self.ctx, control.id).status)
        failed = objects.Server(
            context=self.ctx, name='placed', stack_id=self.env.id,
            kind='vm', role='worker', status=states.ERROR,
            task_state=states.NONE)
        failed.create()
        source = objects.Source(context=self.ctx, type='git')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.env.id, name='placed',
            source_id=source.id, server_id=failed.id, ports='3000',
            replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE)
        service.create()
        self.request(
            'delete', self.path + '/servers/' + failed.id, status=409)
        fresh = objects.Server.get_by_id(self.ctx, failed.id)
        self.assertEqual(states.ERROR, fresh.status)
        self.assertEqual(states.NONE, fresh.task_state)

    def test_busy_delete_and_control_delete(self):
        self.env.acquire_task(states.UPGRADING)
        self.request('delete', self.path, status=409)
        self.server.acquire_task(states.FAILING_OVER)
        self.request('delete', self.server_path, status=409)
        self.server.release_task(states.FAILING_OVER)
        self.server.role = 'control'
        self.server.save()
        self.request('delete', self.server_path, status=400)

    def test_admin_actions_and_state_preconditions(self):
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        for path, body, key, status in (
                (self.server_path, {'failover': {}}, 'server',
                 states.FAILOVER),
                (self.path, {'upgrade': {'image_id': 'published'}},
                 'stack', states.UPGRADING)):
            with mock.patch.object(
                    stack_controllers.engine_upgrade, 'eligible'):
                result = self.request('post', path + '/action', body,
                                      headers=admin, status=202)
            self.assertEqual(status, result.json[key]['status'])
            with mock.patch.object(
                    stack_controllers.engine_upgrade, 'eligible'):
                self.request('post', path + '/action', body,
                             headers=admin, status=409)

    def test_pagination_filters_sort_and_foreign_parent(self):
        self.request('post', '/v1/stacks', {'stack': {
            'name': 'z-last', 'plan': 'small'}}, status=202)
        result = self.request('get', '/v1/stacks?limit=1&sort_key=name')
        self.assertEqual('existing', result.json['stacks'][0]['name'])
        next_url = result.json['links'][0]['href']
        result = self.request('get', next_url)
        self.assertEqual('z-last', result.json['stacks'][0]['name'])
        self.assertEqual([], result.json['links'])
        result = self.request('get', '/v1/stacks?name=missing')
        self.assertEqual([], result.json['stacks'])
        for query in ('limit=bad', 'sort_key=engine_token_ref',
                      'all_projects=true', 'project_id=other'):
            self.request('get', '/v1/stacks?' + query,
                         status=403 if 'projects' in query or
                         'project_id' in query else 400)
        self.request('get', self.path + '/servers?marker=' + self.env.id,
                     status=404)
        self.request('get',
                     '/v1/stacks/missing/servers/' + self.server.id,
                     status=404)

    def test_microversion_and_volume_collection(self):
        headers = dict(self.headers, **{
            'OpenStack-API-Version': 'app-hosting 1.99'})
        self.client.get(self.path, headers=headers, status=406)
        result = self.request('get', self.path + '/volumes')
        self.assertEqual(self.volume.id, result.json['volumes'][0]['id'])

    def test_busy_actions_do_not_change_status(self):
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        self.env.acquire_task(states.PROVISIONING)
        self.server.acquire_task(states.PROVISIONING)
        for path, body in ((self.path, self.upgrade_body),
                           (self.server_path, {'failover': {}})):
            with mock.patch.object(
                    stack_controllers.engine_upgrade, 'eligible'):
                self.request('post', path + '/action', body,
                             headers=admin, status=409)
        self.assertEqual(states.ACTIVE, objects.Stack.get_by_id(
            self.ctx, self.env.id).status)
        self.assertEqual(states.ACTIVE, objects.Server.get_by_id(
            self.ctx, self.server.id).status)

    def test_upgrade_running_deployments_and_server_placement(self):
        source = objects.Source(context=self.ctx, type='git')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.env.id, name='service',
            source_id=source.id, server_id=self.server.id, ports='3000',
            replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE)
        service.create()
        deployment = objects.Deployment(
            context=self.ctx, service_id=service.id, spec={}, trigger='api',
            status=states.PENDING)
        deployment.create()
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        for status in (states.PENDING, states.BUILDING, states.DEPLOYING):
            deployment.status = status
            deployment.save()
            with mock.patch.object(
                    stack_controllers.engine_upgrade, 'eligible'):
                self.request('post', self.path + '/action', self.upgrade_body,
                             headers=admin, status=409)
        self.request('delete', self.server_path, status=409)
        self.assertEqual(states.NONE, objects.Server.get_by_id(
            self.ctx, self.server.id).task_state)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_detail_policy_override_not_role_check(self):
        policy = stack_controllers.policy.get_enforcer()
        policy.load_rules()
        policy.set_rules(policy_module.Rules.from_dict({
            'coast:stack:get_detail': 'role:member',
            'coast:server:get_detail': 'role:member',
            'coast:volume:get_detail': 'role:member'}), overwrite=False)
        for path, key in ((self.path, 'stack'),
                          (self.server_path, 'server')):
            self.assertIn('task_state', self.request('get', path).json[key])
            headers = dict(self.headers, **{'X-Roles': 'reader'})
            self.assertNotIn('task_state', self.request(
                'get', path, headers=headers).json[key])
        self.assertIn('cinder_volume_id', self.request(
            'get', self.volume_path).json['volume'])
        self.assertNotIn('cinder_volume_id', self.request(
            'get', self.volume_path, headers=headers).json['volume'])

    def test_admin_list_scope_and_server_pagination(self):
        other = context.RequestContext(project_id='other')
        row = objects.Stack(
            context=other, name='foreign', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        row.create()
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        result = self.request('get', '/v1/stacks?all_projects=true',
                              headers=admin)
        self.assertEqual(2, len(result.json['stacks']))
        result = self.request('get', '/v1/stacks?project_id=other',
                              headers=admin)
        self.assertEqual(row.id, result.json['stacks'][0]['id'])
        self.request('get', '/v1/stacks?marker=' + row.id, status=404)
        self.request('post', self.path + '/servers', {'server': {
            'name': 'a-first', 'kind': 'vm', 'role': 'worker',
            'plan': 'small'}},
            status=202)
        result = self.request(
            'get', self.path + '/servers?limit=1&sort_key=name')
        self.assertEqual('a-first', result.json['servers'][0]['name'])
        result = self.request('get', result.json['links'][0]['href'])
        self.assertEqual('worker', result.json['servers'][0]['name'])

    def test_same_project_wrong_stack_is_not_found(self):
        row = objects.Stack(
            context=self.ctx, name='second', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        row.create()
        path = '/v1/stacks/' + row.id + '/servers/' + self.server.id
        for method, suffix, body in (
                ('get', '', None), ('delete', '', None),
                ('post', '/action', {'failover': {}})):
            self.request(method, path + suffix, body, status=404)
        volume_path = ('/v1/stacks/' + row.id + '/volumes/' +
                       self.volume.id)
        for method, suffix, body in (
                ('get', '', None),
                ('patch', '', {'volume': {'size_gb': 20}}),
                ('delete', '', None),
                ('post', '/action', {'snapshot': {}})):
            self.request(method, volume_path + suffix, body, status=404)

    def test_get_bad_parameters_every_read_endpoint(self):
        for path in ('/v1/stacks', self.path,
                     self.path + '/servers', self.server_path,
                     self.path + '/volumes', self.volume_path):
            self.request('get', path + '?fields=nonexistent', status=400)

    def test_nested_unknown_fields_and_unclaimed_create_delete(self):
        self.request('post', '/v1/stacks', {'stack': {
            'name': 'bad', 'plan': 'small', 'workers': [
                {'kind': 'vm', 'plan': 'small', 'unknown': 'secret'}]}},
            status=400)
        result = self.request('post', '/v1/stacks', {'stack': {
            'name': 'new', 'plan': 'small'}}, status=202)
        self.request('delete', '/v1/stacks/' +
                     result.json['stack']['id'], status=202)

    def test_delete_rejects_unsupported_offboard_options(self):
        before = objects.Stack.get_by_id(self.ctx, self.env.id)
        bodies = (
            {'snapshot_retention_days': 14},
            {'snapshot_volumes': False, 'snapshot_retention_days': 14},
            {'export_before_delete': True, 'snapshot_retention_days': 14},
            {'snapshot_retention_days': 91},
            {'snapshot_volumes': True, 'snapshot_retention_days': 91},
        )
        for body in bodies:
            with mock.patch.object(stack_controllers.handlers.Handler,
                                   'delete_stack') as handler:
                self.request('delete', self.path, body, status=400)
            handler.assert_not_called()
            row = objects.Stack.get_by_id(self.ctx, self.env.id)
            self.assertEqual(before.status, row.status)
            self.assertEqual(before.task_state, row.task_state)
            self.assertEqual(before.updated_at, row.updated_at)

    def test_delete_retention_defaults_and_cap(self):
        self.config.config(max_snapshot_retention_days=1, group='export')
        before = objects.Stack.get_by_id(self.ctx, self.env.id)
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'delete_stack') as handler:
            self.request('delete', self.path, status=400)
        handler.assert_not_called()
        row = objects.Stack.get_by_id(self.ctx, self.env.id)
        self.assertEqual(before.status, row.status)
        self.assertEqual(before.task_state, row.task_state)
        self.config.config(max_snapshot_retention_days=90, group='export')
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'delete_stack') as handler:
            self.request('delete', self.path, {
                'snapshot_volumes': False,
                'export_before_delete': False}, status=202)
        self.assertEqual(self.conf.export.snapshot_retention_days,
                         handler.call_args.args[2]['snapshot_retention_days'])
        self.assertIs(False, handler.call_args.args[2]['snapshot_volumes'])
        self.assertIs(
            False, handler.call_args.args[2]['export_before_delete'])
        other = objects.Stack(
            context=self.ctx, name='snapshot-delete', plan='small',
            driver='noop', engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        other.create()
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'delete_stack') as handler:
            self.request('delete', '/v1/stacks/' + other.id, {
                'snapshot_volumes': True, 'snapshot_retention_days': 14,
                'export_before_delete': False}, status=202)
        self.assertIs(True, handler.call_args.args[2]['snapshot_volumes'])
        self.assertEqual(
            14, handler.call_args.args[2]['snapshot_retention_days'])
        self.assertIs(
            False, handler.call_args.args[2]['export_before_delete'])
        exported = objects.Stack(
            context=self.ctx, name='export-delete', plan='small',
            driver='noop', engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        exported.create()
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'delete_stack') as handler:
            self.request('delete', '/v1/stacks/' + exported.id, {
                'export_before_delete': True}, status=202)
        self.assertIs(
            True, handler.call_args.args[2]['export_before_delete'])
        self.assertEqual(self.conf.export.snapshot_retention_days,
                         handler.call_args.args[2]['snapshot_retention_days'])
        both = objects.Stack(
            context=self.ctx, name='export-and-snapshot', plan='small',
            driver='noop', engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        both.create()
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'delete_stack') as handler:
            self.request('delete', '/v1/stacks/' + both.id, {
                'snapshot_volumes': True, 'export_before_delete': True,
                'snapshot_retention_days': 14}, status=202)
        self.assertIs(True, handler.call_args.args[2]['snapshot_volumes'])
        self.assertIs(
            True, handler.call_args.args[2]['export_before_delete'])
        self.assertEqual(
            14, handler.call_args.args[2]['snapshot_retention_days'])

    def test_upgrade_checks_services_despite_public_page_limit(self):
        self.config.config(max_limit=1, group='api')
        source = objects.Source(context=self.ctx, type='git')
        source.create()
        for name in ('first', 'second'):
            service = objects.Service(
                context=self.ctx, stack_id=self.env.id, name=name,
                source_id=source.id, ports='3000', replicas=1,
                auto_deploy=False, status=states.ACTIVE,
                task_state=states.NONE)
            service.create()
        deployment = objects.Deployment(
            context=self.ctx, service_id=service.id, spec={}, trigger='api',
            status=states.PENDING)
        deployment.create()
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        with mock.patch.object(stack_controllers.engine_upgrade, 'eligible'):
            self.request('post', self.path + '/action', self.upgrade_body,
                         headers=admin, status=409)

    def test_empty_list_fields_and_datetime_filters(self):
        self.request('get', '/v1/stacks?name=missing&fields=unknown',
                     status=400)
        self.request('get', '/v1/stacks?created_at=invalid', status=400)
        result = self.request('get', self.path).json['stack']
        result = self.request('get', '/v1/stacks?created_at=' +
                              result['created_at'].replace('+', '%2B'))
        self.assertEqual(self.env.id, result.json['stacks'][0]['id'])

    def test_baremetal_feature_flag(self):
        self.config.config(baremetal_servers=1, group='quotas')
        self.config.config(enable_baremetal=True, group='controller_worker')
        result = self.request('post', self.path + '/servers', {'server': {
            'name': 'metal', 'plan': 'small', 'kind': 'baremetal',
            'role': 'worker'}}, status=202)
        self.assertEqual('baremetal', result.json['server']['kind'])

    def test_duplicate_stack_conflict(self):
        result = self.request('post', '/v1/stacks', {'stack': {
            'name': 'existing', 'plan': 'small'}}, status=409)
        self.assertIn('A stack named existing already exists in this '
                      'project.', result.text)

    def test_hostname_collision_checks_only_live_project_peers(self):
        candidate = objects.Server(name='worker-2')
        naming.assign_server_hostname(self.ctx, self.env, candidate,
                                      conf=self.conf)
        first = candidate.hostname
        self.server.hostname = first
        self.server.save()
        naming.assign_server_hostname(self.ctx, self.env, candidate,
                                      conf=self.conf)
        self.assertNotEqual(first, candidate.hostname)
        self.server.destroy()
        naming.assign_server_hostname(self.ctx, self.env, candidate,
                                      conf=self.conf)
        self.assertEqual(first, candidate.hostname)
        other = context.RequestContext(project_id='other')
        stack = objects.Stack(context=other, name='other', plan='small',
                              driver='noop', engine_version='test',
                              status=states.ACTIVE, task_state=states.NONE)
        stack.create()
        objects.Server(context=other, stack_id=stack.id, name='peer',
                       hostname=first, kind='vm', role='worker',
                       status=states.ACTIVE, task_state=states.NONE).create()
        naming.assign_server_hostname(self.ctx, self.env, candidate,
                                      conf=self.conf)
        self.assertEqual(first, candidate.hostname)

    def test_saved_hostname_survives_renames(self):
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'create_server'):
            result = self.request('post', self.path + '/servers', {
                'server': {'name': 'worker-2', 'kind': 'vm',
                           'role': 'worker', 'plan': 'worker'}}, status=202)
        row = objects.Server.get_by_id(self.ctx, result.json['server']['id'])
        saved = row.hostname
        self.assertEqual(saved, result.json['server']['hostname'])
        row.release_task(states.PROVISIONING)
        row.name = 'renamed'
        row.save()
        self.request('patch', self.path, {'stack': {'name': 'new-stack'}},
                     status=200)
        self.assertEqual(saved, objects.Server.get_by_id(
            self.ctx, row.id).hostname)
        self.request('post', self.path + '/servers', {'server': {
            'kind': 'vm', 'role': 'worker', 'plan': 'worker',
            'hostname': 'other'}}, status=400)

    def test_server_name_defaults_per_role(self):
        for name in ('worker-2', 'worker-7x', 'build-1'):
            objects.Server(context=self.ctx, name=name, stack_id=self.env.id,
                           kind='vm', role='worker', status=states.ACTIVE,
                           task_state=states.NONE).create()
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'create_server'):
            names = [self.request('post', self.path + '/servers', {
                'server': {'kind': 'vm', 'role': role, 'plan': 'worker'}},
                status=202).json['server']['name']
                for role in ('worker', 'build', 'worker')]
        self.assertEqual(['worker-3', 'build-2', 'worker-4'], names)
        self.assertEqual('worker', objects.Server.get_by_id(
            self.ctx, self.server.id).name)

    def _volume(self, name, status=states.AVAILABLE, purpose='data',
                size_gb=5):
        row = objects.Volume(
            context=self.ctx, stack_id=self.env.id, name=name,
            size_gb=size_gb, mount_path='/data', purpose=purpose,
            status=status)
        row.create()
        return row

    def test_volume_create_extend_and_detail(self):
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'create_volume') as handler:
            result = self.request('post', self.path + '/volumes', {'volume': {
                'name': 'plain', 'size_gb': 4, 'server_id': self.server.id,
                'volume_type': None, 'mount_path': '/srv/data'}}, status=202)
        body = result.json['volume']
        self.assertEqual(states.CREATING, body['status'])
        self.assertEqual('/srv/data', body['mount_path'])
        self.assertEqual('data', body['purpose'])
        self.assertEqual(self.server.id, body['server_id'])
        self.assertNotIn('cinder_volume_id', body)
        self.assertEqual('plain', handler.call_args.args[2]['name'])
        created = self.path + '/volumes/' + body['id']
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'delete_volume') as deleted:
            self.request('delete', created, status=409)
        deleted.assert_not_called()  # Active creation and attachment block.
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'extend_volume') as extended:
            result = self.request('patch', self.volume_path, {'volume': {
                'size_gb': 12}}, status=202)
        self.assertEqual(12, result.json['volume']['size_gb'])
        self.assertEqual(states.AVAILABLE, result.json['volume']['status'])
        extended.assert_called_once()
        self.request('patch', self.volume_path, {'volume': {'size_gb': 12}},
                     status=400)
        self.request('patch', self.volume_path, {'volume': {'size_gb': 11}},
                     status=400)
        shown = self.request('get', self.volume_path).json['volume']
        self.assertNotIn('cinder_volume_id', shown)
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        shown = self.request('get', self.volume_path, headers=admin).json[
            'volume']
        self.assertEqual('cinder-1', shown['cinder_volume_id'])
        listed = self.request('get', self.path + '/volumes')
        self.assertNotIn('cinder_volume_id', listed.json['volumes'][0])
        listed = self.request('get', self.path + '/volumes', headers=admin)
        self.assertEqual(
            'cinder-1', listed.json['volumes'][0]['cinder_volume_id'])
        self.request('get', self.volume_path + '?fields=cinder_volume_id',
                     status=400)
        filtered = self.request(
            'get', self.volume_path + '?fields=cinder_volume_id',
            headers=admin).json['volume']
        self.assertEqual({'cinder_volume_id', 'links'}, set(filtered))

    def test_volume_snapshot_transition_and_busy(self):
        used = self._volume('used', status=states.IN_USE)
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'snapshot_volume') as snap:
            result = self.request(
                'post', self.path + '/volumes/' + used.id + '/action',
                {'snapshot': {}}, status=202)
            self.assertEqual(states.SNAPSHOTTING,
                             result.json['volume']['status'])
            self.request(
                'post', self.path + '/volumes/' + used.id + '/action',
                {'snapshot': {}}, status=409)
            self.assertEqual(1, snap.call_count)
        self.assertEqual(states.SNAPSHOTTING, objects.Volume.get_by_id(
            self.ctx, used.id).status)
        for status in (states.CREATING, states.ERROR, states.DELETING):
            row = self._volume(status, status=status)
            path = self.path + '/volumes/' + row.id + '/action'
            self.request('post', path, {'snapshot': {}}, status=409)
            self.assertEqual(status, objects.Volume.get_by_id(
                self.ctx, row.id).status)
        with mock.patch.object(stack_controllers.handlers.Handler,
                               'snapshot_volume') as snap:
            result = self.request('post', self.volume_path + '/action',
                                  {'snapshot': {}}, status=202)
            self.assertEqual(1, snap.call_count)
        self.assertEqual(states.SNAPSHOTTING, result.json['volume']['status'])

    def test_coast_managed_volume_purposes(self):
        for purpose in ('docker', 'scratch', 'controller'):
            self.request('post', self.path + '/volumes', {'volume': {
                'name': purpose, 'size_gb': 1, 'purpose': purpose}},
                status=400)
            row = self._volume(purpose + '-row', purpose=purpose)
            path = self.path + '/volumes/' + row.id
            self.request('delete', path, status=400)
            self.request('patch', path, {'volume': {'size_gb': 9}},
                         status=400)
            code = 202 if purpose == 'docker' else 400
            self.request('post', path + '/action', {'snapshot': {}},
                         status=code)
            if purpose == 'docker':
                self.assertEqual(states.SNAPSHOTTING,
                                 objects.Volume.get_by_id(
                                     self.ctx, row.id).status)

    def test_volume_schema_server_scope_and_roles(self):
        for body in (
                {},
                {'volume': {}},
                {'volume': {'name': 'n'}},
                {'volume': {'name': 'n', 'size_gb': 0}},
                {'volume': {'name': 'n', 'size_gb': -1}},
                {'volume': {'name': 'n', 'size_gb': True}},
                {'volume': {'name': 'n', 'size_gb': 1, 'purpose': 'nope'}},
                {'volume': {'name': 'n', 'size_gb': 1, 'extra': 1}},
                {'volume': {'name': 'n', 'size_gb': 1, 'server_id': ''}}):
            self.request('post', self.path + '/volumes', body, status=400)
        self.request('patch', self.volume_path, {'volume': {}}, status=400)
        self.request('patch', self.volume_path, {'volume': {'name': 'x'}},
                     status=400)
        self.request('patch', self.volume_path, {'size_gb': 20}, status=400)
        self.request('post', self.volume_path + '/action', {'failover': {}},
                     status=400)
        self.request('post', self.volume_path + '/action',
                     {'snapshot': {'now': True}}, status=400)
        self.request('delete', self.volume_path, {'unknown': True},
                     status=400)
        self.request('get', self.path + '/volumes/missing', status=404)
        other = objects.Stack(
            context=self.ctx, name='second', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        other.create()
        other_server = objects.Server(
            context=self.ctx, name='other', stack_id=other.id,
            kind='vm', role='worker', status=states.ACTIVE,
            task_state=states.NONE)
        other_server.create()
        self.request('post', self.path + '/volumes', {'volume': {
            'name': 'bad-server', 'size_gb': 1,
            'server_id': other_server.id}}, status=400)
        foreign = context.RequestContext(project_id='other')
        foreign_env = objects.Stack(
            context=foreign, name='foreign', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        foreign_env.create()
        foreign_server = objects.Server(
            context=foreign, name='foreign', stack_id=foreign_env.id,
            kind='vm', role='worker', status=states.ACTIVE,
            task_state=states.NONE)
        foreign_server.create()
        self.request('post', self.path + '/volumes', {'volume': {
            'name': 'hidden', 'size_gb': 1,
            'server_id': foreign_server.id}}, status=404)
        member = dict(self.headers, **{'X-Roles': 'member'})
        self.request('post', self.path + '/volumes', {'volume': {
            'name': 'member', 'size_gb': 1}}, headers=member, status=202)
        self.request('patch', self.volume_path, {'volume': {'size_gb': 11}},
                     headers=member, status=202)
        self.request('post', self.volume_path + '/action', {'snapshot': {}},
                     headers=member, status=202)
        self.request('delete', self.volume_path, headers=member, status=403)
        self.request('delete', self.volume_path, status=409)
        self.assertEqual(states.SNAPSHOTTING, objects.Volume.get_by_id(
            self.ctx, self.volume.id).status)

    def test_volume_pagination(self):
        self.request('post', self.path + '/volumes', {'volume': {
            'name': 'z-data', 'size_gb': 2}}, status=202)
        result = self.request(
            'get', self.path + '/volumes?limit=1&sort_key=name')
        self.assertEqual('service-data', result.json['volumes'][0]['name'])
        result = self.request('get', result.json['links'][0]['href'])
        self.assertEqual('z-data', result.json['volumes'][0]['name'])
        self.assertEqual([], result.json['links'])
        result = self.request('get', self.path + '/volumes?purpose=data')
        self.assertEqual(2, len(result.json['volumes']))
        self.request('get', self.path + '/volumes?cinder_volume_id=cinder-1',
                     status=400)
        self.request('get', self.path + '/volumes?marker=' + self.server.id,
                     status=404)
