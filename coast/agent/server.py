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

"""Bounded standard-library HTTPS service; no guest RPC transport."""

import http.client
from http import server
import ipaddress
import json
import socket
import ssl
import threading
import uuid

from oslo_service import service

from coast.agent import backup
from coast.agent import certificates
from coast.agent import containers
from coast.agent import docker_exec
from coast.agent import egress
from coast.agent import engine_upgrade
from coast.agent.engines import coolify
from coast.agent import export
from coast.agent import export_archive
from coast.agent import heartbeat
from coast.agent import metrics
from coast.agent import namespace
from coast.agent import proxy
from coast.agent import registry as registry_gc


def tls_context(conf):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=conf.agent.client_ca)
    context.load_cert_chain(conf.agent.server_cert, conf.agent.server_key)
    return context


class Handler(server.BaseHTTPRequestHandler):
    server_version = 'Coast-agent'
    sys_version = ''

    def log_message(self, *args):
        # Paths, headers and error text can contain tenant credentials.
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {}, b'')

    def reply(self, status, headers, content):
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() in ('content-type', 'retry-after'):
                self.send_header(key, value)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(content)
        self.close_connection = True

    def handle(self):
        self.deadline = threading.Timer(self.server.conf.agent.request_timeout,
                                        self.expire)
        self.deadline.start()
        try:
            super().handle()
        finally:
            self.deadline.cancel()

    def expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _health(self):
        """Reply to the health probe. Return True when this path matches."""
        if (self.path, self.command) != ('/health', 'GET'):
            return False
        result = {'status': 'ok', 'egress': self.server.egress.result}
        self.reply(200, {'Content-Type': 'application/json'},
                   json.dumps(result).encode('utf-8'))
        return True

    def _registry_gc(self, conf, body):
        """Collect registry blobs. Return True when this path matches."""
        if self.path != '/v1/registry/gc':
            return False
        if conf.agent.role != 'controller':
            self.send_error(404)
        elif self.command != 'POST':
            self.send_error(405)
        else:
            result = registry_gc.garbage_collect(json.loads(body))
            self.reply(200, {'Content-Type': 'application/json'},
                       json.dumps(result).encode('utf-8'))
        return True

    def _engine_upgrade(self, conf, body):
        if self.path not in ('/v1/engine/quiesce', '/v1/engine/resume'):
            return False
        if conf.agent.role != 'controller':
            self.send_error(404)
        elif self.command != 'POST':
            self.send_error(405)
        else:
            operation = (self.server.upgrades.quiesce if
                         self.path.endswith('/quiesce') else
                         self.server.upgrades.resume)
            result = operation(json.loads(body))
            self.reply(200, {'Content-Type': 'application/json'},
                       json.dumps(result).encode('utf-8'))
        return True

    def _export(self, conf, body):
        """Run an export route. Return True when this path matches."""
        export_paths = ('/v1/export/quiesce', '/v1/export/resume',
                        '/v1/export')
        if self.path not in export_paths:
            return False
        if conf.agent.role not in export.OWNER_ROLES:
            self.send_error(404)
        elif self.command != 'POST':
            self.send_error(405)
        elif self.path == '/v1/export':
            self.deadline.cancel()
            timeout = conf.export.archive_timeout
            self.connection.settimeout(timeout)
            self.deadline = threading.Timer(timeout, self.expire)
            self.deadline.start()
            result = self.server.archives.archive(json.loads(body))
            self.reply(200, {'Content-Type': 'application/json'},
                       json.dumps(result).encode('utf-8'))
        else:
            method = (self.server.exports.quiesce if
                      self.path == export_paths[0] else
                      self.server.exports.resume)
            result = method(json.loads(body))
            self.reply(200, {'Content-Type': 'application/json'},
                       json.dumps(result).encode('utf-8'))
        return True

    def _database(self, conf, body):
        paths = {'/v1/backup_database': False, '/v1/restore_database': True}
        if self.path not in paths:
            return False
        if conf.agent.role != 'worker':
            self.send_error(404)
        elif self.command != 'POST':
            self.send_error(405)
        else:
            self.deadline.cancel()
            timeout = conf.export.archive_timeout
            self.connection.settimeout(timeout)
            self.deadline = threading.Timer(timeout, self.expire)
            self.deadline.start()
            try:
                result = self.server.databases.run(
                    json.loads(body), restore=paths[self.path])
                status = 200
            except backup.ERROR as error:
                result = {'error': str(error)}
                status = 412
            self.reply(status, {'Content-Type': 'application/json'},
                       json.dumps(result).encode('utf-8'))
        return True

    def dispatch(self):
        conf = self.server.conf
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if (len(lengths) > 1 or 'Transfer-Encoding' in self.headers or
                    any(len(self.headers.get_all(key, [])) > 1
                        for key in proxy.REQUEST_HEADERS)):
                return self.send_error(400)
            size = int(lengths[0]) if lengths else 0
            if size < 0 or size > conf.agent.max_body_size:
                return self.send_error(413)
            body = self.rfile.read(size)
            if len(body) != size:
                return self.send_error(400)
            if (self._health() or self._registry_gc(conf, body) or
                    self._engine_upgrade(conf, body) or
                    self._export(conf, body) or
                    self._database(conf, body)):
                return
            if self.path == '/v1/certificates/rotate':
                if self.command != 'POST':
                    return self.send_error(405)
                result = self.server.certificates.rotate(
                    json.loads(body), self.server, tls_context)
                return self.reply(200, {'Content-Type': 'application/json'},
                                  json.dumps(result).encode('utf-8'))
            bootstrap_paths = ('/v1/bootstrap', '/v1/bootstrap/rotate-token')
            if self.path in bootstrap_paths:
                if self.command != 'POST':
                    return self.send_error(405)
                self.deadline.cancel()
                self.deadline = threading.Timer(conf.agent.bootstrap_timeout,
                                                self.expire)
                self.deadline.start()
                result = self.server.engine.bootstrap(
                    json.loads(body), rotate=self.path == bootstrap_paths[1])
                return self.reply(200, {'Content-Type': 'application/json'},
                                  json.dumps(result).encode('utf-8'))
            if self.path == '/v1/containers':
                if self.command != 'GET' or conf.agent.role != 'worker':
                    return self.send_error(404)
                result = containers.read(conf, json.loads(body))
                return self.reply(200, {'Content-Type': 'application/json'},
                                  json.dumps(result).encode('utf-8'))
            if self.path == '/v1/exec':
                return docker_exec.serve(self)
            if conf.agent.role != 'controller':
                return self.send_error(404)
            status, headers, content = proxy.forward(
                conf, self.command, self.path, self.headers, body)
            self.reply(status, headers, content)
        except proxy.Denied:
            self.send_error(404)
        except coolify.Conflict:
            self.send_error(409)
        except certificates.MissingServerCA:
            self.reply(412, {'Content-Type': 'application/json'},
                       b'{"error":"Configure [agent] server_ca before '
                       b'certificate rotation."}')
        except coolify.PreconditionFailed:
            self.precondition_failed()
        except (ValueError, UnicodeError):
            self.send_error(400)
        except (OSError, http.client.HTTPException):
            self.send_error(502)

    def precondition_failed(self):
        if self.path == '/v1/certificates/rotate':
            return self.reply(412, {'Content-Type': 'application/json'},
                              b'{"error":"Certificate install requires '
                              b'operator recovery."}')
        if self.path == '/v1/export':
            return self.reply(412, {'Content-Type': 'application/json'},
                              b'{"error":"Export archive operation '
                              b'failed; '
                              b'retry the same export or recover '
                              b'the clone."}')
        if self.path.startswith('/v1/export/'):
            return self.reply(412, {'Content-Type': 'application/json'},
                              b'{"error":"Export consistency operation '
                              b'failed; pending stop records are retained '
                              b'for recovery."}')
        self.reply(412, {'Content-Type': 'application/json'},
                   b'{"error":"Bootstrap precondition failed; verify '
                   b'pinned engine and loopback API access. '
                   b'Incomplete bootstrap requires operator recovery."}')

    do_GET = dispatch
    do_POST = dispatch
    do_PATCH = dispatch
    do_DELETE = dispatch
    do_PUT = dispatch


