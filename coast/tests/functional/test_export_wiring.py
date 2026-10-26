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

"""Export API to conductor over fake RPC with fake cloud services."""

import datetime
import json
from unittest import mock
from urllib import parse

import fixtures

from coast.common import states
from coast.conductor import exports
from coast.drivers import base as driver_base
from coast import objects
from coast.tests import conductor
from coast.tests.unit.test_export_wiring import configure_s3
from coast.tests.unit.test_export_wiring import S3_ACCESS
from coast.tests.unit.test_export_wiring import s3_client_factory
from coast.tests.unit.test_export_wiring import S3_SECRET


class ExportAPIRPCFlowTest(conductor.ConductorTest):
    def test_archive_show_renew_visibility_and_delete(self):
        stack = objects.Stack(
            context=self.ctx, name='export-stack', plan='small',
            driver='coolify', engine_version='4.4.3', status=states.ACTIVE,
            task_state=states.NONE)
        stack.create()
        server = objects.Server(
            context=self.ctx, name='worker', stack_id=stack.id, kind='vm',
            role='worker', compute_id='nova-worker', mgmt_ip='192.0.2.10',
            status=states.ACTIVE, task_state=states.NONE)
        server.create()
        volume = objects.Volume(
            context=self.ctx, stack_id=stack.id, server_id=server.id,
            name='data', size_gb=1, mount_path='/data', purpose='data',
            status=states.IN_USE, cinder_volume_id='cinder-source')
        volume.create()
        storage = mock.Mock()
        storage.urls.return_value = [{
            'volume_id': volume.id,
            'download_url': ('https://objects.example.com/archive'
                             '?temp_url_sig=x'),
            'expires_at': (datetime.datetime.now(datetime.timezone.utc) +
                           datetime.timedelta(hours=1)).isoformat()}]

        def archive(flow, ctx, parent, row, backend):
            flow.owner._save(row, status=states.UPLOADING)
            row.artifacts = [{'volume_id': volume.id, 'name': 'data.tar.gz',
                              'size_bytes': 42, 'sha256': 'a' * 64}]
            row.object_expires_at = datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(hours=72)

        path = '/v1/stacks/' + stack.id + '/exports'
        with (mock.patch.object(exports, 'SwiftStorage', return_value=storage),
              mock.patch.object(exports.ExportFlow, 'create', autospec=True,
                                side_effect=archive)):
            accepted = self.request('post', path, {'export': {}},
                                    status=202, complete=True).json['export']
            item = path + '/' + accepted['id']
            shown = self.request('get', item).json['export']
            self.assertEqual(states.ACTIVE, shown['status'])
            self.assertEqual(42, shown['artifacts'][0]['size_bytes'])
            self.assertIn('download_url', shown['artifacts'][0])
            reader = dict(self.headers, **{'X-Roles': 'reader,member'})
            hidden = self.request('get', item, headers=reader).json['export']
            self.assertNotIn('download_url', hidden['artifacts'][0])
            self.request('post', item + '/action', {'renew_url': {}},
                         headers=reader, status=403)
            self.request('get', item, headers=dict(
                self.headers, **{'X-Project-Id': 'other'}), status=404)
            renewed = self.request('post', item + '/action',
                                   {'renew_url': {}}).json['export']
            self.assertIn('download_url', renewed['artifacts'][0])
            persisted = objects.Export.get_by_id(self.ctx, accepted['id'])
            self.assertNotIn('download_url', str(persisted.artifacts))
            storage.urls.return_value = []
            self.request('post', item + '/action',
                         {'renew_url': {}}, status=409)
            self.request('delete', item, status=202, complete=True)
            self.request('get', item, status=404)

    def test_s3_show_renew_and_delete_use_presigned_urls(self):
        reference = self.store.put(self.ctx, json.dumps({
            'access_key_id': S3_ACCESS, 'secret_access_key': S3_SECRET}))
        configure_s3(self)
        self.config.config(s3_credentials_ref=reference, group='export')
        for name, value in (
                ('AWS_ACCESS_KEY_ID', 'ENVACCESS'),
                ('AWS_SECRET_ACCESS_KEY', 'env-secret-value'),
                ('AWS_SESSION_TOKEN', 'env-session-token'),
                ('AWS_PROFILE', 'missing-profile'),
                ('AWS_ENDPOINT_URL', 'https://evil.example.net')):
            self.useFixture(fixtures.EnvironmentVariable(name, value))
        stack = objects.Stack(
            context=self.ctx, name='s3-export-stack', plan='small',
            driver='coolify', engine_version='4.4.3', status=states.ACTIVE,
            task_state=states.NONE)
        stack.create()
        server = objects.Server(
            context=self.ctx, name='worker', stack_id=stack.id, kind='vm',
            role='worker', compute_id='nova-worker', mgmt_ip='192.0.2.10',
            status=states.ACTIVE, task_state=states.NONE)
        server.create()
        volume = objects.Volume(
            context=self.ctx, stack_id=stack.id, server_id=server.id,
            name='data', size_gb=1, mount_path='/data', purpose='data',
            status=states.IN_USE, cinder_volume_id='cinder-source')
        volume.create()

        def archive(flow, ctx, parent, row, backend):
            flow.owner._save(row, status=states.UPLOADING)
            row.artifacts = [{'volume_id': volume.id, 'name': 'data.tar.gz',
                              'size_bytes': 42, 'sha256': 'a' * 64}]
            row.object_expires_at = datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(hours=72)

        deleted = []
        missing = {'value': False}
        path = '/v1/stacks/' + stack.id + '/exports'
        with mock.patch.object(
                exports, '_s3_client',
                side_effect=s3_client_factory(deleted, missing)), \
                mock.patch.object(exports.ExportFlow, 'create', autospec=True,
                                  side_effect=archive):
            accepted = self.request('post', path, {'export': {}},
                                    status=202, complete=True).json['export']
            item = path + '/' + accepted['id']
            shown = self.request('get', item).json['export']
            url = shown['artifacts'][0]['download_url']
            query = parse.parse_qs(parse.urlsplit(url).query)
            self.assertEqual(
                ['AWS4-HMAC-SHA256'], query['X-Amz-Algorithm'])
            self.assertTrue(query['X-Amz-Credential'][0].startswith(
                S3_ACCESS + '/'))
            self.assertNotIn('content-type',
                             query['X-Amz-SignedHeaders'][0].split(';'))
            self.assertNotIn(S3_SECRET, url)
            self.assertNotIn('ENVACCESS', url)
            self.assertNotIn('evil.example.net', url)
            self.assertIn(volume.id + '.tar.gz', parse.unquote(
                parse.urlsplit(url).path))
            persisted = objects.Export.get_by_id(self.ctx, accepted['id'])
            self.assertEqual('s3', persisted.backend)
            self.assertNotIn('download_url', str(persisted.artifacts))
            self.assertNotIn(S3_SECRET, str(persisted.artifacts))
            renewed = self.request('post', item + '/action',
                                   {'renew_url': {}}).json['export']
            self.assertIn('X-Amz-Signature',
                          renewed['artifacts'][0]['download_url'])
            reader = dict(self.headers, **{'X-Roles': 'reader,member'})
            hidden = self.request('get', item, headers=reader).json['export']
            self.assertNotIn('download_url', hidden['artifacts'][0])
            self.assertNotIn(S3_SECRET, str(hidden))
            missing['value'] = True
            self.request('post', item + '/action', {'renew_url': {}},
                         status=409)
            missing['value'] = False
            self.request('delete', item, status=202, complete=True)
            self.request('get', item, status=404)
        self.assertEqual(1, len(deleted))
        self.assertIn(volume.id + '.tar.gz', deleted[0])

    def test_renew_returns_503_for_engine_connection_and_credential_faults(
            self):
        stack = objects.Stack(
            context=self.ctx, name='export-fault', plan='small',
            driver='coolify', engine_version='4.4.3', status=states.ACTIVE,
            task_state=states.NONE)
        stack.create()
        server = objects.Server(
            context=self.ctx, name='worker', stack_id=stack.id, kind='vm',
            role='worker', compute_id='nova-worker', mgmt_ip='192.0.2.10',
            status=states.ACTIVE, task_state=states.NONE)
        server.create()
        volume = objects.Volume(
            context=self.ctx, stack_id=stack.id, server_id=server.id,
            name='data', size_gb=1, mount_path='/data', purpose='data',
            status=states.IN_USE, cinder_volume_id='cinder-source')
        volume.create()
        storage = mock.Mock()

        def archive(flow, ctx, parent, row, backend):
            flow.owner._save(row, status=states.UPLOADING)
            row.artifacts = [{'volume_id': volume.id, 'name': 'data.tar.gz',
                              'size_bytes': 42, 'sha256': 'a' * 64}]
            row.object_expires_at = datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(hours=72)

        secret = 'password=secret'
        path = '/v1/stacks/' + stack.id + '/exports'
        with (mock.patch.object(exports, 'SwiftStorage', return_value=storage),
              mock.patch.object(exports.ExportFlow, 'create', autospec=True,
                                side_effect=archive)):
            accepted = self.request('post', path, {'export': {}},
                                    status=202, complete=True).json['export']
            item = path + '/' + accepted['id'] + '/action'
            cases = (
                (driver_base.EngineUnreachable(secret), 503,
                 'Service Unavailable',
                 'coast.drivers.base.EngineUnreachable'),
                (driver_base.EngineAuthError(secret), 503,
                 'Service Unavailable',
                 'coast.drivers.base.EngineAuthError'),
                (driver_base.EngineNotFound(secret), 500,
                 'Internal Server Error', None),
            )
            for error, status, title, cause in cases:
                storage.urls.side_effect = error
                logs = self.useFixture(fixtures.FakeLogger(
                    name='coast.api.errors'))
                response = self.request('post', item, {'renew_url': {}},
                                        status=status)
                self.assertEqual(title, response.json['faultstring'])
                self.assertNotIn(secret, response.text)
                self.assertNotIn(secret, logs.output)
                if cause is not None:
                    self.assertIn(cause, logs.output)
            row = objects.Export.get_by_id(self.ctx, accepted['id'])
            self.assertEqual(states.ACTIVE, row.status)
            self.assertNotIn('download_url', str(row.artifacts))
            self.assertNotIn(secret, str(row.artifacts))
