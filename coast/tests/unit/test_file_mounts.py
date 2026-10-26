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

from unittest import mock

from coast.common import file_mounts
from coast.common import states
from coast.conductor import manager
from coast.conductor import stack_teardown
from coast import objects
from coast.tests import app_api


class FileMountAPITest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.base = self.path + '/file_mounts'

    def call(self, method, path=None, values=None, status=200, **kwargs):
        body = {'file_mount': values} if values is not None else None
        return self.request(method, path or self.base, body, status=status,
                            version='1.15', **kwargs)

    def create(self, **values):
        return self.call('post', values=dict(
            container_path='/etc/nginx/conf.d/site.conf', content='private',
            **values), status=201).json['file_mount']

    def test_item_query_error_names_parameter_and_value(self):
        created = self.create()
        response = self.call(
            'get', self.base + '/' + created['id'] + '?unknown=bogus',
            status=400)
        self.assertEqual(
            "Invalid query parameter 'unknown' with value 'bogus'. "
            "This endpoint accepts no query parameters.",
            response.json['faultstring'])

    def test_crud_write_only_and_pending(self):
        created = self.create()
        item = self.base + '/' + created['id']
        self.assertNotIn('content', created)
        self.assertNotIn('content_ref', created)
        self.assertEqual([created], self.call('get').json['file_mounts'])
        self.assertEqual(created, self.call('get', item).json['file_mount'])
        row = objects.FileMount.get_by_id(self.ctx, created['id'])
        self.assertEqual('private', self.store.get(self.ctx, row.content_ref))
        first_ref = row.content_ref
        result = self.call('patch', item, {'content': 'replacement'})
        self.assertEqual('true', result.headers['X-Coast-Redeploy-Required'])
        row = objects.FileMount.get_by_id(self.ctx, created['id'])
        self.assertNotEqual(first_ref, row.content_ref)
        self.assertEqual('replacement',
                         self.store.get(self.ctx, row.content_ref))
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertIsNotNone(service.settings_changed_at)
        self.assertEqual([row.content_ref],
                         service.file_mount_secret_refs)
        deleted = self.call('delete', item, status=204)
        self.assertEqual('true', deleted.headers[
            'X-Coast-Redeploy-Required'])
        self.call('get', item, status=404)
        self.assertEqual([], self.call('get').json['file_mounts'])
        self.assertNotIn(first_ref, self.store.values)
        self.assertEqual({}, self.store.values)

    def test_apps_route_and_microversion(self):
        created = self.create()
        app_path = '/v1/apps/' + self.service.id + '/file_mounts'
        self.assertEqual([created],
                         self.call('get', app_path).json['file_mounts'])
        for base in (self.base, app_path):
            for suffix in ('', '/' + created['id']):
                for version in ('1.11', '1.12', '1.13', '1.14'):
                    self.request('get', base + suffix, version=version,
                                 status=404)
        self.service.kind = 'service'
        self.service.save()
        self.call('get', app_path, status=404)
        self.call('get')

    def test_auth_and_isolation_on_every_operation(self):
        created = self.create()
        item = self.base + '/' + created['id']
        operations = [('get', self.base, None), ('get', item, None),
                      ('post', self.base, {'container_path': '/new',
                                           'content': ''}),
                      ('patch', item, {'content': 'x'}),
                      ('delete', item, None)]
        for method, path, values in operations:
            self.call(method, path, values, headers={}, status=401)
            foreign = dict(self.headers, **{'X-Project-Id': 'foreign'})
            self.call(method, path, values, headers=foreign, status=404)
            reader = dict(self.headers, **{'X-Roles': 'reader'})
            self.call(method, path, values, headers=reader,
                      status=200 if method == 'get' else 403)
        member = dict(self.headers, **{'X-Roles': 'member'})
        self.call('delete', item, headers=member, status=403)
        other = objects.Service(
            context=self.ctx, stack_id=self.stack.id, name='other',
            source_id=self.source.id, replicas=1, auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE)
        other.create()
        path = '/v1/services/' + other.id + '/file_mounts/' + created['id']
        for method in ('get', 'patch', 'delete'):
            self.call(method, path, {'content': 'x'} if method == 'patch'
                      else None, status=404)

    def test_invalid_paths_sizes_and_schema(self):
        paths = ('/', 'etc/site.conf', '/a/../b', '/a/./b', '/a//b',
                 '/a/', '/etc/evil\\x', '/etc/x\n')
        for path in paths:
            self.call('post', values={'container_path': path, 'content': 'x'},
                      status=400)
        for content in ('x' * 65537, 'é' * 32769, '\ud800', 'x\x00y'):
            self.call('post', values={'container_path': '/etc/site.conf',
                                      'content': content}, status=400)
        created = self.create()
        self.call('patch', self.base + '/' + created['id'], {}, status=400)
        self.call('patch', self.base + '/' + created['id'],
                  {'container_path': 'relative'}, status=400)
        self.call('post', values={'container_path': '/etc/site.conf'},
                  status=400)
        self.call('get', self.base + '?content=private', status=400)
        self.call('put', status=405)

    def test_image_service_allows_worker_system_paths(self):
        self.source.type = 'image'
        self.source.image = 'nginx:stable'
        self.source.save()
        for path in ('/proc/x', '/sys/x', '/dev/x', '/run/x',
                     '/var/run/docker.sock', '/var/lib/docker/config'):
            with self.subTest(path=path):
                self.call('post', values={'container_path': path,
                                          'content': 'x'}, status=201)

    def test_empty_and_exact_utf8_limit(self):
        self.config.config(max_secret_bytes=87384, group='secrets')
        row = self.call('post', values={'container_path': '/empty',
                                        'content': ''}, status=201)
        self.call('patch', self.base + '/' + row.json['file_mount']['id'],
                  {'content': 'é' * 32768})

    def test_path_collisions_in_both_directions(self):
        self.create()
        for path in ('/etc/nginx/conf.d/site.conf', '/etc/nginx',
                     '/etc/nginx/conf.d/site.conf/child'):
            self.call('post', values={'container_path': path, 'content': ''},
                      status=409)
        objects.Storage(
            context=self.ctx, stack_id=self.stack.id,
            service_id=self.service.id,
            server_id=self.server.id, name='data', size_gb=1,
            status=states.IN_USE, origin='api',
            container_path='/data').create()
        self.call('post', values={
            'container_path': '/data/file', 'content': ''},
                  status=409)
        volume = objects.Storage(
            context=self.ctx, stack_id=self.stack.id, name='new', size_gb=1,
            status=states.AVAILABLE, origin='api')
        volume.create()
        self.request('post', '/v1/stacks/' + self.stack.id + '/storages/' +
                     volume.id + '/attach', {'attach': {
                         'service_id': self.service.id,
                         'container_path': '/etc'}},
                     version='1.15', status=409)

    def test_failed_database_write_cleans_new_secret(self):
        with mock.patch.object(objects.FileMount, 'create',
                               side_effect=RuntimeError('database failed')):
            self.call('post', values={
                'container_path': '/etc/x', 'content': 'x'},
                      status=500)
        self.assertEqual({}, self.store.values)
        self.assertEqual([], list(file_mounts.rows(self.ctx, self.service.id)))

    def test_pending_content_is_part_of_log_masks(self):
        created = self.create()
        self.call('patch', self.base + '/' + created['id'], {'content': 'new'})
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        masks = manager.Manager._log_masks(
            mock.Mock(_variables=lambda *args: {}), self.ctx, service)
        self.assertEqual({'new'},
                         {item.value for item in masks.values()})
        self.assertTrue(all(item.is_secret for item in masks.values()))

    def test_compose_build_pack_accepted_by_api(self):
        body = self.create_body()
        body['service']['source']['build_pack'] = 'compose'
        self.request('post', '/v1/services', body, version='1.17', status=202)
        self.request('patch', self.path, {'service': {'source': {
            'build_pack': 'compose'}}}, version='1.17')

    def test_edits_refuse_busy_workloads_and_exclusive_stacks(self):
        self.service.acquire_task(states.DEPLOYING)
        self.call('post', values={'container_path': '/etc/x', 'content': ''},
                  status=409)
        self.service.release_task(states.DEPLOYING)
        self.stack.acquire_task(states.DELETING)
        self.call('post', values={'container_path': '/etc/x', 'content': ''},
                  status=409)
        self.assertEqual({}, self.store.values)

    def test_stack_cleanup_includes_all_file_content_versions(self):
        created = self.create()
        self.call('patch', self.base + '/' + created['id'], {'content': 'new'})
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        refs = stack_teardown._secret_refs(self.ctx, self.stack)
        self.assertEqual(service.file_mount_secret_refs, refs)

    def test_template_compose_editor_and_file_paths_cannot_overlap(self):
        self.service.template_id = 'wordpress'
        self.service.save()
        compose_path = self.path + '/compose'
        safe = 'services: {web: {image: nginx}}'
        self.request('put', compose_path, {'compose': {'yaml': safe}},
                     version='1.12')
        created = self.create(resource_name='web')
        overlapping = ('services: {web: {image: nginx, '
                       'volumes: ["config:/etc/nginx"]}}\n'
                       'volumes: {config: {}}')
        for path in ('/etc/nginx', '/etc//nginx/', '/'):
            self.request('put', compose_path, {'compose': {'yaml':
                         overlapping.replace('/etc/nginx', path)}},
                         version='1.12', status=409)
        self.call('delete', self.base + '/' + created['id'], status=204)
        objects.Volume(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='docker', size_gb=100,
            mount_path='/var/lib/docker', purpose='docker',
            status=states.AVAILABLE).create()
        self.request('put', compose_path,
                     {'compose': {'yaml': overlapping}}, version='1.12')
        self.call('post', values={
            'container_path': '/etc/nginx/conf.d/site.conf',
            'resource_name': 'web', 'content': 'private'}, status=409)
