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

"""Octavia-style HMAC-SHA256 authenticated UDP heartbeats."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time

from coast.agent import namespace


def encode(payload, key):
    message = json.dumps(payload, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    # Octavia-inspired envelope: JSON followed by a binary digest. No
    # compression is needed for this bounded payload.
    return message + hmac.new(key, message, hashlib.sha256).digest()


def docker_ok():
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.connect('/var/run/docker.sock')
        sock.sendall(b'GET /_ping HTTP/1.0\r\nHost: localhost\r\n\r\n')
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status == 200 and response.read(16).strip() == b'OK'
    except (OSError, http.client.HTTPException):
        return False
    finally:
        sock.close()


def engine_ok():
    connection = http.client.HTTPConnection('127.0.0.1', 8000, timeout=2)
    try:
        connection.request('GET', '/api/v1/health')
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class Sender:
    def __init__(self, conf):
        self.conf = conf
        self.lock = threading.Lock()
        self.key = bytes.fromhex(conf.agent.heartbeat_key)
        if len(self.key) != 32:
            raise ValueError('Heartbeat key must contain 32 bytes')
        self.targets = [(str(ipaddress.IPv4Address(address)),
                         conf.agent.heartbeat_port)
                        for address in conf.agent.health_manager_ips]
        self.sequence_path = Path(conf.agent.state_path) / 'heartbeat-seq'
        try:
            fd = os.open(self.sequence_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                previous = int(stream.read(64))
        except FileNotFoundError:
            previous = 0
        self.sequence = max(previous, time.time_ns())
        self.socket = namespace.management_socket(conf, socket.SOCK_DGRAM, 0)
        self.socket.settimeout(conf.agent.request_timeout)

    def send(self, metric=None, sampled_at=None):
        with self.lock:
            self._send(metric, sampled_at)

    def _send(self, metric, sampled_at):
        self.sequence += 1
        # Persist before sending. Restarts and clock rollback cannot replay
        # a sequence that the health-manager has already accepted.
        with tempfile.NamedTemporaryFile(dir=self.sequence_path.parent,
                                         mode='w', delete=False) as stream:
            temporary = stream.name
            try:
                stream.write(str(self.sequence))
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, self.sequence_path)
                fd = os.open(self.sequence_path.parent,
                             os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        disk = shutil.disk_usage('/data')
        payload = {'server_id': self.conf.agent.server_id,
                   'env_id': self.conf.agent.stack_id,
                   'seq': self.sequence, 'ts': time.time(),
                   'docker_ok': docker_ok(),
                   'disk_pct': round(disk.used * 100 / disk.total, 2),
                   'engine_ok': (engine_ok() if
                                 self.conf.agent.role == 'controller'
                                 else False)}
        if metric is not None:
            payload['metric'] = metric
            payload['ts'] = sampled_at
        message = encode(payload, self.key)
        for target in self.targets:
            try:
                self.socket.sendto(message, target)
            except OSError:
                # A lost UDP packet must not stop other destinations.
                continue
