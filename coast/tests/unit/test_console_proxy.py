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

"""Console proxy against a local fake agent. No TLS and no real guest."""

import asyncio
import base64
import concurrent.futures
import datetime
import hashlib
import json
import os
import struct
import threading
import urllib.parse

import fixtures
from oslo_db.sqlalchemy import enginefacade
from oslo_utils import timeutils

from coast.common import context
from coast.common import states
from coast.console import agent
from coast.console import frames
from coast.console import proxy as proxy_mod
from coast.console import tokens
from coast.db import api as db_api
from coast.db import base_models
from coast import objects
from coast.tests import base


class FrameTest(base.TestCase):
    def test_accept_value_matches_the_rfc_vector(self):
        # RFC 6455 sample client nonce.
        key = 'dGhlIHNhbXBsZSBub25jZQ=='  # gitleaks:allow
        self.assertEqual('s3pPLMBiTxaQ9kYGzzhZRbK+xOo=',
                         frames.accept_value(key))

    def test_masked_round_trip_and_rejected_frames(self):
        async def check():
            reader = asyncio.StreamReader()
            reader.feed_data(frames.encode(frames.BINARY, b'hello', True))
            frame = await frames.read_frame(reader, True)
            self.assertEqual(b'hello', frame.payload)
            self.assertEqual(frames.BINARY, frame.opcode)
            reader.feed_data(frames.encode(
                frames.TEXT, b'part', False, fin=False))
            partial = await frames.read_frame(reader, False)
            self.assertFalse(partial.fin)
            reader.feed_data(frames.encode(frames.BINARY, b'x', False))
            with self.assertRaises(frames.ProtocolError):
                await frames.read_frame(reader, True)

        asyncio.run(check())


class DigestTest(base.TestCase):
    def test_digest_is_sha256_and_rejects_bad_tokens(self):
        self.assertEqual(hashlib.sha256(b'abc').hexdigest(),
                         tokens.digest('abc'))
        for bad in ('', 'not-ascii-é', 'a' * 129, None, b'abc'):
            self.assertRaises(ValueError, tokens.digest, bad)


