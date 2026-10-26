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

import ast
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import uuid

from cryptography.hazmat.primitives import serialization
import fixtures
from keystoneauth1 import noauth
from keystoneauth1 import session as ks_session
from openstack import connection
from openstack.key_manager.v1 import secret
from requests_mock.contrib import fixture as requests_fixture

from coast.api import secret_store
from coast.common import exception
from coast.common import secrets
from coast.tests import app_api
from coast.tests import base


class BarbicanStoreTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.proxy = mock.Mock()
        self.proxy.get_endpoint.return_value = (
            'https://secrets.example.com')
        self.connection = self.useFixture(fixtures.MockPatch(
            'coast.common.clients.get_connection')).mock
        self.connection.return_value.key_manager = self.proxy
        self.store = secrets.BarbicanSecretStore(self.conf)
        self.identifier = str(uuid.uuid4())
        self.ref = ('https://secrets.example.com/v1/secrets/' +
                    self.identifier)
        self.proxy.create_secret.return_value.secret_id = self.identifier
        self.proxy.put.return_value.status_code = 200
        session = self.useFixture(fixtures.MockPatch(
            'coast.common.clients.get_session')).mock
        session.return_value.get_user_id.return_value = 'service-user'
        self.ctx = SimpleNamespace(project_id='tenant')
        self.identity = secret_store.SecretIdentity(
            'stack', 'env', 'service', 'tenant')
        self.logs = self.useFixture(fixtures.FakeLogger())

    def test_text_roundtrip_and_service_connection(self):
        self.proxy.create_secret.return_value.secret_ref = self.ref
        self.proxy.get_secret.return_value.payload = 'sensitive-value'
        self.assertEqual(self.ref, self.store.put(
            self.ctx, 'sensitive-value', self.identity))
        self.assertEqual('sensitive-value', self.store.get(self.ctx, self.ref))
        self.proxy.create_secret.assert_called_once_with(
            payload='c2Vuc2l0aXZlLXZhbHVl',
            payload_content_type='application/octet-stream',
            payload_content_encoding='base64',
            secret_type='opaque', name=self.identity.name)
        self.proxy.get_secret.assert_called_once_with(self.identifier)
        self.connection.assert_called_with('barbican', self.conf)

    def test_exact_utf8_roundtrip_and_legacy_text(self):
        self.proxy.create_secret.return_value.secret_ref = self.ref
        for value in ('a: 1\nb: 2\n', '  x  \n\n', 'x\r\n', 'é\n'):
            with self.subTest(value=value):
                self.proxy.create_secret.reset_mock()
                self.store.put(self.ctx, value, self.identity)
                self.proxy.create_secret.assert_called_once_with(
                    payload=base64.b64encode(
                        value.encode('utf-8')).decode('ascii'),
                    payload_content_type='application/octet-stream',
                    payload_content_encoding='base64',
                    secret_type='opaque', name=self.identity.name)
                for payload in (value.encode('utf-8'), value):
                    self.proxy.get_secret.return_value.payload = payload
                    self.assertEqual(value, self.store.get(self.ctx, self.ref))

    def test_invalid_utf8_does_not_leak(self):
        self.proxy.get_secret.return_value.payload = b'private-value\xff'
        error = self.assertRaises(exception.CoastException, self.store.get,
                                  self.ctx, self.ref)
        self.assertEqual('Secret storage operation failed.', str(error))
        self.assertTrue(error.__suppress_context__)
        self.assertNotIn('private-value', self.logs.output)

    def test_named_private_secret(self):
        identity = secret_store.SecretIdentity('stack', 'env', 'service',
                                               'tenant')
        self.proxy.create_secret.return_value.secret_ref = self.ref
        with mock.patch.object(secrets.clients, 'get_session') as session:
            session.return_value.get_user_id.return_value = 'service-user'
            self.store.put(self.ctx, 'value', identity)
        self.assertEqual('coast:stack:env:service',
                         self.proxy.create_secret.call_args.kwargs['name'])
        self.proxy.put.assert_any_call(
            self.ref + '/acl',
            json={'read': {'project-access': False,
                           'users': ['service-user']}})
        self.proxy.put.assert_any_call(
            self.ref + '/metadata',
            json={'metadata': {'coast-stack': 'stack', 'coast-kind': 'env',
                               'coast-owner': 'service',
                               'coast-project': 'tenant'}})

    def test_acl_or_metadata_failure_rolls_back(self):
        for fail_at in (1, 2):
            self.proxy.reset_mock()
            self.proxy.create_secret.return_value.secret_ref = self.ref
            self.proxy.put.side_effect = (
                [RuntimeError('private-value')] if fail_at == 1 else
                [mock.Mock(status_code=200), RuntimeError('private-value')])
            with mock.patch.object(secrets.clients, 'get_session') as session:
                session.return_value.get_user_id.return_value = 'service-user'
                error = self.assertRaises(
                    exception.CoastException, self.store.put, self.ctx,
                    'value', secret_store.SecretIdentity(
                        'stack', 'env', 'service', 'tenant'))
            self.assertEqual('Secret storage operation failed.', str(error))
            self.proxy.delete_secret.assert_called_once_with(
                self.identifier, ignore_missing=True)
            self.assertNotIn('private-value', self.logs.output)

    def test_sdk_http_attribution_and_rollback(self):
        http = self.useFixture(requests_fixture.Fixture())
        for path in ('/v1', '/key-manager/v1'):
            for failed in (None, 'acl', 'metadata'):
                endpoint = 'https://secrets.example.com' + path
                reference = endpoint + '/secrets/' + self.identifier
                http.get(endpoint, json={'version': {
                    'id': 'v1.0', 'status': 'CURRENT',
                    'links': [{'rel': 'self', 'href': endpoint}]}})
                session = ks_session.Session(auth=noauth.NoAuth())
                cloud = connection.Connection(
                    session=session, key_manager_endpoint_override=endpoint)
                self.connection.return_value = cloud
                created = http.post(endpoint + '/secrets', status_code=201,
                                    json={'secret_ref': reference})
                acl = http.put(reference + '/acl',
                               status_code=403 if failed == 'acl' else 200,
                               json={})
                metadata = http.put(
                    reference + '/metadata',
                    status_code=403 if failed == 'metadata' else 200, json={})
                deleted = http.delete(reference, status_code=204)
                if failed:
                    error = self.assertRaises(
                        exception.CoastException, self.store.put, self.ctx,
                        'private-value', self.identity)
                    self.assertEqual('Secret storage operation failed.',
                                     str(error))
                    self.assertEqual(1, deleted.call_count)
                else:
                    self.assertEqual(reference, self.store.put(
                        self.ctx, 'private-value', self.identity))
                    self.assertEqual(0, deleted.call_count)
                self.assertEqual(
                    {'payload': base64.b64encode(
                        b'private-value').decode('ascii'),
                     'payload_content_type': 'application/octet-stream',
                     'payload_content_encoding': 'base64',
                     'secret_type': 'opaque', 'name': self.identity.name},
                    created.last_request.json())
                self.assertEqual(
                    {'read': {'project-access': False,
                              'users': ['service-user']}},
                    acl.last_request.json())
                if failed != 'acl':
                    self.assertEqual({'metadata': self.identity.metadata},
                                     metadata.last_request.json())
        self.assertNotIn('private-value', self.logs.output)

    def test_sdk_http_binary_payload(self):
        http = self.useFixture(requests_fixture.Fixture())
        endpoint = 'https://secrets.example.com/v1'
        session = ks_session.Session(auth=noauth.NoAuth())
        self.connection.return_value = connection.Connection(
            session=session, key_manager_endpoint_override=endpoint)
        http.get(endpoint, json={'version': {
            'id': 'v1.0', 'status': 'CURRENT',
            'links': [{'rel': 'self', 'href': endpoint}]}})
        http.get(self.ref, json={
            'secret_ref': self.ref,
            'content_types': {'default': 'application/octet-stream'}})
        value = '  é\r\n\n'
        http.get(self.ref + '/payload', content=value.encode('utf-8'),
                 headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(value, self.store.get(self.ctx, self.ref))

    def test_every_put_call_supplies_identity(self):
        root = Path(__file__).resolve().parents[2]
        count = 0
        for folder in ('api', 'common', 'conductor'):
            for path in (root / folder).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text())):
                    if not (isinstance(node, ast.Call) and node.args and
                            isinstance(node.func, ast.Attribute) and
                            node.func.attr == 'call' and
                            isinstance(node.func.value, ast.Name) and
                            node.func.value.id == 'secret_store' and
                            isinstance(node.args[0], ast.Constant) and
                            node.args[0].value == 'put'):
                        continue
                    self.assertEqual(4, len(node.args), str(path))
                    count += 1
        self.assertEqual(16, count)

    def test_missing_identity_and_user_fail_closed(self):
        self.assertRaises(exception.CoastException, secret_store.call,
                          'put', self.ctx, 'value')
        self.proxy.create_secret.assert_not_called()
        self.proxy.create_secret.return_value.secret_ref = self.ref
        with mock.patch.object(secrets.clients, 'get_session') as session:
            session.return_value.get_user_id.return_value = None
            self.assertRaises(exception.CoastException, self.store.put,
                              self.ctx, 'value', self.identity)
        self.proxy.put.assert_not_called()
        self.proxy.delete_secret.assert_called_once_with(
            self.identifier, ignore_missing=True)

    def test_delete_is_idempotent(self):
        self.store.delete(self.ctx, self.ref)
        self.proxy.delete_secret.assert_called_once_with(
            self.identifier, ignore_missing=True)

    def test_invalid_references_never_reach_sdk(self):
        for ref in ('test-secret:fake', 'http://example.com/v1/secrets/x',
                    'https://user:password@example.com/v1/secrets/x',
                    self.ref + '?token=hidden', self.ref + '/payload',
                    self.ref.replace('/v1/', '/v2/')):
            for operation in ('get', 'delete'):
                self.assertRaises(exception.CoastException,
                                  getattr(self.store, operation),
                                  self.ctx, ref)
        self.proxy.get_secret.assert_not_called()
        self.proxy.delete_secret.assert_not_called()

    def test_http_catalog_roundtrip(self):
        self.proxy.get_endpoint.return_value = (
            'http://secrets.example.com:9311')
        reference = ('http://secrets.example.com:9311/v1/secrets/' +
                     self.identifier)
        self.proxy.create_secret.return_value.secret_ref = reference
        self.proxy.get_secret.return_value.payload = 'value'
        self.assertEqual(reference, self.store.put(self.ctx, 'value',
                                                   self.identity))
        self.assertEqual('value', self.store.get(self.ctx, reference))
        self.store.delete(self.ctx, reference)
        self.proxy.get_secret.assert_called_once_with(self.identifier)
        self.proxy.delete_secret.assert_called_once_with(
            self.identifier, ignore_missing=True)

    def test_endpoint_mismatches_and_url_components_are_rejected(self):
        for reference in (
                self.ref.replace('https:', 'http:'),
                self.ref.replace('secrets.example.com', 'other.example.com'),
                self.ref.replace(
                    'secrets.example.com', 'secrets.example.com:9'),
                self.ref.replace(
                    'secrets.example.com', 'secrets.example.com:0'),
                self.ref.replace('https://', 'https://@'),
                self.ref + '?', self.ref + '#', self.ref + '#fragment',
                self.ref.replace('/v1/', '/prefix/v1/')):
            with self.subTest(reference=reference):
                self.assertRaises(exception.CoastException,
                                  self.store.get, self.ctx, reference)
                self.assertRaises(exception.CoastException,
                                  self.store.delete, self.ctx, reference)
        self.proxy.get_secret.assert_not_called()
        self.proxy.delete_secret.assert_not_called()

    def test_versioned_and_prefixed_catalog_endpoints(self):
        for path in ('/v1', '/v1/', '/key-manager', '/key-manager/v1/'):
            with self.subTest(path=path):
                endpoint = 'https://secrets.example.com:443' + path
                base = path.rstrip('/')
                if not base.endswith('/v1'):
                    base += '/v1'
                reference = ('https://secrets.example.com' + base +
                             '/secrets/' + self.identifier)
                self.assertEqual(self.identifier,
                                 secrets._secret_id(reference, endpoint))

    def test_put_cleans_up_bad_reference_using_object_id(self):
        for reference in (None, self.ref.replace(
                'secrets.example.com', 'other.example.com')):
            with self.subTest(reference=reference):
                self.proxy.delete_secret.reset_mock()
                self.proxy.create_secret.return_value.secret_ref = reference
                self.assertRaises(exception.CoastException,
                                  self.store.put, self.ctx, 'value',
                                  self.identity)
                self.proxy.delete_secret.assert_called_once_with(
                    self.identifier, ignore_missing=True)

    def test_rejected_sdk_secret_does_not_leak(self):
        created = set()

        def create_secret(**kwargs):
            created.add(self.identifier)
            return secret.Secret(secret_ref=self.ref.replace(
                'secrets.example.com', 'other.example.com'))

        def delete_secret(identifier, **kwargs):
            created.remove(identifier)

        self.proxy.create_secret.side_effect = create_secret
        self.proxy.delete_secret.side_effect = delete_secret
        self.assertRaises(exception.CoastException,
                          self.store.put, self.ctx, 'value',
                          self.identity)
        self.assertEqual(set(), created)

    def test_put_cleanup_failure_is_sanitized(self):
        self.proxy.create_secret.return_value.secret_ref = None
        self.proxy.delete_secret.side_effect = RuntimeError('private-value')
        error = self.assertRaises(exception.CoastException,
                                  self.store.put, self.ctx, 'value',
                                  self.identity)
        self.assertEqual('Secret storage operation failed.', str(error))
        self.assertNotIn('private-value', self.logs.output)

    def test_delete_empty_reference_does_not_connect(self):
        for reference in (None, ''):
            self.assertIsNone(self.store.delete(self.ctx, reference))
        self.connection.assert_not_called()
        self.proxy.delete_secret.assert_not_called()

    def test_backend_errors_are_sanitized(self):
        for operation, method, value in (
                ('put', 'create_secret', 'private-value'),
                ('get', 'get_secret', self.ref),
                ('delete', 'delete_secret', self.ref)):
            getattr(self.proxy, method).side_effect = RuntimeError(
                'private-value password=auth-password')
            error = self.assertRaises(exception.CoastException,
                                      getattr(self.store, operation),
                                      self.ctx, value,
                                      *([self.identity] if operation == 'put'
                                        else []))
            self.assertNotIn('private-value', str(error))
            self.assertNotIn('auth-password', str(error))
            self.assertTrue(error.__suppress_context__)
        self.assertNotIn('private-value', self.logs.output)
        self.assertNotIn('auth-password', self.logs.output)

    def test_wrapper_classifies_backend_errors_without_details(self):
        with mock.patch.object(secret_store, 'get_store',
                               side_effect=RuntimeError('sensitive-payload')):
            error = self.assertRaises(exception.SecretStoreUnavailable,
                                      secret_store.call, 'get', self.ctx,
                                      self.ref)
        self.assertEqual('Secret storage operation failed.', str(error))
        self.assertTrue(error.__suppress_context__)
        self.assertNotIn('sensitive-payload', self.logs.output)

    def test_configuration_failure_is_sanitized(self):
        self.connection.side_effect = RuntimeError('password=credential')
        error = self.assertRaises(exception.CoastException,
                                  self.store.put, self.ctx, 'value',
                                  self.identity)
        self.assertNotIn('credential', str(error))

    def test_invalid_payload_type_fails_closed(self):
        for value in (None, 42, bytearray(b'value')):
            self.proxy.get_secret.return_value.payload = value
            self.assertRaises(exception.CoastException,
                              self.store.get, self.ctx, self.ref)

    def test_config_selection_and_default_fail_closed(self):
        self.useFixture(fixtures.MonkeyPatch(
            'coast.api.secret_store.STORE', None))
        self.useFixture(fixtures.MonkeyPatch(
            'coast.api.secret_store.cfg.CONF', self.conf))
        self.assertRaises(exception.CoastException,
                          secret_store.call, 'put', self.ctx, 'value',
                          self.identity)
        self.connection.assert_not_called()
        self.config.config(backend='barbican', group='secrets')
        self.assertIsInstance(secret_store.get_store(),
                              secrets.BarbicanSecretStore)
        self.proxy.create_secret.return_value.secret_ref = self.ref
        self.assertEqual(self.ref, secret_store.call(
            'put', self.ctx, 'value', self.identity))

    def test_put_rejects_invalid_response_and_non_text(self):
        self.proxy.create_secret.return_value.secret_ref = None
        self.assertRaises(exception.CoastException,
                          self.store.put, self.ctx, 'value',
                          self.identity)
        self.proxy.reset_mock()
        self.assertRaises(exception.CoastException,
                          self.store.put, self.ctx, b'value',
                          self.identity)
        self.proxy.create_secret.assert_not_called()


class EnvironmentSecretsTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.store = app_api.MemorySecretStore()
        self.useFixture(fixtures.MonkeyPatch(
            'coast.api.secret_store.STORE', self.store))
        self.ctx = SimpleNamespace(project_id='tenant')
        self.identity = secret_store.SecretIdentity(
            'stack', 'env', 'service', 'tenant')

    def test_ssh_keys_match_and_are_unique(self):
        first = secrets.create_ssh_keypair(self.ctx, self.identity)
        second = secrets.create_ssh_keypair(self.ctx, self.identity)
        self.assertNotEqual(first['ssh_key_ref'], second['ssh_key_ref'])
        self.assertNotEqual(first['ssh_public_key'], second['ssh_public_key'])
        private = self.store.get(self.ctx, first['ssh_key_ref'])
        key = serialization.load_ssh_private_key(private.encode(), None)
        public = key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH).decode()
        self.assertEqual(first['ssh_public_key'], public)
        self.assertTrue(public.startswith('ssh-ed25519 '))

    def test_hmac_entropy_length_and_unique_refs(self):
        first = secrets.create_hmac_key(self.ctx, self.identity)
        second = secrets.create_hmac_key(self.ctx, self.identity)
        value = self.store.get(self.ctx, first)
        self.assertEqual(32, len(bytes.fromhex(value)))
        self.assertNotEqual(value, self.store.get(self.ctx, second))
        self.assertNotEqual(first, second)

    def test_engine_token_stores_returned_value_only(self):
        ref = secrets.store_engine_token(
            self.ctx, 'returned-token', self.identity)
        self.assertEqual('returned-token', self.store.get(self.ctx, ref))
        for token in ('', None, b'token'):
            self.assertRaises(exception.Invalid,
                              secrets.store_engine_token, self.ctx, token,
                              self.identity)

    def test_helpers_propagate_storage_failure(self):
        with mock.patch.object(secret_store, 'STORE',
                               secret_store.SecretStore()):
            for helper, args in ((secrets.create_ssh_keypair, ()),
                                 (secrets.create_hmac_key, ()),
                                 (secrets.store_engine_token, ('token',))):
                self.assertRaises(exception.CoastException,
                                  helper, self.ctx, *args, self.identity)
