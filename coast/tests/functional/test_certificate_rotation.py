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

"""Health scan, serialized fake RPC, durable agent install and TLS identity."""

import io
import json
from types import SimpleNamespace
from unittest import mock

from coast.agent import certificates as agent
from coast.agent import server as https
from coast.common import states
from coast.conductor import certificates
from coast.health_manager import certificates as health
from coast import objects
from coast.tests import certificates as material
from coast.tests import conductor


class CertificateRotationRPCTest(material.Material, conductor.ConductorTest):
    def run_rotation(self, role, missing_ca=False):
        path = self.stack(workers=[])
        stack = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[-1])
        stack.driver = 'coolify_v4'
        stack.save()
        controller = (objects.ServerList.get_all(self.ctx, filters={
            'stack_id': stack.id, 'role': 'control'})[0]
            if role == 'control' else None)
        self.prepare_material(
            controller.id if controller else material.SERVER_ID)
        self.config.config(role='controller' if role == 'control' else role,
                           group='agent')
        if missing_ca:
            # Missing guest server-CA configuration does not change the
            # control-side trust used for TLS authentication.
            self.config.config(server_ca=None, group='agent')
        row = controller or objects.Server(
            context=self.ctx, id=material.SERVER_ID, stack_id=stack.id,
            name=role, role=role, kind='vm', status=states.ACTIVE,
            task_state=states.NONE, mgmt_ip=material.ADDRESS,
            agent_cert_serial=self.body()['expected_serial'])
        if controller:
            row.mgmt_ip = material.ADDRESS
            row.agent_cert_serial = self.body()['expected_serial']
            row.save()
        else:
            row.create()
        old_serial = row.agent_cert_serial
        listener = SimpleNamespace(
            conf=self.conf, certificates=agent.Rotation(self.conf),
            context=https.tls_context(self.conf))
        peers = []

        def exchange(server_id, body=None):
            self.assertEqual(row.id, server_id)
            # A fresh in-memory mTLS handshake authenticates the installed
            # certificate on every call. No real network enters this fixture.
            if missing_ca:
                self.config.config(server_ca=str(self.root / 'ca.pem'),
                                   group='agent')
            peer = self.peer_certificate(listener)
            if missing_ca:
                self.config.config(server_ca=None, group='agent')
            peers.append(str(peer.serial_number))
            if body is None:
                return peer
            data = json.dumps(body).encode()
            output = io.BytesIO()

            class Connection:
                def makefile(self, mode, buffering=None):
                    return io.BytesIO(
                        b'POST /v1/certificates/rotate HTTP/1.0\r\n' +
                        b'Content-Length: ' + str(len(data)).encode() +
                        b'\r\n\r\n' + data)

                def sendall(self, content):
                    output.write(content)

            https.Handler(Connection(), ('192.0.2.1', 1), listener)
            result = output.getvalue()
            if missing_ca:
                self.assertIn(b'412 Precondition Failed', result)
                raise ValueError('Agent rejected certificate rotation')
            self.assertIn(b'200 OK', result)
            return json.loads(result.split(b'\r\n\r\n', 1)[1])

        client = SimpleNamespace(certificate_connection=exchange)
        sweep = health.Rotation(self.conf)
        with mock.patch.object(certificates.agent_client, 'AgentClient',
                               return_value=client), mock.patch.object(
                health.rpcapi, 'ConductorAPI', return_value=self.rpc):
            sweep.run()
            self.manager.done.get(timeout=10)
            fresh = objects.Server.get_by_id(self.ctx, row.id)
            self.assertEqual(states.NONE, fresh.task_state)
            self.assertEqual(states.NONE, objects.Stack.get_by_id(
                self.ctx, stack.id).task_state)
            events = objects.EventList.get_all(self.ctx, filters={
                'resource_id': row.id, 'action': certificates.ACTION})
            if missing_ca:
                self.assertEqual(old_serial, fresh.agent_cert_serial)
                self.assertEqual({'start', 'failure'},
                                 {event.outcome for event in events})
                self.assertIn(certificates.FAULT,
                              [event.message for event in events])
                self.assertEqual(self.current.certificate_pem,
                                 (self.root / 'server.crt').read_bytes())
                self.assertEqual(self.current.private_key_pem,
                                 (self.root / 'server.key').read_bytes())
                self.assertFalse(listener.certificates.pending.exists())
                count = len(peers)
                sweep.run()
                self.assertEqual(count, len(peers))
            else:
                self.assertNotEqual(old_serial, fresh.agent_cert_serial)
                self.assertIsNotNone(fresh.agent_cert_expires_at)
                self.assertEqual([old_serial] * 3 + [fresh.agent_cert_serial],
                                 peers)
                self.assertEqual({'start', 'success'},
                                 {event.outcome for event in events})
                self.rpc.cast(self.ctx, 'rotate_server', row)
                self.manager.done.get(timeout=10)
                self.assertEqual(4, len(peers))
            kinds = [kind for kind, body in self.manager.notifier.events]
            self.assertIn('coast.server.rotate_certificate.start', kinds)
            self.assertIn('coast.server.rotate_certificate.' +
                          ('error' if missing_ca else 'end'), kinds)

    def test_controller_rotation(self):
        self.run_rotation('control')

    def test_worker_rotation(self):
        self.run_rotation('worker')

    def test_build_rotation(self):
        self.run_rotation('build')

    def test_missing_server_ca_is_rejected_without_rotation(self):
        self.run_rotation('worker', missing_ca=True)
