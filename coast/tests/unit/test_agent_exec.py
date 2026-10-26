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

"""Container exec refuses the wrong project, host, and unlabeled targets."""

import io
import json
from pathlib import Path
import struct

import fixtures

from coast.agent import docker_exec
from coast.agent import server
from coast.tests import base


KEY = b'dGhlIHNhbXBsZSBub25jZQ=='
ACCEPT = b's3pPLMBiTxaQ9kYGzzhZRbK+xOo='


class FakeStream:
    def recv(self, size):
        return b''

    def sendall(self, data):
        return None

    def close(self):
        return None


class FakeDocker:
    def __init__(self, containers):
        self.containers = containers
        self.calls = []
        self.stream = FakeStream()

    def list_running(self, labels):
        self.calls.append(('list', list(labels)))
        return self.containers

    def resolve(self, kind, resource):
        return docker_exec.resolve_container(
            self.list_running, kind, resource)

    def exec_create(self, container_id, body):
        self.calls.append(('create', container_id, body))
        return 'exec123'

    def exec_start(self, exec_id, tty):
        self.calls.append(('start', exec_id, tty))
        return self.stream

    def exec_resize(self, exec_id, rows, cols):
        self.calls.append(('resize', exec_id, rows, cols))

    def exec_inspect(self, exec_id):
        self.calls.append(('inspect', exec_id))
        return 0


def _labels(**values):
    base_labels = {'coolify.managed': 'true'}
    base_labels.update(values)
    return {'Id': 'abc123', 'Labels': base_labels}


def _frame(opcode, payload):
    mask = b'\x01\x02\x03\x04'
    masked = bytes(byte ^ mask[index % 4]
                   for index, byte in enumerate(payload))
    return bytes((0x80 | opcode, 0x80 | len(payload))) + mask + masked


def _request(payload):
    body = json.dumps(payload).encode('utf-8')
    if len(body) >= 126:
        raise AssertionError('test frame is too long')
    return (b'GET /v1/exec HTTP/1.1\r\nHost: agent\r\n'
            b'Connection: Upgrade\r\nUpgrade: websocket\r\n'
            b'Sec-WebSocket-Version: 13\r\n'
            b'Sec-WebSocket-Key: ' + KEY + b'\r\n\r\n' + _frame(1, body))


def _frames(response):
    head, body = response.split(b'\r\n\r\n', 1)
    frames = []
    while body:
        first, second = body[0], body[1]
        if second & 0x80 or not first & 0x80:
            raise AssertionError('server frame flags are wrong')
        length = second & 0x7F
        index = 2
        if length == 126:
            length = struct.unpack('!H', body[2:4])[0]
            index = 4
        payload = body[index:index + length]
        if len(payload) != length:
            raise AssertionError('truncated frame')
        frames.append((first & 0x0F, payload))
        body = body[index + length:]
    return head, frames


class ExecTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(role='worker', project_id='project', group='agent')

    def exchange(self, payload, containers):
        output = io.BytesIO()
        raw = _request(payload)

        class Connection:
            def settimeout(self, timeout):
                self.timeout = timeout

            def shutdown(self, how):
                raise AssertionError('Unexpected request deadline')

            def makefile(self, mode, buffering=None):
                if 'r' in mode:
                    return io.BytesIO(raw)
                return output

            def sendall(self, data):
                output.write(data)

        docker = FakeDocker(containers)
        http_server = type('Server', (), {
            'conf': self.conf, 'docker': docker})()
        server.Handler(Connection(), ('192.0.2.1', 1), http_server)
        return output.getvalue(), docker

    def assert_refused(self, payload, containers):
        response, docker = self.exchange(payload, containers)
        head, frames = _frames(response)
        self.assertIn(b'HTTP/1.1 101 Switching Protocols', head)
        self.assertIn(b'Sec-WebSocket-Accept: ' + ACCEPT, head)
        self.assertEqual([(8, struct.pack('!H', 4403) + b'Refused')], frames)
        self.assertNotIn(b'coolify', response.lower())
        self.assertNotIn(b'traefik', response.lower())
        self.assertNotIn('create', [call[0] for call in docker.calls])
        return docker

    def test_every_role_refuses_another_project(self):
        payload = {'project_id': 'other', 'resource_type': 'app',
                   'resource_uuid': 'app-1'}
        containers = [_labels(**{'coolify.type': 'application',
                                 'coolify.applicationUuid': 'app-1'})]
        for role in ('controller', 'worker', 'build'):
            with self.subTest(role=role):
                self.config.config(role=role, group='agent')
                docker = self.assert_refused(payload, containers)
                self.assertEqual([], docker.calls)

    def test_other_project_closes_without_docker(self):
        docker = self.assert_refused(
            {'project_id': 'other', 'resource_type': 'app',
             'resource_uuid': 'app-1'},
            [_labels(**{'coolify.type': 'application',
                        'coolify.applicationUuid': 'app-1'})])
        self.assertEqual([], docker.calls)

    def test_unlabeled_container_is_refused(self):
        docker = self.assert_refused(
            {'project_id': 'project', 'resource_type': 'app',
             'resource_uuid': 'app-1'},
            [{'Id': 'abc123', 'Labels': {}}])
        self.assertEqual(['list'], [call[0] for call in docker.calls])

    def test_missing_labels_are_refused(self):
        docker = self.assert_refused(
            {'project_id': 'project', 'resource_type': 'app',
             'resource_uuid': 'app-1'},
            [_labels(**{'coolify.type': 'application',
                        'coolify.applicationId': '4'})])
        self.assertEqual(['list'], [call[0] for call in docker.calls])

    def test_host_attempt_closes_without_docker(self):
        valid = {'project_id': 'project', 'resource_type': 'app',
                 'resource_uuid': 'app-1'}
        cases = (
            {'project_id': 'project', 'resource_type': 'host',
             'resource_uuid': 'app-1'},
            dict(valid, user='root'),
            dict(valid, command=['nsenter', '-t', '1', 'sh']),
            dict(valid, command=['/usr/bin/chroot', '/']),
            dict(valid, command='sh'),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                docker = self.assert_refused(payload, [_labels(
                    **{'coolify.type': 'application',
                       'coolify.applicationUuid': 'app-1'})])
                self.assertEqual([], docker.calls)

    def test_interactive_exec_uses_the_docker_api_only(self):
        source = Path(docker_exec.__file__).read_text()
        self.assertNotIn('subprocess', source)
        self.assertNotIn('nsenter(', source)
        self.assertNotIn('os.system', source)
        response, docker = self.exchange(
            {'project_id': 'project', 'resource_type': 'app',
             'resource_uuid': 'app-1', 'cols': 100, 'rows': 40},
            [_labels(**{'coolify.type': 'application',
                        'coolify.applicationUuid': 'app-1'})])
        head, frames = _frames(response)
        self.assertIn(b'HTTP/1.1 101 Switching Protocols', head)
        text = [json.loads(payload) for opcode, payload in frames
                if opcode == 1]
        closes = [payload for opcode, payload in frames if opcode == 8]
        self.assertEqual([{'code': 0, 'type': 'exit'}], text)
        self.assertEqual(struct.pack('!H', 1000), closes[-1])
        created = [call for call in docker.calls if call[0] == 'create']
        self.assertEqual(1, len(created))
        body = created[0][2]
        self.assertEqual(
            {'AttachStdin', 'AttachStdout', 'AttachStderr', 'Tty', 'Cmd'},
            set(body))
        self.assertNotIn('User', body)
        self.assertNotIn('Privileged', body)
        self.assertIs(True, body['Tty'])
        self.assertEqual(['/bin/sh'], body['Cmd'])
        self.assertEqual(('resize', 'exec123', 40, 100),
                         next(call for call in docker.calls
                              if call[0] == 'resize'))

    def test_one_off_command_is_not_a_tty(self):
        response, docker = self.exchange(
            {'project_id': 'project', 'resource_type': 'app',
             'resource_uuid': 'app-1', 'command': ['/bin/echo', 'ok']},
            [_labels(**{'coolify.type': 'application',
                        'coolify.applicationUuid': 'app-1'})])
        _head, frames = _frames(response)
        self.assertEqual(
            1000, struct.unpack('!H', frames[-1][1][:2])[0])
        body = next(call[2] for call in docker.calls if call[0] == 'create')
        self.assertIs(False, body['Tty'])
        self.assertEqual(['/bin/echo', 'ok'], body['Cmd'])
        self.assertNotIn('User', body)
        self.assertFalse(any(call[0] == 'resize' for call in docker.calls))

    def test_service_parent_and_child_stay_distinct(self):
        docker = FakeDocker([
            {'Id': 'parentid', 'Labels': {
                'coolify.managed': 'true', 'coolify.type': 'service',
                'coolify.serviceUuid': 'svc'}},
            {'Id': 'childid', 'Labels': {
                'coolify.managed': 'true', 'coolify.type': 'service',
                'coolify.serviceUuid': 'svc',
                'coolify.service.subUuid': 'sub'}}])
        self.assertEqual('parentid', docker.resolve('service', 'svc'))
        self.assertEqual('childid', docker.resolve('service', 'sub'))

    def test_two_matching_containers_are_refused(self):
        rows = [
            _labels(**{'coolify.type': 'application',
                       'coolify.applicationUuid': 'app-1',
                       'Id': 'one111'}),
            _labels(**{'coolify.type': 'application',
                       'coolify.applicationUuid': 'app-1'})]
        rows[0]['Id'] = 'one111'
        rows[1]['Id'] = 'two222'
        docker = self.assert_refused(
            {'project_id': 'project', 'resource_type': 'app',
             'resource_uuid': 'app-1'}, rows)
        self.assertEqual(['list'], [call[0] for call in docker.calls])

    def test_exec_create_body_has_no_user(self):
        body = docker_exec.exec_create_body(['/bin/sh'], True)
        self.assertEqual(
            {'AttachStdin': True, 'AttachStdout': True, 'AttachStderr': True,
             'Tty': True, 'Cmd': ['/bin/sh']}, body)


class DockerApiVersionTest(base.TestCase):

    def test_api_version_accepted_by_docker_29(self):
        # Docker 29.0 to 29.2 reject API versions below 1.44.
        major, minor = docker_exec._API.lstrip('/v').split('.')
        self.assertEqual('1', major)
        self.assertGreaterEqual(int(minor), 44)


class ClientReadBlockingTest(base.TestCase):

    def test_session_reads_client_without_timeout(self):
        # A socket file is unusable after one read timeout, so a quiet
        # client must not end the session.
        seen = {}

        class Connection:
            def settimeout(self, timeout):
                seen['timeout'] = timeout

        class Handler:
            connection = Connection()
            server = type('S', (), {'docker': object()})()

        class FakeSession:
            def __init__(self, handler, docker, spec):
                pass

            def run(self):
                seen['ran'] = True

        self.useFixture(fixtures.MockPatchObject(
            docker_exec, '_request', lambda handler: object()))
        self.useFixture(fixtures.MockPatchObject(
            docker_exec, 'Session', FakeSession))
        docker_exec._session(Handler())
        self.assertIsNone(seen['timeout'])
        self.assertTrue(seen['ran'])
