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

"""Compose parsing and project-scoped editor API tests."""

import json
import traceback
from unittest import mock

from oslo_config import cfg
import yaml

from coast.common import compose
from coast.common import exception
from coast.common import states
from coast import objects
from coast.tests import app_api
from coast.tests import base


SAFE = 'services:\n  web:\n    image: nginx:stable\n'


class ComposeValidationTest(base.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cfg, 'CONF', self.conf)
        patch.start()
        self.addCleanup(patch.stop)

    def test_named_volumes_and_safe_settings(self):
        document = {'services': {'web': {
            'image': 'nginx', 'privileged': False, 'cap_drop': ['ALL'],
            'security_opt': [], 'read_only': True,
            'volumes': ['data:/data:ro',
                        {'type': 'volume', 'source': 'data',
                         'target': '/more'}]}},
                    'volumes': {'data': None}}
        self.assertEqual(document, compose.parse(yaml.safe_dump(document)))

    def test_worker_features_are_not_restricted(self):
        document = {'services': {'web': {
            'image': 'nginx', 'privileged': True, 'network_mode': 'host',
            'pid': 'host', 'ipc': 'host', 'uts': 'host', 'userns_mode': 'host',
            'devices': ['/dev/sda:/dev/sda'], 'cap_add': ['SYS_ADMIN'],
            'security_opt': ['seccomp=unconfined'],
            'env_file': '/host/env', 'build': {'context': '/host'},
            'volumes': ['/host:/data',
                        '/var/run/docker.sock:/var/run/docker.sock']}},
                    'include': ['/host/compose.yaml'],
                    'networks': {'default': {'driver': 'host'}},
                    'volumes': {'external': {'external': True}}}
        self.assertEqual(document, compose.parse(yaml.safe_dump(document)))

    def test_valid_top_level_volume_names(self):
        raw = SAFE + 'volumes:\n  data: {}\n  my_vol.1: {}\n'
        self.assertEqual({'data': {}, 'my_vol.1': {}},
                         compose.parse(raw)['volumes'])

    def test_invalid_top_level_volume_names(self):
        for key in ('Bad Volume!', '""', '-bad', 'データ', '1'):
            with self.subTest(key=key):
                raw = SAFE + 'volumes:\n  %s: {}\n' % key
                error = self.assertRaises(exception.Invalid,
                                          compose.parse, raw)
                self.assertEqual(
                    'Compose service <document>, key volumes: '
                    'invalid volume name.', str(error))

    def test_missing_top_level_volumes(self):
        self.assertEqual({'services': {'web': {'image': 'nginx:stable'}}},
                         compose.parse(SAFE))

    def test_yaml_parser_context_does_not_expose_content(self):
        raw = 'services: {web: {environment: [PASSWORD=violet}'
        error = self.assertRaises(exception.Invalid, compose.parse, raw)
        self.assertNotIn('violet', ''.join(traceback.format_exception(error)))

    def test_yaml_schema_size_duplicates_and_aliases(self):
        for raw in ('[', 'true', '{}', 'services: []', 'services: {}',
                    'services: {web: []}',
                    'services: {web: {privileged: true, privileged: false}}',
                    'services: {web: &web {image: nginx}, other: *web}',
                    'services: {web: {image: nginx}}\n---\n{}',
                    'services: {web: !!python/object:thing {}}',
                    '[' * 2000, 'services: {web: {volumes: [2]}}'):
            with self.subTest(raw=raw[:40]):
                self.assertRaises(exception.Invalid, compose.parse, raw)
        self.config.config(max_compose_bytes=10, group='api')
        self.assertRaises(exception.Invalid, compose.parse, SAFE)
        self.assertRaises(exception.Invalid, compose.parse, 'é' * 6)


class MountTargetsTest(base.TestCase):
    def test_short_syntax_targets(self):
        for mount in ('data:/data', 'data:/data:ro'):
            with self.subTest(mount=mount):
                document = {'services': {'web': {'volumes': [mount]}}}
                self.assertEqual(['/data'],
                                 list(compose.mount_targets(document)))

    def test_bare_mount_is_skipped(self):
        document = {'services': {'web': {'volumes': ['data']}}}
        self.assertEqual([], list(compose.mount_targets(document)))

    def test_long_syntax_target(self):
        mount = {'type': 'volume', 'source': 'data', 'target': '/data'}
        document = {'services': {'web': {'volumes': [mount]}}}
        self.assertEqual(['/data'], list(compose.mount_targets(document)))


