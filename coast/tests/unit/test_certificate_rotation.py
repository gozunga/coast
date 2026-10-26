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

"""Q116 negative requests, private durability and subsequent TLS contexts."""

import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest import mock

from cryptography import x509

from coast.agent import certificates
from coast.agent.engines import coolify
from coast.agent import server
from coast.conductor import agent_client
from coast.drivers import base as driver_base
from coast.tests import base
from coast.tests import certificates as material
from coast.tests.unit import test_tls


class AgentRotationTest(material.Material, base.TestCase):
    def setUp(self):
        super().setUp()
        self.prepare_material()
        self.rotation = certificates.Rotation(self.conf)
        self.listener = SimpleNamespace(
            conf=self.conf, certificates=self.rotation,
            context=server.tls_context(self.conf))

    def request(self, body, method='POST', path='/v1/certificates/rotate'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        output = io.BytesIO()

        class Connection:
            def makefile(self, mode, buffering=None):
                return io.BytesIO(
                    ('%s %s HTTP/1.0\r\nContent-Length: %d\r\n\r\n' %
                     (method, path, len(data))).encode() + data)

            def sendall(self, content):
                output.write(content)

        server.Handler(Connection(), ('192.0.2.1', 1), self.listener)
        return output.getvalue()

    def peer_serial(self):
        return self.peer_certificate(self.listener).serial_number

    def test_all_roles_install_retry_and_new_tls_context(self):
        for role in ('controller', 'worker', 'build'):
            self.config.config(role=role, group='agent')
            body = self.body()
            body['expected_serial'] = str(self.peer_serial())
            previous_context = self.listener.context
            previous_cert = Path(self.conf.agent.server_cert).read_bytes()
            previous_key = Path(self.conf.agent.server_key).read_bytes()
            response = self.request(body)
            self.assertIn(b'200 OK', response)
            self.assertNotIn(body['private_key'].encode(), response)
            expected = x509.load_pem_x509_certificate(
                body['certificate'].encode()).serial_number
            self.assertEqual(expected, self.peer_serial())
            self.assertIsNot(previous_context, self.listener.context)
            self.assertEqual(previous_cert,
                             (self.rotation.root /
                              'previous.crt').read_bytes())
            self.assertEqual(previous_key,
                             (self.rotation.root /
                              'previous.key').read_bytes())
            self.assertEqual(response.split(b'\r\n\r\n', 1)[1],
                             self.request(body).split(b'\r\n\r\n', 1)[1])
            self.listener.certificates = certificates.Rotation(self.conf)
            self.assertEqual(response.split(b'\r\n\r\n', 1)[1],
                             self.request(body).split(b'\r\n\r\n', 1)[1])
            self.assertFalse(self.listener.certificates.pending.exists())
            for path in (self.conf.agent.server_cert,
                         self.conf.agent.server_key,
                         self.rotation.root / 'previous.key'):
                self.assertEqual(0o600, Path(path).stat().st_mode & 0o777)
            self.assertEqual(self.ca_pem,
                             Path(self.conf.agent.server_ca).read_bytes())

    def reject(self, body, status=b'400 Bad Request'):
        old = self.listener.context
        previous = Path(self.conf.agent.server_cert).read_bytes()
        response = self.request(body)
        self.assertIn(status, response)
        self.assertIs(old, self.listener.context)
        self.assertEqual(previous,
                         Path(self.conf.agent.server_cert).read_bytes())
        self.assertFalse(self.rotation.pending.exists())

    def test_wrong_cn(self):
        self.reject(self.body(self.issue(server_id='different-server')))

    def test_wrong_san(self):
        self.reject(self.body(self.issue(address='192.0.2.11')))

    def test_wrong_ca(self):
        old = self.issue()
        # Same issuer name, but a different CA key must still be rejected.
        test_tls.TLSTest._write_ca(self)
        other = self.issue()
        (self.root / 'ca.pem').write_bytes(self.ca_pem)
        self.reject(self.body(other))
        self.assertNotEqual(old.certificate_pem, other.certificate_pem)

    def test_expired_and_not_yet_valid(self):
        for before, after in ((-200, -100), (100, 200)):
            issued = self.issue()
            issued.certificate_pem = self.redate(
                issued.certificate_pem, before, after)
            self.reject(self.body(issued))

    def test_key_mismatch(self):
        body = self.body()
        body['private_key'] = self.issue().private_key_pem.decode()
        self.reject(body)

    def test_client_auth_only(self):
        self.reject(self.body(self.issue(usage='client')))

    def test_stale_serial(self):
        body = self.body()
        body['expected_serial'] = '1'
        self.reject(body, b'409 Conflict')

    def test_missing_ca_rejects_without_partial_rotation(self):
        self.config.config(server_ca=None, group='agent')
        context = self.listener.context
        with mock.patch.object(certificates, 'write') as write, \
                mock.patch.object(coolify, '_claim') as claim:
            response = self.request(self.body())
        self.assertIn(b'412 Precondition Failed', response)
        self.assertEqual(
            {'error': 'Configure [agent] server_ca before '
             'certificate rotation.'},
            json.loads(response.split(b'\r\n\r\n', 1)[1]))
        write.assert_not_called()
        claim.assert_not_called()
        self.assertFalse(self.rotation.pending.exists())
        self.assertFalse((self.rotation.root / 'previous.crt').exists())
        self.assertFalse((self.rotation.root / 'previous.key').exists())
        self.assertEqual(self.current.certificate_pem,
                         Path(self.conf.agent.server_cert).read_bytes())
        self.assertEqual(self.current.private_key_pem,
                         Path(self.conf.agent.server_key).read_bytes())
        self.assertIs(context, self.listener.context)

    def test_reject_methods_and_extra_trust_or_paths(self):
        for method in ('GET', 'PUT', 'PATCH', 'DELETE'):
            self.assertIn(b'405 Method Not Allowed',
                          self.request(self.body(), method=method))
        for field in ('server_ca', 'client_ca', 'heartbeat_key', 'path'):
            self.reject(dict(self.body(), **{field: 'SECRET'}))
        for body in (b'[]', b'null', b'not json', b'{}'):
            self.reject(body)

    def test_uncertain_install_closes_listener_and_blocks_restart(self):
        original = certificates.write

        def fail(path, content):
            if path == Path(self.conf.agent.server_cert):
                raise OSError('SECRET')
            return original(path, content)

        with mock.patch.object(certificates, 'write', side_effect=fail):
            response = self.request(self.body())
        self.assertIn(b'412 Precondition Failed', response)
        self.assertNotIn(b'SECRET', response)
        self.assertIsNone(self.listener.context)
        self.assertTrue(self.rotation.pending.exists())
        self.assertEqual(self.current.private_key_pem,
                         (self.rotation.root / 'previous.key').read_bytes())
        self.assertRaises(ValueError, certificates.Rotation, self.conf)
        self.assertIn(b'412 Precondition Failed', self.request(self.body()))

    def test_parallel_same_expected_serial_only_one_install(self):
        bodies = [self.body(), self.body()]
        results = []

        def rotate(body):
            try:
                results.append(self.rotation.rotate(
                    body, self.listener, server.tls_context)['status'])
            except coolify.Conflict:
                results.append('conflict')

        threads = [threading.Thread(target=rotate, args=(body,))
                   for body in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(['conflict', 'rotated'], sorted(results))

    def test_context_install_failure_retains_backup_and_closes_listener(self):
        body = self.body()
        with mock.patch.object(server, 'tls_context',
                               side_effect=ValueError('SECRET')):
            response = self.request(body)
        self.assertIn(b'412 Precondition Failed', response)
        self.assertNotIn(b'SECRET', response)
        self.assertIsNone(self.listener.context)
        self.assertTrue(self.rotation.pending.exists())
        self.assertEqual(self.current.certificate_pem,
                         (self.rotation.root / 'previous.crt').read_bytes())
        self.assertEqual(self.current.private_key_pem,
                         (self.rotation.root / 'previous.key').read_bytes())

    def test_changed_material_with_current_serial_is_not_a_retry(self):
        issued = self.issue()
        issued.certificate_pem = self.redate(
            self.current.certificate_pem, -8000, 2000)
        issued.private_key_pem = self.current.private_key_pem
        self.reject(self.body(issued), b'409 Conflict')

    def test_extra_ca_certificate_is_rejected(self):
        body = self.body()
        body['certificate'] += self.ca_pem.decode()
        self.reject(body)

    def test_control_connection_checks_uuid_and_bounds_response(self):
        self.config.config(agent_client_cert='client.crt',
                           agent_client_key='client.key',
                           group='controller_worker')
        client = agent_client.AgentClient(self.conf, material.ADDRESS)
        response = mock.Mock(status=200)
        response.read.return_value = b'{"status":"rotated","serial":"1"}'
        context = mock.Mock()
        peer = context.wrap_socket.return_value
        connection = mock.Mock(sock=None)
        connection.getresponse.return_value = response
        with (mock.patch.object(agent_client.ssl, 'create_default_context',
                                return_value=context),
              mock.patch.object(agent_client.http.client, 'HTTPSConnection',
                                return_value=connection),
              mock.patch.object(agent_client.management, 'socket_for_agent')):
            for issued, valid in ((self.current, True),
                                  (self.issue(server_id='wrong-server'),
                                   False),
                                  (self.issue(usage='client'), False)):
                cert = x509.load_pem_x509_certificate(issued.certificate_pem)
                peer.getpeercert.return_value = cert.public_bytes(
                    certificates.serialization.Encoding.DER)
                if valid:
                    observed = client.certificate_connection(
                        material.SERVER_ID)
                    self.assertEqual(cert.serial_number,
                                     observed.serial_number)
                    result = client.certificate_connection(
                        material.SERVER_ID, self.body())
                    self.assertEqual({'status': 'rotated', 'serial': '1'},
                                     result)
                else:
                    self.assertRaises(driver_base.EngineUnreachable,
                                      client.certificate_connection,
                                      material.SERVER_ID)
            cert = x509.load_pem_x509_certificate(self.current.certificate_pem)
            peer.getpeercert.return_value = cert.public_bytes(
                certificates.serialization.Encoding.DER)
            response.read.return_value = (
                b'SECRET' * self.conf.agent.max_body_size)
            try:
                client.certificate_connection(material.SERVER_ID, self.body())
            except driver_base.EngineUnreachable as error:
                self.assertNotIn('SECRET', str(error))
            else:
                self.fail('Unbounded response was accepted')
