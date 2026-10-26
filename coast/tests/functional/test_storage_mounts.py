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


class StorageMountFlowTest(deployments.DeploymentTest):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.19'
        self.base = '/v1/stacks/' + self.env.id + '/storages'
        self.path = '/v1/services/' + self.service.id
        self.mounts = []
        self.storage_path = self.url + '/applications/app-uuid/storages'
        self.http.get(self.storage_path, json=lambda request, context: {
            'persistent_storages': copy.deepcopy([
                row for row in self.mounts if row['type'] == 'persistent']),
            'file_storages': copy.deepcopy([
                row for row in self.mounts if row['type'] == 'file'])})

        def create(request, context):
            row = request.json()
            row['uuid'] = 'mount-ref'
            self.mounts.append(row)
            return row

        def delete(request, context):
            self.mounts.clear()
            return {}

        self.http.post(self.storage_path, json=create)
        self.http.delete(self.storage_path + '/mount-ref', json=delete)

    def deploy(self):
        created = self.request('post', self.path + '/deployments',
                               {'deployment': {}}, status=202,
                               complete=True).json['deployment']
        return self.settled(created['id'], states.ACTIVE)

    def test_both_mount_types_create_edit_delete_and_redeploy(self):
        for kind in ('bind', 'volume'):
            with self.subTest(type=kind):
                values = dict(type=kind, service_id=self.service.id,
                              container_path='/data', read_only=True)
                if kind == 'bind':
                    values['host_path'] = '/srv/data'
                else:
                    values.update(name='data', size_gb=1)
                created = self.request('post', self.base, {'storage': values},
                                       status=201).json['storage']
                self.assertEqual([], self.mounts)
                self.deploy()
                self.assertEqual('/data:ro', self.mounts[0]['mount_path'])
                self.assertEqual('file' if kind == 'bind' else 'persistent',
                                 self.mounts[0]['type'])
                if kind == 'bind':
                    self.assertTrue(self.mounts[0]['is_directory'])
                    self.assertEqual('/srv/data', self.mounts[0]['fs_path'])
                item = self.base + '/' + created['id']
                self.request('patch', item, {'storage': {'read_only': False}})
                self.deploy()
                self.assertEqual('/data', self.mounts[0]['mount_path'])
                self.request('delete', item, status=204)
                self.deploy()
                self.assertEqual([], self.mounts)
                self.assertEqual([], list(objects.StorageList.get_all(
                    self.ctx, filters={'service_id': self.service.id})))

    def test_storage_apply_failure_prevents_enqueue(self):
        self.request('post', self.base, {'storage': {
            'type': 'bind', 'service_id': self.service.id,
            'host_path': '/srv/data', 'container_path': '/data'}}, status=201)
        self.http.post(self.storage_path, status_code=422,
                       json={'message': 'private'})
        created = self.request('post', self.path + '/deployments',
                               {'deployment': {}}, status=202,
                               complete=True).json['deployment']
        failed = self.settled(created['id'], states.FAILED)
        self.assertNotIn('private', str(failed.fault))
        self.assertEqual(0, self.enqueue.call_count)
