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

"""Repository-free content through the API, fake RPC and noop driver."""

import json
from unittest import mock

from coast.api import microversions
from coast.common import app_secrets
from coast.common import states
from coast import objects
from coast.tests import conductor


class PastedSourceFlowTest(conductor.ConductorTest):
    def setUp(self):
        super().setUp()
        self.stack_id = self.stack().split('/')[-1]
        self.worker = next(row for row in objects.ServerList.get_all(self.ctx)
                           if row.role == 'worker')
        objects.Volume(
            context=self.ctx, stack_id=self.stack_id,
            server_id=self.worker.id, name='docker', size_gb=20,
            mount_path='/var/lib/docker', purpose='docker',
            status=states.AVAILABLE).create()
        self.headers['OpenStack-API-Version'] = (
            'app-hosting ' + microversions.PASTED_SOURCE_VERSION)

    def create(self, source_type, content):
        body = {'service': {**self.service_parents(self.stack_id),
                            'name': source_type, 'server_id': self.worker.id,
                            'source': {'type': source_type,
                                       source_type: content}}}
        row = self.request('post', '/v1/services', body, status=202,
                           complete=True).json['service']
        service = objects.Service.get_by_id(self.ctx, row['id'])
        self.assertEqual(states.ACTIVE, service.status)
        return service

    def test_dockerfile_create_edit_deploy_rollback_and_delete(self):
        original = 'FROM busybox\nRUN echo pasted-secret-marker\n'
        service = self.create('dockerfile', original)
        path = '/v1/services/' + service.id
        first = objects.Deployment.get_by_id(
            self.ctx, service.current_deployment_id)
        original_ref = service.dockerfile_ref
        self.assertEqual(original_ref, first.spec['dockerfile_ref'])
        changed = original + 'EXPOSE 8080\n'
        self.request('put', path + '/dockerfile',
                     {'dockerfile': {'text': changed}})
        service = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(states.ACTIVE, service.status)
        self.assertEqual(first.id, service.current_deployment_id)
        changed_ref = service.dockerfile_ref
        self.assertNotEqual(original_ref, changed_ref)
        driver = self.manager._driver(objects.Stack.get_by_id(
            self.ctx, self.stack_id))
        with mock.patch.object(driver, 'set_dockerfile',
                               wraps=driver.set_dockerfile) as write:
            self.request('post', path + '/deployments', {'deployment': {}},
                         status=202, complete=True)
            self.assertEqual(changed, write.call_args.args[-1])
            self.request('post', path + '/deployments/' + first.id + '/action',
                         {'rollback': {}}, status=202, complete=True)
            self.assertEqual(original, write.call_args.args[-1])
        for event, payload in self.manager.notifier.events:
            self.assertNotIn('pasted-secret-marker', json.dumps(
                payload, default=str))
        refs = app_secrets.references(self.ctx, service)
        self.assertIn(original_ref, refs)
        self.assertIn(changed_ref, refs)
        self.request('delete', path, status=202, complete=True)
        for ref in refs:
            self.assertNotIn(ref, self.store.values)

    def test_compose_edit_redeploy_and_delete(self):
        content = ('services: {web: {image: nginx, privileged: true, '
                   'volumes: ["data:/data"]}}\nvolumes: {data: {}}\n')
        service = self.create('compose', content)
        self.assertTrue(service.is_engine_service)
        path = '/v1/services/' + service.id
        self.assertEqual(content, self.request('get', path + '/compose').json[
            'compose']['yaml'])
        changed = content.replace('nginx', 'busybox')
        self.request('put', path + '/compose', {'compose': {'yaml': changed}})
        self.request('post', path + '/deployments', {'deployment': {}},
                     status=202, complete=True)
        service = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(states.ACTIVE, service.status)
        self.assertEqual(1, len(objects.StorageList.get_all(
            self.ctx, filters={'service_id': service.id})))
        refs = app_secrets.references(self.ctx, service)
        self.request('delete', path, status=202, complete=True)
        for ref in refs:
            self.assertNotIn(ref, self.store.values)
