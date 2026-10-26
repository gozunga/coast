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

"""Editor API through fake RPC, Barbican and Coolify HTTP fixtures."""

import base64

from coast.common import states
from coast import objects
from coast.tests import deployments


SAFE = 'services:\n  web:\n    image: nginx:stable\n'


class ComposeFlowTest(deployments.DeploymentTest):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.12'
        self.service.template_id = 'wordpress-with-mariadb'
        self.service.health_check = 'none'
        self.service.ports = None
        self.service.save()
        self.path = '/v1/services/' + self.service.id
        self.compose_path = self.path + '/compose'
        self.engine_path = self.url + '/services/app-uuid'
        self.http.get(self.engine_path, json={
            'uuid': 'app-uuid', 'docker_compose_raw': SAFE,
            'status': 'running:healthy', 'applications': []})
        self.http.get(self.engine_path + '/envs', json=[])
        self.patched = self.http.patch(self.engine_path, json={})
        self.restarted = self.http.post(self.engine_path + '/restart', json={})

    def test_import_edit_deploy_and_desired_state_wins(self):
        response = self.request('get', self.compose_path)
        self.assertEqual(SAFE, response.json['compose']['yaml'])
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertIsNotNone(service.compose_ref)
        self.http.get(self.engine_path, json={
            'uuid': 'app-uuid', 'docker_compose_raw': 'engine drift',
            'status': 'running:healthy', 'applications': []})
        self.assertEqual(SAFE, self.request('get', self.compose_path).json[
            'compose']['yaml'])
        changed = SAFE.replace('stable', 'alpine')
        self.request('put', self.compose_path, {'compose': {'yaml': changed}})
        self.assertFalse(self.patched.called)
        # The public read after PATCH must report the applied document.
        self.http.get(self.engine_path, json={
            'uuid': 'app-uuid', 'docker_compose_raw': changed,
            'status': 'running:healthy', 'applications': []})
        deployment = self.request(
            'post', self.path + '/deployments', {'deployment': {}},
            status=202, complete=True).json['deployment']
        self.settled(deployment['id'], states.ACTIVE)
        self.assertEqual(changed, base64.b64decode(
            self.patched.last_request.json()['docker_compose_raw']).decode())
        self.assertFalse(self.patched.last_request.json()['instant_deploy'])
        self.assertTrue(self.restarted.called)
        row = objects.Deployment.get_by_id(self.ctx, deployment['id'])
        self.assertEqual(objects.Service.get_by_id(
            self.ctx, self.service.id).compose_ref, row.spec['compose_ref'])
        self.assertFalse(self.request('get', self.path).json['service'][
            'pending_changes'])

    def test_bad_yaml_is_rejected_before_engine_calls(self):
        before = len(self.http.request_history)
        for raw in ('services: {web: [', 'services: {web: []}'):
            with self.subTest(raw=raw):
                self.request('put', self.compose_path,
                             {'compose': {'yaml': raw}}, status=400)
        self.assertEqual(before, len(self.http.request_history))
        self.assertFalse(self.patched.called)
        self.assertFalse(self.restarted.called)
        self.assertIsNone(objects.Service.get_by_id(
            self.ctx, self.service.id).compose_ref)

    def test_worker_host_access_is_saved_without_engine_calls(self):
        before = len(self.http.request_history)
        for raw in (
                'services: {web: {volumes: ["/host:/data"]}}',
                'services: {web: {image: nginx, privileged: true, '
                'network_mode: host, pid: host, volumes: ["/host:/data", '
                '"/var/run/docker.sock:/var/run/docker.sock"]}}'):
            with self.subTest(raw=raw):
                response = self.request('put', self.compose_path,
                                        {'compose': {'yaml': raw}})
                self.assertEqual(raw, response.json['compose']['yaml'])
                service = objects.Service.get_by_id(self.ctx, self.service.id)
                self.assertEqual(raw, self.store.get(
                    self.ctx, service.compose_ref))
                self.assertEqual(raw, self.request(
                    'get', self.compose_path).json['compose']['yaml'])
        self.assertEqual(before, len(self.http.request_history))
        self.assertFalse(self.patched.called)
        self.assertFalse(self.restarted.called)

    def test_compose_sync_failure_prevents_restart(self):
        self.request('put', self.compose_path, {'compose': {'yaml': SAFE}})
        self.http.patch(self.engine_path, status_code=500,
                        json={'message': 'sensitive compose'})
        deployment = self.request(
            'post', self.path + '/deployments', {'deployment': {}},
            status=202, complete=True).json['deployment']
        row = self.settled(deployment['id'], states.FAILED)
        self.assertIn('Compose sync failed', row.fault)
        self.assertFalse(self.restarted.called)
        self.assertNotIn('sensitive compose', self.request(
            'get', self.path + '/deployments/' + deployment['id']).text)

    def test_import_failure_and_foreign_project_do_not_persist(self):
        self.request('get', self.compose_path, headers=dict(
            self.headers, **{'X-Project-Id': 'other'}), status=404)
        self.http.get(self.engine_path, status_code=503)
        self.request('get', self.compose_path, status=503)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertIsNone(service.compose_ref)
        self.assertEqual(states.NONE, service.task_state)

    def test_pinned_engine_git_compose_is_unavailable(self):
        self.service.template_id = None
        self.service.compose_ref = self.store.put(self.ctx, SAFE)
        self.service.save()
        source = objects.Source.get_by_id(self.ctx, self.service.source_id)
        source.build_pack = 'compose'
        source.save()
        self.request('get', self.compose_path, status=409)
        self.request('put', self.compose_path, {'compose': {'yaml': SAFE}},
                     status=409)

    def test_import_busy_service_stack_size_and_missing_content(self):
        for resource in (self.service, self.env):
            resource.acquire_task(states.DELETING)
            self.request('get', self.compose_path, status=409)
            resource.release_task(states.DELETING)
        for raw in ('x' * (self.conf.api.max_compose_bytes + 1), None):
            self.http.get(self.engine_path, json={'docker_compose_raw': raw})
            self.request('get', self.compose_path, status=409)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertIsNone(service.compose_ref)
        self.assertEqual(states.NONE, service.task_state)

    def test_deploy_uses_frozen_reference(self):
        self.request('put', self.compose_path, {'compose': {'yaml': SAFE}})
        deployment_id, message = self.accepted()
        self.request('put', self.compose_path,
                     {'compose': {'yaml': SAFE.replace('stable', 'alpine')}},
                     status=409)
        self.manager.execute(self.ctx, **message)
        self.settled(deployment_id, states.ACTIVE)
        self.assertEqual(SAFE, base64.b64decode(
            self.patched.last_request.json()['docker_compose_raw']).decode())

    def test_compose_112_and_file_mount_115_apply_in_order(self):
        self.http.get(self.engine_path, json={
            'uuid': 'app-uuid', 'docker_compose_raw': SAFE,
            'status': 'running:healthy',
            'applications': [{'name': 'web', 'uuid': 'web-child'}]})
        self.http.get(self.engine_path + '/storages', json={
            'persistent_storages': [], 'file_storages': []})
        posted = self.http.post(self.engine_path + '/storages', json={})
        self.request('put', self.compose_path, {'compose': {'yaml': SAFE}})
        self.request('post', self.path + '/file_mounts', {'file_mount': {
            'container_path': '/etc/site.conf', 'content': 'config file',
            'resource_name': 'web'}}, status=404)
        self.request('post', self.path + '/file_mounts', {'file_mount': {
            'container_path': '/etc/site.conf', 'content': 'config file',
            'resource_name': 'web'}}, status=201, headers={
                **self.headers, 'OpenStack-API-Version': 'app-hosting 1.15'})
        row = self.request('post', self.path + '/deployments',
                           {'deployment': {}}, status=202,
                           complete=True).json['deployment']
        self.settled(row['id'], states.ACTIVE)
        self.assertEqual('config file', posted.last_request.json()['content'])
        writes = [r.path for r in self.http.request_history if r.method in
                  ('POST', 'PATCH') and r.path.startswith('/api/v1/services/')]
        self.assertEqual([
            '/api/v1/services/app-uuid',
            '/api/v1/services/app-uuid/storages',
            '/api/v1/services/app-uuid/restart'], writes[-3:])

    def test_secret_payload_boundary(self):
        self.config.config(max_compose_bytes=30000, group='api')
        raw = SAFE + '#' + 'é' * ((15000 - len(SAFE) - 1) // 2)
        raw += 'x' * (15000 - len(raw.encode('utf-8')))
        before = dict(self.store.values)
        response = self.request('put', self.compose_path,
                                {'compose': {'yaml': raw + 'x'}}, status=400)
        self.assertIn('15000 bytes', response.text)
        self.assertEqual(before, self.store.values)
        self.request('put', self.compose_path, {'compose': {'yaml': raw}})
