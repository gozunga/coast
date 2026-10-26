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

"""Plain HTTP console session listener.

A front proxy terminates TLS. This process checks one single-use token,
then copies frames to the workload agent and back.
"""

import asyncio
import json
import struct
import time
import urllib.parse

from oslo_log import log as logging

from coast.common import agent_auth
from coast.common import safe_log
from coast.console import access
from coast.console import agent
from coast.console import frames
from coast.console import tokens
from coast.db import api as db_api


LOG = logging.getLogger(__name__)
_LIMIT = 8192
_DENIED = (
    b'{"faultstring":"The console session is not valid.",'
    b'"faultcode":"Client","debuginfo":null}')
_BAD = (
    b'{"faultstring":"The request is not valid.",'
    b'"faultcode":"Client","debuginfo":null}')


def _http(code, reason, body, content_type):
    head = (
        'HTTP/1.1 %s %s\r\n'
        'Content-Type: %s\r\n'
        'Content-Length: %s\r\n'
        'Connection: close\r\n'
        '\r\n' % (code, reason, content_type, len(body)))
    return head.encode('ascii') + body


async def _send(writer, payload, close=True):
    writer.write(payload)
    await writer.drain()
    if close:
        writer.close()


def _headers(raw):
    lines = raw.decode('iso-8859-1', 'replace').split('\r\n')
    request = lines[0].split(' ')
    if len(request) != 3:
        return None
    headers = {}
    for line in lines[1:]:
        if ':' not in line:
            continue
        name, value = line.split(':', 1)
        headers[name.strip().lower()] = value.strip()
    return request[0], request[1], headers


async def _read(reader):
    try:
        raw = await reader.readuntil(b'\r\n\r\n')
    except asyncio.IncompleteReadError:
        return None
    if len(raw) > _LIMIT:
        return None
    return _headers(raw)


def _query_token(target):
    parsed = urllib.parse.urlsplit(target)
    values = urllib.parse.parse_qs(
        parsed.query, keep_blank_values=True).get('token', [])
    if len(values) != 1 or not values[0]:
        return ''
    return values[0]


def _upgrade_ok(headers):
    connection = [part.strip().lower()
                  for part in headers.get('connection', '').split(',')]
    return (headers.get('upgrade', '').lower() == 'websocket' and
            'upgrade' in connection and
            headers.get('sec-websocket-version') == '13' and
            bool(headers.get('sec-websocket-key')))


def _switching(key):
    accept = frames.accept_value(key)
    return (
        'HTTP/1.1 101 Switching Protocols\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Accept: %s\r\n'
        '\r\n' % accept).encode('ascii')


def _grant(token):
    """Return a usable session, or None. Do not consume the token."""
    try:
        digest = tokens.digest(token)
    except ValueError:
        return None
    view = db_api.find_console_token(digest)
    if view is None:
        return None
    target = access.target_for(view)
    if target is None:
        return None
    target['digest'] = digest
    return target


def _hello(target):
    return json.dumps({
        'project_id': target['project_id'],
        'resource_type': target.get('engine_type',
                                    target['resource_type']),
        'resource_uuid': target['engine_ref'],
        'command': target['command'],
        'cols': 80,
        'rows': 24,
    }, separators=(',', ':'))


async def _close(writer, code, masked):
    if writer is None:
        return
    try:
        await frames.write_frame(
            writer, frames.CLOSE, struct.pack('!H', code), masked)
        writer.close()
    except Exception:
        return


