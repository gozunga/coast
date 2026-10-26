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

"""Private Git input, normalization and public response boundaries."""

import base64
import concurrent.futures
import hashlib
import json
from pathlib import Path
import traceback
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives.asymmetric import dsa
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from oslo_config import cfg
import requests_mock

from coast.api import microversions
from coast.common import context
from coast.common import exception
from coast.common import janitor
from coast.common import notifications
from coast.common import secrets
from coast.common import states
from coast.conductor import deploy_keys
from coast.conductor import manager
from coast.drivers import base as engine_base
from coast.drivers.coolify import v4
from coast import objects
from coast.tests import app_api
from coast.tests import base


class DeployKeyParsingTest(base.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cfg, 'CONF', self.conf)
        patch.start()
        self.addCleanup(patch.stop)

    def test_supported_formats_and_fingerprint(self):
        for key in (ed25519.Ed25519PrivateKey.generate(),
                    ec.generate_private_key(ec.SECP256R1()),
                    rsa.generate_private_key(65537, 2048),
                    rsa.generate_private_key(65537, 4096)):
            for fmt in (serialization.PrivateFormat.OpenSSH,
                        serialization.PrivateFormat.PKCS8):
                raw = key.private_bytes(serialization.Encoding.PEM, fmt,
                                        serialization.NoEncryption()).decode()
                private, public = secrets.parse_deploy_key(raw)
                self.assertTrue(private.startswith(
                    '-----BEGIN OPENSSH PRIVATE KEY-----'))
                self.assertEqual(key.public_key().public_bytes(
                    serialization.Encoding.OpenSSH,
                    serialization.PublicFormat.OpenSSH).decode(), public)
                digest = hashlib.sha256(base64.b64decode(
                    public.split()[1])).digest()
                self.assertEqual('SHA256:' + base64.b64encode(
                    digest).decode().rstrip('='),
                    secrets.key_fingerprint(public))

    def test_secret_size_error_does_not_echo_key(self):
        self.config.config(max_secret_bytes=4, group='secrets')
        error = self.assertRaises(exception.Invalid,
                                  secrets.parse_deploy_key, 'pasted-secret')
        self.assertEqual(
            'deploy_key.private_key: not a supported unencrypted private key',
            str(error))
        self.assertNotIn('pasted-secret', str(error))

    def test_normalized_key_above_secret_size_limit_is_rejected(self):
        key = ed25519.Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        self.config.config(
            max_secret_bytes=len(base64.b64encode(raw.encode())),
            group='secrets')
        error = self.assertRaises(exception.Invalid,
                                  secrets.parse_deploy_key, raw)
        self.assertEqual(
            'deploy_key.private_key: not a supported unencrypted private key',
            str(error))

    def test_invalid_encrypted_and_weak_keys_are_redacted(self):
        key = rsa.generate_private_key(65537, 1024)
        encrypted = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
            serialization.BestAvailableEncryption(b'passphrase')).decode()
        weak = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        for raw in ('pasted-secret', encrypted, weak):
            error = self.assertRaises(exception.Invalid,
                                      secrets.parse_deploy_key, raw)
            self.assertEqual(
                'deploy_key.private_key: not a supported unencrypted '
                'private key',
                str(error))
            self.assertNotIn(raw, str(error))


