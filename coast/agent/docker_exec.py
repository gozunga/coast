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

"""Start one process in a labeled container. Never a host shell.

Docker Engine API 1.44 list and exec. Docker 29.0 to 29.2 reject
requests below 1.44, and 1.44 works on Docker 25 and later:
https://docs.docker.com/reference/api/engine/version/v1.44/
https://docs.docker.com/engine/release-notes/29/
"""

import http.client
import json
import logging
import re
import socket
import struct
import threading
import time
import urllib.parse

from coast.agent import namespace
from coast.agent import websocket


_ALLOWED = frozenset((
    'project_id', 'resource_type', 'resource_uuid', 'command', 'cols',
    'rows'))
_HOST_ARGV = frozenset(('nsenter', 'chroot', 'unshare'))
_SOCKET = '/var/run/docker.sock'
_API = '/v1.44'

LOG = logging.getLogger(__name__)


class Refuse(Exception):
    """Close 4403. Do not include a resource id or a command."""


class Spec:
    def __init__(self, kind, resource, command, tty, cols, rows):
        self.kind = kind
        self.resource = resource
        self.command = command
        self.tty = tty
        self.cols = cols
        self.rows = rows


def _dimension(value):
    return (isinstance(value, int) and not isinstance(value, bool) and
            1 <= value <= 1000)


def _command(command):
    if command is None:
        return ['/bin/sh'], True
    if not isinstance(command, list) or not 1 <= len(command) <= 32:
        raise Refuse()
    argv = []
    for arg in command:
        if (not isinstance(arg, str) or not arg or len(arg) > 256 or
                '\x00' in arg):
            raise Refuse()
        argv.append(arg)
    if argv[0].rsplit('/', 1)[-1] in _HOST_ARGV:
        raise Refuse()
    return argv, False


def _window(body, tty):
    if not tty:
        return None, None
    cols = body.get('cols', 80)
    rows = body.get('rows', 24)
    if not _dimension(cols) or not _dimension(rows):
        raise Refuse()
    return cols, rows


def authorize(body, conf):
    """Return a Spec or raise Refuse. This function does not call Docker."""
    if not isinstance(body, dict) or set(body) - _ALLOWED:
        raise Refuse()
    if not namespace.same_project(conf, body.get('project_id')):
        raise Refuse()
    kind = body.get('resource_type')
    if kind not in ('app', 'service'):
        raise Refuse()
    resource = body.get('resource_uuid')
    if (not isinstance(resource, str) or not re.fullmatch(
            r'[A-Za-z0-9_-]{1,128}', resource)):
        raise Refuse()
    command, tty = _command(body.get('command', None))
    cols, rows = _window(body, tty)
    return Spec(kind, resource, command, tty, cols, rows)


def exec_create_body(command, tty):
    """Docker exec create body. No user and no privileged flag."""
    return {'AttachStdin': True, 'AttachStdout': True, 'AttachStderr': True,
            'Tty': True if tty else False, 'Cmd': list(command)}


def _ids(rows, predicate):
    found = []
    if not isinstance(rows, list):
        raise Refuse()
    for item in rows:
        if not isinstance(item, dict):
            raise Refuse()
        labels = item.get('Labels') or {}
        if not isinstance(labels, dict):
            raise Refuse()
        if labels.get('coolify.managed') != 'true' or not predicate(labels):
            continue
        cid = item.get('Id')
        if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9]+', cid):
            raise Refuse()
        found.append(cid)
    return found


def _app_labels(labels, resource):
    return (labels.get('coolify.type') == 'application' and
            labels.get('coolify.applicationUuid') == resource)


def _sub_labels(labels, resource):
    return (labels.get('coolify.type') == 'service' and
            labels.get('coolify.service.subUuid') == resource)


def _parent_labels(labels, resource):
    return (labels.get('coolify.type') == 'service' and
            not labels.get('coolify.service.subUuid') and
            labels.get('coolify.serviceUuid') == resource)


def resolve_container(list_running, kind, resource):
    """Return one running container id, or raise Refuse."""
    if kind == 'app':
        rows = list_running([
            'coolify.managed=true', 'coolify.type=application',
            'coolify.applicationUuid=' + resource])
        found = _ids(rows, lambda labels: _app_labels(labels, resource))
        if len(found) != 1:
            raise Refuse()
        return found[0]
    if kind != 'service':
        raise Refuse()
    rows = list_running([
        'coolify.managed=true', 'coolify.type=service',
        'coolify.service.subUuid=' + resource])
    found = _ids(rows, lambda labels: _sub_labels(labels, resource))
    if len(found) > 1:
        raise Refuse()
    if len(found) == 1:
        return found[0]
    rows = list_running([
        'coolify.managed=true', 'coolify.type=service',
        'coolify.serviceUuid=' + resource])
    found = _ids(rows, lambda labels: _parent_labels(labels, resource))
    if len(found) != 1:
        raise Refuse()
    return found[0]