class Session:
    """One browser-to-agent pipe. Tests can shorten the time limits."""

    def __init__(self, proxy, reader, writer, target):
        self.proxy = proxy
        self.reader = reader
        self.writer = writer
        self.target = target
        self.agent_writer = None
        self.opened = False
        self.failed = False
        self.agent_code = None
        self.browser_code = None
        self.started = 0.0
        self.last_data = 0.0

    def final_code(self):
        if self.agent_code == 4403:
            return 4403
        if self.failed:
            return 1011
        return self.browser_code or self.agent_code or 1000

    async def run(self):
        try:
            await self._open()
        except Exception as exc:
            self.failed = True
            safe_log.log_failure(
                LOG, 'Console session failed cause=%(cause)s '
                'where=%(where)s', exc)
        try:
            if self.opened:
                duration = int(time.monotonic() - self.started)
                access.record(self.target, 'console.closed', duration)
        finally:
            code = self.final_code()
            await _close(self.writer, code, False)
            await _close(self.agent_writer, code, True)

    async def _open(self):
        agent_reader, agent_writer = await self.proxy.connector(
            self.proxy.conf, self.target['address'],
            self.target['server_id'])
        self.agent_writer = agent_writer
        await frames.write_frame(
            agent_writer, frames.TEXT, _hello(self.target), True)
        access.record(self.target, 'console.opened')
        self.opened = True
        self.started = time.monotonic()
        self.last_data = self.started
        await self.pipe(agent_reader, agent_writer)

    async def pipe(self, agent_reader, agent_writer):
        stop = asyncio.Event()
        tasks = [asyncio.create_task(item) for item in (
            self.pump(self.reader, agent_writer, self.writer,
                      True, True, False, stop, True),
            self.pump(agent_reader, self.writer, agent_writer,
                      False, False, True, stop, False),
            self.watch(stop))]
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            error = task.exception()
            if error is not None:
                raise error

    async def pump(self, reader, forward, reply, expect_mask, forward_mask,
                   reply_mask, stop, from_browser):
        while not stop.is_set():
            frame = await frames.read_frame(reader, expect_mask)
            if frame.opcode == frames.PING:
                await frames.write_frame(
                    reply, frames.PONG, frame.payload, reply_mask)
                continue
            if frame.opcode == frames.PONG:
                continue
            if frame.opcode == frames.CLOSE:
                code = frames.close_code(frame.payload)
                if from_browser:
                    self.browser_code = code
                else:
                    self.agent_code = code
                stop.set()
                return
            self.last_data = time.monotonic()
            await frames.write_frame(
                forward, frame.opcode, frame.payload, forward_mask, frame.fin)

    async def watch(self, stop):
        idle = self.proxy.idle_timeout
        limit = self.proxy.max_duration
        step = min(0.05, idle / 4)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), step)
                return
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if now - self.started >= limit or now - self.last_data >= idle:
                stop.set()
                return


class Proxy:
    def __init__(self, conf, connector=None):
        self.conf = conf
        self.connector = connector or agent.open_exec
        self.idle_timeout = conf.console.idle_timeout
        self.max_duration = conf.console.max_duration

    async def handle(self, reader, writer):
        try:
            await self.dispatch(reader, writer)
        except Exception as exc:
            safe_log.log_failure(
                LOG, 'Console request failed cause=%(cause)s '
                'where=%(where)s', exc)
            writer.close()

    async def dispatch(self, reader, writer):
        parsed = await _read(reader)
        if parsed is None:
            await _send(writer, _http(400, 'Bad Request', _BAD,
                                      'application/json'))
            return
        method, target, headers = parsed
        path = urllib.parse.urlsplit(target).path or '/'
        if method == 'GET' and path == '/health':
            await _send(writer, _http(200, 'OK', b'ok', 'text/plain'))
            return
        if method != 'GET' or path != '/':
            await _send(writer, _http(404, 'Not Found', b'', 'text/plain'))
            return
        grant = _grant(_query_token(target))
        if grant is None or not _upgrade_ok(headers):
            await self._refuse(writer, grant)
            return
        if access.consume(grant.pop('digest')) is None:
            await _send(writer, _http(403, 'Forbidden', _DENIED,
                                      'application/json'))
            return
        await _send(writer, _switching(headers['sec-websocket-key']),
                    close=False)
        await Session(self, reader, writer, grant).run()

    async def _refuse(self, writer, grant):
        # A bad handshake must not burn a token that is still valid.
        if grant is None:
            await _send(writer, _http(403, 'Forbidden', _DENIED,
                                      'application/json'))
            return
        await _send(writer, _http(400, 'Bad Request', _BAD,
                                  'application/json'))


async def serve(conf, connector=None):
    agent_auth.prepare(conf)
    proxy = Proxy(conf, connector)
    server = await asyncio.start_server(
        proxy.handle, conf.console.bind_host, conf.console.bind_port)
    async with server:
        await server.serve_forever()
