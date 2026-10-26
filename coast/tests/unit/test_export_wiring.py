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

"""Real export dispatch, signing and clone cleanup regressions."""

import datetime
import hashlib
import hmac
import json
import logging
import os
import shutil
import tempfile
import time
from types import SimpleNamespace
from unittest import mock
from urllib import parse

from botocore.stub import Stubber
import fixtures
from openstack import exceptions as sdk_exceptions

from coast.api import secret_store
from coast.common import context
from coast.common import exception
from coast.common import states
from coast.conductor import exports
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.drivers import base as driver_base
from coast import objects
from coast.tests import base
from coast.tests.unit import test_controllers

S3_ACCESS = 'BARBICANACCESS'
S3_SECRET = 'barbican-secret-value'
S3_PAYLOAD = json.dumps({
    'access_key_id': S3_ACCESS, 'secret_access_key': S3_SECRET})


def s3_client_factory(deleted, missing):
    """Return a client factory with fake HeadObject and DeleteObject."""
    real = exports._s3_client

    def factory(endpoint, region, access_key, secret_key):
        client = real(endpoint, region, access_key, secret_key)

        def head_object(**kwargs):
            if missing['value']:
                body = {
                    'Error': {'Code': 'NoSuchKey', 'Message': 'missing'},
                    'ResponseMetadata': {'HTTPStatusCode': 404}}
                raise client.exceptions.NoSuchKey(body, 'HeadObject')
            return {'ContentLength': 4, 'ETag': '"a"'}

        def delete_object(**kwargs):
            deleted.append(kwargs['Key'])
            return {}

        client.head_object = head_object
        client.delete_object = delete_object
        return client

    return factory


def configure_s3(case, endpoint='https://objects.example.com'):
    case.config.config(
        backend='s3', upload_via='guest', container='coast-exports',
        s3_endpoint=endpoint, s3_region='us-east-1',
        s3_credentials_ref='s3-secret', group='export')


class SwiftStorageTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.proxy = mock.Mock()
        self.proxy.get_endpoint.return_value = (
            'https://objects.example.com/swift/v1/AUTH_service')
        self.proxy.get_account_metadata.return_value = SimpleNamespace(
            metadata={'coast-temp-url-key-ref': 'opaque-reference'})
        self.key = self.useFixture(fixtures.MockPatchObject(
            secret_store, 'call')).mock
        self.key.return_value = 'test-key'
        self.storage = exports.SwiftStorage(self.conf, mock.Mock(), self.proxy)
        self.export = SimpleNamespace(
            project_id='project', stack_id='stack', id='export',
            volume_ids=['volume'], backend='swift', status=states.ACTIVE,
            artifacts=[{'volume_id': 'volume'}],
            object_expires_at=datetime.datetime.now(datetime.timezone.utc) +
            datetime.timedelta(hours=72))

    def test_signing_keeps_rgw_prefix_and_binds_method_and_expiry(self):
        url = self.storage.url(self.export, 'volume', 'PUT', 1234)
        parts = parse.urlsplit(url)
        self.assertEqual('/swift/v1/AUTH_service/coast-exports/'
                         'project/stack/export/volume.tar.gz', parts.path)
        query = parse.parse_qs(parts.query)
        expected = hmac.new(b'test-key',
                            ('PUT\n1234\n' + parts.path).encode(),
                            hashlib.sha1).hexdigest()
        self.assertEqual([expected], query['temp_url_sig'])
        self.assertEqual(['1234'], query['temp_url_expires'])
        self.assertNotEqual(url, self.storage.url(
            self.export, 'volume', 'GET', 1234))

    def test_resource_prefix_keeps_swift_container_and_object_path(self):
        self.config.config(resource_name_prefix='operator')
        self.config.config(container='operator-archives', group='export')
        storage = exports.SwiftStorage(self.conf, mock.Mock(), self.proxy)
        self.assertEqual('operator-archives', storage.container)
        self.assertEqual('project/stack/export/volume.tar.gz',
                         storage.object_name(self.export, 'volume'))

    def test_urls_are_ephemeral_and_bounded_by_retention(self):
        self.export.object_expires_at = datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(seconds=30)
        result = self.storage.urls(self.export)
        self.assertEqual(1, len(result))
        self.assertLessEqual(datetime.datetime.fromisoformat(
            result[0]['expires_at']),
                             self.export.object_expires_at)
        self.assertNotIn('download_url', self.export.artifacts[0])
        self.export.status = states.ERROR
        self.assertEqual([], self.storage.urls(self.export))

    def test_missing_object_omits_url(self):
        self.proxy.get_object_metadata.side_effect = (
            sdk_exceptions.ResourceNotFound('missing'))
        self.assertEqual([], self.storage.urls(self.export))

    def test_no_container_key_fallback_or_unsupported_backend(self):
        self.proxy.get_account_metadata.return_value.metadata = {}
        self.assertRaises(exception.CoastException, exports.SwiftStorage,
                          self.conf, mock.Mock(), self.proxy)
        self.config.config(backend='s3', group='export')
        self.assertRaises(exception.CoastException, exports.SwiftStorage,
                          self.conf, mock.Mock(), self.proxy)

    def test_expiration_and_uncertain_put_cleanup(self):
        self.storage.retain(self.export, 'volume')
        self.assertEqual(int(self.export.object_expires_at.timestamp()),
                         self.proxy.set_object_metadata.call_args.kwargs[
                             'delete_at'])
        self.export.artifacts = []
        self.storage.delete(self.export)
        self.assertEqual('project/stack/export/volume.tar.gz',
                         self.proxy.delete_object.call_args.args[0])

    def test_storage_for_uses_the_swift_constructor(self):
        with mock.patch.object(exports, 'SwiftStorage',
                               return_value=self.storage) as swift:
            found = exports.storage_for(
                self.conf, mock.Mock(), self.export)
        swift.assert_called_once()
        self.assertIs(found, self.storage)


class ExportRPCLoggingTest(base.TestCase):
    def test_late_reply_log_masks_url_signature_and_other_credentials(self):
        response = {'download_url': (
            'https://objects.example.com/archive?'
            'temp_url_sig=capability&temp_url_expires=1'),
                    'password': 'private-value'}
        record = logging.LogRecord(
            'oslo_messaging._drivers.amqpdriver', logging.DEBUG,
            __file__, 1, 'late message: %s', (response,), None)
        rpcapi.ExportURLLogFilter().filter(record)
        logged = record.getMessage()
        self.assertNotIn('capability', logged)
        self.assertNotIn('private-value', logged)
        self.assertIn('temp_url_sig=***', logged)
        self.assertIn('capability', response['download_url'])

    def test_late_reply_log_masks_sigv4_credential_and_signature(self):
        response = {'download_url': (
            'https://objects.example.com/coast-exports/key'
            '?X-Amz-Credential=BARBICANACCESS%2F20261013%2Fus-east-1'
            '%2Fs3%2Faws4_request'
            '&X-Amz-Signature=sigvalue'
            '&X-Amz-Security-Token=tokvalue')}
        record = logging.LogRecord(
            'oslo_messaging._drivers.amqpdriver', logging.DEBUG,
            __file__, 1, 'late message: %s', (response,), None)
        rpcapi.ExportURLLogFilter().filter(record)
        logged = record.getMessage()
        self.assertNotIn('BARBICANACCESS', logged)
        self.assertNotIn('sigvalue', logged)
        self.assertNotIn('tokvalue', logged)
        self.assertIn('X-Amz-Credential=***', logged)
        self.assertIn('X-Amz-Signature=***', logged)
        self.assertIn('BARBICANACCESS', response['download_url'])


class ExportFlowTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.env.driver = 'coolify'
        self.env.save()
        self.env.acquire_task(states.RECONCILING)
        self.export = objects.Export(
            context=self.ctx, stack_id=self.env.id, backend='swift',
            volume_ids=[self.volume.id], status=states.PENDING)
        self.export.create()
        self.storage_patch = mock.patch.object(exports, 'SwiftStorage')
        self.storage = self.storage_patch.start().return_value
        self.addCleanup(self.storage_patch.stop)
        self.manager = manager.Manager(self.conf, notifier=mock.Mock())

    def archive(self, flow, ctx, stack, export, storage):
        flow.owner._save(export, status=states.UPLOADING)
        export.artifacts = [{'volume_id': self.volume.id,
                             'name': 'data.tar.gz', 'size_bytes': 12,
                             'sha256': 'a' * 64}]
        export.object_expires_at = datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(hours=72)

    def test_manager_dispatch_and_duplicate(self):
        with mock.patch.object(exports.ExportFlow, 'create', autospec=True,
                               side_effect=self.archive) as create:
            self.manager.execute(self.ctx, 'create_export', self.export)
            self.manager.execute(self.ctx, 'create_export', self.export)
        self.assertEqual(1, create.call_count)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ACTIVE, current.status)
        self.assertEqual(12, current.artifacts[0]['size_bytes'])
        self.assertNotIn('download_url', str(current.artifacts))
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_failure_is_sanitized_and_releases_stack(self):
        with mock.patch.object(exports.ExportFlow, 'create',
                               side_effect=RuntimeError('signature-secret')):
            self.manager.execute(self.ctx, 'create_export', self.export)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertNotIn('signature-secret', current.fault)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_foreign_project_never_reaches_storage(self):
        foreign = context.RequestContext(project_id='foreign')
        self.assertRaises(exception.NotFound, self.manager.execute,
                          foreign, 'create_export', self.export)

    def test_cleanup_attempts_all_resources(self):
        flow = exports.ExportFlow(self.manager)
        flow.block = mock.Mock()
        flow.compute = mock.Mock()
        flow.clones = [(SimpleNamespace(id='clone'),
                        SimpleNamespace(compute_id='server'), True)]
        flow.snapshots = [SimpleNamespace(id='snapshot')]
        self.assertFalse(flow.cleanup())
        self.assertEqual(('server', 'clone'),
                         flow.compute.delete_volume_attachment.call_args.args)
        flow.compute.delete_volume_attachment.side_effect = RuntimeError()
        self.assertTrue(flow.cleanup())
        self.assertEqual(2, flow.block.delete_snapshot.call_count)

    def fake_clouds(self, invalid_receipt=False):
        self.server.compute_id = 'nova-owner'
        self.server.mgmt_ip = '192.0.2.10'
        self.server.save()
        self.volume.server_id = self.server.id
        self.volume.save()
        block = mock.Mock()
        block.snapshots.return_value = []
        block.create_snapshot.return_value = SimpleNamespace(id='snapshot')
        block.create_volume.return_value = SimpleNamespace(id='clone')
        compute = mock.Mock()
        compute.create_volume_attachment.return_value = SimpleNamespace(
            device='/dev/vdc')
        agent = mock.Mock()
        self.posts = []

        def post(server, path, body):
            try:
                in_transaction = self.ctx.session.in_transaction()
            except Exception:
                in_transaction = False
            self.assertFalse(in_transaction)
            self.posts.append((path, body))
            if path.endswith('quiesce'):
                return {'status': 'quiesced', 'deadline': time.time() + 900}
            if path.endswith('resume'):
                return {'status': 'resumed'}
            return {'status': 'uploaded', 'size': 100,
                    'sha256': 'bad' if invalid_receipt else 'a' * 64}

        agent.post.side_effect = post
        patches = [
            mock.patch.object(exports.clients, 'get_connection',
                              return_value=SimpleNamespace(
                                  block_storage=block)),
            mock.patch.object(exports.clients, 'get_sensitive_proxy',
                              return_value=compute),
            mock.patch.object(exports, 'AgentClient', return_value=agent)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return block, compute

    def test_real_clone_upload_and_cleanup_publish_valid_receipt(self):
        block, compute = self.fake_clouds()
        self.manager.execute(self.ctx, 'create_export', self.export)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ACTIVE, current.status)
        self.assertEqual(100, current.artifacts[0]['size_bytes'])
        self.assertEqual('a' * 64, current.artifacts[0]['sha256'])
        self.assertEqual(['/v1/export/quiesce', '/v1/export/resume',
                          '/v1/export'], [path for path, body in self.posts])
        self.assertEqual('/dev/vdc', self.posts[-1][1]['device'])
        self.assertEqual('quiesced-snapshot',
                         self.posts[-1][1]['manifest']['consistency'])
        self.assertEqual(('nova-owner', 'clone'),
                         compute.delete_volume_attachment.call_args.args)
        self.assertEqual('snapshot',
                         block.delete_snapshot.call_args.args[0].id)

    def assert_export_names(self, prefix):
        block, compute = self.fake_clouds()
        self.manager.execute(self.ctx, 'create_export', self.export)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ACTIVE, current.status)
        expected = '%s-export-%s-%s' % (
            prefix, self.export.id, self.volume.id)
        self.assertEqual(expected.encode('ascii'),
                         block.create_snapshot.call_args.kwargs[
                             'name'].encode('ascii'))
        self.assertEqual(expected.encode('ascii'),
                         block.create_volume.call_args.kwargs[
                             'name'].encode('ascii'))

    def test_default_snapshot_and_clone_names_are_byte_identical(self):
        self.assert_export_names('coast')

    def test_snapshot_and_clone_names_use_isolated_config_prefix(self):
        self.config.config(resource_name_prefix='operator')
        self.assert_export_names('operator')

    def test_snapshot_and_clone_names_sanitize_config_prefix(self):
        self.config.config(resource_name_prefix=' operator / exports ')
        self.assert_export_names('operator-exports')

    def test_invalid_upload_receipt_cleans_clouds_without_active_archive(self):
        self.fake_clouds(invalid_receipt=True)
        self.manager.execute(self.ctx, 'create_export', self.export)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertFalse(current.artifacts)

    def test_clone_cleanup_failure_does_not_publish_active(self):
        block, compute = self.fake_clouds()
        compute.delete_volume_attachment.side_effect = RuntimeError('secret')
        self.manager.execute(self.ctx, 'create_export', self.export)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertFalse(current.artifacts)

    def test_size_limit_rechecked_before_snapshot_and_clone(self):
        self.fake_clouds()
        self.config.config(max_size_gb=1, group='export')
        self.manager.execute(self.ctx, 'create_export', self.export)
        self.assertEqual([], self.posts)
        self.assertEqual(states.ERROR, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_delete_during_upload_preserves_lock(self):
        def deleting(flow, ctx, stack, export, storage):
            self.archive(flow, ctx, stack, export, storage)
            row = objects.Export.get_by_id(ctx, export.id)
            row.status = states.DELETING
            row.save()
            self.manager.execute(ctx, 'delete_export', row)
            self.assertEqual(states.RECONCILING, objects.Stack.get_by_id(
                ctx, stack.id).task_state)

        with mock.patch.object(exports.ExportFlow, 'create', autospec=True,
                               side_effect=deleting):
            self.manager.execute(self.ctx, 'create_export', self.export)
        self.assertRaises(exception.NotFound, objects.Export.get_by_id,
                          self.ctx, self.export.id)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        receipt = objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.export.id, 'action': 'create'})
        self.assertEqual(['failure'], [item.outcome for item in receipt])

    def test_housekeeping_expires_active_and_retries_failed_cleanup(self):
        for status in (states.SNAPSHOTTING, states.UPLOADING, states.ACTIVE):
            self.export.status = status
            self.export.save()
        self.export.object_expires_at = datetime.datetime.now(
            datetime.timezone.utc) - datetime.timedelta(seconds=1)
        self.export.save()
        self.storage.delete.side_effect = RuntimeError('secret')
        exports.housekeep(self.manager)
        self.assertEqual(states.ACTIVE, objects.Export.get_by_id(
            self.ctx, self.export.id).status)
        self.storage.delete.side_effect = None
        exports.housekeep(self.manager)
        self.assertEqual(states.EXPIRED, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_failed_delete_before_create_releases_stack(self):
        self.export.status = states.DELETING
        self.export.save()
        self.storage.delete.side_effect = RuntimeError('signature-secret')
        self.manager.execute(self.ctx, 'delete_export', self.export)
        self.assertEqual(states.ERROR, objects.Export.get_by_id(
            self.ctx, self.export.id).status)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_url_engine_fault_keeps_its_type_and_drops_text(self):
        for status in (states.SNAPSHOTTING, states.UPLOADING, states.ACTIVE):
            self.export.status = status
            self.export.save()
        self.export.object_expires_at = datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(hours=1)
        self.export.save()
        secret = 'password=secret'
        ctx = context.RequestContext(
            project_id=self.ctx.project_id, roles=['manager'])
        for error, expected in (
                (driver_base.EngineUnreachable(secret),
                 driver_base.EngineUnreachable),
                (driver_base.EngineAuthError(secret),
                 driver_base.EngineAuthError)):
            self.storage.urls.side_effect = error
            with self.assertRaises(expected) as caught:
                self.manager.get_export_urls(ctx, self.export.id)
            self.assertEqual('', str(caught.exception))
            self.assertNotIn(secret, repr(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
        for error in (driver_base.EngineNotFound(secret),
                      RuntimeError(secret)):
            self.storage.urls.side_effect = error
            with self.assertRaises(exception.CoastException) as caught:
                self.manager.get_export_urls(ctx, self.export.id)
            self.assertEqual('Export URL request failed.',
                             str(caught.exception))
            self.assertNotIn(secret, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_url_rpc_checks_role_and_project_before_signing(self):
        reader = context.RequestContext(project_id=self.ctx.project_id,
                                        roles=['reader', 'member'])
        self.assertRaises(exception.NotAuthorized,
                          self.manager.get_export_urls,
                          reader, self.export.id)
        foreign = context.RequestContext(project_id='foreign',
                                         roles=['admin'])
        self.assertRaises(exception.NotFound, self.manager.get_export_urls,
                          foreign, self.export.id)
        self.assertFalse(self.storage.urls.called)

    def test_manifest_contains_env_names_not_secret_refs_or_source_auth(self):
        source = objects.Source(
            context=self.ctx, type='git',
            repo_url='https://example.com/project.git', compose_path='app.yml',
            credentials_ref='source-secret-reference')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.env.id, source_id=source.id,
            name='database', ports='8080', replicas=1, auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE)
        service.create()
        env = objects.Secret(
            context=self.ctx, service_id=service.id, key='PASSWORD',
            is_secret=True, value_ref='env-secret-reference',
            build_time=False, version=1)
        env.create()
        manifest = exports.ExportFlow.manifest(self.ctx, self.env.id)
        self.assertEqual(['PASSWORD'], manifest[0]['env_names'])
        self.assertEqual('app.yml', manifest[0]['spec']['compose_path'])
        self.assertNotIn('env-secret-reference', str(manifest))
        self.assertNotIn('source-secret-reference', str(manifest))
        self.assertNotIn('repo_url', str(manifest))

    def test_s3_clone_records_presigned_put(self):
        self.export.backend = 's3'
        self.export.save()
        configure_s3(self)
        self.fake_clouds()
        deleted = []
        missing = {'value': False}
        with mock.patch.object(
                secret_store, 'call', return_value=S3_PAYLOAD), \
                mock.patch.object(
                    exports, '_s3_client',
                    side_effect=s3_client_factory(deleted, missing)):
            self.manager.execute(self.ctx, 'create_export', self.export)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ACTIVE, current.status)
        put_url = self.posts[-1][1]['put_url']
        query = parse.parse_qs(parse.urlsplit(put_url).query)
        signed = query['X-Amz-SignedHeaders'][0].split(';')
        self.assertIn('content-type', signed)
        self.assertIn('host', signed)
        self.assertNotIn(S3_SECRET, put_url)
        self.assertNotIn('download_url', str(current.artifacts))
        self.storage.delete.assert_not_called()

    def test_s3_housekeeping_expires_after_delete(self):
        self.export.backend = 's3'
        for status in (states.SNAPSHOTTING, states.UPLOADING, states.ACTIVE):
            self.export.status = status
            self.export.save()
        self.export.object_expires_at = datetime.datetime.now(
            datetime.timezone.utc) - datetime.timedelta(seconds=1)
        self.export.save()
        configure_s3(self)
        deleted = []
        missing = {'value': False}
        with mock.patch.object(
                secret_store, 'call', return_value=S3_PAYLOAD), \
                mock.patch.object(
                    exports, '_s3_client',
                    side_effect=s3_client_factory(deleted, missing)):
            exports.housekeep(self.manager)
        self.assertEqual(states.EXPIRED, objects.Export.get_by_id(
            self.ctx, self.export.id).status)
        self.assertEqual(1, len(deleted))
        self.assertIn(self.volume.id + '.tar.gz', deleted[0])
        self.storage.delete.assert_not_called()

    def test_housekeeping_leaves_a_different_backend(self):
        self.export.backend = 's3'
        for status in (states.SNAPSHOTTING, states.UPLOADING, states.ACTIVE):
            self.export.status = status
            self.export.save()
        self.export.object_expires_at = datetime.datetime.now(
            datetime.timezone.utc) - datetime.timedelta(seconds=1)
        self.export.save()
        exports.housekeep(self.manager)
        self.assertEqual(states.ACTIVE, objects.Export.get_by_id(
            self.ctx, self.export.id).status)
        self.storage.delete.assert_not_called()


class S3StorageTest(base.TestCase):
    def setUp(self):
        super().setUp()
        configure_s3(self)
        self.secret = self.useFixture(fixtures.MockPatchObject(
            secret_store, 'call')).mock
        self.secret.return_value = S3_PAYLOAD
        self.useFixture(fixtures.EnvironmentVariable(
            'AWS_ACCESS_KEY_ID', 'ENVACCESS'))
        self.useFixture(fixtures.EnvironmentVariable(
            'AWS_SECRET_ACCESS_KEY', 'env-secret-value'))
        self.useFixture(fixtures.EnvironmentVariable(
            'AWS_SESSION_TOKEN', 'env-session-token'))
        self.useFixture(fixtures.EnvironmentVariable(
            'AWS_PROFILE', 'missing-profile'))
        self.useFixture(fixtures.EnvironmentVariable(
            'AWS_DEFAULT_REGION', 'eu-west-1'))
        self.useFixture(fixtures.EnvironmentVariable(
            'AWS_ENDPOINT_URL', 'https://evil.example.net'))
        self.useFixture(fixtures.EnvironmentVariable(
            'AWS_CONFIG_FILE', '/no/such/coast-aws-config'))
        self.useFixture(fixtures.EnvironmentVariable(
            'AWS_SHARED_CREDENTIALS_FILE', '/no/such/coast-aws-credentials'))
        self.export = SimpleNamespace(
            project_id='project', stack_id='stack', id='export',
            volume_ids=['volume'], backend='s3', status=states.ACTIVE,
            artifacts=[{'volume_id': 'volume'}],
            object_expires_at=datetime.datetime.now(datetime.timezone.utc) +
            datetime.timedelta(hours=72))

    def storage(self, client=None):
        return exports.S3Storage(self.conf, mock.Mock(), client)

    def test_put_and_get_ignore_ambient_credentials(self):
        with mock.patch('botocore.session.Session.get_credentials',
                        side_effect=AssertionError('ambient credentials')):
            storage = self.storage()
            put_url = storage.url(self.export, 'volume', 'PUT',
                                  int(time.time()) + 600)
            get_url = storage.url(self.export, 'volume', 'GET',
                                  int(time.time()) + 600)
        for method, url in (('PUT', put_url), ('GET', get_url)):
            parts = parse.urlsplit(url)
            query = {name.lower(): values for name, values in
                     parse.parse_qs(parts.query).items()}
            self.assertEqual('objects.example.com', parts.hostname)
            self.assertEqual(
                '/coast-exports/project/stack/export/volume.tar.gz',
                parse.unquote(parts.path))
            self.assertEqual(['AWS4-HMAC-SHA256'], query['x-amz-algorithm'])
            self.assertNotIn('x-amz-security-token', query)
            self.assertNotIn(S3_SECRET, url)
            self.assertNotIn('env-secret-value', url)
            self.assertNotIn('ENVACCESS', url)
            self.assertNotIn('evil.example.net', url)
            signed = query['x-amz-signedheaders'][0].split(';')
            self.assertIn('host', signed)
            credential = query['x-amz-credential'][0]
            self.assertTrue(credential.startswith(S3_ACCESS + '/'))
            self.assertIn('/us-east-1/s3/aws4_request', credential)
            if method == 'PUT':
                self.assertIn('content-type', signed)
            else:
                self.assertNotIn('content-type', signed)
        self.assertNotEqual(put_url, get_url)

    def test_second_client_ignores_a_removed_temp_directory(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        with mock.patch('tempfile.tempdir', directory):
            first = self.storage()
            first.url(self.export, 'volume', 'GET', int(time.time()) + 60)
        shutil.rmtree(directory)
        self.assertFalse(os.path.exists(directory))
        url = self.storage().url(
            self.export, 'volume', 'GET', int(time.time()) + 60)
        self.assertIn(S3_ACCESS, url)
        self.assertNotIn('ENVACCESS', url)
        self.assertNotIn(S3_SECRET, url)

    def test_custom_port_stays_in_the_signed_url(self):
        configure_s3(self, 'https://objects.example.com:8443')
        url = self.storage().url(
            self.export, 'volume', 'PUT', int(time.time()) + 600)
        self.assertEqual('objects.example.com:8443',
                         parse.urlsplit(url).netloc)
        self.assertNotIn(S3_SECRET, url)

    def test_presign_rejects_a_lifetime_over_seven_days(self):
        storage = self.storage()
        too_long = int(time.time()) + exports._MAX_PRESIGN_SECONDS + 60
        self.assertRaises(exception.CoastException, storage.url,
                          self.export, 'volume', 'GET', too_long)
        accepted = int(time.time()) + exports._MAX_PRESIGN_SECONDS - 30
        url = storage.url(self.export, 'volume', 'GET', accepted)
        self.assertNotIn(S3_SECRET, url)

    def test_bad_secret_endpoint_and_conductor_upload_fail_closed(self):
        self.secret.return_value = json.dumps({
            'access_key_id': S3_ACCESS, 'secret_access_key': S3_SECRET,
            'session_token': 'no-session'})
        self.assertRaises(exception.CoastException, self.storage)
        self.secret.return_value = json.dumps({
            'access_key_id': S3_ACCESS, 'secret_access_key': 'has space'})
        self.assertRaises(exception.CoastException, self.storage)
        self.secret.return_value = S3_PAYLOAD
        self.config.config(s3_credentials_ref='', group='export')
        self.assertRaises(exception.CoastException, self.storage)
        self.config.config(s3_credentials_ref='s3-secret', group='export')
        self.config.config(
            s3_endpoint='https://user:supersecret@objects.example.com',
            group='export')
        try:
            self.storage()
        except exception.CoastException as exc:
            self.assertNotIn('supersecret', str(exc))
        else:
            self.fail('endpoint with userinfo was accepted')
        configure_s3(self)
        # Conductor relay upload is not implemented (#87): the option
        # rejects it, so no storage can be built for it.
        self.assertRaises(ValueError, self.config.config,
                          upload_via='conductor', group='export')
        self.assertEqual('guest', self.conf.export.upload_via)

    def test_signer_errors_do_not_return_the_secret(self):
        client = mock.Mock()
        client.generate_presigned_url.side_effect = RuntimeError(S3_SECRET)
        storage = self.storage(client)
        try:
            storage.url(self.export, 'volume', 'PUT', int(time.time()) + 60)
        except exception.CoastException as exc:
            self.assertNotIn(S3_SECRET, str(exc))
        else:
            self.fail('signer error was accepted')

    def test_urls_skip_missing_objects_and_are_not_stored(self):
        client = exports._s3_client(
            'https://objects.example.com', 'us-east-1', S3_ACCESS, S3_SECRET)
        stub = Stubber(client)
        stub.add_client_error(
            'head_object', service_error_code='NoSuchKey',
            service_message='missing', http_status_code=404)
        stub.activate()
        self.addCleanup(stub.deactivate)
        storage = self.storage(client)
        self.assertEqual([], storage.urls(self.export))
        self.assertNotIn('download_url', self.export.artifacts[0])
        stub.add_response('head_object', {'ContentLength': 4, 'ETag': '"a"'})
        result = storage.urls(self.export)
        self.assertEqual(1, len(result))
        self.assertNotIn(S3_SECRET, result[0]['download_url'])
        self.assertIn('X-Amz-Signature', result[0]['download_url'])
        self.assertNotIn('download_url', self.export.artifacts[0])
        self.export.status = states.ERROR
        self.assertEqual([], storage.urls(self.export))

    def test_retain_and_delete_use_fake_s3_responses(self):
        client = exports._s3_client(
            'https://objects.example.com', 'us-east-1', S3_ACCESS, S3_SECRET)
        stub = Stubber(client)
        stub.add_client_error(
            'head_object', service_error_code='404',
            service_message='missing', http_status_code=404)
        stub.activate()
        self.addCleanup(stub.deactivate)
        storage = self.storage(client)
        self.assertRaises(exception.CoastException, storage.retain,
                          self.export, 'volume')
        stub.add_response('head_object', {'ContentLength': 4, 'ETag': '"a"'})
        storage.retain(self.export, 'volume')
        stub.add_client_error(
            'delete_object', service_error_code='NoSuchKey',
            service_message='missing', http_status_code=404)
        storage.delete(self.export)
        stub.add_client_error(
            'delete_object', service_error_code='AccessDenied',
            service_message=S3_SECRET, http_status_code=403)
        try:
            storage.delete(self.export)
        except exception.CoastException as exc:
            self.assertNotIn(S3_SECRET, str(exc))
        else:
            self.fail('delete error was accepted')

    def test_presign_logs_do_not_include_the_secret_or_signature(self):
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Handler()
        for name in ('botocore', 'coast.conductor.exports'):
            logger = logging.getLogger(name)
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            self.addCleanup(logger.removeHandler, handler)
        url = self.storage().url(
            self.export, 'volume', 'GET', int(time.time()) + 600)
        text = '\n'.join(records)
        self.assertNotIn(S3_SECRET, text)
        self.assertNotIn('env-secret-value', text)
        signature = parse.parse_qs(parse.urlsplit(url).query)[
            'X-Amz-Signature'][0]
        self.assertNotIn(signature, text)
