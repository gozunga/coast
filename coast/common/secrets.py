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

"""Service-project secret storage and stack credential helpers.

No payloads are logged. Callers must checkpoint returned references and use
those references on retry; these helpers do not write Coast database rows.
"""

import base64
import hashlib
import secrets
from urllib import parse
import uuid

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from openstack import exceptions as sdk_exceptions
from oslo_config import cfg

from coast.api import secret_store
from coast.common import clients
from coast.common import exception
from coast.common import secret_limits


def _secret_id(reference, endpoint):
    # Never pass a caller-controlled URL to the SDK. Use its catalog endpoint.
    if not isinstance(reference, str) or not reference:
        raise ValueError('Invalid secret reference')
    url = parse.urlsplit(reference)
    catalog = parse.urlsplit(endpoint)
    default_ports = {'http': 80, 'https': 443}
    if (catalog.scheme not in default_ports or not catalog.hostname or
            url.scheme != catalog.scheme or url.hostname != catalog.hostname or
            (url.port if url.port is not None else
             default_ports[url.scheme]) !=
            (catalog.port if catalog.port is not None else
             default_ports[catalog.scheme]) or
            url.username is not None or url.password is not None or
            '?' in reference or '#' in reference):
        raise ValueError('Invalid secret reference')
    base = catalog.path.rstrip('/')
    if not base.endswith('/v1'):
        base += '/v1'
    prefix, separator, identifier = url.path.rpartition('/secrets/')
    if not separator or prefix != base:
        raise ValueError('Invalid secret reference')
    return str(uuid.UUID(identifier))


class BarbicanSecretStore(secret_store.SecretStore):
    def __init__(self, conf=None):
        self.conf = cfg.CONF if conf is None else conf

    def _call(self, operation, value, identity=None):
        try:
            # Tenant context never selects credentials or the Barbican project.
            proxy = clients.get_sensitive_proxy('barbican', self.conf)
            if operation == 'put':
                if (not isinstance(value, str) or
                        not isinstance(identity, secret_store.SecretIdentity)):
                    raise ValueError('Text payload required')
                result = proxy.create_secret(
                    payload=base64.b64encode(
                        value.encode('utf-8')).decode('ascii'),
                    payload_content_type='application/octet-stream',
                    payload_content_encoding='base64',
                    secret_type='opaque', name=identity.name)
                try:
                    endpoint = proxy.get_endpoint().rstrip('/')
                    identifier = _secret_id(result.secret_ref, endpoint)
                    if not endpoint.endswith('/v1'):
                        endpoint += '/v1'
                    url = endpoint + '/secrets/' + identifier
                    user_id = clients.get_session(self.conf).get_user_id()
                    if not isinstance(user_id, str) or not user_id:
                        raise ValueError('Service user is required')
                    for suffix, body in (
                            ('acl', {'read': {'project-access': False,
                                              'users': [user_id]}}),
                            ('metadata', {'metadata': identity.metadata})):
                        response = proxy.put(url + '/' + suffix, json=body)
                        sdk_exceptions.raise_from_response(response)
                except Exception:
                    try:
                        # SDK Secret.id is a URL; secret_id is its UUID.
                        identifier = str(uuid.UUID(result.secret_id))
                        proxy.delete_secret(identifier, ignore_missing=True)
                    except Exception:
                        # Cleanup is best effort; never expose SDK errors.
                        pass
                    raise
                return result.secret_ref
            identifier = _secret_id(value, proxy.get_endpoint())
            if operation == 'get':
                payload = proxy.get_secret(identifier).payload
                if isinstance(payload, bytes):
                    return payload.decode('utf-8')
                if not isinstance(payload, str):
                    raise ValueError('Text payload required')
                return payload
            proxy.delete_secret(identifier, ignore_missing=True)
        except Exception:
            # SDK errors can contain payloads or authentication credentials.
            raise exception.CoastException(
                'Secret storage operation failed.') from None

    def put(self, context, value, identity: secret_store.SecretIdentity):
        return self._call('put', value, identity)

    def get(self, context, reference):
        return self._call('get', reference)

    def delete(self, context, reference):
        if reference is None or reference == '':
            return
        return self._call('delete', reference)


def create_ssh_keypair(context, identity):
    """Return private-key ref and the non-secret OpenSSH public key."""
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption()).decode('utf-8')
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH).decode('utf-8')
    return {'ssh_key_ref': secret_store.call(
                'put', context, private, identity),
            'ssh_public_key': public}


def create_hmac_key(context, identity):
    """Store 32 random bytes as hex text; consumers decode with bytes.fromhex.

    Return the reference for the provisioning flow's checkpoint. No model
    field or implicit name-based discovery is introduced here.
    """
    return secret_store.call('put', context, secrets.token_hex(32), identity)


def store_engine_token(context, token, identity):
    """Store the token returned over mTLS; never generate an engine token."""
    if not isinstance(token, str) or not token:
        raise exception.Invalid('Engine token is required.')
    return secret_store.call('put', context, token, identity)


def parse_deploy_key(value):
    """Normalize a supported key without exposing parser errors."""
    try:
        secret_limits.validate(value, 'deploy_key.private_key')
        raw = value.encode('utf-8')
        loader = (serialization.load_ssh_private_key
                  if raw.startswith(b'-----BEGIN OPENSSH PRIVATE KEY-----')
                  else serialization.load_pem_private_key)
        key = loader(raw, password=None)
        if not isinstance(key, (ed25519.Ed25519PrivateKey,
                                ec.EllipticCurvePrivateKey,
                                rsa.RSAPrivateKey)):
            raise ValueError()
        if isinstance(key, rsa.RSAPrivateKey) and key.key_size < 2048:
            raise ValueError()
        private = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption()).decode('utf-8')
        public = key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH).decode('utf-8')
        secret_limits.validate(private, 'deploy_key.private_key')
    except (ValueError, TypeError, UnsupportedAlgorithm, UnicodeError,
            exception.Invalid):
        raise exception.Invalid(
            'deploy_key.private_key: not a supported unencrypted private key'
        ) from None
    return private, public


def key_fingerprint(public):
    blob = base64.b64decode(public.split()[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest())
    return 'SHA256:' + digest.decode('ascii').rstrip('=')


def store_deploy_key(ctx, resource, options):
    identity = secret_store.SecretIdentity.for_resource(resource, 'deploy-key')
    if options.get('generate') is True:
        result = create_ssh_keypair(ctx, identity)
        return result['ssh_key_ref'], result['ssh_public_key']
    private, public = parse_deploy_key(options['private_key'])
    return secret_store.call('put', ctx, private, identity), public
