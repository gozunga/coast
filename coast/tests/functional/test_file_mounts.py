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

import copy

from coast.common import states
from coast import objects
from coast.tests import deployments


class FileMountFlowTest(deployments.DeploymentTest):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.15'
        self.path = '/v1/services/' + self.service.id
        self.base = self.path + '/file_mounts'
        self.files = []
        self.storage_path = self.url + '/applications/app-uuid/storages'
        self.http.get(self.storage_path, json=lambda request, context: {
            'persistent_storages': [],
            'file_storages': copy.deepcopy(self.files)})

        def create(request, context):
            row = request.json()
            row['uuid'] = 'file-ref'
            self.files.append(row)
            return row

        def delete(request, context):
            self.files.clear()
            return {}

        self.http.post(self.storage_path, json=create)
        self.http.delete(self.storage_path + '/file-ref', json=delete)

    def deploy(self):
        row = self.request('post', self.path + '/deployments',
                           {'deployment': {}}, status=202,
                           complete=True).json['deployment']
        return self.settled(row['id'], states.ACTIVE)

    def test_content_create_update_and_delete_apply_only_at_deploy(self):
        created = self.request('post', self.base, {'file_mount': {
            'container_path': '/etc/site.conf', 'content': 'first secret'}},
            status=201).json['file_mount']
        self.assertEqual([], self.files)
        self.deploy()
        self.assertEqual('first secret', self.files[0]['content'])
        self.assertEqual('coast-files/' + created['id'],
                         self.files[0]['fs_path'])
        item = self.base + '/' + created['id']
        self.request('patch', item, {
            'file_mount': {'content': 'second secret'}})
        self.assertEqual('first secret', self.files[0]['content'])
        self.deploy()
        self.assertEqual('second secret', self.files[0]['content'])
        self.request('delete', item, status=204)
        self.assertEqual(1, len(self.files))
        self.deploy()
        self.assertEqual([], self.files)
        self.assertTrue(objects.Service.get_by_id(
            self.ctx, self.service.id).file_mount_secret_refs)
        calls = [request.path for request in self.http.request_history]
        self.assertLess(calls.index('/api/v1/applications/app-uuid/storages'),
                        calls.index('/api/v1/deploy'))

    def test_storage_failure_prevents_enqueue(self):
        self.request('post', self.base, {'file_mount': {
            'container_path': '/etc/site.conf', 'content': 'private'}},
            status=201)
        self.http.post(self.storage_path, status_code=422,
                       json={'message': 'private'})
        row = self.request('post', self.path + '/deployments',
                           {'deployment': {}}, status=202,
                           complete=True).json['deployment']
        failed = self.settled(row['id'], states.FAILED)
        self.assertNotIn('private', str(failed.fault))
        self.assertEqual(0, self.enqueue.call_count)

    def test_current_and_old_multiline_content_are_masked(self):
        created = self.request('post', self.base, {'file_mount': {
            'container_path': '/etc/site.conf',
            'content': 'first secret\nother secret\n'}},
            status=201).json['file_mount']
        # Stored logs can contain both versions after each has been deployed.
        self.deploy()
        item = self.base + '/' + created['id']
        self.request('patch', item, {
            'file_mount': {'content': 'second secret'}})
        self.deploy()
        self.request('delete', item, status=204)
        self.http.get(self.url + '/applications/app-uuid/logs', json={
            'logs': '2026-10-10T01:00:00Z first secret\n'
                    '2026-10-10T01:00:01Z other secret\n'
                    '2026-10-10T01:00:02Z second secret'})
        logs = self.request('get', self.path + '/logs').text
        for text in ('first secret', 'other secret', 'second secret'):
            self.assertNotIn(text, logs)

    def test_template_container_file_is_applied_before_restart(self):
        self.service.template_id = 'wordpress-with-mariadb'
        self.service.kind = 'service'
        self.service.health_check = 'none'
        self.service.save()
        path = self.url + '/services/app-uuid'
        self.http.get(path, json={
            'status': 'running:healthy',
            'docker_compose_raw': ('services:\n  web:\n'
                                   '    image: example/web\n'
                                   '  db:\n    image: example/db\n'),
            'applications': [{'name': 'web', 'uuid': 'web-child'}],
            'databases': [{'name': 'db', 'uuid': 'db-child'}]})
        self.http.get(path + '/envs', json=[])
        self.http.patch(path, json={})
        self.http.get(path + '/storages', json={
            'persistent_storages': [], 'file_storages': []})
        posted = self.http.post(path + '/storages', json={})
        restart = self.http.post(path + '/restart', json={})
        self.request('post', self.base, {'file_mount': {
            'container_path': '/etc/site.conf', 'content': 'template file',
            'resource_name': 'web'}}, status=201)
        self.assertEqual(0, posted.call_count)
        self.deploy()
        self.assertEqual('web-child', posted.last_request.json()[
            'resource_uuid'])
        self.assertEqual(1, restart.call_count)
        paths = [r.path for r in self.http.request_history]
        self.assertLess(paths.index('/api/v1/services/app-uuid/storages'),
                        paths.index('/api/v1/services/app-uuid/restart'))

    def test_file_only_edit_sets_and_clears_pending_changes(self):
        self.deploy()
        self.assertFalse(self.request('get', self.path).json['service'][
            'pending_changes'])
        self.request('post', self.base, {'file_mount': {
            'container_path': '/etc/site.conf', 'content': 'pending'}},
            status=201)
        self.assertTrue(self.request('get', self.path).json['service'][
            'pending_changes'])
        self.deploy()
        self.assertFalse(self.request('get', self.path).json['service'][
            'pending_changes'])

    def test_rollback_uses_current_desired_files(self):
        first = self.deploy()
        created = self.request('post', self.base, {'file_mount': {
            'container_path': '/etc/site.conf', 'content': 'current file'}},
            status=201).json['file_mount']
        self.deploy()
        self.http.post(self.url + '/applications/app-uuid/rollback',
                       json=self.fixtures['rollback'])
        self.http.get(self.url + '/deployments/rollback-uuid',
                      json=dict(self.fixtures['deployment'],
                                deployment_uuid='rollback-uuid'))
        rollback = self.request(
            'post', self.path + '/deployments/' + first.id + '/action',
            {'rollback': {}}, status=202, complete=True).json['deployment']
        row = self.settled(rollback['id'], states.ACTIVE)
        self.assertEqual('current file', self.files[0]['content'])
        self.assertEqual(created['id'], row.spec['file_mounts'][0]['id'])

    def test_secret_payload_boundaries(self):
        for kind in ('file', 'env'):
            with self.subTest(kind=kind):
                before = dict(self.store.values)
                value = 'é' * 7500 + 'x'
                if kind == 'file':
                    method, path = 'post', self.base
                    body = {'file_mount': {'container_path': '/etc/limit',
                                           'content': value}}
                else:
                    method, path = 'patch', self.path + '/env'
                    body = {'env': {'FIRST': {'value': 'small'},
                                    'LAST': {'value': value}}}
                response = self.request(method, path, body, status=400)
                self.assertIn('15000 bytes', response.text)
                self.assertEqual(before, self.store.values)
                if kind == 'file':
                    body['file_mount']['content'] = value[:-1]
                else:
                    body['env']['LAST']['value'] = value[:-1]
                self.request(method, path, body,
                             status=201 if kind == 'file' else 200)
