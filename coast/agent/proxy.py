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

"""Closed method/path and header allow-lists for the pinned driver."""

import http.client
import re
import socket
import threading
import time
from urllib import parse

from coast.common import engine_versions


ID = r'[A-Za-z0-9_-]{1,128}'
RULES = [
    ('GET', r'/(health|version)'),
    ('PATCH', r'/team'),
    ('GET|POST', r'/(projects|servers|security/keys)'),
    ('PATCH', r'/security/keys/' + ID),
    ('GET|DELETE', r'/(projects|servers|security/keys)/' + ID),
    ('GET|POST', r'/projects/' + ID + r'/environments'),
    ('DELETE', r'/projects/' + ID + r'/environments/' + ID),
    ('POST', r'/servers/' + ID + r'/(validate|proxy/restart)'),
    ('GET|POST', r'/servers/' + ID + r'/registries'),
    ('GET', r'/servers/' + ID + r'/resources'),
    ('PATCH', r'/servers/' + ID + r'/proxy'),
    ('PATCH', r'/servers/' + ID),
    ('GET', r'/databases'),
    ('GET', r'/databases/' + ID + r'/logs'),
    ('POST', r'/databases/(' + '|'.join(engine_versions.DATABASES) + ')'),
    ('GET|PATCH|DELETE', r'/databases/' + ID),
    ('POST', r'/databases/' + ID + r'/(start|stop|restart)'),
    ('GET', r'/applications'),
    ('POST', r'/applications/'
     r'(public|private-deploy-key|dockerimage|dockerfile)'),
    ('GET|PATCH|DELETE', r'/applications/' + ID),
    ('GET|POST|PATCH', r'/applications/' + ID + r'/(envs|storages)'),
    ('DELETE', r'/applications/' + ID + r'/(envs|storages)/' + ID),
    ('GET', r'/applications/' + ID + r'/logs'),
    ('POST', r'/applications/' + ID + r'/(start|stop|restart|rollback)'),
    # One-click (template) services: only the routes the v4 driver calls.
    ('GET|POST', r'/services'),
    ('GET|PATCH|DELETE', r'/services/' + ID),
    ('GET|POST', r'/services/' + ID + r'/storages'),
    ('DELETE', r'/services/' + ID + r'/storages/' + ID),
    ('GET', r'/services/' + ID + r'/envs'),
    ('GET', r'/services/' + ID + r'/logs'),
    ('PATCH', r'/services/' + ID + r'/envs/bulk'),
    ('DELETE', r'/services/' + ID + r'/envs/' + ID),
    ('POST', r'/services/' + ID + r'/(start|stop|restart)'),
    ('POST', r'/deploy'),
    ('GET', r'/deployments/' + ID),
    ('POST', r'/deployments/' + ID + r'/cancel'),
]
REQUEST_HEADERS = {'authorization', 'content-type', 'accept'}
RESPONSE_HEADERS = {'content-type'}
# A throttled engine names its own wait in whole seconds. Pass only that
# value on so the conductor waits for the engine's window instead of
# guessing and giving up while the engine is still throttling.
RETRY_AFTER = re.compile(r'[0-9]{1,4}')


class Denied(Exception):
    pass


def allowed(method, target):
    if (len(target) > 4096 or not target.startswith('/api/v1/') or
            any(c in target for c in ('%', '\\', '#', '\r', '\n', '\x00'))):
        return False
    url = parse.urlsplit(target)
    path = url.path.removeprefix('/api/v1')
    return any(re.fullmatch(verbs, method) and re.fullmatch(pattern, path)
               for verbs, pattern in RULES)


def forward(conf, method, target, headers, body):
    if not allowed(method, target):
        raise Denied()
    connection = http.client.HTTPConnection(
        '127.0.0.1', 8000, timeout=conf.agent.request_timeout)
    clean = {key: value for key, value in headers.items()
             if key.lower() in REQUEST_HEADERS}
    timer = None
    expired = threading.Event()
    deadline = time.monotonic() + conf.agent.request_timeout
    try:
        connection.connect()
        sock = connection.sock

        def expire():
            expired.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OSError('Engine request deadline exceeded')
        timer = threading.Timer(remaining, expire)
        timer.start()
        connection.request(method, target, body=body, headers=clean)
        response = connection.getresponse()
        # No redirects: neither credentials nor internal Location headers
        # can be handed to another host. Engine error bodies are discarded.
        if response.status >= 300:
            kept = {}
            if response.status == 429:
                wait = (response.getheader('Retry-After') or '').strip()
                if RETRY_AFTER.fullmatch(wait):
                    kept['Retry-After'] = wait
            return (response.status if response.status >= 400 else 502,
                    kept, b'')
        content = response.read(conf.agent.max_body_size + 1)
        if expired.is_set() or (response.length not in (None, 0)):
            raise OSError('Incomplete engine response')
        if len(content) > conf.agent.max_body_size:
            raise OSError('Engine response too large')
        result = {key: value for key, value in response.getheaders()
                  if key.lower() in RESPONSE_HEADERS}
        if any('\r' in value or '\n' in value for value in result.values()):
            raise OSError('Invalid engine response headers')
        return response.status, result, content
    finally:
        if timer:
            timer.cancel()
        connection.close()