def _safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]+', value):
        raise OSError('docker id rejected')
    return value


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self._unix = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix)
        self.sock = sock


class Stream:
    def __init__(self, sock, pending):
        self.sock = sock
        self.pending = pending

    def recv(self, size):
        if self.pending:
            data = self.pending[:size]
            self.pending = self.pending[size:]
            return data
        return self.sock.recv(size)

    def sendall(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class DockerClient:
    def __init__(self, path=_SOCKET):
        self.path = path

    def list_running(self, labels):
        filters = json.dumps({'label': labels, 'status': ['running']},
                             separators=(',', ':'))
        path = _API + '/containers/json?filters=' + urllib.parse.quote(
            filters)
        payload = self._json('GET', path, None)
        if not isinstance(payload, list):
            raise OSError('docker list rejected')
        return payload

    def resolve(self, kind, resource):
        return resolve_container(self.list_running, kind, resource)

    def exec_create(self, container_id, body):
        payload = self._json(
            'POST', _API + '/containers/%s/exec' % _safe_id(container_id),
            body)
        if not isinstance(payload, dict):
            raise OSError('docker exec rejected')
        return _safe_id(payload.get('Id'))

    def exec_start(self, exec_id, tty):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect(self.path)
            data = json.dumps({'Detach': False,
                               'Tty': True if tty else False}).encode()
            path = _API + '/exec/%s/start' % _safe_id(exec_id)
            header = (
                'POST %s HTTP/1.1\r\nHost: localhost\r\n'
                'Content-Type: application/json\r\nContent-Length: %d\r\n'
                'Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n' % (
                    path, len(data))).encode()
            sock.sendall(header + data)
            pending = _read_upgrade(sock)
            sock.settimeout(1)
            return Stream(sock, pending)
        except Exception:
            sock.close()
            raise

    def exec_resize(self, exec_id, rows, cols):
        if not _dimension(rows) or not _dimension(cols):
            raise Refuse()
        path = _API + '/exec/%s/resize?h=%d&w=%d' % (
            _safe_id(exec_id), rows, cols)
        self._http('POST', path, b'')

    def exec_inspect(self, exec_id):
        payload = self._json(
            'GET', _API + '/exec/%s/json' % _safe_id(exec_id), None)
        code = payload.get('ExitCode') if isinstance(payload, dict) else None
        if isinstance(code, bool) or not isinstance(code, int):
            raise OSError('docker exit rejected')
        return code

    def _json(self, method, path, body):
        status, raw = self._http(method, path, body)
        if not raw:
            return None
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError):
            raise OSError('docker response rejected') from None

    def _http(self, method, path, body):
        data = None if body is None else (
            body if isinstance(body, bytes) else json.dumps(body).encode())
        conn = UnixConnection(self.path, 10)
        try:
            headers = {}
            if data is not None and not isinstance(body, bytes):
                headers['Content-Type'] = 'application/json'
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            if response.status < 200 or response.status >= 300:
                raise OSError('docker request rejected')
            content = response.read(1048577)
            if len(content) > 1048576:
                raise OSError('docker response too large')
            return response.status, content
        finally:
            conn.close()


def _read_upgrade(sock):
    data = bytearray()
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError('docker exec rejected')
        data.extend(chunk)
        if len(data) > 65536:
            raise OSError('docker exec rejected')
    head, _, rest = bytes(data).partition(b'\r\n\r\n')
    parts = head.split(b'\r\n', 1)[0].split(b' ')
    if len(parts) < 2 or not parts[1].isdigit():
        raise OSError('docker exec rejected')
    if int(parts[1]) not in (101, 200):
        raise OSError('docker exec rejected')
    return rest


def _mux(payload):
    return struct.pack('>BxxxI', 0, len(payload)) + payload


