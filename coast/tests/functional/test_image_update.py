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

"""Image edits through the API, fake RPC and the real Coolify driver."""

import copy

from coast.api import microversions
from coast.common import states
from coast import objects
from coast.tests import deployments


class ImageUpdateFlowTest(deployments.DeploymentTest):
    def test_edit_and_deploy_preserves_resources(self):
        self.headers['OpenStack-API-Version'] = (
            'app-hosting ' + microversions.IMAGE_UPDATE_VERSION)
        source = objects.Source.get_by_id(self.ctx, self.service.source_id)
        source.type = 'image'
        source.image = 'nginx:1.27'
        source.save()
        self.service.ports = '80'
        self.service.health_check = 'none'
        self.service.save()
        path = '/v1/services/' + self.service.id
        engine_path = self.url + '/applications/app-uuid'
        engine = {
            'uuid': 'app-uuid', 'status': 'running:healthy',
            'docker_registry_image_name': 'nginx',
            'docker_registry_image_tag': '1.27',
            'fqdn': 'https://web.example.com',
            'persistent_storages': [{'name': 'data', 'mount_path': '/data'}],
            'file_storages': [{'id': 'file-1', 'mount_path': '/etc/app.conf'}]}
        before = copy.deepcopy(engine)
        self.http.get(engine_path, json=lambda request, context: engine)
        self.http.get(engine_path + '/storages',
                      json=lambda request, context: {
                          key: engine[key] for key in
                          ('persistent_storages', 'file_storages')})

        def update(request, context):
            engine.update(request.json())
            return {'uuid': engine['uuid']}

        self.http.patch(engine_path, json=update)
        storage = objects.Storage(
            context=self.ctx, stack_id=self.env.id, name='data', size_gb=1,
            status=states.IN_USE, origin='api', service_id=self.service.id,
            server_id=self.worker.id, container_path='/data')
        storage.create()
        domain = objects.Domain(
            context=self.ctx, service_id=self.service.id,
            fqdn='web.example.com', auto_dns=False, dns_mode='manual',
            verification_token='challenge', tls='letsencrypt',
            status=states.ACTIVE)
        domain.create()
        self.request('patch', path + '/env', {'env': {
            'MODE': {'value': 'production', 'build_time': False}}})
        env_before = self.request('get', path + '/env').json
        domains_before = self.request('get', path + '/domains').json
        old = objects.Deployment(
            context=self.ctx, service_id=self.service.id,
            server_id=self.worker.id, source_ref='1.27', trigger='api',
            status=states.ACTIVE, spec={'source': {
                'type': 'image', 'image': 'nginx', 'tag': '1.27'}})
        old.create()
        old_spec = copy.deepcopy(old.spec)
        self.service.current_deployment_id = old.id
        self.service.save()
        self.http.post(engine_path + '/envs', json={})
        saved = self.request('patch', path, {'service': {'source': {
            'image': 'registry.example.com/web', 'tag': '1.29-alpine'}}})
        self.assertTrue(saved.json['service']['pending_changes'])
        self.assertEqual(before, engine)  # Save waits for explicit deploy.
        deployment = self.request(
            'post', path + '/deployments', {'deployment': {}},
            status=202, complete=True).json['deployment']
        self.settled(deployment['id'], states.ACTIVE)
        self.assertEqual('registry.example.com/web',
                         engine['docker_registry_image_name'])
        self.assertEqual('1.29-alpine', engine['docker_registry_image_tag'])
        for key in ('uuid', 'fqdn', 'persistent_storages', 'file_storages'):
            self.assertEqual(before[key], engine[key])
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual('app-uuid', service.engine_ref)
        self.assertEqual(source.id, service.source_id)
        self.assertEqual('/data', objects.Storage.get_by_id(
            self.ctx, storage.id).container_path)
        self.assertEqual(env_before, self.request('get', path + '/env').json)
        self.assertEqual(domains_before,
                         self.request('get', path + '/domains').json)
        self.assertEqual(old_spec, objects.Deployment.get_by_id(
            self.ctx, old.id).spec)
        self.assertFalse(self.request('get', path).json['service'][
            'pending_changes'])
        self.assertFalse(any(
            request.method == 'DELETE' or
            (request.method == 'POST' and
             request.url.startswith(self.url + '/applications/') and
             request.url != engine_path + '/envs')
            for request in self.http.request_history))
        self.assertEqual('app-uuid', self.enqueue.last_request.qs['uuid'][0])
        patches = [request for request in self.http.request_history
                   if request.method == 'PATCH' and
                   request.url == engine_path]
        self.assertEqual(1, len(patches))
        env_writes = [request.json() for request in self.http.request_history
                      if request.method == 'POST' and
                      request.url == engine_path + '/envs']
        self.assertEqual('production', env_writes[0]['value'])
        self.assertEqual('MODE', env_writes[0]['key'])
        # Mount reconcile is now part of deploy; unchanged mounts need only
        # a read. Source edits must still preserve volumes and content files.
        self.assertEqual(['GET'], [
            request.method for request in self.http.request_history
            if '/storages' in request.url])
