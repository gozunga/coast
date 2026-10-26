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

"""Bounded conductor-to-agent mTLS calls in the management namespace."""

import errno
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time

from cryptography import x509
from oslo_log import log as logging
import requests

from coast.agent import certificates
from coast.agent import namespace
from coast.common import exception
from coast.common import management
from coast.drivers import base


LOG = logging.getLogger(__name__)


def _failure_reason(error):
    if isinstance(error, requests.exceptions.SSLError):
        return 'TLS'
    if isinstance(error, requests.exceptions.Timeout):
        return 'timeout'
    # Requests/urllib3 wrap socket errors. Inspect types, never messages.
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if (isinstance(current, OSError) and
                current.errno == errno.ECONNREFUSED):
            return 'connect refused'
        pending.extend(current.args)
        pending.extend((current.__cause__, getattr(current, 'reason', None)))
    if isinstance(error, requests.exceptions.ConnectionError):
        return 'connection error'
    if isinstance(error, ValueError):
        return 'non-ok body'
    return 'request error'


def endpoint(conf, address, token=None):
    address = str(ipaddress.IPv4Address(address))
    options = conf.controller_worker
    if not all((options.agent_client_cert, options.agent_client_key,
                conf.certificates.ca_certificate)):
        raise base.DriverError('Agent mTLS configuration is incomplete')
    return base.EngineEndpoint(
        'https://%s:%s/api/v1' % (address, options.agent_port),
        options.agent_client_cert, options.agent_client_key,
        conf.certificates.ca_certificate, token=token,
        timeout=conf.coolify.api_timeout)


class AgentClient:
    def __init__(self, conf, address):
        self.conf = conf
        self.address = str(ipaddress.IPv4Address(address))
        self.ep = endpoint(conf, address)

    def request(self, method, path, body=None):
        # No ambient proxies, redirects, error bodies or POST retries.
        try:
            with requests.Session() as session:
                session.trust_env = False
                with namespace.control_management_namespace(
                        self.conf.controller_worker.mgmt_netns):
                    response = session.request(
                        method,
                        self.ep.base_url.removesuffix('/api/v1') + path,
                        json=body,
                        cert=(self.ep.client_cert, self.ep.client_key),
                        verify=self.ep.ca_cert, allow_redirects=False,
                        timeout=(self.conf.export.archive_timeout if path in (
                            '/v1/backup_database', '/v1/restore_database') else
                            self.conf.controller_worker.build_timeout
                            if method == 'POST' else
                            self.conf.coolify.api_timeout), stream=True)
                    with response:
                        if response.status_code != 200:
                            error_class = (base.EngineUnreachable if
                                           response.status_code in
                                           (502, 503, 504) else
                                           base.DriverError)
                            error = error_class('Agent request failed')
                            error.reason = ('HTTP status %d' %
                                            response.status_code)
                            raise error
                        content = bytearray()
                        for chunk in response.iter_content(65536):
                            content.extend(chunk)
                            if len(content) > self.conf.agent.max_body_size:
                                error = base.DriverError(
                                    'Agent response too large')
                                error.reason = 'non-ok body'
                                raise error
                        return json.loads(content)
        except (requests.RequestException, ValueError, OSError) as exc:
            error = base.EngineUnreachable('Agent request failed')
            error.reason = _failure_reason(exc)
            raise error from None

    def certificate_connection(self, server_id, body=None):
        """Use a fresh authenticated connection, never a pooled TLS session."""
        context = ssl.create_default_context(cafile=self.ep.ca_cert)
        context.load_cert_chain(self.ep.client_cert, self.ep.client_key)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(
            self.address, self.conf.controller_worker.agent_port,
            timeout=self.conf.agent.request_timeout, context=context)
        sock = None

        def expire():
            active = connection.sock or sock
            if active:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                active.close()

        deadline = threading.Timer(self.conf.agent.request_timeout, expire)
        deadline.start()
        try:
            sock = management.socket_for_agent(self.conf)
            sock.settimeout(self.conf.agent.request_timeout)
            sock.connect((self.address,
                          self.conf.controller_worker.agent_port))
            connection.sock = context.wrap_socket(
                sock, server_hostname=self.address)
            cert = x509.load_der_x509_certificate(
                connection.sock.getpeercert(binary_form=True))
            with open(self.ep.ca_cert, 'rb') as stream:
                ca = x509.load_pem_x509_certificate(stream.read())
            certificates.validate(cert, None, ca, server_id, self.address)
            if body is None:
                return cert
            connection.request('POST', '/v1/certificates/rotate',
                               body=json.dumps(body),
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            content = response.read(self.conf.agent.max_body_size + 1)
            if (response.status != 200 or
                    len(content) > self.conf.agent.max_body_size):
                raise ValueError('Certificate update failed')
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError('Certificate response invalid')
            return result
        except Exception:
            raise base.EngineUnreachable('Certificate connection failed') \
                from None
        finally:
            deadline.cancel()
            connection.close()
            if sock:
                sock.close()

    def wait_health(self):
        started = time.monotonic()
        deadline = started + self.conf.controller_worker.build_timeout
        last_log = None
        while True:
            try:
                result = self.request('GET', '/health')
                reason = 'non-ok body'
                if isinstance(result, dict) and result.get('status') == 'ok':
                    egress = result.get('egress')
                    if isinstance(egress, dict):
                        if egress.get('status') == 'error':
                            raise exception.GuestEgressError()
                        if egress.get('status') == 'ok':
                            return
                        if egress.get('status') == 'pending':
                            reason = 'egress check pending'
            except base.DriverError as exc:
                reason = getattr(exc, 'reason', 'request error')
            now = time.monotonic()
            if last_log is None or now - last_log >= 60:
                LOG.info('Waiting for agent health at %(address)s: '
                         '%(elapsed)d seconds elapsed; '
                         'last failure: %(reason)s',
                         {'address': self.address, 'elapsed': now - started,
                          'reason': reason})
                last_log = now
            if now >= deadline:
                raise base.EngineUnreachable(
                    'Agent health deadline exceeded: %s' % reason)
            time.sleep(self.conf.controller_worker.build_poll_interval)

    def bootstrap(self, version):
        result = self.request('POST', '/v1/bootstrap',
                              {'engine_version': version})
        if (not isinstance(result, dict) or
                result.get('engine_version') != version or
                any(not isinstance(result.get(key), str) or not result[key]
                    for key in ('token',))):
            raise base.DriverError('Agent bootstrap response is invalid')
        return result

    def rotate_token(self, version, identifier, previous_name):
        result = self.request('POST', '/v1/bootstrap/rotate-token', {
            'engine_version': version, 'rotation_id': identifier,
            'previous_name': previous_name, 'phase': 'create'})
        if (not isinstance(result, dict) or
                result.get('engine_version') != version or
                not isinstance(result.get('token'), str) or
                not result['token']):
            raise base.DriverError('Agent token response is invalid')
        return result['token']

    def revoke_token(self, version, identifier, previous_name):
        result = self.request('POST', '/v1/bootstrap/rotate-token', {
            'engine_version': version, 'rotation_id': identifier,
            'previous_name': previous_name, 'phase': 'revoke'})
        if result != {'status': 'ok'}:
            raise base.DriverError('Agent token revocation is unconfirmed')