class DeployKeyAPITest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.service.engine_ref = 'engine-app'
        self.service.save()

    def create_private(self, repo='git@example.com:org/repo.git', **options):
        body = self.create_body(**options)
        body['service']['source'] = {
            'type': 'git', 'repo_url': repo,
            'deploy_key': {'generate': True}}
        return body

    def test_missing_git_build_pack_is_defaulted_and_persisted(self):
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast'):
            response = self.request(
                'post', '/v1/services', self.create_private(), status=202,
                version=microversions.DEPLOY_KEY_VERSION)
        service = objects.Service.get_by_id(
            self.ctx, response.json['service']['id'])
        source = objects.Source.get_by_id(self.ctx, service.source_id)
        self.assertEqual('nixpacks', source.build_pack)
        self.assertEqual('nixpacks', response.json['service']['source'][
            'build_pack'])
        self.assertEqual('nixpacks', v4.source_settings(source)['build_pack'])

    def test_rotation_without_engine_app_is_rejected_before_secret_write(self):
        self.service.engine_ref = None
        self.service.save()
        self.source.repo_url = 'git@example.com:org/repo'
        self.source.save()
        with mock.patch.object(self.store, 'put') as put:
            response = self.request('patch', self.path, {'service': {
                'source': {'deploy_key': {'generate': True}}}}, status=409,
                version=microversions.DEPLOY_KEY_VERSION)
        put.assert_not_called()
        self.assertIn(
            'Deploy the app once before changing its deploy key.',
            response.text)
        self.assertIsNone(objects.Source.get_by_id(
            self.ctx, self.source.id).deploy_key_rotation)
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast'):
            self.request('delete', self.path, status=202)

    def test_rotation_during_current_operation_is_rejected(self):
        self.source.repo_url = 'git@example.com:org/repo'
        self.source.save()
        for task_state, rotation in ((states.RECONCILING, None),
                                     (states.NONE, {'phase': 'queued'})):
            if task_state != states.NONE:
                self.service.acquire_task(task_state)
            self.source.deploy_key_rotation = rotation
            self.source.save()
            with mock.patch.object(self.store, 'put') as put:
                response = self.request('patch', self.path, {'service': {
                    'source': {'deploy_key': {'generate': True}}}}, status=409,
                    version=microversions.DEPLOY_KEY_VERSION)
            put.assert_not_called()
            self.assertIn(
                'Wait for the current operation on this app to finish, '
                'then try again.', response.text)
            if task_state != states.NONE:
                self.service.release_task(task_state)

    def test_large_rsa_accepted_on_create_and_rotation(self):
        # A fixed test-only key avoids unbounded RSA prime generation within
        # the test timeout while exercising real parsing and API writes.
        raw = (Path(__file__).parent / 'fixtures' /
               'deploy-key-rsa-8192.pem').read_text()
        private, public = secrets.parse_deploy_key(raw)
        self.assertLessEqual(len(raw.encode()),
                             self.conf.secrets.max_secret_bytes)
        self.assertLessEqual(len(private.encode()),
                             self.conf.secrets.max_secret_bytes)
        self.assertGreater(len(public), 1024)
        body = self.create_private()
        body['service']['source']['deploy_key'] = {'private_key': raw}
        created = self.request('post', '/v1/services', body, status=202,
                               version=microversions.DEPLOY_KEY_VERSION)
        self.assertEqual(public, created.json['service']['source'][
            'deploy_key']['public_key'])
        source = objects.Source.get_by_id(self.ctx, objects.Service.get_by_id(
            self.ctx, created.json['service']['id']).source_id)
        self.assertEqual(public, source.deploy_public_key)
        self.source.repo_url = 'git@example.com:org/repo'
        self.source.save()
        with mock.patch(
                'coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key'):
            self.request('patch', self.path, {'service': {'source': {
                'deploy_key': {'private_key': raw}}}}, status=200,
                version=microversions.DEPLOY_KEY_VERSION)
        saved = objects.Source.get_by_id(self.ctx, self.source.id)
        self.assertEqual(public, saved.deploy_key_rotation['public_key'])

    def test_invalid_uploads_rejected_before_create_or_rotation(self):
        encrypted = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
            serialization.BestAvailableEncryption(b'passphrase')).decode()
        invalid = ['malformed', encrypted]
        for key in (rsa.generate_private_key(65537, 1024),
                    dsa.generate_private_key(1024)):
            invalid.append(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()).decode())
        valid = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption()).decode()
        invalid.append(valid + ' ' * self.conf.secrets.max_secret_bytes)
        self.source.repo_url = 'git@example.com:org/repo'
        self.source.save()
        with mock.patch.object(self.store, 'put') as put, mock.patch(
                'coast.conductor.rpcapi.ConductorAPI.cast') as cast:
            for raw in invalid:
                body = self.create_private()
                body['service']['source']['deploy_key'] = {'private_key': raw}
                error = self.assertRaises(exception.Invalid,
                                          secrets.parse_deploy_key, raw)
                self.assertEqual(
                    'deploy_key.private_key: not a supported unencrypted '
                    'private key', str(error))
                for method, path, data in (
                        ('post', '/v1/services', body),
                        ('patch', self.path, {'service': {'source': {
                            'deploy_key': {'private_key': raw}}}})):
                    response = self.request(
                        method, path, data, status=400,
                        version=microversions.DEPLOY_KEY_VERSION)
                    self.assertEqual(
                        'deploy_key.private_key: not a supported unencrypted '
                        'private key', response.json['faultstring'])
            put.assert_not_called()
            cast.assert_not_called()
        self.assertIsNone(objects.Source.get_by_id(
            self.ctx, self.source.id).deploy_key_rotation)

    def test_generate_default_and_public_views(self):
        created = self.request('post', '/v1/services', self.create_private(),
                               status=202,
                               version=microversions.DEPLOY_KEY_VERSION)
        view = created.json['service']
        self.assertTrue(view['deploy_required'])
        self.assertNotIn('deployment_id', view)
        public = view['source']['deploy_key']
        self.assertTrue(public['public_key'].startswith('ssh-ed25519 '))
        source = objects.Source.get_by_id(self.ctx, objects.Service.get_by_id(
            self.ctx, view['id']).source_id)
        private = self.store.get(self.ctx, source.credentials_ref)
        self.assertNotIn(private, created.text)
        self.assertNotIn('credentials_ref', created.text)
        self.assertNotIn('PRIVATE KEY', created.text)
        self.assertNotIn(source.credentials_ref, str(
            notifications.payload_for(source, self.ctx)))
        for path in ('/v1/services/' + view['id'], '/v1/services'):
            response = self.request('get', path,
                                    version=microversions.DEPLOY_KEY_VERSION)
            self.assertIn(public['fingerprint'], response.text)
            self.assertNotIn('PRIVATE KEY', response.text)
        old = self.request('get', '/v1/services/' + view['id'])
        self.assertNotIn('deploy_key', old.json['service']['source'])

    def test_deployed_source_pending_changes_ignore_response_metadata(self):
        created = self.request(
            'post', '/v1/services', self.create_private(deploy=True),
            status=202, version=microversions.DEPLOY_KEY_VERSION
        ).json['service']
        deployment = objects.Deployment.get_by_id(
            self.ctx, created['deployment_id'])
        self.assertNotIn('deploy_key', deployment.spec['source'])
        service = self.unlock(created['id'])
        service.current_deployment_id = deployment.id
        service.status = states.ACTIVE
        service.save()
        path = '/v1/services/' + service.id

        def check_views():
            for version in ('1.29', microversions.DEPLOY_KEY_VERSION):
                view = self.request('get', path, version=version
                                    ).json['service']
                self.assertFalse(view['pending_changes'])
                self.assertEqual(version == microversions.DEPLOY_KEY_VERSION,
                                 'deploy_key' in view['source'])

        check_views()
        # Credentials have always been excluded from the source spec; key
        # replacement alone does not change the release's build settings.
        source = objects.Source.get_by_id(self.ctx, service.source_id)
        source.credentials_ref, source.deploy_public_key = (
            secrets.store_deploy_key(self.ctx, service, {'generate': True}))
        source.save()
        check_views()
        # Existing deployments may contain metadata from the old writer.
        legacy = objects.Deployment(
            context=self.ctx, service_id=service.id,
            server_id=service.server_id, source_ref='main', trigger='api',
            status=states.ACTIVE, spec=dict(deployment.spec, source=dict(
                deployment.spec['source'],
                deploy_key={'fingerprint': 'obsolete'})))
        legacy.create()
        service.current_deployment_id = legacy.id
        service.save()
        check_views()
        self.request(
            'patch', path, {'service': {'source': {'branch': 'next'}}},
            version=microversions.DEPLOY_KEY_VERSION)
        for version in ('1.29', microversions.DEPLOY_KEY_VERSION):
            self.assertTrue(self.request('get', path, version=version
                                         ).json['service']['pending_changes'])

    def test_explicit_deploy_and_upload(self):
        for uploaded in (False, True):
            body = self.create_private('ssh://git@example.com:2222/org/repo',
                                       deploy=True)
            body['service']['name'] = 'uploaded' if uploaded else 'generated'
            if uploaded:
                key = ed25519.Ed25519PrivateKey.generate().private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption()).decode()
                body['service']['source']['deploy_key'] = {'private_key': key}
            result = self.request('post', '/v1/services', body, status=202,
                                  version=microversions.DEPLOY_KEY_VERSION)
            self.assertIn('deployment_id', result.json['service'])
            self.assertNotIn('PRIVATE KEY', result.text)

    def test_schema_version_and_url_pairing(self):
        body = self.create_private()
        self.request('post', '/v1/services', body, status=400)
        self.request('post', '/v1/services', body, status=400,
                     version=microversions.DATABASE_VERSION)
        for repo, key in (
                ('git@example.com:org/repo', None),
                ('https://example.com/repo', {'generate': True}),
                ('https://user:secret@example.com/repo', None),
                ('ssh://root@example.com/repo', {'generate': True}),
                ('git@example.com:org/repo', {'generate': False}),
                ('git@example.com:org/repo',
                 {'generate': True, 'private_key': 'secret'}),
                ('git@example.com:org/repo',
                 {'private_key': 'pasted-secret'})):
            source = body['service']['source']
            source['repo_url'] = repo
            source.pop('deploy_key', None)
            if key is not None:
                source['deploy_key'] = key
            response = self.request('post', '/v1/services', body, status=400,
                                    version=microversions.DEPLOY_KEY_VERSION)
            self.assertNotIn('pasted-secret', response.text)
            self.assertNotIn(repo, response.text)
        self.assertEqual({}, self.store.values)

    def test_patch_auth_and_project(self):
        body = {'service': {'source': {'deploy_key': {'generate': True}}}}
        for headers, status in (({}, 401),
                                (dict(self.headers, **{'X-Roles': 'reader'}),
                                 403),
                                (dict(self.headers,
                                      **{'X-Project-Id': 'other'}), 404)):
            self.request('patch', self.path, body, status=status,
                         headers=headers,
                         version=microversions.DEPLOY_KEY_VERSION)
        self.request('patch', self.path, body, status=400)
        self.request('patch', self.path, body, status=400,
                     version=microversions.DEPLOY_KEY_VERSION)

    def test_busy_stack_rejects_rotation_before_side_effects(self):
        self.source.repo_url = 'git@example.com:org/repo'
        self.source.credentials_ref = 'old-ref'
        self.source.save()
        for task in (states.UPGRADING, states.DELETING, states.FAILING_OVER):
            self.stack.acquire_task(task)
            with mock.patch.object(self.store, 'put') as put, mock.patch(
                    'coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key'
                    ) as rpc:
                self.request('patch', self.path, {'service': {'source': {
                    'deploy_key': {'generate': True}}}}, status=409,
                    version=microversions.DEPLOY_KEY_VERSION)
            self.stack.release_task(task)
            put.assert_not_called()
            rpc.assert_not_called()
            self.assertIsNone(objects.Source.get_by_id(
                self.ctx, self.source.id).deploy_key_rotation)
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, self.service.id).task_state)

    def test_rejected_patch_does_not_rotate_key(self):
        self.source.repo_url = 'git@example.com:org/repo'
        old_ref = self.store.put(self.ctx, 'old-private-key')
        self.source.credentials_ref = old_ref
        self.source.save()
        before = dict(self.store.values)
        invalid_options = (
            {'health_check': 'http', 'ports': ''},
            {'health_check_path': '/health', 'healthcheck_path': '/other'},
            {'default_domain_enabled': False},
            {'cpu_limit': -1},
            {'source': {'branch': 123}},
        )
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key'
                        ) as rpc, mock.patch.object(
                            self.store, 'put', wraps=self.store.put) as put, \
                mock.patch.object(
                    self.store, 'delete', wraps=self.store.delete) as delete:
            for invalid in invalid_options:
                options = {'source': {'deploy_key': {'generate': True}}}
                for field, value in invalid.items():
                    if field == 'source':
                        options['source'].update(value)
                    else:
                        options[field] = value
                self.request('patch', self.path, {'service': options},
                             status=409 if 'default_domain_enabled' in invalid
                             else 400,
                             version=microversions.DEPLOY_KEY_VERSION)
                saved = objects.Source.get_by_id(self.ctx, self.source.id)
                self.assertEqual(old_ref, saved.credentials_ref)
                self.assertIsNone(saved.deploy_key_rotation)
                # Do not include secret payloads in failure output.
                unchanged = before == self.store.values
                self.assertTrue(unchanged)
                self.assertEqual(states.NONE, objects.Service.get_by_id(
                    self.ctx, self.service.id).task_state)
            rpc.assert_not_called()
            self.assertFalse(put.called)
            self.assertFalse(delete.called)

    def test_patch_passes_only_reference_and_public_key(self):
        self.source.repo_url = 'git@example.com:org/repo'
        self.source.credentials_ref = 'old-ref'
        self.source.save()
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key'
                        ) as rotate:
            response = self.request('patch', self.path, {
                'service': {'source': {
                    'deploy_key': {'generate': True}}}},
                version=microversions.DEPLOY_KEY_VERSION)
        args = rotate.call_args.args
        self.assertEqual(self.service.id, args[1])
        self.assertIn(args[2], self.store.values)
        self.assertTrue(args[3].startswith('ssh-ed25519 '))
        self.assertNotIn('PRIVATE KEY', str(args))
        self.assertNotIn('PRIVATE KEY', response.text)