class ComposeAPITest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.service.template_id = 'wordpress'
        self.service.compose_ref = self.store.put(self.ctx, SAFE)
        self.service.save()
        self.compose_path = self.path + '/compose'

    def call(self, method='get', body=None, status=200, **kwargs):
        return self.request(method, self.compose_path, body, status=status,
                            version=kwargs.pop('version', '1.12'), **kwargs)

    def test_get_put_and_deployment_snapshot(self):
        response = self.call()
        self.assertEqual(SAFE, response.json['compose']['yaml'])
        self.assertEqual('no-store', response.headers['Cache-Control'])
        updated = SAFE.replace('stable', 'alpine')
        response = self.call('put', {'compose': {'yaml': updated}})
        self.assertEqual('true',
                         response.headers['X-Coast-Redeploy-Required'])
        self.assertEqual(updated, self.call().json['compose']['yaml'])
        saved = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertNotIn(updated, json.dumps(saved.obj_to_primitive(),
                                             default=str))
        row = self.request('post', self.path + '/deployments',
                           {'deployment': {}}, status=202).json['deployment']
        deployment = objects.Deployment.get_by_id(self.ctx, row['id'])
        self.assertEqual(saved.compose_ref, deployment.spec['compose_ref'])
        self.assertNotIn(updated, json.dumps(deployment.spec))

    def test_validation_does_not_write_or_deploy(self):
        before = objects.Service.get_by_id(self.ctx, self.service.id)
        values = dict(self.store.values)
        raw = 'services: {web: []}'
        response = self.call('put', {'compose': {'yaml': raw}}, status=400)
        self.assertIn('web', response.text)
        self.assertIn('services', response.text)
        self.assertEqual(before.compose_ref, objects.Service.get_by_id(
            self.ctx, self.service.id).compose_ref)
        self.assertEqual(values, self.store.values)
        self.call('put', {'compose': {'yaml': SAFE, 'redeploy': True}},
                  status=400)
        self.call('put', {'compose': {}}, status=400)
        self.call('put', {'compose': {'yaml': 3}}, status=400)

    def test_project_member_only_and_not_found(self):
        for method, body in (('get', None),
                             ('put', {'compose': {'yaml': SAFE}})):
            for headers, status in (({}, 401),
                                    (dict(self.headers,
                                          **{'X-Roles': 'reader'}),
                                     403),
                                    (dict(self.headers, **{'X-Project-Id':
                                                           'other'}), 404)):
                with self.subTest(method=method, status=status):
                    self.call(method, body, headers=headers, status=status)

    def test_version_method_params_and_unmanaged_source(self):
        self.request('get', self.compose_path, version='1.11', status=404)
        self.call('post', {}, status=405)
        self.request('get', self.compose_path + '?unknown=1',
                     version='1.12', status=400)
        self.service.template_id = None
        self.service.compose_ref = None
        self.service.save()
        self.call(status=409)
        self.call('put', {'compose': {'yaml': SAFE}}, status=409)
        self.source.build_pack = 'compose'
        self.source.save()
        self.call(status=409)
        self.call('put', {'compose': {'yaml': SAFE}}, status=409)

    def test_coast_owned_compose_app(self):
        self.service.template_id = None
        self.service.save()
        self.source.build_pack = 'compose'
        self.source.save()
        self.assertEqual(SAFE, self.call().json['compose']['yaml'])
        self.call('put', {'compose': {'yaml': SAFE}})

    def test_busy_service_and_stack_reject_write(self):
        for resource in (self.service, self.stack):
            resource.acquire_task(states.DELETING)
            self.call('put', {'compose': {'yaml': SAFE}}, status=409)
            resource.release_task(states.DELETING)

    def test_barbican_failure_does_not_change_saved_compose(self):
        with mock.patch.object(self.store, 'put', side_effect=RuntimeError(
                'sensitive compose content')):
            response = self.call('put', {'compose': {'yaml': SAFE}},
                                 status=500)
        self.assertNotIn('sensitive compose content', response.text)
        self.assertEqual(SAFE, self.call().json['compose']['yaml'])

    def test_named_volume_accounting_and_quota(self):
        objects.Volume(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='docker', size_gb=20,
            mount_path='/var/lib/docker', purpose='docker',
            status=states.AVAILABLE).create()
        raw = SAFE + '    volumes: [data:/data]\nvolumes: {data: {}}\n'
        self.call('put', {'compose': {'yaml': raw}})
        self.call('put', {'compose': {'yaml': raw}})
        rows = objects.StorageList.get_all(
            self.ctx, filters={'service_id': self.service.id})
        self.assertEqual(1, len(rows))
        self.assertEqual('web-data', rows[0].name)
        self.assertEqual(1, rows[0].size_gb)
        self.call('put', {'compose': {'yaml': SAFE}})
        self.assertEqual(1, len(objects.StorageList.get_all(
            self.ctx, filters={'service_id': self.service.id})))
        self.config.config(storage_gb=1, group='quotas')
        self.call('put', {'compose': {'yaml': raw.replace('data', 'more')}},
                  status=403)
        self.assertEqual(SAFE, self.call().json['compose']['yaml'])

    def test_customer_and_template_compose_allow_worker_access(self):
        self.source.build_pack = 'compose'
        self.source.save()
        raw = yaml.safe_dump({'services': {'web': {
            'image': 'nginx', 'privileged': True, 'network_mode': 'host',
            'pid': 'host', 'ipc': 'host', 'uts': 'host', 'userns_mode': 'host',
            'devices': ['/dev/sda:/dev/sda'], 'cap_add': ['SYS_ADMIN'],
            'volumes': ['/host:/data',
                        '/var/run/docker.sock:/var/run/docker.sock']}}})
        for template in (self.service.template_id, None):
            with self.subTest(template=template):
                self.service.template_id = template
                self.service.save()
                self.call('put', {'compose': {'yaml': raw}}, version='1.17')
                self.assertEqual(raw, self.call().json['compose']['yaml'])

    def test_unknown_ids_invalid_yaml_and_size(self):
        for method, body in (('get', None),
                             ('put', {'compose': {'yaml': SAFE}})):
            self.request(method, '/v1/services/missing/compose', body,
                         version='1.12', status=404)
            self.call(method, body, headers=dict(
                self.headers, **{'X-Roles': 'unrelated'}), status=403)
        oversized = 'x' * (self.conf.api.max_compose_bytes + 1)
        for raw in ('[', 'services: []', 'services: {}', oversized):
            self.call('put', {'compose': {'yaml': raw}}, status=400)