class Server(server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, conf, sock, engine):
        # Use only the explicit management-address listener.
        self.conf = conf
        self.engine = engine
        self.egress = egress.Check(conf)
        self.exports = export.Quiesce(conf)
        self.upgrades = engine_upgrade.Quiesce(conf)
        self.archives = export_archive.Archive(conf)
        self.databases = backup.Runner(timeout=conf.export.archive_timeout)
        self.certificates = certificates.Rotation(conf)
        self.context = tls_context(conf)
        self.slots = threading.BoundedSemaphore(conf.agent.max_connections)
        super().__init__(sock.getsockname(), Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(self.conf.agent.request_timeout)
            request = self.context.wrap_socket(request, server_side=True)
            self.finish_request(request, address)
        except Exception:
            # No SSL, parser or application exception text may reach logs.
            pass
        finally:
            self.shutdown_request(request)
            self.slots.release()


class Service(service.Service):
    def __init__(self, conf):
        super().__init__()
        self.conf = conf
        self.server = None
        self.sender = None
        self.thread = None

    def start(self):
        super().start()
        conf = self.conf
        # Fail closed: never bind a wildcard, loopback or guessed address.
        address = ipaddress.IPv4Address(conf.agent.bind_ip)
        if (address.is_unspecified or address.is_loopback
                or address.is_multicast):
            raise ValueError('A management NIC address is required')
        uuid.UUID(conf.agent.server_id)
        uuid.UUID(conf.agent.stack_id)
        if not conf.agent.health_manager_ips:
            raise ValueError('Health-manager addresses are required')
        sock = None
        try:
            engine = coolify.Engine(conf)
            sock = namespace.management_socket(conf, socket.SOCK_STREAM,
                                               conf.agent.bind_port)
            self.server = Server(conf, sock, engine)
            self.server.exports.recover()
            self.tg.add_timer_args(1, self.server.exports.recover)
            self.sender = heartbeat.Sender(conf)
            if conf.agent.role == 'worker':
                self.tg.add_timer_args(
                    conf.agent.metrics_interval,
                    lambda: metrics.collect(self.sender))
            threading.Thread(target=self.server.egress.run,
                             daemon=True).start()
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           daemon=True)
            self.thread.start()
            self.tg.add_timer_args(conf.agent.heartbeat_interval,
                                   self.sender.send)
        except Exception:
            if sock:
                sock.close()
            self.stop()
            raise ValueError(
                'Agent startup failed; verify configuration') from None

    def stop(self, graceful=False):
        self.tg.stop_timers()
        self.tg.wait()
        if self.thread:
            self.server.shutdown()
            self.thread.join()
        if self.server:
            self.server.exports.recover(force=True)
            self.server.server_close()
        if self.sender:
            self.sender.socket.close()
        super().stop(graceful=graceful)
