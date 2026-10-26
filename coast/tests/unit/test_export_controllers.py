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

"""Export API validation, isolation, locks and ephemeral URLs."""

import datetime
from unittest import mock

import fixtures
from oslo_policy import policy as policy_module

from coast.api import handlers
from coast.api.v1 import stack_controllers
from coast.common import context
from coast.common import exception
from coast.common import states
from coast.conductor import rpcapi
from coast.db import api as db_api
from coast.drivers import base as driver_base
from coast import objects
from coast.tests.unit import test_controllers


class ExportControllerTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.config.config(max_concurrent_per_project=10, group='export')
        self.export = objects.Export(
            context=self.ctx, stack_id=self.env.id, backend='swift',
            volume_ids=[self.volume.id], status=states.PENDING)
        self.export.create()
        self.exports_path = self.path + '/exports'
        self.export_path = self.exports_path + '/' + self.export.id

    def endpoints(self):
        return [('get', self.exports_path, None),
                ('post', self.exports_path, {'export': {}}),
                ('get', self.export_path, None),
                ('delete', self.export_path, None),
                ('post', self.export_path + '/action', {'renew_url': {}})]

    def test_authentication_wrong_role_and_foreign_project(self):
        for method, path, body in self.endpoints():
            self.request(method, path, body, headers={}, status=401)
            wrong = dict(self.headers, **{'X-Roles': 'unrelated'})
            self.request(method, path, body, headers=wrong, status=403)
            other = dict(self.headers, **{'X-Project-Id': 'other'})
            self.request(method, path, body, headers=other, status=404)
            admin = dict(other, **{'X-Roles': 'admin'})
            self.request(method, path, body, headers=admin, status=404)

    def test_unsupported_methods(self):
        self.request('patch', self.export_path, {'export': {}}, status=405)
        self.request('delete', self.exports_path, status=405)
        self.request('get', self.export_path + '/action', status=405)

    def activate(self):
        for status in (states.SNAPSHOTTING, states.UPLOADING, states.ACTIVE):
            self.export.status = status
            self.export.save()
        self.export.object_expires_at = datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(hours=72)
        self.export.artifacts = [{'volume_id': self.volume.id,
                                 'name': 'data.tar.gz', 'size_bytes': 10,
                                  'sha256': 'digest', 'object_key': 'internal',
                                  'download_url': 'must-not-be-returned'}]
        self.export.save()

    def release(self):
        objects.Stack.get_by_id(self.ctx, self.env.id).release_task(
            states.RECONCILING)

    def test_create_null_selects_data_and_docker_across_pages(self):
        self.config.config(max_limit=1, group='api')
        docker = objects.Volume(
            context=self.ctx, stack_id=self.env.id, name='docker',
            size_gb=1, mount_path='/var/lib/docker', purpose='docker',
            status=states.IN_USE)
        docker.create()
        scratch = objects.Volume(
            context=self.ctx, stack_id=self.env.id, name='scratch',
            size_gb=1, mount_path='/scratch', purpose='scratch',
            status=states.AVAILABLE)
        scratch.create()
        for options in ({}, {'volume_ids': None},
                        {'volume_ids': [self.volume.id]}):
            with mock.patch.object(handlers.Handler, 'create_export') as call:
                response = self.request('post', self.exports_path,
                                        {'export': options}, status=202)
            row = objects.Export.get_by_id(self.ctx, response.json['export'][
                'id'])
            expected = ([self.volume.id] if options.get('volume_ids') else
                        [self.volume.id, docker.id])
            self.assertEqual(set(expected), set(row.volume_ids))
            self.assertEqual(states.PENDING, row.status)
            self.assertEqual('swift', row.backend)
            env = objects.Stack.get_by_id(self.ctx, self.env.id)
            self.assertEqual(states.ACTIVE, env.status)
            self.assertEqual(states.RECONCILING, env.task_state)
            self.assertEqual(options, call.call_args.args[2])
            self.request('post', self.exports_path, {'export': options},
                         status=409)
            self.release()

    def test_bad_schema(self):
        for body in ({}, {'export': None}, {'export': {'unknown': True}},
                     {'export': {'volume_ids': []}},
                     {'export': {'volume_ids': [self.volume.id] * 2}},
                     {'export': {'volume_ids': 'all'}},
                     {'export': {'volume_ids': [True]}},
                     {'export': {'volume_ids': ['x' * 37]}},
                     {'export': {'project_id': 'other'}}):
            self.request('post', self.exports_path, body, status=400)
        self.client.post(self.exports_path, '{bad',
                         content_type='application/json', headers=self.headers,
                         status=400)
        self.request('delete', self.export_path, {'unknown': True}, status=400)
        for body in ({'renew_url': {'unknown': True}}, {'unknown': {}},
                     {'renew_url': {}, 'delete': {}}):
            self.request('post', self.export_path + '/action', body,
                         status=400)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_selected_volume_isolation_and_rollback(self):
        other = context.RequestContext(project_id='other')
        env = objects.Stack(
            context=other, name='foreign', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        env.create()
        volume = objects.Volume(
            context=other, stack_id=env.id, name='foreign', size_gb=1,
            mount_path='/data', purpose='data', status=states.AVAILABLE)
        volume.create()
        for id in ('missing', volume.id):
            self.request('post', self.exports_path,
                         {'export': {'volume_ids': [id]}}, status=404)
        second = objects.Stack(
            context=self.ctx, name='second', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        second.create()
        self.request('post', '/v1/stacks/' + second.id + '/exports',
                     {'export': {'volume_ids': [self.volume.id]}}, status=400)
        for purpose in ('scratch', 'controller'):
            self.volume.purpose = purpose
            self.volume.save()
            self.request('post', self.exports_path,
                         {'export': {'volume_ids': [self.volume.id]}},
                         status=400)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        self.assertEqual(1, len(objects.ExportList.get_all(self.ctx)))

    def test_empty_or_unready_selection(self):
        self.volume.purpose = 'scratch'
        self.volume.save()
        self.request('post', self.exports_path, {'export': {}}, status=400)
        self.volume.purpose = 'data'
        self.volume.save()
        self.volume.transition_status(states.SNAPSHOTTING)
        self.request('post', self.exports_path, {'export': {}}, status=409)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_busy_create_and_invalid_stack_do_not_insert(self):
        self.env.acquire_task(states.UPGRADING)
        self.request('post', self.exports_path, {'export': {}}, status=409)
        self.env.release_task(states.UPGRADING)
        self.env.status = states.ERROR
        self.env.save()
        self.request('post', self.exports_path, {'export': {}}, status=409)
        self.assertEqual(1, len(objects.ExportList.get_all(self.ctx)))
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_delete_retains_row_and_repeated_delete_conflicts(self):
        result = self.request('delete', self.export_path, status=202)
        self.assertEqual(states.DELETING, result.json['export']['status'])
        self.assertEqual(states.DELETING, objects.Export.get_by_id(
            self.ctx, self.export.id).status)
        self.request('delete', self.export_path, status=409)

    def test_delete_from_every_forward_state(self):
        for status in states.EXPORT_STATES - {states.DELETING}:
            row = objects.Export(
                context=self.ctx, stack_id=self.env.id, backend='swift',
                volume_ids=[self.volume.id], status=status)
            row.create()
            self.request('delete', self.exports_path + '/' + row.id,
                         status=202)
            self.assertEqual(states.DELETING, objects.Export.get_by_id(
                self.ctx, row.id).status)

    def test_delete_accepted_create_does_not_take_second_task(self):
        data = self.request('post', self.exports_path, {'export': {}},
                            status=202).json['export']
        self.request('delete', self.exports_path + '/' + data['id'],
                     status=202)
        self.assertEqual(states.RECONCILING, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_delete_cas_rejects_stale_status(self):
        original = db_api.cas_status

        def race(ctx, model, id, **kwargs):
            db_api.exports.update(ctx, id, {'status': states.ERROR})
            return original(ctx, model, id, **kwargs)

        with mock.patch.object(db_api, 'cas_status', side_effect=race):
            self.request('delete', self.export_path, status=409)
        # The entire failed acceptance transaction rolls back.
        self.assertEqual(states.PENDING, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_member_reads_metadata_but_cannot_write_or_download(self):
        member = dict(self.headers, **{'X-Roles': 'reader,member'})
        self.activate()
        result = self.request('get', self.export_path, headers=member)
        self.assertEqual([{'volume_id': self.volume.id, 'name': 'data.tar.gz',
                           'size_bytes': 10, 'sha256': 'digest'}],
                         result.json['export']['artifacts'])
        for method, path, body in self.endpoints():
            if method != 'get':
                self.request(method, path, body, headers=member, status=403)
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        for method, path, body in self.endpoints():
            self.request(method, path, body, headers=reader, status=403)

    def test_url_policy_validity_and_never_persisted(self):
        self.activate()
        expiry = datetime.datetime.now(datetime.timezone.utc) + (
            datetime.timedelta(hours=1))
        urls = [{'volume_id': self.volume.id,
                 'download_url': 'https://objects.example.com/signed',
                 'expires_at': expiry}]
        with mock.patch.object(handlers.Handler, 'export_urls',
                               return_value=urls):
            for method, path, body in (
                    ('get', self.export_path, None),
                    ('get', self.exports_path, None),
                    ('post', self.export_path + '/action', {'renew_url': {}})):
                data = self.request(method, path, body).json
                artifact = (data['exports'][0] if 'exports' in data else
                            data['export'])['artifacts'][0]
                self.assertEqual(urls[0]['download_url'],
                                 artifact['download_url'])
            urls[0]['expires_at'] = expiry - datetime.timedelta(hours=2)
            data = self.request('get', self.export_path).json['export']
            self.assertNotIn('download_url', data['artifacts'][0])
            urls[0]['expires_at'] = expiry + datetime.timedelta(days=4)
            data = self.request('get', self.export_path).json['export']
            self.assertNotIn('download_url', data['artifacts'][0])
        row = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual('must-not-be-returned',
                         row.artifacts[0]['download_url'])
        self.assertNotIn('expires_at', row.artifacts[0])
        self.assertNotIn('backend', data)
        self.assertNotIn('fault', data)
        self.assertNotIn('object_key', data['artifacts'][0])

    def test_renew_returns_503_for_engine_faults(self):
        self.activate()
        self.env.driver = 'coolify'
        self.env.save()
        secret = 'password=secret'
        cases = (
            (driver_base.EngineUnreachable(secret), 503,
             'Service Unavailable', 'coast.drivers.base.EngineUnreachable'),
            (driver_base.EngineAuthError(secret), 503,
             'Service Unavailable', 'coast.drivers.base.EngineAuthError'),
            (driver_base.EngineNotFound(secret), 500,
             'Internal Server Error', None),
            (RuntimeError(secret), 500, 'Internal Server Error', None),
        )
        for error, status, title, cause in cases:
            logs = self.useFixture(fixtures.FakeLogger(
                name='coast.api.errors'))
            with mock.patch.object(rpcapi.ConductorAPI, 'get_export_urls',
                                   side_effect=error):
                response = self.request(
                    'post', self.export_path + '/action', {'renew_url': {}},
                    status=status)
            self.assertEqual(title, response.json['faultstring'])
            self.assertNotIn(secret, response.text)
            self.assertNotIn('Traceback', response.text)
            self.assertNotIn(secret, logs.output)
            if cause is not None:
                self.assertIn(cause, logs.output)
        self.assertEqual(states.ACTIVE, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_renew_expired_pending_and_noop(self):
        self.request('post', self.export_path + '/action', {'renew_url': {}},
                     status=409)
        self.activate()
        result = self.request('post', self.export_path + '/action',
                              {'renew_url': {}}).json['export']
        self.assertNotIn('download_url', result['artifacts'][0])
        self.export.object_expires_at = datetime.datetime.now(
            datetime.timezone.utc) - datetime.timedelta(seconds=1)
        self.export.save()
        self.request('post', self.export_path + '/action', {'renew_url': {}},
                     status=409)
        self.assertNotIn('download_url', self.request(
            'get', self.export_path).json['export']['artifacts'][0])

    def test_policy_override_controls_url_visibility(self):
        self.activate()
        enforcer = stack_controllers.policy.get_enforcer()
        enforcer.load_rules()
        enforcer.set_rules(policy_module.Rules.from_dict({
            'coast:export:renew_url': 'role:member'}), overwrite=False)
        urls = [{'volume_id': self.volume.id,
                 'download_url': 'https://objects.example.com/signed',
                 'expires_at': datetime.datetime.now(datetime.timezone.utc) +
                 datetime.timedelta(minutes=1)}]
        member = dict(self.headers, **{'X-Roles': 'member'})
        with mock.patch.object(handlers.Handler, 'export_urls',
                               return_value=urls):
            data = self.request('get', self.export_path,
                                headers=member).json['export']
        self.assertIn('download_url', data['artifacts'][0])

    def test_listing_fields_filters_pagination_and_parent_binding(self):
        second = objects.Export(
            context=self.ctx, stack_id=self.env.id, backend='swift',
            volume_ids=[self.volume.id], status=states.ERROR)
        second.create()
        result = self.request('get', self.exports_path + '?limit=1')
        self.assertEqual(self.export.id, result.json['exports'][0]['id'])
        result = self.request('get', result.json['links'][0]['href'])
        self.assertEqual(second.id, result.json['exports'][0]['id'])
        self.assertEqual([], result.json['links'])
        result = self.request('get', self.exports_path + '?status=ERROR')
        self.assertEqual(second.id, result.json['exports'][0]['id'])
        for path in (self.exports_path, self.export_path):
            result = self.request('get', path + '?fields=id,status').json
            row = result['exports'][0] if 'exports' in result else result[
                'export']
            self.assertEqual({'id', 'status', 'links'}, set(row))
            self.request('get', path + '?fields=fault', status=400)
        for query in ('limit=bad', 'limit=-1', 'sort_key=backend',
                      'sort_key=artifacts', 'sort_dir=bad', 'unknown=true',
                      'object_expires_at=bad', 'all_projects=bad'):
            self.request('get', self.exports_path + '?' + query, status=400)
        for query in ('all_projects=true', 'project_id=other'):
            self.request('get', self.exports_path + '?' + query, status=403)
        self.request('get', self.exports_path + '?marker=' + self.env.id,
                     status=404)
        env = objects.Stack(
            context=self.ctx, name='second', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        env.create()
        base = '/v1/stacks/' + env.id + '/exports'
        for method, _, body in self.endpoints():
            if method != 'post' or body == {'renew_url': {}}:
                suffix = '/action' if body else ''
                self.request(method, base + '/' + self.export.id + suffix,
                             body, status=404)
        self.request('get', base + '?marker=' + self.export.id, status=404)

    def test_microversion_all_export_endpoints(self):
        headers = dict(self.headers, **{
            'OpenStack-API-Version': 'app-hosting 1.99'})
        for method, path, body in self.endpoints():
            if body is None:
                getattr(self.client, method)(path, headers=headers, status=406)
            else:
                getattr(self.client, method + '_json')(
                    path, body, headers=headers, status=406)

    def test_quota_hook_denial_does_not_write(self):
        with mock.patch.object(stack_controllers, 'quota_hook',
                               side_effect=exception.NotAuthorized()):
            self.request('post', self.exports_path, {'export': {}}, status=403)
        self.assertEqual(1, len(objects.ExportList.get_all(self.ctx)))
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
