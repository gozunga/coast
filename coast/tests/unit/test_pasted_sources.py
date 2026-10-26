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

"""Protected repository-free sources and their editor contracts."""

import json
from unittest import mock

from coast.api import microversions
from coast.common import states
from coast import objects
from coast.tests import app_api


VERSION = microversions.PASTED_SOURCE_VERSION
DOCKERFILE = 'FROM busybox\nRUN echo private-content-marker\n'
COMPOSE = 'services: {web: {image: nginx, privileged: true}}'


class PastedSourceAPITest(app_api.AppAPITest):
    def create(self, source_type='dockerfile', content=DOCKERFILE, **extra):
        body = self.create_body()
        body['service'].update(
            source={'type': source_type, source_type: content}, **extra)
        return self.request('post', '/v1/services', body, version=VERSION,
                            status=202).json['service']

    def test_source_content_is_stored_only_in_barbican(self):
        for kind, content in (('dockerfile', DOCKERFILE),
                              ('compose', COMPOSE)):
            row = self.create(kind, content)
            self.assertEqual({'type': kind}, row['source'])
            self.assertEqual('service' if kind == 'compose' else 'app',
                             row['kind'])
            saved = self.unlock(row['id'])
            self.assertEqual(content, self.store.get(
                self.ctx, getattr(saved, kind + '_ref')))
            self.assertNotIn(content, json.dumps(saved.obj_to_primitive(),
                                                 default=str))
            deployment = objects.DeploymentList.get_all(
                self.ctx, filters={'service_id': row['id']})[0]
            self.assertEqual(getattr(saved, kind + '_ref'),
                             deployment.spec[kind + '_ref'])
            self.assertNotIn('private-content-marker', json.dumps(
                deployment.spec))
            saved.name = kind
            saved.save()

    def test_compose_app_and_dockerfile_kind(self):
        row = self.create('compose', COMPOSE, kind='app')
        self.assertEqual('app', row['kind'])
        body = self.create_body()
        body['service'].update(
            source={'type': 'dockerfile', 'dockerfile': DOCKERFILE},
            kind='service')
        self.request('post', '/v1/services', body, version=VERSION, status=400)

    def test_schema_version_and_size_errors_redact_content(self):
        body = self.create_body()
        for kind, content in (('dockerfile', DOCKERFILE),
                              ('compose', COMPOSE)):
            body['service']['source'] = {'type': kind, kind: content}
            self.request('post', '/v1/services', body, version='1.30',
                         status=400)
            for raw in ('', 123, '\ud800', 'private-content-marker' * 1000):
                body['service']['source'][kind] = raw
                response = self.request('post', '/v1/services', body,
                                        version=VERSION, status=400)
                self.assertNotIn('private-content-marker', response.text)
            body['service']['source'] = {
                'type': kind, kind: content,
                'extra': 'private-content-marker'}
            self.request('post', '/v1/services', body, version=VERSION,
                         status=400)
        self.assertFalse(objects.ServiceList.get_all(
            self.ctx, filters={'name': 'new'}))

    def test_compose_validation_names_only_the_field(self):
        body = self.create_body()
        body['service']['source'] = {
            'type': 'compose', 'compose': 'services: {private-marker: []}'}
        response = self.request('post', '/v1/services', body,
                                version=VERSION, status=400)
        self.assertNotIn('private-marker', response.text)
        row = self.create('compose', COMPOSE)
        self.unlock(row['id'])
        response = self.request(
            'put', '/v1/services/' + row['id'] + '/compose',
            {'compose': {'yaml': 'services: {private-marker: []}'}},
            version=VERSION, status=400)
        self.assertNotIn('private-marker', response.text)

    def dockerfile(self):
        row = self.create()
        self.unlock(row['id'])
        return '/v1/services/' + row['id'], row['id']

    def test_dockerfile_edit_frozen_snapshots_and_no_deploy(self):
        path, identifier = self.dockerfile()
        editor = path + '/dockerfile'
        response = self.request('get', editor, version=VERSION)
        self.assertEqual(DOCKERFILE, response.json['dockerfile']['text'])
        self.assertEqual('no-store', response.headers['Cache-Control'])
        old = objects.Service.get_by_id(self.ctx, identifier).dockerfile_ref
        changed = DOCKERFILE + 'EXPOSE 8080\n'
        response = self.request('put', editor,
                                {'dockerfile': {'text': changed}},
                                version=VERSION)
        self.assertEqual('true', response.headers['X-Coast-Redeploy-Required'])
        self.assertEqual(DOCKERFILE, self.store.get(self.ctx, old))
        saved = objects.Service.get_by_id(self.ctx, identifier)
        self.assertEqual(changed, self.store.get(
            self.ctx, saved.dockerfile_ref))
        self.assertEqual(states.NONE, saved.task_state)
        self.assertEqual(1, len(objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': identifier})))
        self.request('post', path + '/deployments', {'deployment': {}},
                     version=VERSION, status=202)
        dep = objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': identifier},
            sort_key='number', sort_dir='desc')[0]
        self.assertEqual(saved.dockerfile_ref, dep.spec['dockerfile_ref'])

    def test_dockerfile_authorization_gating_and_bad_requests(self):
        path, identifier = self.dockerfile()
        editor = path + '/dockerfile'
        body = {'dockerfile': {'text': DOCKERFILE}}
        for method in ('get', 'put'):
            for headers, status in (({}, 401),
                                    (dict(self.headers, **{
                                        'X-Roles': 'reader'}), 403),
                                    (dict(self.headers, **{
                                        'X-Project-Id': 'other'}), 404)):
                self.request(method, editor, body if method == 'put' else None,
                             version=VERSION, headers=headers, status=status)
            self.request(method, editor, body if method == 'put' else None,
                         version='1.30', status=404)
        self.request('post', editor, body, version=VERSION, status=405)
        self.request('get', editor + '?unknown=1', version=VERSION, status=400)
        self.request('get', '/v1/services/missing/dockerfile',
                     version=VERSION, status=404)
        for incoming in ({}, {'dockerfile': {}},
                         {'dockerfile': {'text': DOCKERFILE, 'extra': True}},
                         {'dockerfile': {'text': ''}}):
            self.request('put', editor, incoming, version=VERSION, status=400)
        for resource in (objects.Service.get_by_id(self.ctx, identifier),
                         self.stack):
            resource.acquire_task(states.DELETING)
            self.request('put', editor, body, version=VERSION, status=409)
            resource.release_task(states.DELETING)
        with mock.patch.object(self.store, 'put', side_effect=RuntimeError(
                'private-content-marker')):
            response = self.request('put', editor, body, version=VERSION,
                                    status=500)
        self.assertNotIn('private-content-marker', response.text)
        self.request('get', self.path + '/dockerfile', version=VERSION,
                     status=409)

    def test_pasted_compose_uses_existing_editor_and_storage_accounting(self):
        objects.Volume(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='docker', size_gb=20,
            mount_path='/var/lib/docker', purpose='docker',
            status=states.AVAILABLE).create()
        raw = COMPOSE + '\nvolumes: {data: {}}\n'
        row = self.create('compose', raw)
        saved = self.unlock(row['id'])
        self.assertTrue(saved.is_engine_service)
        path = '/v1/services/' + row['id'] + '/compose'
        self.assertEqual(raw, self.request('get', path, version=VERSION).json[
            'compose']['yaml'])
        self.request('put', path, {'compose': {'yaml': raw}}, version=VERSION)
        storage = objects.StorageList.get_all(
            self.ctx, filters={'service_id': row['id']})
        self.assertEqual(1, len(storage))
        self.assertEqual('template', storage[0].origin)
