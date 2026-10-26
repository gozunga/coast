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

"""Offline agent security boundaries; TLS handshakes use MemoryBIO."""

import contextlib
import datetime
import hashlib
import hmac
from http import client
import io
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography import x509

from coast.agent.engines import bootstrap
from coast.agent.engines import coolify
from coast.agent import export_archive
from coast.agent import heartbeat
from coast.agent import namespace
from coast.agent import proxy
from coast.agent import server
from coast.common import tls
from coast.tests import base


SERVER_ID = '11111111-1111-1111-1111-111111111111'
TOKEN = '1|' + 'A' * 48


class AgentTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config.config(role='controller', state_path=str(self.root),
                           runtime_path=str(self.root),
                           server_id=SERVER_ID, stack_id=SERVER_ID,
                           heartbeat_key='ab' * 32,
                           health_manager_ips=['192.0.2.20'], group='agent')

    def assert_payload_too_large(self, response):
        # http.client uses the reason phrase of this interpreter.
        # Python 3.14 says "Content Too Large" (RFC 9110).
        phrase = client.responses[413].encode('ascii')
        self.assertIn(b'413 ' + phrase, response)

    def fake_bootstrap(self, output, **kwargs):
        Path(output).write_text(TOKEN)
        Path(output).chmod(0o600)

    def engine(self):
        patcher = mock.patch.object(bootstrap, 'check_image')
        patcher.start()
        self.addCleanup(patcher.stop)
        return coolify.Engine(self.conf)

    def test_bootstrap_one_shot_survives_restart_and_wipes_file(self):
        real_open = os.open
        with mock.patch.object(bootstrap, 'bootstrap', self.fake_bootstrap), \
                mock.patch.object(bootstrap, 'docker'), \
                mock.patch.object(os, 'open', wraps=os.open) as open_file:
            backup = self.root / 'backup'
            backup.write_bytes(b'backup')
            open_file.side_effect = lambda path, *args, **kwargs: real_open(
                backup if str(path) == '/data/coolify/source/.env' else path,
                *args, **kwargs)
            result = self.engine().bootstrap({'engine_version': '4.4.7'})
        self.assertEqual(TOKEN, result['token'])
        self.assertNotIn('env_file_backup', result)
        backup.unlink()
        self.assertEqual(['.lock', 'bootstrap-done', 'bootstrap-started'],
                         sorted(p.name for p in self.root.iterdir()))
        self.assertRaises(coolify.Conflict, self.engine().bootstrap,
                          {'engine_version': '4.4.7'})
        for path in self.root.iterdir():
            self.assertNotIn(TOKEN.encode(), path.read_bytes())

    def test_startup_wipes_abandoned_token_without_resetting_markers(self):
        abandoned = self.root / 'token-abandoned'
        abandoned.mkdir(mode=0o700)
        (abandoned / 'token').write_text(TOKEN)
        (self.root / 'bootstrap-started').touch(mode=0o600)
        self.engine()
        self.assertFalse(abandoned.exists())
        self.assertTrue((self.root / 'bootstrap-started').exists())
        self.assertRaises(coolify.Conflict, self.engine().bootstrap,
                          {'engine_version': '4.4.7'})

    def test_failed_bootstrap_is_sanitized_and_cannot_repeat(self):
        with mock.patch.object(bootstrap, 'bootstrap',
                               side_effect=RuntimeError(TOKEN)), \
                mock.patch.object(bootstrap, 'docker'):
            failure = self.assertRaises(coolify.PreconditionFailed,
                                        self.engine().bootstrap,
                                        {'engine_version': '4.4.7'})
        self.assertNotIn(TOKEN, str(failure))
        self.assertRaises(coolify.Conflict, self.engine().bootstrap,
                          {'engine_version': '4.4.7'})
        self.assertFalse((self.root / 'bootstrap-done').exists())

    def test_wrong_pin_or_role_does_not_claim_bootstrap(self):
        self.assertRaises(coolify.PreconditionFailed, self.engine().bootstrap,
                          {'engine_version': 'latest'})
        self.config.config(role='worker', group='agent')
        self.assertRaises(coolify.PreconditionFailed, self.engine().bootstrap,
                          {'engine_version': '4.4.7'})
        self.assertFalse((self.root / 'bootstrap-started').exists())

    def rotation_body(self, phase='create'):
        return {'engine_version': bootstrap.VERSION, 'rotation_id': SERVER_ID,
                'previous_name': 'coast-agent-bootstrap-v1', 'phase': phase}

    def test_rotation_requires_successful_bootstrap(self):
        self.assertRaises(coolify.PreconditionFailed, self.engine().bootstrap,
                          self.rotation_body(), rotate=True)

    def test_rotation_create_replays_then_revokes_by_name(self):
        (self.root / 'bootstrap-done').touch(mode=0o600)
        with mock.patch.object(bootstrap, 'bootstrap', self.fake_bootstrap), \
                mock.patch.object(coolify.Engine, '_revoke') as revoke:
            result = self.engine().bootstrap(self.rotation_body(), rotate=True)
            self.assertEqual(TOKEN, result['token'])
            again = self.engine().bootstrap(self.rotation_body(), rotate=True)
            self.assertEqual(result, again)
            revoke.assert_not_called()
            other = dict(self.rotation_body(),
                         rotation_id='22222222-2222-2222-2222-222222222222')
            self.assertRaises(coolify.Conflict, self.engine().bootstrap,
                              other, rotate=True)
            self.engine().bootstrap(self.rotation_body('revoke'), rotate=True)
            self.engine().bootstrap(self.rotation_body('revoke'), rotate=True)
            revoke.assert_called_once_with('coast-agent-bootstrap-v1')
            self.assertFalse((self.root / 'rotation-pending').exists())
            self.assertFalse((self.root / ('rotation-' + SERVER_ID) /
                              'token').exists())
            self.assertRaises(coolify.Conflict, self.engine().bootstrap,
                              self.rotation_body(), rotate=True)

    def test_failed_rotation_can_retry_same_identifier(self):
        (self.root / 'bootstrap-done').touch(mode=0o600)
        with mock.patch.object(bootstrap, 'bootstrap',
                               side_effect=RuntimeError('private')):
            self.assertRaises(coolify.PreconditionFailed,
                              self.engine().bootstrap,
                              self.rotation_body(), rotate=True)
        with mock.patch.object(bootstrap, 'bootstrap', self.fake_bootstrap):
            result = self.engine().bootstrap(self.rotation_body(), rotate=True)
        self.assertEqual(TOKEN, result['token'])

    def test_rotation_creates_only_new_named_token(self):
        name = 'coast-agent-rotation-' + SERVER_ID

        def rotate(output, token_payload=None, container_token=None):
            self.assertIn("$name = '" + name + "';", token_payload)
            self.assertNotIn('coast-agent-bootstrap-v1', token_payload)
            self.assertIn(bootstrap.VERSION, token_payload)
            self.assertIn(name, container_token)
            self.fake_bootstrap(output)
        with mock.patch.object(bootstrap, 'bootstrap', rotate):
            self.engine()._rotate(self.root / 'token', name)

    def test_revoke_is_version_gated_and_scoped_to_previous_name(self):
        with mock.patch.object(bootstrap, 'docker') as docker:
            self.engine()._revoke('coast-agent-bootstrap-v1')
        payload = docker.call_args.kwargs['payload'].decode()
        self.assertIn(bootstrap.VERSION, payload)
        self.assertIn("where('name', 'coast-agent-bootstrap-v1')->delete()",
                      payload)
        self.assertNotIn('createToken', payload)

    def test_verified_bootstrap_rejects_wrong_image(self):
        inspect = SimpleNamespace(stdout=b'coollabsio/coolify:latest',
                                  returncode=0)
        with mock.patch.object(bootstrap.subprocess, 'run',
                               return_value=inspect):
            self.assertRaises(bootstrap.BootstrapError, bootstrap.bootstrap,
                              self.root / 'token')
        self.assertFalse((self.root / 'token').exists())

    def test_verified_payload_and_api_checks(self):
        inspect = SimpleNamespace(stdout=b'coollabsio/coolify:4.4.7',
                                  returncode=0)
        observed = []

        def docker(*args, payload=None):
            if args[0] == 'exec' and payload:
                observed.append(payload)
            if args[0] == 'cp':
                self.fake_bootstrap(args[-1])
        with mock.patch.object(bootstrap.subprocess, 'run',
                               return_value=inspect), \
                mock.patch.object(bootstrap, 'docker', docker), \
                mock.patch.object(bootstrap, 'enable_api'):
            bootstrap.bootstrap(self.root / 'token')
        self.assertIn(rb'App\Models\User', observed[0])
        self.assertNotIn(rb'Service\Models', observed[0])
        self.assertEqual([bootstrap.PHP.encode()], observed)
        self.assertEqual(TOKEN, bootstrap.read_token(self.root / 'token'))

    def test_bootstrap_php_failure_stops_before_token_copy(self):
        inspect = SimpleNamespace(stdout=b'coollabsio/coolify:4.4.7',
                                  returncode=0)
        failure = SimpleNamespace(returncode=1)
        with mock.patch.object(bootstrap.subprocess, 'run',
                               side_effect=[inspect, failure]) as run:
            self.assertRaises(bootstrap.BootstrapError, bootstrap.bootstrap,
                              self.root / 'token')
        self.assertEqual(2, run.call_count)
        self.assertIn('php', run.call_args.args[0])
        self.assertFalse((self.root / 'token').exists())

    def test_bootstrap_creates_missing_root_without_dns(self):
        payload = bootstrap.PHP
        self.assertIn(r'if (!App\Models\User::find(0)) {', payload)
        for name in ('ROOT_USERNAME', 'ROOT_USER_EMAIL', 'ROOT_USER_PASSWORD'):
            self.assertIn("env('%s')" % name, payload)
        self.assertIn('if (!$username || !$email || !$password) { exit(1); }',
                      payload)
        self.assertIn("'id' => 0, 'name' => $username, 'email' => $email",
                      payload)
        self.assertIn(r'Illuminate\Support\Facades\Hash::make($password)',
                      payload)
        self.assertIn('$user->save();', payload)
        self.assertIn(r'$team = App\Models\Team::find(0);', payload)
        self.assertIn("!$user->teams()->where('team_id', 0)->exists()",
                      payload)
        self.assertIn("$user->teams()->attach($team, ['role' => 'owner']);",
                      payload)
        self.assertIn(r'App\Models\InstanceSettings::updateOrCreate(', payload)
        self.assertIn("['id' => 0], ['is_registration_enabled' => false]",
                      payload)
        self.assertNotIn('Validator::', payload)

    def test_bootstrap_catches_failures_without_output(self):
        self.assertTrue(bootstrap.PHP.startswith('<?php\ntry {\n'))
        self.assertTrue(bootstrap.PHP.endswith(
            r'} catch (\Throwable $e) { exit(1); }' + '\n'))
        self.assertNotIn('echo ', bootstrap.PHP)
        self.assertNotIn('getMessage', bootstrap.PHP)

    def test_proxy_allow_list(self):
        accepted = [('GET', '/api/v1/health'),
                    ('PATCH', '/api/v1/team'),
                    ('POST', '/api/v1/applications/private-deploy-key'),
                    ('POST', '/api/v1/applications/public'),
                    ('POST', '/api/v1/applications/dockerimage'),
                    ('POST', '/api/v1/applications/dockerfile'),
                    ('POST', '/api/v1/services'),
                    ('PATCH', '/api/v1/applications/service-id'),
                    ('GET', '/api/v1/applications/service-id/logs?lines=200'),
                    ('DELETE', '/api/v1/applications/service-id/storages/1'),
                    ('POST', '/api/v1/deploy?uuid=service-id'),
                    ('POST', '/api/v1/deployments/deployment-id/cancel')]
        for method, target in accepted:
            self.assertTrue(proxy.allowed(method, target), target)
        rejected = ['/api/v1/enable', '/api/v1/settings/email',
                    '/api/v1/security/integration-tokens', '/',
                    '/api/v1/../health', '/api/v1/%2e%2e/health',
                    '/api/v1/applications/service-id/../../../enable',
                    '/api/v1/health#fragment', '//example.com/api/v1/health',
                    '/api/v1/health\r\nX: secret',
                    '/api/v1/applications/service-id/unknown']
        for target in rejected:
            self.assertFalse(proxy.allowed('GET', target), target)
        self.assertFalse(proxy.allowed('POST', '/api/v1/health'))
        self.assertFalse(proxy.allowed(
            'PUT', '/api/v1/applications/service-id'))

    def test_proxy_application_env_existing_routes(self):
        path = '/api/v1/applications/app-uuid/envs/bulk'
        for method in ('GET', 'POST', 'PATCH'):
            self.assertTrue(proxy.allowed(method, path.removesuffix('/bulk')))
        for method in ('GET', 'POST', 'PATCH', 'PUT'):
            self.assertFalse(proxy.allowed(method, path))

    def test_proxy_template_service_allow_list(self):
        item = '/api/v1/services/service-uuid'
        accepted = [('GET', '/api/v1/services'),
                    ('POST', '/api/v1/services'),
                    ('GET', item), ('PATCH', item),
                    ('DELETE', item + '?delete_volumes=false'),
                    ('GET', item + '/envs'),
                    ('GET', item + '/storages'),
                    ('POST', item + '/storages'),
                    ('DELETE', item + '/storages/storage-id'),
                    ('GET', item + '/logs'),
                    ('GET', item + '/logs?sub_service_name=web&lines=200'),
                    ('PATCH', item + '/envs/bulk'),
                    ('DELETE', item + '/envs/env-uuid'),
                    ('POST', item + '/start'), ('POST', item + '/stop'),
                    ('POST', item + '/restart')]
        for method, target in accepted:
            with self.subTest(method=method, target=target):
                self.assertTrue(proxy.allowed(method, target))
        rejected = [('DELETE', '/api/v1/services'),
                    ('PUT', item), ('POST', item),
                    ('POST', item + '/envs'), ('PATCH', item + '/envs'),
                    ('GET', item + '/start'), ('POST', item + '/clone'),
                    ('POST', item + '/move'),
                    ('POST', item + '/logs'), ('PATCH', item + '/logs'),
                    ('DELETE', item + '/logs'), ('PUT', item + '/logs'),
                    ('GET', item + '/logs/other'),
                    ('GET', item + '/logs/../envs'),
                    ('PATCH', item + '/storages'),
                    ('PUT', item + '/storages'),
                    ('DELETE', item + '/storages'),
                    ('GET', item + '/applications'),
                    ('DELETE', item + '/envs'),
                    ('GET', item + '/../applications'),
                    ('GET', '/api/v1/services/' + 'a' * 129),
                    ('GET', '/api/v1/services//envs')]
        for method, target in rejected:
            with self.subTest(method=method, target=target):
                self.assertFalse(proxy.allowed(method, target))

    def test_proxy_private_key_patch_allow_list(self):
        item = '/api/v1/security/keys/key-uuid'
        self.assertTrue(proxy.allowed('PATCH', item))
        for target in ('/api/v1/security/keys', item + '/', item + '/extra',
                       '/api/v1/security/keys/..', item + '%2fextra',
                       item.replace('key-uuid', '%6bey-uuid')):
            self.assertFalse(proxy.allowed('PATCH', target))
        self.assertFalse(proxy.allowed('PUT', item))

    def test_proxy_server_patch_allow_list(self):
        item = '/api/v1/servers/server-uuid'
        for identifier in ('server-uuid', 'abc123', 'server_123', 'a' * 128):
            self.assertTrue(proxy.allowed(
                'PATCH', '/api/v1/servers/' + identifier))
        for method in ('POST', 'PUT', 'HEAD', 'OPTIONS', 'patch'):
            with self.subTest(method=method):
                self.assertFalse(proxy.allowed(method, item))
        rejected = [
            '/api/v1/servers', '/api/v1/servers/',
            '/api/v1/projects/server-uuid',
            '/api/v1/servers/' + 'a' * 129,
            item + '/', item + '/extra', item + '/settings',
            item + '/resources', item + '/validate', item + '/proxy/restart',
            item + '/logs', item + '/../server-2',
            '/api/v1/servers/..', '/api/v1/servers/./server-uuid',
            '/api/v1/servers//server-uuid', item + '%2fextra',
            item.replace('server-uuid', '%73erver-uuid'),
            item + r'\extra', item + '#fragment', item + '\r\nX: value',
            item + '\x00',
        ]
        for target in rejected:
            with self.subTest(target=target):
                self.assertFalse(proxy.allowed('PATCH', target))
        # Preserve previously approved routes, without adding methods to them.
        for method in ('GET', 'DELETE'):
            self.assertTrue(proxy.allowed(method, item))
        self.assertTrue(proxy.allowed('PATCH', item + '/proxy'))
        self.assertFalse(proxy.allowed('PUT', item + '/proxy'))

    def test_proxy_environment_delete_allow_list(self):
        item = '/api/v1/projects/project-1/environments/environment-1'
        for environment in ('environment-1', 'production', 'env_123'):
            target = '/api/v1/projects/project-1/environments/' + environment
            self.assertTrue(proxy.allowed('DELETE', target), target)
        for method in ('GET', 'PATCH', 'POST', 'PUT', 'HEAD', 'OPTIONS'):
            with self.subTest(method=method):
                self.assertFalse(proxy.allowed(method, item))
        rejected = [
            '/api/v1/projects/project-1/environments',
            '/api/v1/projects/project-1/environments/',
            item + '/extra', item + '/envs/1', item + '/',
            item + '/../environment-2',
            '/api/v1/projects/../environments/environment-1',
            '/api/v1/projects/project-1/environments/..',
            '/api/v1/projects/project-1/environments/./environment-1',
            '/api/v1/projects/project-1/environments//environment-1',
            item.replace('project-1', 'project%2d1'),
            item.replace('environment-1', '%65nvironment-1'),
            item + '%2fextra', item + '%252fextra',
            item + '/%2e%2e', item + r'\extra',
            item + '#fragment', item + '\r\nX: value', item + '\x00',
        ]
        for target in rejected:
            with self.subTest(target=target):
                self.assertFalse(proxy.allowed('DELETE', target))

    def test_proxy_server_registry_allow_list(self):
        item = '/api/v1/servers/server-uuid/registries'
        for identifier in ('server-uuid', 'abc123', 'server_123', 'a' * 128):
            target = '/api/v1/servers/' + identifier + '/registries'
            for method in ('GET', 'POST'):
                self.assertTrue(proxy.allowed(method, target), target)
        for method in ('PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'get'):
            with self.subTest(method=method):
                self.assertFalse(proxy.allowed(method, item))
        rejected = [
            '/api/v1/servers/server-uuid/registries/',
            '/api/v1/servers/server-uuid/registries/name',
            '/api/v1/servers/server-uuid/registries/name/check',
            item + '/extra', item + '/../name',
            '/api/v1/servers/' + 'a' * 129 + '/registries',
            '/api/v1/projects/server-uuid/registries',
            '/api/v1/servers/./server-uuid/registries',
            '/api/v1/servers//server-uuid/registries',
            item.replace('server-uuid', '%73erver-uuid'),
            item + '%2fextra', item + r'\extra', item + '#fragment',
            item + '\r\nX: value', item + '\x00',
        ]
        for method in ('GET', 'POST'):
            for target in rejected:
                with self.subTest(method=method, target=target):
                    self.assertFalse(proxy.allowed(method, target))
        # The server read route stays approved. Registry login does not add
        # a method to it.
        server = '/api/v1/servers/server-uuid'
        self.assertTrue(proxy.allowed('GET', server))
        self.assertFalse(proxy.allowed('POST', server))

    def test_server_inventory_proxy_is_read_only_and_bounded(self):
        target = '/api/v1/servers/server-uuid/resources'
        self.assertTrue(proxy.allowed('GET', target))
        for method in ('POST', 'PATCH', 'DELETE', 'PUT', 'HEAD'):
            self.assertFalse(proxy.allowed(method, target))
        for path in (target + '/extra', target + '/',
                     '/api/v1/servers//resources',
                     '/api/v1/servers/' + 'a' * 129 + '/resources',
                     '/api/v1/servers/../resources',
                     target.replace('server-uuid', '%73erver-uuid')):
            self.assertFalse(proxy.allowed('GET', path))
        upstream = mock.Mock()
        upstream.getresponse.return_value.status = 200
        upstream.getresponse.return_value.length = 0
        upstream.getresponse.return_value.read.return_value = b'[]'
        upstream.getresponse.return_value.getheaders.return_value = []
        with mock.patch.object(client, 'HTTPConnection',
                               return_value=upstream) as connection:
            self.assertEqual((200, {}, b'[]'), proxy.forward(
                self.conf, 'GET', target,
                {'Authorization': 'Bearer ' + TOKEN}, b''))
            connection.assert_called_once_with(
                '127.0.0.1', 8000, timeout=self.conf.agent.request_timeout)
        upstream.request.assert_called_once_with(
            'GET', target, body=b'',
            headers={'Authorization': 'Bearer ' + TOKEN})

    def test_proxy_strips_headers_and_uses_only_loopback(self):
        upstream = mock.Mock()
        upstream.getresponse.return_value.status = 200
        upstream.getresponse.return_value.length = 0
        upstream.getresponse.return_value.read.return_value = b'{}'
        upstream.getresponse.return_value.getheaders.return_value = [
            ('Content-Type', 'application/json'), ('Set-Cookie', TOKEN),
            ('Server', 'engine'), ('X-Internal', 'private')]
        with mock.patch.object(client, 'HTTPConnection',
                               return_value=upstream):
            status, headers, body = proxy.forward(
                self.conf, 'GET', '/api/v1/health',
                {'Authorization': 'Bearer ' + TOKEN, 'Cookie': 'private',
                 'Host': 'example.com', 'X-Forwarded-For': '192.0.2.1',
                 'Connection': 'keep-alive'}, b'')
        self.assertEqual(200, status)
        self.assertEqual({'Content-Type': 'application/json'}, headers)
        self.assertEqual(b'{}', body)
        forwarded = upstream.request.call_args.kwargs['headers']
        self.assertEqual({'Authorization': 'Bearer ' + TOKEN}, forwarded)

    def test_proxy_discards_redirects_errors_and_oversized_responses(self):
        upstream = mock.Mock()
        upstream.getresponse.return_value.length = 0
        self.config.config(max_body_size=5, group='agent')
        with mock.patch.object(client, 'HTTPConnection',
                               return_value=upstream):
            for code in (302, 403, 500):
                upstream.getresponse.return_value.status = code
                upstream.getresponse.return_value.read.return_value = TOKEN
                status, headers, body = proxy.forward(
                    self.conf, 'GET', '/api/v1/health', {}, b'')
                self.assertEqual(502 if code == 302 else code, status)
                self.assertEqual({}, headers)
                self.assertEqual(b'', body)
            # A throttle keeps only a whole-second Retry-After.
            upstream.getresponse.return_value.status = 429
            for value, kept in (('42', {'Retry-After': '42'}),
                                (' 7 ', {'Retry-After': '7'}),
                                (None, {}), ('', {}), ('-1', {}),
                                ('1.5', {}), ('12345', {}),
                                ('Wed, 21 Oct 2026 07:28:00 GMT', {}),
                                ('5\r\nSet-Cookie: x', {})):
                upstream.getresponse.return_value.getheader.return_value = (
                    value)
                self.assertEqual((429, kept, b''), proxy.forward(
                    self.conf, 'GET', '/api/v1/servers', {}, b''))
            upstream.getresponse.return_value.status = 200
            upstream.getresponse.return_value.read.return_value = b'123456'
            self.assertRaises(OSError, proxy.forward, self.conf, 'GET',
                              '/api/v1/health', {}, b'')

    def test_reply_sends_only_content_type_and_retry_after(self):
        handler = object.__new__(server.Handler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()
        handler.reply(429, {'Retry-After': '42', 'Set-Cookie': TOKEN,
                            'Content-Type': 'application/json'}, b'')
        sent = {call.args[0]: call.args[1]
                for call in handler.send_header.call_args_list}
        self.assertEqual('42', sent['Retry-After'])
        self.assertEqual('application/json', sent['Content-Type'])
        self.assertNotIn('Set-Cookie', sent)

    def test_proxy_deadline_and_truncated_response_fail_closed(self):
        upstream = mock.Mock()
        upstream.getresponse.return_value.status = 200
        upstream.getresponse.return_value.read.return_value = b'{'
        upstream.getresponse.return_value.length = 1
        with mock.patch.object(client, 'HTTPConnection',
                               return_value=upstream):
            self.assertRaises(OSError, proxy.forward, self.conf, 'GET',
                              '/api/v1/health', {}, b'')
        upstream.getresponse.return_value.length = None

        class ImmediateTimer:
            def __init__(self, interval, callback):
                self.callback = callback

            def start(self):
                self.callback()

            def cancel(self):
                pass
        with mock.patch.object(client, 'HTTPConnection',
                               return_value=upstream), \
                mock.patch.object(proxy.threading, 'Timer', ImmediateTimer):
            self.assertRaises(OSError, proxy.forward, self.conf, 'GET',
                              '/api/v1/health', {}, b'')

    def test_connection_limit_includes_failed_tls_handshake(self):
        listener = server.Server.__new__(server.Server)
        listener.conf = self.conf
        listener.slots = threading.BoundedSemaphore(1)
        listener.context = mock.Mock()
        listener.context.wrap_socket.side_effect = ssl.SSLError('rejected')
        request = mock.Mock()
        listener.slots.acquire()
        listener.process_request(request, ('192.0.2.1', 1))
        self.assertFalse(listener.slots.acquire(blocking=False))
        # The rejected request's socket is shut down without another thread.
        self.assertTrue(request.close.called)
        listener.process_request_thread(mock.Mock(), ('192.0.2.1', 1))
        self.assertTrue(listener.slots.acquire(blocking=False))

    def request(self, method, path, content=b'', headers=b'', engine=None,
                archives=None, egress_result=None):
        output = io.BytesIO()

        class Connection:
            def settimeout(self, timeout):
                self.timeout = timeout

            def shutdown(self, how):
                raise AssertionError('Unexpected request deadline')

            def makefile(self, mode, buffering=None):
                if mode == 'rb':
                    return io.BytesIO(
                        method.encode() + b' ' + path.encode() +
                        b' HTTP/1.0\r\nContent-Length: ' +
                        str(len(content)).encode() + b'\r\n' + headers +
                        b'\r\n' + content)
                return output

            def sendall(self, data):
                output.write(data)
        if engine is None:
            engine = mock.Mock()
            engine.bootstrap.return_value = {'token': TOKEN}
            engine.quiesce.return_value = {
                'status': 'quiesced', 'deadline': 1000}
            engine.resume.return_value = {'status': 'resumed'}
            engine.archive.return_value = {
                'status': 'uploaded', 'size': 100, 'sha256': 'a' * 64}
        http_server = SimpleNamespace(conf=self.conf, engine=engine,
                                      exports=engine,
                                      archives=archives or engine,
                                      egress=SimpleNamespace(
                                          result=egress_result or
                                          {'status': 'ok'}))
        server.Handler(Connection(), ('192.0.2.1', 1), http_server)
        return output.getvalue(), engine

    def test_health_and_request_framing(self):
        response, engine = self.request('GET', '/health')
        self.assertIn(b'200 OK', response)
        payload = json.loads(response.split(b'\r\n\r\n', 1)[1])
        self.assertEqual({'status': 'ok', 'egress': {'status': 'ok'}}, payload)
        self.assertIn(b'Cache-Control: no-store', response)
        for headers in (b'Transfer-Encoding: chunked\r\n',
                        b'Content-Length: 1\r\n',
                        b'Authorization: a\r\nAuthorization: b\r\n'):
            response, engine = self.request('GET', '/health', headers=headers)
            self.assertIn(b'400 Bad Request', response)
        self.config.config(max_body_size=1, group='agent')
        response, engine = self.request('GET', '/health', content=b'12')
        self.assert_payload_too_large(response)

    def test_health_exposes_cached_pending_and_failed_egress(self):
        for result in ({'status': 'pending'},
                       {'status': 'error', 'reason': 'dns'}):
            response, engine = self.request('GET', '/health',
                                            egress_result=result)
            payload = json.loads(response.split(b'\r\n\r\n', 1)[1])
            self.assertEqual({'status': 'ok', 'egress': result}, payload)
            self.assertIn(b'200 OK', response)

    def test_http_bootstrap_paths_and_errors(self):
        for path in ('/v1/bootstrap', '/v1/bootstrap/rotate-token'):
            response, engine = self.request('POST', path, b'{}')
            self.assertIn(b'200 OK', response)
            self.assertIn(TOKEN.encode(), response)
            self.assertEqual(path.endswith('rotate-token'),
                             engine.bootstrap.call_args.kwargs['rotate'])
        response, engine = self.request('POST', '/v1/bootstrap', b'invalid')
        self.assertIn(b'400 Bad Request', response)
        response, engine = self.request('GET', '/v1/bootstrap')
        self.assertIn(b'405 Method Not Allowed', response)
        response, engine = self.request('POST', '/v1/rotate-token', b'{}')
        self.assertIn(b'404 Not Found', response)
        for failure, code in ((coolify.Conflict(), b'409 Conflict'),
                              (coolify.PreconditionFailed(),
                               b'412 Precondition Failed')):
            with mock.patch.object(server.json, 'loads', side_effect=failure):
                response, engine = self.request('POST', '/v1/bootstrap', b'{}')
            self.assertIn(code, response)
            self.assertNotIn(TOKEN.encode(), response)

    def test_export_routes_allow_owners_and_stay_post_only(self):
        for role in ('controller', 'worker', 'build'):
            self.config.config(role=role, group='agent')
            for path in ('/v1/export/quiesce', '/v1/export/resume'):
                response, engine = self.request('POST', path, b'{}')
                self.assertIn(b'200 OK', response)
                called = (engine.quiesce if path.endswith('quiesce') else
                          engine.resume)
                self.assertTrue(called.called)
                response, engine = self.request('GET', path)
                self.assertIn(b'405 Method Not Allowed', response)
                self.assertFalse(engine.quiesce.called)
                self.assertFalse(engine.resume.called)

    def archive_body(self):
        return {'export_id': SERVER_ID, 'volume_id': SERVER_ID,
                'device': '/dev/vdb', 'filesystem': 'ext4', 'purpose': 'data',
                'put_url': 'https://storage.example.com/object?sig=SECRET',
                'manifest': {'services': []}}

    def test_archive_http_route_uses_real_helper_and_durable_receipt(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'database').write_bytes(b'persistent data')
        uploads = []

        @contextlib.contextmanager
        def mount(*args):
            yield source

        def put(url, stream, size, deadline):
            stream.seek(0)
            uploads.append(stream.read())

        archives = export_archive.Archive(self.conf, mount, put)
        body = self.archive_body()
        receipt = None
        for role in ('controller', 'worker', 'build'):
            self.config.config(role=role, group='agent')
            body['put_url'] = body['put_url'].replace('SECRET', 'RENEWED')
            response, engine = self.request(
                'POST', '/v1/export', json.dumps(body).encode(),
                archives=archives)
            self.assertIn(b'200 OK', response)
            self.assertIn(b'Cache-Control: no-store', response)
            result = json.loads(response.split(b'\r\n\r\n', 1)[1])
            self.assertEqual('uploaded', result['status'])
            self.assertEqual(len(uploads[0]), result['size'])
            self.assertEqual(hashlib.sha256(uploads[0]).hexdigest(),
                             result['sha256'])
            self.assertEqual(result if receipt is None else receipt, result)
            receipt = result
            self.assertNotIn(b'RENEWED', response)
        self.assertEqual(1, len(uploads))
        changed = dict(body, purpose='docker')
        response, engine = self.request(
            'POST', '/v1/export', json.dumps(changed).encode(),
            archives=archives)
        self.assertIn(b'409 Conflict', response)
        self.assertNotIn(b'RENEWED', response)

    def test_archive_http_route_rejects_methods_and_non_owners(self):
        for role in ('controller', 'worker', 'build'):
            self.config.config(role=role, group='agent')
            for method in ('GET', 'PUT', 'PATCH', 'DELETE'):
                response, engine = self.request(method, '/v1/export')
                self.assertIn(b'405 Method Not Allowed', response)
                self.assertFalse(engine.archive.called)
        with mock.patch.object(server.export, 'OWNER_ROLES', ()):
            response, engine = self.request('POST', '/v1/export', b'{}')
        self.assertIn(b'404 Not Found', response)
        self.assertFalse(engine.archive.called)

    def test_archive_http_route_validates_json_schema_and_body_limit(self):
        archives = export_archive.Archive(self.conf)
        valid = self.archive_body()
        for body in (b'invalid', b'[]', b'{}',
                     json.dumps(dict(valid, export_id='../SECRET')).encode(),
                     json.dumps(dict(valid, extra='SECRET')).encode()):
            response, engine = self.request('POST', '/v1/export', body,
                                            archives=archives)
            self.assertIn(b'400 Bad Request', response)
            self.assertNotIn(b'SECRET', response)
        self.config.config(max_body_size=1, group='agent')
        response, engine = self.request('POST', '/v1/export', b'{}',
                                        archives=archives)
        self.assert_payload_too_large(response)

    def test_archive_http_errors_are_sanitized_and_export_specific(self):
        for failure, code in (
                (coolify.PreconditionFailed('SECRET'),
                 b'412 Precondition Failed'),
                (coolify.Conflict('SECRET'), b'409 Conflict'),
                (ValueError('SECRET'), b'400 Bad Request'),
                (OSError('SECRET'), b'502 Bad Gateway'),
                (client.HTTPException('SECRET'), b'502 Bad Gateway')):
            archives = mock.Mock()
            archives.archive.side_effect = failure
            response, engine = self.request('POST', '/v1/export', b'{}',
                                            archives=archives)
            self.assertIn(code, response)
            self.assertNotIn(b'SECRET', response)
            self.assertNotIn(b'Bootstrap', response)
            if isinstance(failure, coolify.PreconditionFailed):
                self.assertIn(b'Export archive operation failed', response)
                self.assertNotIn(b'pending stop records', response)

    def test_archive_timeout_replaces_normal_request_deadline(self):
        timers = []
        observed = []

        class Timer:
            def __init__(self, interval, callback):
                self.interval = interval
                self.callback = callback
                self.active = False
                timers.append(self)

            def start(self):
                self.active = True

            def cancel(self):
                self.active = False

        def archive(body):
            elapsed = self.conf.agent.request_timeout + 1
            for timer in timers:
                if timer.active and elapsed >= timer.interval:
                    timer.callback()
            observed.extend(
                (timer.interval, timer.callback.__self__.connection.timeout)
                for timer in timers if timer.active)
            return {'status': 'uploaded', 'size': 100, 'sha256': 'a' * 64}

        archives = SimpleNamespace(archive=archive)
        with mock.patch.object(server.threading, 'Timer', Timer):
            response, engine = self.request('POST', '/v1/export', b'{}',
                                            archives=archives)
        self.assertIn(b'200 OK', response)
        timeout = self.conf.export.archive_timeout
        self.assertEqual([(timeout, timeout)], observed)
        self.assertFalse(any(timer.active for timer in timers))

    def test_worker_bootstrap_and_proxy_stay_refused(self):
        self.config.config(role='worker', group='agent')
        response, engine = self.request('GET', '/api/v1/health')
        self.assertIn(b'404 Not Found', response)
        self.assertFalse(engine.bootstrap.called)
        response, engine = self.request(
            'POST', '/v1/bootstrap', b'{}', engine=self.engine())
        self.assertIn(b'412 Precondition Failed', response)
        self.assertNotIn(TOKEN.encode(), response)
        self.assertFalse((self.root / 'bootstrap-started').exists())

    def test_export_failure_has_export_specific_sanitized_error(self):
        with mock.patch.object(server.json, 'loads',
                               side_effect=coolify.PreconditionFailed()):
            response, engine = self.request(
                'POST', '/v1/export/quiesce', b'{}')
        self.assertIn(b'412 Precondition Failed', response)
        self.assertIn(b'Export consistency operation failed', response)
        self.assertNotIn(b'Bootstrap', response)
        self.assertNotIn(TOKEN.encode(), response)

    def test_worker_has_no_proxy(self):
        self.config.config(role='worker', group='agent')
        response, engine = self.request('GET', '/api/v1/health')
        self.assertIn(b'404 Not Found', response)

    def test_registry_gc_is_controller_post_only(self):
        body = b'{"repositories":[]}'
        for role in ('worker', 'build'):
            self.config.config(role=role, group='agent')
            response, engine = self.request('POST', '/v1/registry/gc', body)
            self.assertIn(b'404 Not Found', response)
        self.config.config(role='controller', group='agent')
        response, engine = self.request('GET', '/v1/registry/gc')
        self.assertIn(b'405 Method Not Allowed', response)
        with mock.patch.object(server.registry_gc, 'garbage_collect',
                               return_value={'collected': 0}) as collect:
            response, engine = self.request('POST', '/v1/registry/gc', body)
        self.assertIn(b'200 OK', response)
        self.assertEqual({'repositories': []}, collect.call_args.args[0])
        payload = json.loads(response.split(b'\r\n\r\n', 1)[1])
        self.assertEqual({'collected': 0}, payload)
        response, engine = self.request('POST', '/v1/registry/gc', b'[]')
        self.assertIn(b'400 Bad Request', response)
        with mock.patch.object(server.registry_gc, 'garbage_collect',
                               side_effect=OSError('secret-token')):
            response, engine = self.request('POST', '/v1/registry/gc', body)
        self.assertIn(b'502 Bad Gateway', response)
        self.assertNotIn(b'secret-token', response)

    def test_hmac_authenticates_exact_json_and_detects_tampering(self):
        key = bytes.fromhex(self.conf.agent.heartbeat_key)
        payload = {'server_id': SERVER_ID, 'env_id': SERVER_ID,
                   'seq': 1, 'ts': 1.0, 'docker_ok': True,
                   'disk_pct': 1.0, 'engine_ok': True}
        packet = heartbeat.encode(payload, key)
        self.assertEqual(payload, json.loads(packet[:-32]))
        expected = hmac.new(key, packet[:-32], hashlib.sha256).digest()
        self.assertTrue(hmac.compare_digest(packet[-32:], expected))
        tampered = packet[:-33] + b' ' + packet[-32:]
        self.assertFalse(hmac.compare_digest(
            tampered[-32:], hmac.new(key, tampered[:-32],
                                     hashlib.sha256).digest()))
        self.assertFalse(hmac.compare_digest(
            packet[-32:], hmac.new(b'wrong key', packet[:-32],
                                   hashlib.sha256).digest()))
        self.assertLess(len(packet), 1024)

    def test_heartbeat_fields_sequence_and_multiple_targets(self):
        self.config.config(health_manager_ips=['192.0.2.20', '192.0.2.21'],
                           group='agent')
        sock = mock.Mock()
        sock.sendto.side_effect = [OSError(), None, None, None]
        with mock.patch.object(namespace, 'management_socket',
                               return_value=sock), \
                mock.patch.object(heartbeat, 'docker_ok', return_value=True), \
                mock.patch.object(heartbeat, 'engine_ok', return_value=True), \
                mock.patch.object(heartbeat.shutil, 'disk_usage',
                                  return_value=SimpleNamespace(
                                      used=1, total=4)):
            sender = heartbeat.Sender(self.conf)
            namespace.management_socket.assert_called_once_with(
                self.conf, socket.SOCK_DGRAM, 0)
            sender.send()
            sender.send()
        first = json.loads(sock.sendto.call_args_list[0].args[0][:-32])
        second = json.loads(sock.sendto.call_args_list[2].args[0][:-32])
        self.assertEqual(SERVER_ID, first['server_id'])
        self.assertEqual(SERVER_ID, first['env_id'])
        self.assertTrue(first['docker_ok'])
        self.assertTrue(first['engine_ok'])
        self.assertEqual(25.0, first['disk_pct'])
        self.assertGreater(second['seq'], first['seq'])
        self.assertEqual(('192.0.2.21', 5565),
                         sock.sendto.call_args_list[-1].args[1])

    def test_heartbeat_restart_with_clock_rollback_keeps_sequence(self):
        checkpoint = self.root / 'heartbeat-seq'
        checkpoint.write_text('100000')
        with mock.patch.object(namespace, 'management_socket',
                               return_value=mock.Mock()), \
                mock.patch.object(heartbeat.time, 'time_ns', return_value=1):
            sender = heartbeat.Sender(self.conf)
        self.assertEqual(100000, sender.sequence)

    def test_bad_heartbeat_key_fails_before_socket_creation(self):
        self.config.config(heartbeat_key='short', group='agent')
        self.assertRaises(ValueError, heartbeat.Sender, self.conf)

    def test_guest_socket_failure_does_not_enter_namespace(self):
        with mock.patch.object(namespace.ctypes, 'CDLL') as libc, \
                mock.patch.object(socket, 'socket', side_effect=OSError()):
            self.assertRaises(OSError, namespace.management_socket,
                              self.conf, socket.SOCK_STREAM, 9443)
        libc.assert_not_called()

    def test_management_namespace_option_is_control_side_only(self):
        self.assertNotIn('mgmt_netns', self.conf.agent)
        self.assertIn('mgmt_netns', self.conf.controller_worker)
        self.assertEqual('coast-mgmt', self.conf.controller_worker.mgmt_netns)

    def test_guest_socket_binds_management_address_without_setns(self):
        with mock.patch.object(namespace.ctypes, 'CDLL') as libc, \
                mock.patch.object(socket, 'socket') as create:
            sock = namespace.management_socket(
                self.conf, socket.SOCK_STREAM, 9443)
        self.assertIs(create.return_value, sock)
        sock.bind.assert_called_once_with((self.conf.agent.bind_ip, 9443))
        libc.assert_not_called()


class MutualTLSTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.make_ca('server')
        issued = tls.issue_server_certificate(SERVER_ID, '192.0.2.10',
                                              conf=self.conf)
        self.write_leaf('server', issued)
        self.make_ca('client')
        issued = tls.issue_agent_certificate(SERVER_ID, '192.0.2.10',
                                             conf=self.conf)
        self.write_leaf('client', issued)
        wrong_usage = tls.issue_server_certificate(SERVER_ID, '192.0.2.10',
                                                   conf=self.conf)
        self.write_leaf('wrong-usage', wrong_usage)
        self.make_ca('foreign')
        issued = tls.issue_agent_certificate(SERVER_ID, '192.0.2.10',
                                             conf=self.conf)
        self.write_leaf('foreign', issued)
        self.config.config(server_cert=str(self.root / 'server.pem'),
                           server_key=str(self.root / 'server.key'),
                           client_ca=str(self.root / 'client-ca.pem'),
                           group='agent')

    def make_ca(self, name):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(
            x509.NameOID.COMMON_NAME, name)])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject)
                .issuer_name(subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=30))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                               critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=False, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False), critical=True)
                .add_extension(
                    x509.SubjectKeyIdentifier.from_public_key(
                        key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path = self.root / (name + '-ca.pem')
        key_path = self.root / (name + '-ca.key')
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        self.config.config(ca_certificate=str(cert_path),
                           ca_private_key=str(key_path), group='certificates')

    def write_leaf(self, name, issued):
        (self.root / (name + '.pem')).write_bytes(issued.certificate_pem)
        (self.root / (name + '.key')).write_bytes(issued.private_key_pem)

    def handshake(self, name):
        context = ssl.create_default_context(
            cafile=str(self.root / 'server-ca.pem'))
        if name:
            context.load_cert_chain(str(self.root / (name + '.pem')),
                                    str(self.root / (name + '.key')))
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        peer_in, peer_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        peer = context.wrap_bio(peer_in, peer_out,
                                server_hostname='192.0.2.10')
        context = server.tls_context(self.conf)
        local = context.wrap_bio(incoming, outgoing, server_side=True)
        ready = [False, False]
        for step in range(30):
            for index, session in enumerate((peer, local)):
                if not ready[index]:
                    try:
                        session.do_handshake()
                        ready[index] = True
                    except ssl.SSLWantReadError:
                        pass
            incoming.write(peer_out.read())
            peer_in.write(outgoing.read())
            if all(ready):
                return local.getpeercert()
        self.fail('TLS handshake did not complete')

    def test_accepts_client_signed_by_client_ca(self):
        peer = self.handshake('client')
        self.assertEqual(SERVER_ID, peer['subject'][0][0][1])

    def test_rejects_expired_client_certificate(self):
        issued = x509.load_pem_x509_certificate(
            (self.root / 'client.pem').read_bytes())
        key = serialization.load_pem_private_key(
            (self.root / 'client-ca.key').read_bytes(), password=None)
        now = datetime.datetime.now(datetime.timezone.utc)
        builder = (x509.CertificateBuilder().subject_name(issued.subject)
                   .issuer_name(issued.issuer).public_key(issued.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=2))
                   .not_valid_after(now - datetime.timedelta(days=1)))
        for extension in issued.extensions:
            builder = builder.add_extension(extension.value,
                                            critical=extension.critical)
        expired = builder.sign(key, hashes.SHA256())
        (self.root / 'expired.pem').write_bytes(expired.public_bytes(
            serialization.Encoding.PEM))
        (self.root / 'expired.key').write_bytes(
            (self.root / 'client.key').read_bytes())
        self.assertRaises(ssl.SSLError, self.handshake, 'expired')

    def test_rejects_missing_foreign_and_wrong_usage_client_certs(self):
        for name in (None, 'foreign', 'wrong-usage', 'server'):
            self.assertRaises(ssl.SSLError, self.handshake, name)