class Session:
    def __init__(self, handler, docker, spec):
        self.handler = handler
        self.docker = docker
        self.spec = spec
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.activity = self.started
        self.idle = handler.server.conf.agent.exec_idle_timeout
        self.limit = handler.server.conf.agent.exec_max_duration
        self.exec_id = None
        self.stream = None
        self.normal_end = False

    def run(self):
        cid = self.docker.resolve(self.spec.kind, self.spec.resource)
        body = exec_create_body(self.spec.command, self.spec.tty)
        self.exec_id = self.docker.exec_create(cid, body)
        self.stream = self.docker.exec_start(self.exec_id, self.spec.tty)
        if self.spec.tty:
            self.docker.exec_resize(
                self.exec_id, self.spec.rows, self.spec.cols)
        reader = threading.Thread(target=self._read_client, daemon=True)
        reader.start()
        try:
            self._read_docker()
        finally:
            self.stop.set()
            self.stream.close()
            reader.join(timeout=2)
        code = self.docker.exec_inspect(self.exec_id)
        self._send_text({'type': 'exit', 'code': code})
        self._close(1000 if self.normal_end else 1001)

    def _overdue(self):
        now = time.monotonic()
        return (now - self.started >= self.limit or
                now - self.activity >= self.idle)

    def _read_docker(self):
        pending = b''
        while True:
            if self._overdue():
                return
            try:
                chunk = self.stream.recv(4096)
            except TimeoutError:
                if self.stop.is_set():
                    return
                continue
            if not chunk:
                self.normal_end = True
                return
            self.activity = time.monotonic()
            if self.spec.tty:
                self._send(websocket.BINARY, chunk)
            else:
                pending = self._demux(pending + chunk)
            if self.stop.is_set():
                return

    def _demux(self, pending):
        while len(pending) >= 8:
            kind, size = struct.unpack('>BxxxI', pending[:8])
            if size > 1048576:
                raise OSError('docker stream rejected')
            if len(pending) < 8 + size:
                break
            payload = pending[8:8 + size]
            pending = pending[8 + size:]
            if kind in (1, 2) and payload:
                self._send(websocket.BINARY, payload)
        if len(pending) > 1048576 + 8:
            raise OSError('docker stream rejected')
        return pending

    def _read_client(self):
        try:
            while not self.stop.is_set():
                if self._overdue():
                    return
                try:
                    opcode, payload = websocket.read_frame(
                        self.handler.rfile)
                except TimeoutError:
                    continue
                self.activity = time.monotonic()
                self._on_client(opcode, payload)
        except Exception:
            self.stop.set()

    def _on_client(self, opcode, payload):
        if opcode == websocket.CLOSE:
            self.stop.set()
        elif opcode == websocket.PING:
            self._send(websocket.PONG, payload)
        elif opcode == websocket.BINARY:
            self._stdin(payload)
        elif opcode == websocket.TEXT:
            self._resize(payload)

    def _stdin(self, payload):
        if not payload or self.stream is None:
            return
        if self.spec.tty:
            self.stream.sendall(payload)
        else:
            self.stream.sendall(_mux(payload))

    def _resize(self, payload):
        if not self.spec.tty or self.exec_id is None:
            return
        try:
            message = json.loads(payload.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict) or message.get('type') != 'resize':
            return
        cols = message.get('cols')
        rows = message.get('rows')
        if not _dimension(cols) or not _dimension(rows):
            return
        self.docker.exec_resize(self.exec_id, rows, cols)

    def _send(self, opcode, payload):
        with self.lock:
            websocket.write_frame(self.handler.wfile, opcode, payload)
            self.handler.wfile.flush()

    def _send_text(self, message):
        data = json.dumps(message, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
        self._send(websocket.TEXT, data)

    def _close(self, code):
        with self.lock:
            websocket.write_close(self.handler.wfile, code, b'')
            self.handler.wfile.flush()


def _cancel(handler):
    deadline = getattr(handler, 'deadline', None)
    if deadline is not None:
        deadline.cancel()


def _switch(handler, accept):
    # The handler answers HTTP/1.0 by default. RFC 6455 needs HTTP/1.1.
    handler.wfile.write(
        b'HTTP/1.1 101 Switching Protocols\r\n'
        b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
        b'Sec-WebSocket-Accept: ' + accept.encode('ascii') + b'\r\n\r\n')
    handler.wfile.flush()


def _close(handler, code):
    reason = b'Refused' if code == 4403 else b''
    try:
        websocket.write_close(handler.wfile, code, reason)
        handler.wfile.flush()
    except (OSError, websocket.ProtocolError):
        pass


def _request(handler):
    deadline = time.monotonic() + handler.server.conf.agent.exec_idle_timeout
    while True:
        try:
            opcode, payload = websocket.read_frame(handler.rfile)
            break
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise websocket.ProtocolError() from None
    if opcode != websocket.TEXT:
        raise websocket.ProtocolError()
    try:
        body = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise websocket.ProtocolError() from None
    return authorize(body, handler.server.conf)


def _session(handler):
    try:
        spec = _request(handler)
        docker = getattr(handler.server, 'docker', None) or DockerClient()
        try:
            # Block on client reads. A socket file object cannot be read
            # again after one timeout, so a 1 s timeout ended every
            # session after 1 s without input. The docker read loop
            # still enforces the idle and duration limits.
            handler.connection.settimeout(None)
        except (AttributeError, OSError):
            pass
        Session(handler, docker, spec).run()
    except websocket.ProtocolError:
        _close(handler, 4400)
    except Refuse:
        _close(handler, 4403)
    except Exception as exc:
        # The class name only. No resource id, command or output.
        LOG.warning('exec session failed: %s', type(exc).__name__)
        _close(handler, 1011)


def serve(handler):
    """Upgrade GET /v1/exec. Any role may call this path."""
    handler.close_connection = True
    _cancel(handler)
    if handler.command != 'GET':
        handler.send_error(405)
        return
    if handler.request_version != 'HTTP/1.1':
        handler.send_error(400)
        return
    try:
        accept = websocket.handshake(handler.headers)
    except websocket.ProtocolError:
        handler.send_error(400)
        return
    try:
        _switch(handler, accept)
        _session(handler)
    except Exception:
        _close(handler, 1011)