class FakeAgent:
    """Speak the agent side of /v1/exec on plain TCP."""

    def __init__(self, action='echo'):
        self.action = action
        self.first = None
        self.frames = []
        self.ready = asyncio.Event()
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(
            self.client, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def connect(self, conf, address, server_id):
        reader, writer = await asyncio.open_connection(
            '127.0.0.1', self.port)
        await agent.client_upgrade(reader, writer, '127.0.0.1', self.port)
        return reader, writer

    async def client(self, reader, writer):
        try:
            await self.speak(reader, writer)
        except Exception:
            return
        finally:
            writer.close()

    async def speak(self, reader, writer):
        raw = await reader.readuntil(b'\r\n\r\n')
        key = ''
        for line in raw.decode('iso-8859-1').split('\r\n'):
            if line.lower().startswith('sec-websocket-key:'):
                key = line.split(':', 1)[1].strip()
        accept = frames.accept_value(key).encode('ascii')
        writer.write(
            b'HTTP/1.1 101 Switching Protocols\r\n'
            b'Upgrade: websocket\r\n'
            b'Connection: Upgrade\r\n'
            b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        await writer.drain()
        first = await frames.read_frame(reader, True)
        self.first = json.loads(first.payload)
        self.ready.set()
        if self.action == 'close4403':
            await frames.write_frame(
                writer, frames.CLOSE, struct.pack('!H', 4403), False)
            return
        if self.action == 'exit':
            await frames.write_frame(
                writer, frames.TEXT, b'{"type":"exit","code":0}', False)
            await frames.write_frame(
                writer, frames.CLOSE, struct.pack('!H', 1000), False)
            return
        while True:
            frame = await frames.read_frame(reader, True)
            self.frames.append((frame.opcode, frame.payload, frame.fin))
            if frame.opcode == frames.CLOSE:
                payload = frame.payload or struct.pack('!H', 1000)
                await frames.write_frame(
                    writer, frames.CLOSE, payload, False)
                return
            if frame.opcode in (frames.PING, frames.PONG):
                continue
            await frames.write_frame(
                writer, frame.opcode, frame.payload, False, frame.fin)


class ProxyTest(base.TestCase):
    def setUp(self):
        super().setUp()
        # In-memory SQLite uses StaticPool: concurrent sessions share one
        # connection and interfere with transactions and UPDATE row counts.
        # Use independent connections, as the deployed database does.
        directory = self.useFixture(fixtures.TempDir()).path
        facade = enginefacade.transaction_context()
        facade.configure(connection='sqlite:///' + directory + '/db',
                         sqlite_fk=True)
        engine = facade.writer.get_engine()
        self.addCleanup(engine.dispose)
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(facade, name)))
        self.useFixture(fixtures.MonkeyPatch(
            'coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(engine)
        self.ctx = context.RequestContext(project_id='project', user_id='user')
        self.stack = objects.Stack(
            context=self.ctx, name='existing', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        self.stack.create()
        self.server = objects.Server(
            context=self.ctx, name='worker', stack_id=self.stack.id,
            kind='vm', role='worker', status=states.ACTIVE,
            task_state=states.NONE, mgmt_ip='198.51.100.10')
        self.server.create()
        self.source = objects.Source(
            context=self.ctx, type='git',
            repo_url='https://example.com/web.git')
        self.source.create()
        self.service = objects.Service(
            context=self.ctx, stack_id=self.stack.id, name='web',
            source_id=self.source.id, server_id=self.server.id, ports='3000',
            replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE, engine_ref='engine-app-1')
        self.service.create()

    def grant(self, command=None, expires=None, used_at=None, kind='app'):
        secret = 'session-' + base64.urlsafe_b64encode(os.urandom(24)).decode(
            'ascii').rstrip('=')
        values = dict(
            token_hash=tokens.digest(secret), stack_id=self.stack.id,
            resource_type=kind, resource_id=self.service.id,
            command=command, user_id='user',
            expires_at=expires or (
                timeutils.utcnow() + datetime.timedelta(seconds=60)))
        if used_at is not None:
            values['used_at'] = used_at
        objects.ConsoleToken(context=self.ctx, **values).create()
        return secret

    def events(self):
        return list(objects.EventList.get_all(
            self.ctx, filters={'stack_id': self.stack.id}))

    async def listen(self, fake=None, connector=None, idle=30, limit=30):
        proxy = proxy_mod.Proxy(self.conf, connector or fake.connect)
        proxy.idle_timeout = idle
        proxy.max_duration = limit
        server = await asyncio.start_server(proxy.handle, '127.0.0.1', 0)
        return proxy, server, server.sockets[0].getsockname()[1]

    async def browser(self, port, secret=None, upgrade=True, path='/'):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        target = path
        if secret is not None:
            target = '/?' + urllib.parse.urlencode({'token': secret})
        lines = ['GET %s HTTP/1.1' % target, 'Host: localhost']
        if upgrade:
            lines.extend([
                'Upgrade: websocket', 'Connection: Upgrade',
                'Sec-WebSocket-Key: %s' % key,
                'Sec-WebSocket-Version: 13'])
        writer.write(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii'))
        await writer.drain()
        raw = await reader.readuntil(b'\r\n\r\n')
        status = int(raw.split(None, 2)[1])
        return reader, writer, status

    def test_health_and_unknown_path(self):
        async def check():
            _proxy, server, port = await self.listen(connector=self._unused)
            try:
                reader, writer, status = await self.browser(
                    port, upgrade=False, path='/health')
                body = await reader.readexactly(2)
                self.assertEqual(200, status)
                self.assertEqual(b'ok', body)
                writer.close()
                _reader, writer, status = await self.browser(
                    port, upgrade=False, path='/missing')
                self.assertEqual(404, status)
                writer.close()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(check())

    async def _unused(self, conf, address, server_id):
        raise AssertionError('agent')

    def test_bad_token_is_rejected_before_upgrade(self):
        async def check():
            _proxy, server, port = await self.listen(connector=self._unused)
            try:
                for secret in (None, 'nope', 'a' * 200):
                    _reader, writer, status = await self.browser(
                        port, secret, upgrade=False)
                    self.assertEqual(403, status)
                    writer.close()
                expired = self.grant(expires=timeutils.utcnow() -
                                     datetime.timedelta(seconds=5))
                _reader, writer, status = await self.browser(port, expired)
                self.assertEqual(403, status)
                writer.close()
                used = self.grant(used_at=timeutils.utcnow())
                _reader, writer, status = await self.browser(port, used)
                self.assertEqual(403, status)
                writer.close()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(check())
        self.assertEqual([], self.events())

    def test_bad_handshake_does_not_consume(self):
        async def check():
            secret = self.grant(command=['sh'])
            _proxy, server, port = await self.listen(connector=self._unused)
            try:
                _reader, writer, status = await self.browser(
                    port, secret, upgrade=False)
                self.assertEqual(400, status)
                writer.close()
            finally:
                server.close()
                await server.wait_closed()
            return secret

        secret = asyncio.run(check())
        row = objects.ConsoleTokenList.get_all(self.ctx)[0]
        self.assertIsNone(row.used_at)
        self.assertEqual(tokens.digest(secret), row.token_hash)

    def test_unavailable_workload_does_not_consume(self):
        self.server.mgmt_ip = None
        self.server.save()

        async def check():
            secret = self.grant()
            _proxy, server, port = await self.listen(connector=self._unused)
            try:
                _reader, writer, status = await self.browser(port, secret)
                self.assertEqual(403, status)
                writer.close()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(check())
        self.assertIsNone(objects.ConsoleTokenList.get_all(
            self.ctx)[0].used_at)

    def test_pipe_resize_exit_and_events(self):
        async def check():
            fake = FakeAgent()
            await fake.start()
            secret = self.grant(command=['sh'])
            _proxy, server, port = await self.listen(fake)
            try:
                reader, writer, status = await self.browser(port, secret)
                self.assertEqual(101, status)
                await asyncio.wait_for(fake.ready.wait(), 2)
                self.assertEqual({
                    'project_id': 'project', 'resource_type': 'app',
                    'resource_uuid': 'engine-app-1', 'command': ['sh'],
                    'cols': 80, 'rows': 24,
                }, fake.first)
                payload = b'{"type":"resize","cols":100,"rows":40}'
                await frames.write_frame(
                    writer, frames.TEXT, payload, True, fin=False)
                echo = await frames.read_frame(reader, False)
                self.assertEqual(payload, echo.payload)
                self.assertFalse(echo.fin)
                await frames.write_frame(writer, frames.BINARY, b'hi', True)
                echo = await frames.read_frame(reader, False)
                self.assertEqual(b'hi', echo.payload)
                await frames.write_frame(
                    writer, frames.CLOSE, struct.pack('!H', 1000), True)
                closed = await frames.read_frame(reader, False)
                self.assertEqual(1000, frames.close_code(closed.payload))
                writer.close()
            finally:
                server.close()
                await server.wait_closed()
                fake.server.close()
                await fake.server.wait_closed()

        asyncio.run(check())
        rows = {row.action: row for row in self.events()}
        self.assertEqual({'console.opened', 'console.closed'}, set(rows))
        opened = rows['console.opened']
        closed = rows['console.closed']
        self.assertEqual('success', opened.outcome)
        self.assertIsNone(opened.message)
        self.assertEqual({
            'user_id': 'user', 'resource_type': 'app',
            'resource_id': self.service.id,
        }, opened.detail)
        self.assertEqual(self.service.id, closed.detail['resource_id'])
        self.assertIsInstance(closed.detail['duration'], int)
        encoded = json.dumps(closed.detail)
        self.assertNotIn('hi', encoded)
        self.assertNotIn('sh', encoded)
        self.assertIsNotNone(objects.ConsoleTokenList.get_all(
            self.ctx)[0].used_at)

    def test_image_service_uses_the_engine_application(self):
        # Regression: an image service is a Coast service but the engine
        # runs it as an application, so the agent must look up an app.
        self.service.kind = 'service'
        self.service.save()

        async def check():
            fake = FakeAgent()
            await fake.start()
            secret = self.grant(command=['id'], kind='service')
            _proxy, server, port = await self.listen(fake)
            try:
                reader, writer, status = await self.browser(port, secret)
                self.assertEqual(101, status)
                await asyncio.wait_for(fake.ready.wait(), 2)
                self.assertEqual('app', fake.first['resource_type'])
                await frames.write_frame(
                    writer, frames.CLOSE, struct.pack('!H', 1000), True)
                await frames.read_frame(reader, False)
                writer.close()
            finally:
                server.close()
                await server.wait_closed()
                fake.server.close()
                await fake.server.wait_closed()
        asyncio.run(check())

    def test_agent_exit_and_forbidden_close(self):
        async def one(action, opcode, code):
            fake = FakeAgent(action)
            await fake.start()
            secret = self.grant()
            _proxy, server, port = await self.listen(fake)
            try:
                reader, writer, status = await self.browser(port, secret)
                self.assertEqual(101, status)
                frame = await asyncio.wait_for(
                    frames.read_frame(reader, False), 2)
                self.assertEqual(opcode, frame.opcode)
                if opcode == frames.TEXT:
                    self.assertEqual(b'{"type":"exit","code":0}',
                                     frame.payload)
                    frame = await frames.read_frame(reader, False)
                self.assertEqual(code, frames.close_code(frame.payload))
                writer.close()
            finally:
                server.close()
                await server.wait_closed()
                fake.server.close()
                await fake.server.wait_closed()

        async def check():
            await one('exit', frames.TEXT, 1000)
            await one('close4403', frames.CLOSE, 4403)

        asyncio.run(check())

    def test_connect_failure_closes_without_an_opened_event(self):
        async def down(conf, address, server_id):
            raise OSError('down')

        async def run():
            secret = self.grant()
            _proxy, server, port = await self.listen(connector=down)
            try:
                reader, writer, status = await self.browser(port, secret)
                self.assertEqual(101, status)
                frame = await asyncio.wait_for(
                    frames.read_frame(reader, False), 2)
                self.assertEqual(1011, frames.close_code(frame.payload))
                writer.close()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(run())
        self.assertEqual([], self.events())
        self.assertIsNotNone(objects.ConsoleTokenList.get_all(
            self.ctx)[0].used_at)

    def test_idle_ignores_ping_and_max_duration_closes(self):
        async def idle_case():
            fake = FakeAgent()
            await fake.start()
            secret = self.grant()
            _proxy, server, port = await self.listen(fake, idle=0.2, limit=10)
            try:
                reader, writer, status = await self.browser(port, secret)
                self.assertEqual(101, status)
                deadline = timeutils.utcnow() + datetime.timedelta(seconds=1)
                while timeutils.utcnow() < deadline:
                    await frames.write_frame(writer, frames.PING, b'x', True)
                    frame = await asyncio.wait_for(
                        frames.read_frame(reader, False), 1)
                    if frame.opcode == frames.CLOSE:
                        self.assertEqual(1000, frames.close_code(
                            frame.payload))
                        self.assertNotIn(
                            frames.PING, [item[0] for item in fake.frames])
                        writer.close()
                        return
                    self.assertEqual(frames.PONG, frame.opcode)
                self.fail('idle timeout did not close')
            finally:
                server.close()
                await server.wait_closed()
                fake.server.close()
                await fake.server.wait_closed()

        async def max_case():
            fake = FakeAgent()
            await fake.start()
            secret = self.grant()
            _proxy, server, port = await self.listen(fake, idle=10, limit=0.2)
            try:
                reader, writer, status = await self.browser(port, secret)
                self.assertEqual(101, status)
                frame = await asyncio.wait_for(
                    frames.read_frame(reader, False), 1)
                self.assertEqual(frames.CLOSE, frame.opcode)
                self.assertEqual(1000, frames.close_code(frame.payload))
                writer.close()
            finally:
                server.close()
                await server.wait_closed()
                fake.server.close()
                await fake.server.wait_closed()

        asyncio.run(idle_case())
        asyncio.run(max_case())

    def test_one_token_serves_one_session(self):
        async def check():
            fake = FakeAgent()
            await fake.start()
            secret = self.grant()
            _proxy, server, port = await self.listen(fake)

            async def once():
                _reader, writer, status = await self.browser(port, secret)
                writer.close()
                return status

            try:
                statuses = await asyncio.gather(once(), once())
            finally:
                server.close()
                await server.wait_closed()
                fake.server.close()
                await fake.server.wait_closed()
            self.assertCountEqual([101, 403], statuses)

        asyncio.run(check())

    def test_consume_is_single_use_under_threads(self):
        secret = self.grant()
        digest = tokens.digest(secret)
        now = timeutils.utcnow()
        barrier = threading.Barrier(2)

        def once():
            barrier.wait(timeout=10)
            return db_api.consume_console_token(digest, now)

        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(once) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(item is not None for item in results))
        winner = next(item for item in results if item is not None)
        self.assertEqual(self.service.id, winner['resource_id'])
        self.assertEqual(now, winner['used_at'])
        self.assertEqual(now, db_api.find_console_token(digest)['used_at'])
        self.assertIsNone(db_api.consume_console_token(digest, now))
