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

"""Q99 image validation and persisted source snapshots."""

import copy
from unittest import mock

import jsonschema

from coast.api.v1 import schemas
from coast.common import states
from coast import objects
from coast.tests import app_api


class ImageSourceTest(app_api.AppAPITest):
    image_source = {
        'type': 'image', 'image': 'registry.example.com:5000/web',
        'tag': 'release'}

    def image_body(self, source):
        body = self.create_body()
        body['service']['source'] = source
        return body

    def test_create_patch_and_deploy_snapshot(self):
        created = self.request('post', '/v1/services',
                               self.image_body(self.image_source),
                               status=202).json['service']
        self.assertEqual(self.image_source, created['source'])
        old = objects.Deployment.get_by_id(self.ctx, created['deployment_id'])
        self.assertEqual('release', old.source_ref)
        self.assertEqual(self.image_source, old.spec['source'])
        path = '/v1/services/' + created['id']
        row = self.unlock(created['id'])
        row.status = states.ACTIVE
        row.save()
        patched = self.request('patch', path, {'service': {
            'source': {'tag': '_v2.0-rc'}}}).json['service']
        self.assertEqual('_v2.0-rc', patched['source']['tag'])
        self.assertEqual('release', old.spec['source']['tag'])
        for patch in ({'branch': 'main'}, {'image': 'other'},
                      {'tag': 'v2', 'build_pack': 'static'},
                      {'tag': 'sha256:bad'}):
            self.request('patch', path, {'service': {'source': patch}},
                         status=400)
        for tag in ('sha256:bad', 'web@sha256:bad', 'v1:bad', '',
                    'a' * 129, 'v1\n'):
            self.request('post', path + '/deployments',
                         {'deployment': {'source_ref': tag}}, status=400)
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, row.id).task_state)
        deployed = self.request('post', path + '/deployments',
                                {'deployment': {'source_ref': 'override'}},
                                status=202).json['deployment']
        frozen = objects.Deployment.get_by_id(self.ctx, deployed['id'])
        self.assertEqual('override', frozen.source_ref)
        self.assertEqual('_v2.0-rc', frozen.spec['source']['tag'])

    def test_source_grammar(self):
        for image in ('nginx', 'library/nginx', 'registry.example.com:5000/a',
                      'localhost/a_b/c__d/e--f', '[2001:db8::1]:5000/web'):
            jsonschema.validate(dict(self.image_source, image=image),
                                schemas.IMAGE_SOURCE)
        for image in ('https://example.com/web', 'user:pass@example.com/web',
                      'web:v1', 'web@sha256:abc', 'Web', '/web', 'web/',
                      'web\n', 'a//b', 'a..b', 'a' * 256):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(dict(self.image_source, image=image),
                                    schemas.IMAGE_SOURCE)
        for tag in ('a', '_', 'A0_1.2-rc', 'x' * 128):
            jsonschema.validate(tag, schemas.IMAGE_TAG)

    def test_bad_sources_fail_without_writes(self):
        invalid = []
        for field, value in (
                ('image', 'web@sha256:abc'), ('image', 'web:v1'),
                ('image', 'https://example.com/web'),
                ('image', 'user:password@example.com/web'),
                ('tag', '.bad'), ('tag', '-bad'), ('tag', 'x' * 129),
                ('tag', 'v1\n'), ('credentials', {'password': 'hidden'}),
                ('credentials_ref', 'hidden'), ('repo_url', 'https://a/b'),
                ('branch', 'main'), ('build_pack', 'static'),
                ('dockerfile_path', 'Dockerfile'), ('compose_path', 'x'),
                ('base_directory', '/')):
            invalid.append(dict(self.image_source, **{field: value}))
        missing = copy.deepcopy(self.image_source)
        del missing['tag']
        invalid.append(missing)
        before = len(objects.SourceList.get_all(self.ctx))
        for source in invalid:
            self.request('post', '/v1/services',
                         self.image_body(source), status=400)
        self.assertEqual(before, len(objects.SourceList.get_all(self.ctx)))
        self.assertEqual({}, self.store.values)

    def test_image_source_auth_and_isolation(self):
        for roles, status in (('reader', 403), ('', 403)):
            self.request('post', '/v1/services',
                         self.image_body(self.image_source), status=status,
                         headers=dict(self.headers, **{'X-Roles': roles}))
        self.request('post', '/v1/services',
                     self.image_body(self.image_source), status=401,
                     headers={})
        self.request('post', '/v1/services',
                     self.image_body(self.image_source), status=404,
                     headers=dict(self.headers, **{'X-Project-Id': 'other'}))
        self.request(
            'patch', self.path, {'service': {'source': {'tag': 'v2'}}},
            status=400)

    def test_kind_is_stored_not_derived(self):
        # Regression: an image created through /v1/services at 1.11 is a
        # service (listed, shown and opened as one), not an app.
        def make(name, version, **extra):
            body = self.image_body(self.image_source)
            body['service'].update(name=name, **extra)
            return self.request('post', '/v1/services', body, status=202,
                                version=version).json['service']
        svc = make('img-svc', '1.11')
        app = make('img-app', '1.11', kind='app')
        old = make('img-old', '1.10')
        self.assertEqual('service', svc['kind'])
        self.assertEqual('app', app['kind'])
        self.assertEqual('app', old['kind'])
        for kind, want in (('service', {svc['id']}),
                           ('app', {app['id'], old['id']})):
            rows = self.request('get', '/v1/services?kind=' + kind,
                                version='1.11').json['services']
            self.assertEqual(want, {r['id'] for r in rows} & {
                svc['id'], app['id'], old['id']})
        self.assertEqual('service', objects.Service.get_by_id(
            self.ctx, svc['id']).kind)
        body = self.image_body(self.image_source)
        body['service'].update(name='img-bad', kind='app')
        self.request('post', '/v1/services', body, status=400, version='1.10')
        body = self.create_body()
        body['service'].update(name='git-svc', kind='service')
        self.request('post', '/v1/services', body, status=400, version='1.11')

    def test_kind_defaults_by_microversion(self):
        for version, expected in (('1.27', 'service'), ('1.28', 'app')):
            for requested in (None, 'app', 'service'):
                with self.subTest(version=version, kind=requested):
                    body = self.image_body(self.image_source)
                    body['service'].update(
                        name='image-' + version.replace('.', '-') + '-' +
                        (requested or 'default'), deploy=False)
                    if requested is not None:
                        body['service']['kind'] = requested
                    created = self.request(
                        'post', '/v1/services', body, status=202,
                        version=version).json['service']
                    self.assertEqual(requested or expected, created['kind'])
                    self.assertEqual(requested or expected,
                                     objects.Service.get_by_id(
                                         self.ctx, created['id']).kind)
                    rows = self.request(
                        'get', '/v1/services?appgroup_id=' +
                        self.application.id + '&kind=app',
                        version=version).json['services']
                    self.assertEqual((requested or expected) == 'app',
                                     created['id'] in {r['id'] for r in rows})

    def test_git_build_packs_default_to_app(self):
        for version in ('1.27', '1.28'):
            for pack in ('nixpacks', 'dockerfile', 'static', 'compose'):
                with self.subTest(version=version, pack=pack):
                    body = self.create_body()
                    body['service'].update(
                        name=pack + '-' + version.replace('.', '-'),
                        deploy=False)
                    body['service']['source']['build_pack'] = pack
                    created = self.request(
                        'post', '/v1/services', body, status=202,
                        version=version).json['service']
                    self.assertEqual('app', created['kind'])

    @mock.patch('coast.api.v1.template_controllers.get_template',
                return_value={'ports': []})
    def test_template_defaults_and_explicit_kind(self, get_template):
        for version in ('1.27', '1.28'):
            for requested in (None, 'service', 'app'):
                with self.subTest(version=version, kind=requested):
                    body = self.create_body()
                    body['service'].update(
                        name='template-' + version.replace('.', '-') + '-' +
                        (requested or 'default'),
                        source='template:example', deploy=False)
                    if requested is not None:
                        body['service']['kind'] = requested
                    status = (400 if version == '1.27' and
                              requested == 'app' else 202)
                    response = self.request(
                        'post', '/v1/services', body, status=status,
                        version=version)
                    if status == 202:
                        self.assertEqual(requested or 'service',
                                         response.json['service']['kind'])

    def test_explicit_git_kind_at_1_28(self):
        for requested in ('app', 'service'):
            body = self.create_body()
            body['service'].update(name='git-' + requested, kind=requested,
                                   deploy=False)
            created = self.request('post', '/v1/services', body, status=202,
                                   version='1.28').json['service']
            self.assertEqual(requested, created['kind'])

    def test_patch_rejects_create_only_kind(self):
        for version in ('1.27', '1.28'):
            for kind in ('app', 'service'):
                with self.subTest(version=version, kind=kind):
                    self.request('patch', self.path,
                                 {'service': {'kind': kind}},
                                 version=version, status=400)
        self.assertEqual(self.service.kind, objects.Service.get_by_id(
            self.ctx, self.service.id).kind)
