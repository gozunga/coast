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

import hashlib
import hmac
import json
import logging
from types import SimpleNamespace
from unittest import mock
from urllib import parse

from keystoneauth1 import exceptions as ks_exceptions
from openstack import connection
import requests_mock

from coast.common import context
from coast.common import exception
from coast.conductor import backup_storage as storage
from coast.tests import base


class StorageTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = context.RequestContext(project_id='a' * 32)
        self.proxy = mock.Mock()
        self.proxy.get_endpoint.return_value = (
            'https://objects.example.com/swift/v1/AUTH_' + 'a' * 32)
        self.backend = storage.SwiftStorage(
            self.conf, self.ctx, 'service-id', proxy=self.proxy)
        self.policy = SimpleNamespace(service_id='service-id',
                                      signing_key_ref='signing-ref',
                                      save=lambda: None)
        self.backup = SimpleNamespace(id='backup-id')

    def test_customer_account_uses_service_session_and_selected_region(self):
        session = mock.Mock()
        session.get_endpoint.return_value = (
            'https://objects.example.com/prefix/swift/v1/AUTH_' + 'b' * 32)
        self.config.config(region_name='region-test', interface='internal',
                           group='backup')
        with mock.patch.object(storage.clients, 'get_session',
                               return_value=session), mock.patch.object(
                                   storage.connection, 'Connection') as conn:
            proxy = storage.proxy_for(self.conf, 'a' * 32)
        args = conn.call_args.kwargs
        self.assertIs(session, args['session'])
        self.assertEqual('https://objects.example.com/prefix/swift/v1/AUTH_' +
                         'a' * 32, args['object_store_endpoint_override'])
        self.assertTrue(proxy.logger.disabled)
        session.get_endpoint.assert_called_once_with(
            service_type='object-store', interface='internal',
            region_name='region-test')
        self.assertNotIn('auth_token', args)

    def test_catalog_missing_or_insecure_fails_closed(self):
        for endpoint in (None, 'http://objects.example.com/v1/AUTH_service',
                         'https://objects.example.com/v1/unknown',
                         'https://objects.example.com/v1/AUTH_service?q=bad'):
            session = mock.Mock()
            session.get_endpoint.return_value = endpoint
            with mock.patch.object(storage.clients, 'get_session',
                                   return_value=session):
                self.assertRaises(storage.StorageUnavailable,
                                  storage.proxy_for, self.conf, 'a' * 32)
        session.get_endpoint.side_effect = (
            ks_exceptions.catalog.EndpointNotFound)
        with mock.patch.object(storage.clients, 'get_session',
                               return_value=session):
            self.assertRaises(storage.StorageUnavailable, storage.proxy_for,
                              self.conf, 'a' * 32)
        self.assertRaises(exception.Invalid, storage.proxy_for,
                          self.conf, '../other-account')

    def test_prepare_preserves_customer_key_ignores_metadata_ref(self):
        self.proxy.get_account_metadata.return_value = SimpleNamespace(
            meta_temp_url_key='customer-key', meta_temp_url_key_2=None,
            metadata={'coast-backup-temp-url-key-ref': 'untrusted-secret-ref'})
        with mock.patch.object(storage, 'account_key',
                               return_value=('owned-ref', 'owned-key')):
            self.backend.prepare(self.policy)
        metadata = self.proxy.set_account_metadata.call_args.kwargs
        self.assertEqual({'temp-url-key-2': 'owned-key'}, metadata)
        self.assertEqual('owned-ref', self.policy.signing_key_ref)
        self.proxy.get_account_metadata.return_value = SimpleNamespace(
            meta_temp_url_key='customer-key', meta_temp_url_key_2='owned-key')
        self.proxy.set_account_metadata.reset_mock()
        with mock.patch.object(storage, 'account_key',
                               return_value=('owned-ref', 'owned-key')):
            self.backend.prepare(self.policy)
        self.assertFalse(self.proxy.set_account_metadata.called)

    def test_occupied_keys_are_never_rotated(self):
        self.proxy.get_account_metadata.return_value = SimpleNamespace(
            meta_temp_url_key='one', meta_temp_url_key_2='two')
        with mock.patch.object(storage, 'account_key',
                               return_value=('owned-ref', 'owned-key')):
            self.assertRaises(exception.Conflict,
                              self.backend.prepare, self.policy)
        self.assertFalse(self.proxy.set_account_metadata.called)

    def test_temp_url_signs_complete_rgW_path_and_only_one_method(self):
        with mock.patch.object(storage.secret_store, 'call',
                               return_value='key'), \
                mock.patch.object(storage.time, 'time', return_value=100):
            urls = self.backend.urls(self.policy, self.backup, 'PUT', 2)
        self.assertEqual(2, len(urls))
        first = parse.urlsplit(urls[0])
        params = parse.parse_qs(first.query)
        expiry = 100 + self.conf.backup.url_ttl
        expected = hmac.new(b'key', ('PUT\n%s\n%s' % (
            expiry, first.path)).encode(), hashlib.sha1).hexdigest()
        self.assertEqual([expected], params['temp_url_sig'])
        self.assertEqual([str(expiry)], params['temp_url_expires'])
        self.assertEqual('/swift/v1/AUTH_' + 'a' * 32 +
                         '/coast-backups-service-id/backup-id/000000',
                         first.path)
        self.assertNotIn('key', urls[0])

    def test_manifest_checks_head_sizes_and_uses_separate_etags(self):
        self.proxy.get_object_metadata.side_effect = [
            SimpleNamespace(content_length=10, etag='a' * 32),
            SimpleNamespace(content_length=5, etag='b' * 32)]
        self.proxy.put.return_value.status_code = 201
        self.proxy.put.return_value.reason = 'Created'
        with mock.patch.object(storage.sdk_exceptions, 'raise_from_response'):
            self.backend.complete(self.backup, [dict(size_bytes=10),
                                                dict(size_bytes=5)])
        args = self.proxy.put.call_args
        manifest = json.loads(args.kwargs['data'])
        self.assertEqual([10, 5], [item['size_bytes'] for item in manifest])
        self.assertEqual(['a' * 32, 'b' * 32],
                         [item['etag'] for item in manifest])
        self.assertEqual({'multipart-manifest': 'put'}, args.kwargs['params'])
        self.assertEqual('coast-backups-service-id/backup-id/manifest',
                         args.args[0])

    def test_manifest_not_written_on_mismatched_object(self):
        self.proxy.get_object_metadata.return_value = SimpleNamespace(
            content_length=9, etag='a' * 32)
        self.assertRaises(ValueError, self.backend.complete, self.backup,
                          [dict(size_bytes=10)])
        self.assertFalse(self.proxy.put.called)

    def test_cleanup_includes_uncertain_uploads_and_manifest(self):
        self.proxy.objects.return_value = [
            SimpleNamespace(name='backup-id/000000'),
            SimpleNamespace(name='backup-id/manifest')]
        self.backend.delete(self.backup)
        self.assertEqual(['backup-id/000000', 'backup-id/manifest'], [
            call.args[0] for call in self.proxy.delete_object.call_args_list])
        self.assertEqual('backup-id/',
                         self.proxy.objects.call_args.kwargs['prefix'])

    def test_debug_does_not_enable_storage_payload_logging(self):
        session = mock.Mock()
        session.get_endpoint.return_value = (
            self.proxy.get_endpoint.return_value)
        with mock.patch.object(storage.clients, 'get_session',
                               return_value=session), mock.patch.object(
                                   storage.connection, 'Connection') as conn:
            conn.return_value.object_store = self.proxy
            proxy = storage.proxy_for(self.conf, 'a' * 32)
        with self.assertLogs('coast.backup.storage', logging.DEBUG) as logs:
            logging.getLogger('coast.backup.storage').info('public probe')
            proxy.logger.debug('private signing-key payload')
        self.assertEqual(1, len(logs.output))
        self.assertNotIn('private', str(logs.output))

    def test_sdk_wire_headers_preserve_key_and_publish_slo(self):
        endpoint = self.proxy.get_endpoint.return_value
        container = endpoint + '/' + self.backend.container
        with requests_mock.Mocker() as http:
            http.put(container, status_code=201)
            http.head(endpoint + '/', headers={
                'X-Account-Meta-Temp-URL-Key': 'customer-key'})
            http.post(endpoint + '/', status_code=204)
            http.head(container + '/backup-id/000000', headers={
                'Content-Length': '10', 'ETag': 'a' * 32})
            http.put(container + '/backup-id/manifest', status_code=201)
            self.backend.proxy = connection.Connection(
                auth_type='none', service_types=['object-store'],
                object_store_endpoint_override=endpoint,
                object_store_api_version='1').object_store
            with mock.patch.object(storage, 'account_key',
                                   return_value=('ref', 'owned-key')):
                self.backend.prepare(self.policy)
            self.backend.complete(self.backup, [dict(size_bytes=10)])
            posts = [request for request in http.request_history
                     if request.method == 'POST']
            self.assertEqual(1, len(posts))
            self.assertEqual('owned-key', posts[0].headers[
                'X-Account-Meta-Temp-URL-Key-2'])
            self.assertNotIn('X-Account-Meta-Temp-URL-Key', posts[0].headers)
            put = http.request_history[-1]
            self.assertEqual(['put'], put.qs['multipart-manifest'])
            manifest = put.json()
            self.assertEqual('a' * 32, manifest[0]['etag'])
            self.assertEqual(10, manifest[0]['size_bytes'])
