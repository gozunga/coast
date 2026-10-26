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

"""Per-stack private registry credentials and guest files (Q125)."""

import dataclasses
import ipaddress
import json
import re
import secrets

import bcrypt

from coast.api import secret_store
from coast.common import exception
from coast.common import tls


USERNAME = 'coast'
_REPO = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/'
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
CONFIG_YML = """version: 0.1
storage:
  filesystem:
    rootdirectory: /var/lib/registry
  delete:
    enabled: true
http:
  addr: :5000
  tls:
    certificate: /certs/tls.crt
    key: /certs/tls.key
auth:
  htpasswd:
    realm: Coast
    path: /auth/htpasswd
"""


def repository(stack_id, service_id):
    """Return the registry repository name for one service."""
    name = '%s/%s' % (stack_id, service_id)
    if not _REPO.fullmatch(name):
        raise exception.Invalid('Registry repository name is invalid')
    return name


def create_secret(context, identity):
    """Store the registry username and password. Return the Barbican ref."""
    password = secrets.token_urlsafe(32)
    payload = json.dumps({'username': USERNAME, 'password': password},
                         separators=(',', ':'))
    return secret_store.call('put', context, payload, identity)


def load_secret(context, reference):
    """Return the registry username and password. Raise on a bad payload."""
    raw = secret_store.call('get', context, reference)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise exception.Invalid('Registry secret is invalid') from None
    if (not isinstance(payload, dict) or
            set(payload) != {'username', 'password'} or
            payload.get('username') != USERNAME or
            not isinstance(payload.get('password'), str) or
            not payload['password'] or
            len(payload['password'].encode('utf-8')) > 72):
        raise exception.Invalid('Registry secret is invalid')
    return payload


def htpasswd(username, password):
    """Return one bcrypt htpasswd line.

    The distribution registry accepts bcrypt hashes only.
    """
    if username != USERNAME or not isinstance(password, str):
        raise exception.Invalid('Registry secret is invalid')
    hashed = bcrypt.hashpw(password.encode('utf-8'),
                           bcrypt.gensalt(rounds=12))
    text = hashed.decode('ascii')
    if not text.startswith(('$2a$', '$2b$', '$2y$')):
        raise exception.Invalid('Registry hash is invalid')
    return '%s:%s\n' % (username, text)


def files_for(conf, stack, server, controller_ip):
    """Return extra coast.conf text and config-drive files.

    Every guest receives the server CA for the registry address. The
    controller also receives the registry config, bcrypt htpasswd, and
    the registry TLS key. The plaintext password stays in Barbican.
    """
    if not getattr(stack, 'registry_secret_ref', None):
        return '', {}
    host = str(ipaddress.IPv4Address(controller_ip))
    port = conf.controller_worker.registry_port
    with open(conf.certificates.ca_certificate) as stream:
        ca_pem = stream.read()
    if not ca_pem.strip():
        raise exception.Invalid('Registry CA is required')
    files = {'/etc/docker/certs.d/%s:%s/ca.crt' % (host, port): ca_pem}
    if server.role != 'control':
        return '', files
    payload = load_secret(stack._context, stack.registry_secret_ref)
    cert = tls.issue_certificate(server.id, ip_addresses=(host,), conf=conf)
    files.update({
        '/etc/coast/registry/config.yml': CONFIG_YML,
        '/etc/coast/registry/htpasswd': htpasswd(
            payload['username'], payload['password']),
        '/etc/coast/registry/tls.crt': cert.certificate_pem.decode('ascii'),
        '/etc/coast/registry/tls.key': cert.private_key_pem.decode('ascii'),
    })
    image = conf.controller_worker.registry_image
    extra = '\n[registry]\naddress=%s\nport=%s\nimage=%s\n' % (
        host, port, image)
    return extra, files


def attach(conf, endpoint, stack, controller):
    """Add the per-stack template and login fields when the secret exists."""
    if (not getattr(stack, 'registry_secret_ref', None) or
            not getattr(controller, 'private_ip', None)):
        return endpoint
    payload = load_secret(stack._context, stack.registry_secret_ref)
    port = conf.controller_worker.registry_port
    host = '%s:%s' % (controller.private_ip, port)
    return dataclasses.replace(
        endpoint,
        registry_image_template='%s/{environment_id}/{app_id}' % host,
        registry_host=host,
        registry_username=payload['username'],
        registry_password=payload['password'])