class DeployKeyRecoveryTest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.service.engine_ref = 'engine-app'
        self.service.save()
        self.source.repo_url = 'git@example.com:org/repo'
        self.old_ref = self.store.put(self.ctx, 'old-private-key')
        self.source.credentials_ref = self.old_ref
        self.source.save()
        self.manager = manager.Manager(self.conf)
        self.driver = mock.Mock()
        self.driver.prepare_deploy_key.return_value = ('old-key', 'new-key')
        patch = mock.patch.object(self.manager, '_driver',
                                  return_value=self.driver)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(self.manager, '_endpoint', return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def admit(self):
        # Pause delivery so tests can exercise each durable phase.
        method = 'coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key'
        with mock.patch(method):
            self.request('patch', self.path, {'service': {'source': {
                'deploy_key': {'generate': True}}}}, status=200,
                version=microversions.DEPLOY_KEY_VERSION)
        pending = self.pending()
        self.assertIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)
        return pending

    def pending(self):
        return objects.Source.get_by_id(
            self.ctx, self.source.id).deploy_key_rotation

    def run_rotation(self, pending):
        return self.manager.rotate_deploy_key(
            self.ctx, self.service.id, pending['reference'],
            pending['public_key'])

    def assert_delete_available(self):
        saved = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.NONE, saved.task_state)
        self.assertIsNone(saved.task_owner)
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast'):
            self.request('delete', self.path, status=202)

    def test_dispatch_failure_returns_conflict_and_releases_claim(self):
        with mock.patch(
                'coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key',
                side_effect=exception.CoastException('private-data')):
            response = self.request('patch', self.path, {'service': {
                'source': {'deploy_key': {'generate': True}}}}, status=409,
                version=microversions.DEPLOY_KEY_VERSION)
        self.assertNotIn('private-data', response.text)
        pending = self.pending()
        self.assertEqual('cleanup_new', pending['phase'])
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)
        self.assert_delete_available()

    def test_stale_rpc_failure_preserves_newer_rotation_and_claim(self):
        newer = {}

        def delayed_failure(ctx, service_id, reference, public_key):
            # Finish the original operation and admit another PATCH before
            # the original RPC caller receives its delayed failure.
            self.assertTrue(self.manager.rotate_deploy_key(
                ctx, service_id, reference, public_key))
            with mock.patch(
                    'coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key'), \
                    concurrent.futures.ThreadPoolExecutor() as executor:
                executor.submit(
                    self.request, 'patch', self.path, {'service': {
                        'source': {'deploy_key': {'generate': True}}}},
                    status=200,
                    version=microversions.DEPLOY_KEY_VERSION).result()
            newer['pending'] = self.pending()
            newer['service'] = objects.Service.get_by_id(ctx, service_id)
            raise exception.CoastException('Delayed RPC failure')

        with mock.patch(
                'coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key',
                side_effect=delayed_failure):
            self.request('patch', self.path, {'service': {
                'source': {'deploy_key': {'generate': True}}}}, status=409,
                version=microversions.DEPLOY_KEY_VERSION)

        self.assertEqual(newer['pending'], self.pending())
        saved = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.RECONCILING, saved.task_state)
        self.assertEqual(deploy_keys.OWNER, saved.task_owner)
        self.assertEqual(newer['service'].task_started_at,
                         saved.task_started_at)
        self.assertIn(newer['pending']['reference'], self.store.values)

    def test_conductor_failure_returns_conflict_and_allows_delete(self):
        self.driver.prepare_deploy_key.side_effect = RuntimeError(
            'private-data')
        with mock.patch(
                'coast.conductor.rpcapi.ConductorAPI.rotate_deploy_key',
                side_effect=self.manager.rotate_deploy_key):
            response = self.request('patch', self.path, {'service': {
                'source': {'deploy_key': {'generate': True}}}}, status=409,
                version=microversions.DEPLOY_KEY_VERSION)
        self.assertNotIn('private-data', response.text)
        self.assertEqual('abort_preparing', self.pending()['phase'])
        self.assert_delete_available()

    def failed_phase(self, phase, method):
        pending = self.admit()
        source = objects.Source.get_by_id(self.ctx, self.source.id)
        source.deploy_key_rotation = dict(
            pending, phase=phase, old_engine_key='old-key',
            new_engine_key='new-key')
        source.save()
        with mock.patch.object(deploy_keys, method,
                               side_effect=RuntimeError('private-data')):
            self.assertRaises(exception.Conflict, deploy_keys.run,
                              self.manager, self.ctx, self.service.id,
                              recover=True)
        saved = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.NONE, saved.task_state)
        self.assertIsNone(saved.task_owner)
        self.assertEqual(self.old_ref, objects.Source.get_by_id(
            self.ctx, self.source.id).credentials_ref)
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)
        self.assert_delete_available()

    def test_claim_failure_releases_claim(self):
        self.failed_phase('queued', '_claim')

    def test_preparation_failure_releases_claim(self):
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            self.failed_phase('preparing', '_prepare')

    def test_prepared_failure_releases_claim(self):
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            self.failed_phase('prepared', '_bind')

    def test_binding_failure_releases_claim(self):
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            self.failed_phase('binding', '_rollback')

    def test_rollback_failure_releases_claim(self):
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            self.failed_phase('rollback', '_rollback')

    def test_failed_cleanup_releases_claim(self):
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            self.failed_phase('cleanup_new', '_cleanup')

    def test_missing_engine_key_cleans_new_secret_and_allows_delete(self):
        pending = self.admit()
        self.driver.prepare_deploy_key.side_effect = (
            engine_base.EngineNotFound())
        self.assertRaises(exception.Conflict, self.run_rotation, pending)
        self.assertIsNone(self.pending())
        self.assertIn(self.old_ref, self.store.values)
        self.assertNotIn(pending['reference'], self.store.values)
        self.assert_delete_available()

    def test_service_delete_retries_unreachable_engine_key_cleanup(self):
        http, _observed = self.real_adapter()
        endpoint = self.manager._endpoint(self.stack)
        self.service.engine_ref = 'app-uuid'
        self.service.save()
        http.delete(endpoint.base_url + '/applications/app-uuid',
                    [{'status_code': 200}, {'status_code': 404}])
        key_delete = http.delete(
            endpoint.base_url + '/security/keys/key-uuid', status_code=503)
        with mock.patch.object(self.manager, '_known', return_value=False), \
                mock.patch.object(self.manager, 'collect_registry'):
            for status in (states.ACTIVE, states.ERROR):
                self.stack.status = status
                self.stack.save()
                with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast'):
                    self.request('delete', self.path, status=202)
                self.manager.execute(
                    self.ctx, 'delete_service', objects.Service.get_by_id(
                        self.ctx, self.service.id))
                saved = objects.Service.get_by_id(self.ctx, self.service.id)
                self.assertEqual(states.ERROR, saved.status)
                self.assertIn(self.old_ref, self.store.values)
            self.assertTrue(key_delete.called)
            http.delete(endpoint.base_url + '/security/keys/key-uuid',
                        status_code=200)
            with mock.patch('coast.conductor.rpcapi.ConductorAPI.cast'):
                self.request('delete', self.path, status=202)
            self.manager.execute(
                self.ctx, 'delete_service', objects.Service.get_by_id(
                    self.ctx, self.service.id))
        self.assertRaises(exception.NotFound, objects.Service.get_by_id,
                          self.ctx, self.service.id)
        self.assertNotIn(self.old_ref, self.store.values)

    def test_dispatch_failure_is_recovered_from_checkpoint(self):
        pending = self.admit()
        self.assertEqual('queued', pending['phase'])
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        saved = objects.Source.get_by_id(self.ctx, self.source.id)
        self.assertEqual(pending['reference'], saved.credentials_ref)
        self.assertNotIn(self.old_ref, self.store.values)
        self.driver.restore_deploy_key.assert_called_once_with(
            None, mock.ANY, mock.ANY, 'new-key')

    def test_engine_delete_failure_keeps_cleanup_checkpoint(self):
        pending = self.admit()
        self.driver.delete_deploy_key.side_effect = RuntimeError('secret-data')
        self.assertTrue(self.run_rotation(pending))
        self.assertEqual('cleanup_old', self.pending()['phase'])
        self.assertIn(self.old_ref, self.store.values)
        self.assertEqual('old-key', self.pending()['old_engine_key'])
        self.driver.delete_deploy_key.side_effect = None
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertNotIn(self.old_ref, self.store.values)

    def test_barbican_delete_failure_does_not_repeat_engine_delete(self):
        pending = self.admit()
        with mock.patch.object(self.store, 'delete',
                               side_effect=RuntimeError('secret-data')):
            self.assertTrue(self.run_rotation(pending))
        self.assertEqual('cleanup_old', self.pending()['phase'])
        self.assertIsNone(self.pending()['old_engine_key'])
        self.assertIn(self.old_ref, self.store.values)
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.driver.delete_deploy_key.assert_called_once()
        self.assertIsNone(self.pending())
        self.assertNotIn(self.old_ref, self.store.values)

    def test_restore_error_is_redacted_and_recoverable(self):
        pending = self.admit()
        self.driver.restore_deploy_key.side_effect = RuntimeError(
            'sensitive-private-pasted-text')
        error = self.assertRaises(exception.CoastException,
                                  self.run_rotation, pending)
        self.assertEqual('Deploy key update failed.', str(error))
        self.assertNotIn('sensitive-private-pasted-text', str(error))
        rendered = ''.join(traceback.format_exception(error))
        self.assertNotIn('sensitive-private-pasted-text', rendered)
        self.assertEqual('rollback', self.pending()['phase'])
        self.assertEqual('old-key', self.pending()['old_engine_key'])
        self.assertEqual('new-key', self.pending()['new_engine_key'])
        self.assertIn(self.old_ref, self.store.values)
        self.assertIn(pending['reference'], self.store.values)
        self.driver.delete_deploy_key.assert_not_called()
        self.driver.restore_deploy_key.side_effect = None
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)
        self.assertEqual(self.old_ref, objects.Source.get_by_id(
            self.ctx, self.source.id).credentials_ref)
        self.driver.restore_deploy_key.assert_called_with(
            None, mock.ANY, mock.ANY, 'old-key')
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_uncertain_binding_recovers_by_restoring_old_key(self):
        pending = self.admit()
        source = objects.Source.get_by_id(self.ctx, self.source.id)
        pending.update(phase='binding', old_engine_key='old-key',
                       new_engine_key='new-key')
        source.deploy_key_rotation = pending
        source.save()
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertIn(self.old_ref, self.store.values)
        self.assertNotIn(pending['reference'], self.store.values)
        self.driver.prepare_deploy_key.assert_not_called()
        self.driver.restore_deploy_key.assert_called_once_with(
            None, mock.ANY, mock.ANY, 'old-key')

    def test_busy_conductor_releases_claim_and_retries_cleanup(self):
        pending = self.admit()
        self.stack.acquire_task(states.UPGRADING)
        with mock.patch.object(self.store, 'delete', side_effect=RuntimeError):
            self.assertRaises(exception.CoastException,
                              self.run_rotation, pending)
        self.assertEqual('cleanup_new', self.pending()['phase'])
        saved = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.NONE, saved.task_state)
        self.assertIsNone(saved.task_owner)
        self.assertIn(pending['reference'], self.store.values)
        self.driver.prepare_deploy_key.assert_not_called()
        self.driver.restore_deploy_key.assert_not_called()
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)

    def test_busy_conductor_discards_unstarted_rotation(self):
        pending = self.admit()
        self.stack.acquire_task(states.DELETING)
        self.assertRaises(exception.CoastException, self.run_rotation, pending)
        self.assertIsNone(self.pending())
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.driver.prepare_deploy_key.assert_not_called()

    def test_janitor_protects_checkpoint_only_refs_in_every_phase(self):
        pending = self.admit()
        pending['reference'] = (
            'https://secrets.example.com/v1/secrets/'
            '11111111-1111-4111-8111-111111111111')
        pending['previous'] = (
            'https://secrets.example.com/v1/secrets/'
            '22222222-2222-4222-8222-222222222222')
        admin = context.RequestContext(is_admin=True)
        for phase in ('queued', 'preparing', 'abort_preparing', 'prepared',
                      'binding', 'rollback',
                      'cleanup_new', 'cleanup_old'):
            source = objects.Source.get_by_id(self.ctx, self.source.id)
            source.credentials_ref = None
            source.deploy_key_rotation = dict(pending, phase=phase)
            source.save()
            protected = janitor.references(admin)
            self.assertIn(pending['reference'], protected)
            self.assertIn(pending['previous'], protected)
            for key in ('reference', 'previous'):
                identifier = pending[key].rsplit('/', 1)[-1]
                self.assertIn(identifier, protected)
                resource = SimpleNamespace(
                    secret_id=identifier,
                    name='coast:%s:deploy-key:%s' % (
                        self.stack.id, self.service.id))
                self.assertFalse(janitor.is_orphan(
                    'secret', resource, protected, 'coast',
                    janitor.secret_owners(admin)))

    def test_unowned_rpc_reference_cannot_discard_an_admitted_key(self):
        pending = self.admit()
        self.assertRaises(exception.CoastException,
                          self.manager.rotate_deploy_key, self.ctx,
                          self.service.id, 'unowned', pending['public_key'])
        self.assertEqual(pending, self.pending())
        self.driver.prepare_deploy_key.assert_not_called()

    def test_uncertain_preparation_remains_owned_for_adoption(self):
        pending = self.admit()
        self.driver.prepare_deploy_key.side_effect = RuntimeError(
            'secret-data')
        self.assertRaises(exception.CoastException, self.run_rotation, pending)
        self.assertEqual('abort_preparing', self.pending()['phase'])
        self.assertIn(pending['reference'], self.store.values)
        self.driver.prepare_deploy_key.side_effect = None
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertIn(self.old_ref, self.store.values)
        self.assertNotIn(pending['reference'], self.store.values)

    def test_definitive_unsupported_preflight_releases_and_cleans(self):
        pending = self.admit()
        self.driver.SUPPORTS_DEPLOY_KEY_ROTATION = False
        self.assertRaises(exception.CoastException, self.run_rotation, pending)
        self.assertIsNone(self.pending())
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)
        self.driver.prepare_deploy_key.assert_not_called()
        self.driver.restore_deploy_key.assert_not_called()

    def test_unsupported_cleanup_failure_does_not_hold_service_claim(self):
        pending = self.admit()
        self.driver.SUPPORTS_DEPLOY_KEY_ROTATION = False
        with mock.patch.object(self.store, 'delete', side_effect=RuntimeError):
            self.assertRaises(exception.CoastException,
                              self.run_rotation, pending)
        self.assertEqual('cleanup_new', self.pending()['phase'])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertNotIn(pending['reference'], self.store.values)

    def test_pending_refs_are_discoverable_for_delete(self):
        from coast.common import app_secrets
        pending = self.admit()
        refs = app_secrets.references(self.ctx, self.service)
        self.assertIn(pending['reference'], refs)
        self.assertIn(self.old_ref, refs)

    def in_place(self):
        self.driver.DEPLOY_KEY_IN_PLACE = True
        self.driver.prepare_deploy_key.return_value = ('live-key', 'live-key')
        return self.admit()

    def test_in_place_cleanup_retry_preserves_live_engine_key(self):
        pending = self.in_place()
        with mock.patch.object(self.store, 'delete',
                               side_effect=RuntimeError('private-data')):
            self.assertTrue(self.run_rotation(pending))
        saved = objects.Source.get_by_id(self.ctx, self.source.id)
        self.assertEqual(pending['reference'], saved.credentials_ref)
        self.assertEqual('cleanup_old', self.pending()['phase'])
        self.driver.delete_deploy_key.assert_not_called()
        self.driver.write_deploy_key.assert_called_once_with(
            None, mock.ANY, mock.ANY, 'live-key',
            self.store.get(self.ctx, pending['reference']))
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertNotIn(self.old_ref, self.store.values)
        self.assertIn(pending['reference'], self.store.values)
        self.driver.write_deploy_key.assert_called_once()
        self.driver.delete_deploy_key.assert_not_called()

    def test_in_place_failure_restores_old_material_and_credentials(self):
        pending = self.in_place()
        self.driver.write_deploy_key.side_effect = [
            RuntimeError('sensitive-private-text'), None]
        error = self.assertRaises(exception.CoastException,
                                  self.run_rotation, pending)
        self.assertNotIn('sensitive-private-text',
                         ''.join(traceback.format_exception(error)))
        self.assertIsNone(self.pending())
        self.assertEqual(self.old_ref, objects.Source.get_by_id(
            self.ctx, self.source.id).credentials_ref)
        self.driver.write_deploy_key.assert_called_with(
            None, mock.ANY, mock.ANY, 'live-key', 'old-private-key')
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)
        self.driver.delete_deploy_key.assert_not_called()

    def test_rollback_cleanup_failure_releases_claim_and_keeps_refs(self):
        pending = self.in_place()
        self.driver.write_deploy_key.side_effect = [RuntimeError(), None]
        with mock.patch.object(self.store, 'delete', side_effect=RuntimeError):
            self.assertRaises(exception.CoastException,
                              self.run_rotation, pending)
        self.assertEqual('cleanup_new', self.pending()['phase'])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.assertIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIn(self.old_ref, self.store.values)

    def test_in_place_restoration_error_is_durable_and_redacted(self):
        pending = self.in_place()
        self.driver.write_deploy_key.side_effect = RuntimeError('private-text')
        error = self.assertRaises(exception.CoastException,
                                  self.run_rotation, pending)
        self.assertNotIn('private-text', str(error))
        self.assertEqual('rollback', self.pending()['phase'])
        self.assertIn(self.old_ref, self.store.values)
        self.assertIn(pending['reference'], self.store.values)
        self.driver.write_deploy_key.side_effect = None
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertIn(self.old_ref, self.store.values)
        self.assertNotIn(pending['reference'], self.store.values)
        self.driver.delete_deploy_key.assert_not_called()

    def test_generic_task_recovery_preserves_pending_rotation(self):
        pending = self.in_place()
        with mock.patch('coast.conductor.manager._seconds_since',
                        return_value=301):
            self.assertIsNone(self.manager._recover_domain_task(
                self.ctx, self.service.id))
        self.assertEqual(pending, self.pending())
        saved = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(deploy_keys.OWNER, saved.task_owner)
        self.assertTrue(self.run_rotation(pending))
        self.assertIsNone(self.pending())

    def real_adapter(self, reject_first=False):
        self.config.config(insecure_direct_url=True, retry_wait=0,
                           group='coolify')
        endpoint = engine_base.EngineEndpoint(
            'https://engine.example.com/api/v1', '', '', '', 'test-token')
        self.manager._driver.return_value = v4.CoolifyV4Driver(self.conf)
        self.manager._endpoint.return_value = endpoint
        directory = Path(__file__).parent / 'drivers' / 'coolify' / 'fixtures'
        fixture = json.loads(
            (directory / 'deploy-key-rotation.json').read_text())
        fixture['keys'][0]['name'] = v4.name(self.service)
        contract = json.loads((directory / 'requests.json').read_text())[
            'PATCH /security/keys/{uuid}']['body']
        http = requests_mock.Mocker()
        http.start()
        self.addCleanup(http.stop)
        http.get(endpoint.base_url + '/security/keys', json=fixture['keys'])
        observed = {'material': 'old-private-key', 'writes': 0}

        def update(request, context):
            from coast.api.v1 import schemas
            schemas.jsonschema.validate(request.json(), contract)
            observed['writes'] += 1
            if reject_first and observed['writes'] == 1:
                context.status_code = 422
                return fixture['rejected']
            observed['material'] = request.json()['private_key']
            return fixture['updated']

        http.patch(endpoint.base_url + '/security/keys/key-uuid', json=update)
        return http, observed

    def test_real_adapter_rotation_retries_secret_cleanup(self):
        http, observed = self.real_adapter()
        pending = self.admit()
        with mock.patch.object(self.store, 'delete',
                               side_effect=RuntimeError('private-data')):
            self.assertTrue(self.run_rotation(pending))
        new_private = self.store.get(self.ctx, pending['reference'])
        self.assertEqual(new_private, observed['material'])
        self.assertEqual(pending['reference'], objects.Source.get_by_id(
            self.ctx, self.source.id).credentials_ref)
        self.assertEqual('cleanup_old', self.pending()['phase'])
        with mock.patch.object(deploy_keys, 'RECOVERY_DELAY', 0):
            deploy_keys.sweep(self.manager)
        self.assertIsNone(self.pending())
        self.assertNotIn(self.old_ref, self.store.values)
        self.assertIn(pending['reference'], self.store.values)
        self.assertEqual(1, observed['writes'])
        self.assertEqual(['GET', 'PATCH'],
                         [request.method for request in http.request_history])

    def test_real_adapter_failure_retains_old_credentials(self):
        http, observed = self.real_adapter(reject_first=True)
        pending = self.admit()
        error = self.assertRaises(exception.CoastException,
                                  self.run_rotation, pending)
        self.assertNotIn('pasted-private-data', str(error))
        self.assertNotIn('old-private-key', str(error))
        self.assertEqual('old-private-key', observed['material'])
        self.assertEqual(self.old_ref, objects.Source.get_by_id(
            self.ctx, self.source.id).credentials_ref)
        self.assertIn(self.old_ref, self.store.values)
        self.assertNotIn(pending['reference'], self.store.values)
        self.assertIsNone(self.pending())
        self.assertEqual(2, observed['writes'])
        self.assertEqual(['GET', 'PATCH', 'PATCH'],
                         [request.method for request in http.request_history])
