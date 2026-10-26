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

"""Snapshot consistency tasks using the approved Q91 agent contract."""

from collections import deque
import datetime
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging as std_logging
import math
import os
import re
import ssl
import time
from urllib import parse

from botocore.config import Config
import botocore.session
from openstack import connection
from openstack import exceptions as sdk_exceptions

from oslo_log import log as logging
from taskflow import engines
from taskflow.patterns import linear_flow
from taskflow import task

from coast.api import secret_store
from coast.common import clients
from coast.common import context as context_module
from coast.common import exception
from coast.common import management
from coast.common import naming
from coast.common import states
from coast.conductor import rpcapi
from coast.db import api as db_api
from coast.db import models
from coast import objects


LOG = logging.getLogger(__name__)


class AgentClient:
    def __init__(self, conf):
        self.conf = conf

    def post(self, server, path, body):
        if (path not in ('/v1/export/quiesce', '/v1/export/resume',
                         '/v1/export') or
                server.role not in ('control', 'worker', 'build') or
                not self.conf.export.server_ca or
                not self.conf.export.client_certificate or
                not self.conf.export.client_key):
            raise exception.CoastException(
                'Export agent TLS is not configured.')
        ip = ipaddress.IPv4Address(server.mgmt_ip)
        if ip.is_unspecified or ip.is_loopback or ip.is_multicast:
            raise exception.CoastException('Export agent address is invalid.')
        address = str(ip)
        context = ssl.create_default_context(cafile=self.conf.export.server_ca)
        context.load_cert_chain(self.conf.export.client_certificate,
                                self.conf.export.client_key)
        timeout = (self.conf.export.archive_timeout if path == '/v1/export'
                   else self.conf.agent.export_auto_resume_timeout)
        connection = http.client.HTTPSConnection(
            address, self.conf.controller_worker.agent_port,
            timeout=timeout,
            context=context)
        sock = None
        try:
            sock = management.socket_for_agent(self.conf)
            sock.settimeout(timeout)
            sock.connect((address, self.conf.controller_worker.agent_port))
            connection.sock = context.wrap_socket(
                sock, server_hostname=address)
            connection.request('POST', path, body=json.dumps(body),
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            content = response.read(self.conf.agent.max_body_size + 1)
            if (response.status != 200 or
                    len(content) > self.conf.agent.max_body_size):
                raise exception.CoastException('Export agent request failed.')
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError('Invalid export response')
            return result
        except Exception:
            raise exception.CoastException('Export agent request failed.') \
                from None
        finally:
            connection.close()
            if sock:
                sock.close()


class Quiesce(task.Task):
    default_provides = 'quiesce_deadline'

    def __init__(self, agent, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent

    def execute(self, servers, export_id, volume_ids):
        # Every owner is quiesced before this task returns. Snapshots follow.
        deadlines = []
        for server in servers:
            response = self.agent.post(server, '/v1/export/quiesce', {
                'export_id': export_id, 'volume_ids': volume_ids})
            deadline = response.get('deadline')
            if (response.get('status') != 'quiesced' or
                    isinstance(deadline, bool) or
                    not isinstance(deadline, (int, float)) or
                    not math.isfinite(deadline) or deadline <= time.time()):
                raise exception.CoastException('Export quiesce failed.')
            deadlines.append(deadline)
        return min(deadlines)

    def revert(self, servers, export_id, **kwargs):
        # taskflow also calls revert when execute fails with unknown outcome.
        resume_on_revert(self.agent, servers, export_id)


def resume_each(agent, servers, export_id):
    """Resume every owner. One failure must not skip the other owners."""
    failed = False
    for server in servers:
        try:
            resume(agent, server, export_id)
        except Exception:
            failed = True
            LOG.warning('Export resume requires another attempt.')
    return failed


def resume_on_revert(agent, servers, export_id):
    # Keep reverting other resources. The durable guest watchdog retries.
    resume_each(agent, servers, export_id)


def resume(agent, server, export_id):
    response = agent.post(server, '/v1/export/resume', {
        'export_id': export_id})
    if response.get('status') != 'resumed':
        raise exception.CoastException('Export resume failed.')


class Snapshots(task.Task):
    default_provides = 'export_snapshots'

    def __init__(self, block_storage, timeout, agent, conf, **kwargs):
        super().__init__(**kwargs)
        self.block_storage = block_storage
        self.conf = conf
        self.agent = agent
        self.timeout = timeout
        self.created = []

    def execute(self, volumes, export_id, project_id, stack_id,
                quiesce_deadline):
        for volume in volumes:
            remaining = quiesce_deadline - time.time()
            if remaining <= 0:
                raise exception.CoastException('Export quiesce lease expired.')
            name = naming.resource_name(
                'export', export_id, volume.id, conf=self.conf)
            metadata = {'coast': 'true', 'project_id': project_id,
                        'stack_id': stack_id,
                        'export_id': export_id, 'volume_id': volume.id}
            matches = [snapshot for snapshot in
                       self.block_storage.snapshots(details=True)
                       if snapshot.name == name]
            if (len(matches) > 1 or any(
                    snapshot.volume_id != volume.cinder_volume_id or
                    any((snapshot.metadata or {}).get(key) != value
                        for key, value in metadata.items())
                    for snapshot in matches)):
                raise exception.CoastException(
                    'Export snapshot ownership is ambiguous.')
            snapshot = matches[0] if matches else (
                self.block_storage.create_snapshot(
                    volume_id=volume.cinder_volume_id, force=True,
                    name=name, metadata=metadata))
            self.created.append(snapshot)
            self.block_storage.wait_for_status(
                snapshot, status='available', failures=['error'], interval=1,
                wait=max(1, int(min(self.timeout, remaining))))
        if time.time() >= quiesce_deadline:
            raise exception.CoastException('Export quiesce lease expired.')
        return list(self.created)

    def revert(self, servers, export_id, **kwargs):
        # Resume before cloud cleanup, which can be slow or unavailable.
        resume_on_revert(self.agent, servers, export_id)
        for snapshot in reversed(self.created):
            try:
                self.block_storage.delete_snapshot(snapshot,
                                                   ignore_missing=True)
            except Exception:
                LOG.warning('Export snapshot cleanup requires recovery.')


class Resume(task.Task):
    def __init__(self, agent, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent

    def execute(self, servers, export_id, export_snapshots):
        if resume_each(self.agent, servers, export_id):
            raise exception.CoastException('Export resume failed.')

    def revert(self, servers, export_id, **kwargs):
        resume_on_revert(self.agent, servers, export_id)


def _servers(servers):
    if isinstance(servers, (str, bytes)) or servers is None:
        return []
    if not isinstance(servers, (list, tuple)):
        return [servers]
    return list(servers)


def _selection_ok(servers, export, volumes):
    if (not servers or
            len({server.id for server in servers}) != len(servers)):
        return False
    for server in servers:
        if (server.role not in ('control', 'worker', 'build') or
                server.project_id != export.project_id or
                server.stack_id != export.stack_id):
            return False
    if (not volumes or
            len({volume.id for volume in volumes}) != len(volumes) or
            {volume.id for volume in volumes} != set(export.volume_ids)):
        return False
    server_ids = {server.id for server in servers}
    single = servers[0].id if len(servers) == 1 else None
    used = set()
    for volume in volumes:
        if (volume.project_id != export.project_id or
                volume.stack_id != export.stack_id or
                volume.purpose not in ('data', 'docker') or
                not volume.cinder_volume_id):
            return False
        owner = volume.server_id if volume.server_id is not None else single
        if owner not in server_ids:
            return False
        used.add(owner)
    return used == server_ids


def snapshot_volumes(conf, servers, export, volumes, agent=None,
                     block_storage=None):
    """Resume every owner on success and on every revert.

    Cloud requests run outside a DB transaction. The caller holds the
    existing stack RECONCILING task, not a DB row lock. Quiesce
    every server that owns a selected volume before the first snapshot.
    """
    servers = _servers(servers)
    if not _selection_ok(servers, export, volumes):
        raise exception.CoastException('Export snapshot selection is invalid.')
    servers = sorted(servers, key=lambda server: server.id)
    agent = agent or AgentClient(conf)
    if block_storage is None:
        block_storage = clients.get_connection(
            'cinder', conf).block_storage
    flow = linear_flow.Flow('export-snapshots').add(
        Quiesce(agent),
        Snapshots(block_storage, conf.conductor.build_timeout, agent, conf),
        Resume(agent))
    store = {'servers': servers, 'export_id': export.id,
             'volume_ids': list(export.volume_ids), 'volumes': volumes,
             'project_id': export.project_id,
             'stack_id': export.stack_id}
    engine = engines.load(flow, store=store)
    engine.run()
    return engine.storage.fetch('export_snapshots')


def archive_volume(conf, server, export, volume, device, filesystem,
                   put_url, manifest, agent=None):
    """Upload an attached clone; callers retain ownership of cloud cleanup.

    This helper neither updates DB state nor mints download URLs. A valid
    receipt is required before the caller can mark an archive complete.
    """
    if (volume.id not in export.volume_ids or
            server.role not in ('control', 'worker', 'build') or
            server.project_id != export.project_id or
            server.stack_id != export.stack_id or
            volume.project_id != export.project_id or
            volume.stack_id != export.stack_id or
            volume.server_id not in (None, server.id) or
            volume.purpose not in ('data', 'docker') or
            not volume.cinder_volume_id):
        raise exception.CoastException('Export archive selection invalid.')
    agent = agent or AgentClient(conf)
    try:
        response = agent.post(server, '/v1/export', {
            'export_id': export.id, 'volume_id': volume.id,
            'device': device, 'filesystem': filesystem,
            'purpose': volume.purpose, 'put_url': put_url,
            'manifest': manifest})
        size = response.get('size')
        digest = response.get('sha256')
        if (response.get('status') != 'uploaded' or
                isinstance(size, bool) or not isinstance(size, int) or
                not 0 < size <= conf.export.max_object_size_gb * 1024 ** 3 or
                not isinstance(digest, str) or
                not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('Invalid archive receipt')
        return {'size': size, 'sha256': digest}
    except Exception:
        raise exception.CoastException(
            'Export archive upload failed.') from None


# The agent PUT sends this Content-Type and no other signed header.
_ARCHIVE_CONTENT_TYPE = 'application/gzip'
# SigV4 query credentials cannot exceed seven days.
_MAX_PRESIGN_SECONDS = 7 * 24 * 60 * 60
_S3_KEY_FIELDS = frozenset(('access_key_id', 'secret_access_key'))


def _silence_botocore():
    """Stop botocore loggers. Their debug lines include signatures."""
    parent = std_logging.getLogger('botocore')
    parent.disabled = True
    parent.propagate = False
    for name, logger in list(std_logging.Logger.manager.loggerDict.items()):
        if isinstance(name, str) and name.startswith('botocore.'):
            if isinstance(logger, std_logging.PlaceHolder):
                continue
            logger.disabled = True
            logger.propagate = False


def _https_origin(url):
    if not isinstance(url, str) or not url:
        raise exception.CoastException('Export endpoint is invalid.')
    parts = parse.urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or
            parts.username or parts.password or parts.query or
            parts.fragment or parts.path not in ('', '/')):
        raise exception.CoastException('Export endpoint is invalid.')
    return url.rstrip('/')


def _s3_region(value):
    if (not isinstance(value, str) or
            not re.fullmatch(r'[A-Za-z0-9-]+', value)):
        raise exception.CoastException('Export endpoint is invalid.')
    return value


def _bucket_name(value):
    if (not isinstance(value, str) or not value or '/' in value or
            any(char.isspace() for char in value)):
        raise exception.CoastException('Export container is invalid.')
    return value


def _credential_text(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 256 or
            any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise exception.CoastException('Export signing key is not set.')
    return value


def _s3_keys(payload):
    if not isinstance(payload, str) or not 2 <= len(payload) <= 4096:
        raise exception.CoastException('Export signing key is not set.')
    try:
        document = json.loads(payload)
    except ValueError:
        raise exception.CoastException(
            'Export signing key is not set.') from None
    if not isinstance(document, dict) or set(document) != _S3_KEY_FIELDS:
        raise exception.CoastException('Export signing key is not set.')
    return (_credential_text(document['access_key_id']),
            _credential_text(document['secret_access_key']))


def _load_s3_keys(ctx, reference):
    if not isinstance(reference, str) or not reference:
        raise exception.CoastException('Export signing key is not set.')
    return _s3_keys(secret_store.call('get', ctx, reference))


def _s3_config():
    # SigV4 query auth. Do not let the client fall back to SigV2.
    options = {
        'signature_version': 's3v4',
        'connect_timeout': 10,
        'read_timeout': 30,
        'retries': {'total_max_attempts': 1},
        'ignore_configured_endpoint_urls': True,
        'inject_host_prefix': False,
        'defaults_mode': 'legacy',
        's3': {'addressing_style': 'path',
               'payload_signing_enabled': False},
    }
    # boto3 1.36 default checksums fail on Ceph RGW. 2026.1 pins 1.35.99.
    # https://bugs.launchpad.net/glance/+bug/2121144
    defaults = Config.OPTION_DEFAULTS
    if 'request_checksum_calculation' in defaults:
        options['request_checksum_calculation'] = 'when_required'
    if 'response_checksum_validation' in defaults:
        options['response_checksum_validation'] = 'when_required'
    return Config(**options)


def _s3_client(endpoint, region, access_key, secret_key):
    """Build a client that cannot fall back to env or instance credentials."""
    _silence_botocore()
    session = botocore.session.Session()
    # Overrides win over environment variables and shared files. An empty
    # profile does not have to exist. os.devnull has no profiles, so shared
    # AWS config and credentials are not loaded. A temp file is not used:
    # the unit-test fixture deletes the process temp directory.
    store = session.get_component('config_store')
    store.set_config_variable('profile', None)
    store.set_config_variable('config_file', os.devnull)
    store.set_config_variable('credentials_file', os.devnull)
    return session.create_client(
        's3', region_name=region, endpoint_url=endpoint, use_ssl=True,
        verify=True, aws_access_key_id=access_key,
        aws_secret_access_key=secret_key, aws_session_token=None,
        config=_s3_config())


def _object_missing(exc):
    response = getattr(exc, 'response', None)
    if not isinstance(response, dict):
        return False
    meta = response.get('ResponseMetadata') or {}
    error = response.get('Error') or {}
    code = str(error.get('Code', ''))
    return meta.get('HTTPStatusCode') == 404 or code in (
        '404', 'NoSuchKey', 'NotFound')


def _presign_lifetime(expires):
    if isinstance(expires, bool) or not isinstance(expires, int):
        raise exception.CoastException('Export URL request failed.')
    lifetime = expires - int(time.time())
    if not 1 <= lifetime <= _MAX_PRESIGN_SECONDS:
        raise exception.CoastException('Export URL request failed.')
    return lifetime


def _query_map(url):
    return {name.lower(): values for name, values in
            parse.parse_qs(parse.urlsplit(url).query).items()}


def _require_https_parts(url):
    if not isinstance(url, str):
        raise exception.CoastException('Export URL request failed.')
    parts = parse.urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or
            parts.username or parts.password or parts.fragment):
        raise exception.CoastException('Export URL request failed.')
    return parts


def _require_sigv4_query(url):
    query = _query_map(url)
    algorithm = (query.get('x-amz-algorithm') or [''])[0]
    signature = (query.get('x-amz-signature') or [''])[0]
    if (algorithm != 'AWS4-HMAC-SHA256' or not signature or
            'x-amz-credential' not in query or
            'x-amz-security-token' in query):
        raise exception.CoastException('Export URL request failed.')
    signed = (query.get('x-amz-signedheaders') or [''])[0]
    return signed.split(';')


def _require_sigv4_url(url, method, bucket, key, netloc):
    parts = _require_https_parts(url)
    headers = _require_sigv4_query(url)
    expected = '/%s/%s' % (bucket, key)
    if (parts.netloc.lower() != netloc.lower() or
            parse.unquote(parts.path) != expected or 'host' not in headers):
        raise exception.CoastException('Export URL request failed.')
    if method == 'PUT' and 'content-type' not in headers:
        raise exception.CoastException('Export URL request failed.')
    if method == 'GET' and 'content-type' in headers:
        raise exception.CoastException('Export URL request failed.')
    return url


class SwiftStorage:
    """Account-key temp URLs, including the catalog's full RGW prefix.

    The operator sets Coast-Temp-URL-Key-Ref account metadata to a Barbican
    reference and sets the matching account Temp-URL-Key on Swift/RGW.
    No container key fallback is used: RGW requires an account key.
    """

    def __init__(self, conf, ctx, proxy=None):
        self.conf = conf
        self.ctx = ctx
        if conf.export.backend != 'swift' or conf.export.upload_via != 'guest':
            raise exception.CoastException('Export backend is not supported.')
        self.proxy = proxy or connection.Connection(
            session=clients.get_session(conf),
            service_types=['object-store']).object_store
        logger = std_logging.Logger('coast.export.storage')
        logger.disabled = True
        self.proxy.logger = logger
        self.endpoint = self.proxy.get_endpoint().rstrip('/')
        parts = parse.urlsplit(self.endpoint)
        if (parts.scheme != 'https' or not parts.hostname or
                parts.username or parts.password or parts.query or
                parts.fragment):
            raise exception.CoastException('Export endpoint is invalid.')
        self.container = conf.export.container
        if not self.container or '/' in self.container:
            raise exception.CoastException('Export container is invalid.')
        metadata = self.proxy.get_account_metadata().metadata
        reference = metadata.get('coast-temp-url-key-ref')
        if not reference:
            raise exception.CoastException('Export signing key is not set.')
        self.key = secret_store.call('get', ctx, reference)
        if not isinstance(self.key, str) or not self.key:
            raise exception.CoastException('Export signing key is not set.')

    def object_name(self, export, volume_id):
        if export.backend != 'swift':
            raise exception.CoastException('Export backend is not supported.')
        return '%s/%s/%s/%s.tar.gz' % (
            export.project_id, export.stack_id, export.id, volume_id)

    def url(self, export, volume_id, method, expires):
        name = self.object_name(export, volume_id)
        path = (parse.urlsplit(self.endpoint).path + '/' +
                parse.quote(self.container, safe='') + '/' +
                parse.quote(name, safe='/'))
        # Swift/RGW temp URL protocol uses HMAC-SHA1, not archive SHA-256.
        signature = hmac.new(
            self.key.encode(), ('%s\n%s\n%s' % (
                method, expires, path)).encode(), hashlib.sha1).hexdigest()
        return parse.urlunsplit((
            'https', parse.urlsplit(self.endpoint).netloc, path,
            parse.urlencode({'temp_url_sig': signature,
                             'temp_url_expires': expires}), ''))

    def retain(self, export, volume_id):
        # Schedule backend housekeeping only after the PUT receipt is valid.
        self.proxy.set_object_metadata(
            self.object_name(export, volume_id), self.container,
            delete_at=int(export.object_expires_at.timestamp()))

    def delete(self, export):
        # Derive all names, including objects whose PUT outcome was uncertain.
        for volume_id in export.volume_ids:
            self.proxy.delete_object(self.object_name(export, volume_id),
                                     self.container, ignore_missing=True)

    def urls(self, export):
        now = int(time.time())
        retention = export.object_expires_at.replace(
            tzinfo=datetime.timezone.utc).timestamp()
        expires = min(now + self.conf.export.url_ttl, int(retention))
        if export.status != states.ACTIVE or expires <= now:
            return []
        result = []
        for artifact in export.artifacts or []:
            volume_id = artifact['volume_id']
            if volume_id not in export.volume_ids:
                raise exception.CoastException('Export artifact is invalid.')
            try:
                metadata = self.proxy.get_object_metadata(
                    self.object_name(export, volume_id), self.container)
            except sdk_exceptions.ResourceNotFound:
                continue
            if metadata:
                result.append({
                    'volume_id': volume_id,
                    'download_url': self.url(
                        export, volume_id, 'GET', expires),
                    'expires_at': datetime.datetime.fromtimestamp(
                        expires, datetime.timezone.utc).isoformat()})
        return result


class S3Storage:
    """Path-style SigV4 URLs for the same guest PUT the Swift path uses.

    Credentials come only from the Barbican secret named by
    s3_credentials_ref. The client does not read environment or instance
    credentials. S3 has no X-Delete-At. The conductor sweep deletes objects.
    """

    def __init__(self, conf, ctx, client=None):
        if conf.export.backend != 's3' or conf.export.upload_via != 'guest':
            raise exception.CoastException('Export backend is not supported.')
        self.conf = conf
        self.ctx = ctx
        self.endpoint = _https_origin(conf.export.s3_endpoint)
        self.region = _s3_region(conf.export.s3_region)
        self.container = _bucket_name(conf.export.container)
        if client is None:
            access_key, secret_key = _load_s3_keys(
                ctx, conf.export.s3_credentials_ref)
            client = _s3_client(
                self.endpoint, self.region, access_key, secret_key)
            del access_key, secret_key
        self.client = client

    def object_name(self, export, volume_id):
        if export.backend != 's3':
            raise exception.CoastException('Export backend is not supported.')
        return '%s/%s/%s/%s.tar.gz' % (
            export.project_id, export.stack_id, export.id, volume_id)

    def url(self, export, volume_id, method, expires):
        _silence_botocore()
        key = self.object_name(export, volume_id)
        operation = {'PUT': 'put_object', 'GET': 'get_object'}.get(method)
        if operation is None:
            raise exception.CoastException('Export URL request failed.')
        params = {'Bucket': self.container, 'Key': key}
        if method == 'PUT':
            params['ContentType'] = _ARCHIVE_CONTENT_TYPE
        try:
            signed = self.client.generate_presigned_url(
                ClientMethod=operation, Params=params,
                ExpiresIn=_presign_lifetime(expires), HttpMethod=method)
        except exception.CoastException:
            raise
        except Exception:
            raise exception.CoastException(
                'Export URL request failed.') from None
        return _require_sigv4_url(
            signed, method, self.container, key,
            parse.urlsplit(self.endpoint).netloc)

    def _call(self, method, **kwargs):
        _silence_botocore()
        try:
            return getattr(self.client, method)(**kwargs)
        except Exception as exc:
            if _object_missing(exc):
                return None
            raise exception.CoastException(
                'Export storage request failed.') from None

    def retain(self, export, volume_id):
        # HeadObject confirms the PUT. Do not write a bucket lifecycle rule.
        found = self._call(
            'head_object', Bucket=self.container,
            Key=self.object_name(export, volume_id))
        if found is None:
            raise exception.CoastException('Export storage request failed.')

    def delete(self, export):
        for volume_id in export.volume_ids:
            self._call('delete_object', Bucket=self.container,
                       Key=self.object_name(export, volume_id))

    def urls(self, export):
        now = int(time.time())
        retention = export.object_expires_at.replace(
            tzinfo=datetime.timezone.utc).timestamp()
        expires = min(now + self.conf.export.url_ttl, int(retention))
        if export.status != states.ACTIVE or expires <= now:
            return []
        result = []
        for artifact in export.artifacts or []:
            volume_id = artifact['volume_id']
            if volume_id not in export.volume_ids:
                raise exception.CoastException('Export artifact is invalid.')
            found = self._call(
                'head_object', Bucket=self.container,
                Key=self.object_name(export, volume_id))
            if found is None:
                continue
            result.append({
                'volume_id': volume_id,
                'download_url': self.url(export, volume_id, 'GET', expires),
                'expires_at': datetime.datetime.fromtimestamp(
                    expires, datetime.timezone.utc).isoformat()})
        return result


def storage_for(conf, ctx, export):
    """Return guest storage for the export backend recorded on the row."""
    if (export.backend != conf.export.backend or
            conf.export.upload_via != 'guest'):
        raise exception.CoastException('Export backend is not supported.')
    if export.backend == 'swift':
        return SwiftStorage(conf, ctx)
    if export.backend == 's3':
        return S3Storage(conf, ctx)
    raise exception.CoastException('Export backend is not supported.')


def _export_stack_ready(stack):
    """A normal export needs ACTIVE. A delete export needs DELETING."""
    if (stack.status == states.ACTIVE and
            stack.task_state == states.RECONCILING):
        return True
    return (stack.status == states.DELETING and
            stack.task_state == states.DELETING)


class ExportFlow:
    """Cloud I/O outside transactions; a durable receipt claims each cast.

    Interrupted creates fail closed on redelivery. Tagged cloud resources
    retain their export identity for operator recovery, not blind re-upload.
    """

    def __init__(self, manager):
        from coast.conductor import manager as manager_module
        self.owner = manager_module.Manager(manager.conf,
                                            notifier=manager.notifier)
        self.owner._pending = []
        self.conf = manager.conf
        self.snapshots = []
        self.clones = []

    def flush(self):
        pending = self.owner._pending
        self.owner._pending = []
        self.owner._seen = set()
        self.owner._flush(pending)

    def execute(self, ctx, operation, resource, revision=None):
        action = operation.split('_', 1)[0]
        accepted_revision = revision or rpcapi.revision(resource)
        receipt = rpcapi.receipt_id(operation, resource, revision)
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            try:
                export = objects.Export.obj_from_db(
                    ctx, db_api.exports._get(ctx, resource.id, lock=True))
            except exception.NotFound:
                return
            stack = objects.Stack.obj_from_db(
                ctx, db_api.stacks._get(ctx, export.stack_id, lock=True))
            previous = objects.EventList.get_all(
                ctx, filters={'id': receipt}, limit=1)
            if previous:
                return  # Never run another upload for an uncertain attempt.
            if (rpcapi.revision(export) != accepted_revision or
                    export.status != resource.status):
                return
            if action == 'create':
                if not _export_stack_ready(stack):
                    return
                self.owner._save(stack, conductor_host=self.conf.host)
                self.owner._save(export, status=states.SNAPSHOTTING)
            self.owner._event(ctx, export, stack, action, 'start', receipt)
        self.flush()
        success = False
        storage = None
        try:
            storage = storage_for(self.conf, ctx, export)
            if action == 'delete':
                storage.delete(export)
            else:
                self.create(ctx, stack, export, storage)
            success = True
        except Exception:
            LOG.warning('Export operation failed; operator recovery may '
                        'be required.')
        finally:
            if self.cleanup():
                success = False
        self.finish(ctx, export, stack, action, receipt, success,
                    storage)

    def finish(self, ctx, export, stack, action, receipt, success, storage):
        cancelled = False
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            try:
                current = objects.Export.obj_from_db(
                    ctx, db_api.exports._get(ctx, export.id, lock=True))
            except exception.NotFound:
                current = None
            cancelled = action == 'create' and (
                current is None or current.status not in (
                    states.SNAPSHOTTING, states.UPLOADING))
            if cancelled:
                # Keep the original stack task until its upload has stopped.
                self.owner._event(ctx, current or export, stack, action,
                                  'failure', receipt)
            elif success and action == 'delete':
                current.destroy()
            elif success:
                self.owner._save(current, artifacts=export.artifacts,
                                 object_expires_at=export.object_expires_at,
                                 status=states.ACTIVE)
            else:
                self.owner._save(current, status=states.ERROR,
                                 fault='Export failed; operator recovery '
                                       'may be required.')
            if not cancelled:
                if action == 'create':
                    self._release_export_lock(stack)
                else:
                    active_create = objects.EventList.get_all(ctx, filters={
                        'resource_id': export.id, 'resource_type': 'export',
                        'action': 'create', 'outcome': 'start'}, limit=1)
                    if not active_create:
                        self.owner._release_preheld_export_lock(
                            ctx, current, stack)
                self.owner._event(ctx, current, stack, action,
                                  'success' if success else 'failure', receipt)
        self.flush()
        if cancelled:
            try:
                if storage is not None:
                    storage.delete(export)
            except Exception:
                LOG.warning('Cancelled export objects require recovery.')
            with db_api.writer.using(ctx):
                self._release_export_lock(stack)

    @staticmethod
    def _release_export_lock(stack):
        # A normal export holds RECONCILING. Stack delete already holds
        # DELETING. Do not clear that lock or teardown cannot finish.
        if getattr(stack, 'task_state', None) == states.RECONCILING:
            stack.release_task(states.RECONCILING)

    def create(self, ctx, stack, export, storage):
        from coast.conductor import manager
        volumes = [objects.Volume.get_by_id(ctx, identifier)
                   for identifier in export.volume_ids]
        servers = []
        for volume in volumes:
            if volume.server_id is None:
                controllers = list(manager.rows(objects.ServerList, ctx, {
                    'stack_id': stack.id, 'role': 'control'}))
                if len(controllers) != 1:
                    raise exception.CoastException('Export owner is missing.')
                server = controllers[0]
            else:
                server = objects.Server.get_by_id(ctx, volume.server_id)
            if (server.kind != 'vm' or not server.compute_id or
                    server.status != states.ACTIVE):
                raise exception.CoastException('Export owner is invalid.')
            if server.id not in {item.id for item in servers}:
                servers.append(server)
        if (sum(volume.size_gb for volume in volumes) >
                self.conf.export.max_size_gb):
            raise exception.CoastException('Export capacity is too large.')
        self.block = clients.get_connection('cinder', self.conf).block_storage
        self.compute = clients.get_sensitive_proxy('nova', self.conf)
        self.snapshots = snapshot_volumes(
            self.conf, servers, export, volumes, block_storage=self.block)
        self.owner._save(export, status=states.UPLOADING)
        self.owner._queue(ctx, export, 'create', 'start')
        self.flush()
        self.owner._save(
            export, object_expires_at=datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(
                    hours=self.conf.export.retention_hours))
        artifacts = []
        for volume, snapshot in zip(volumes, self.snapshots):
            server = next(item for item in servers if item.id ==
                          volume.server_id) if volume.server_id else next(
                              item for item in servers
                              if item.role == 'control')
            clone = self.block.create_volume(
                snapshot_id=snapshot.id, size=volume.size_gb,
                name=naming.resource_name(
                    'export', export.id, volume.id, conf=self.conf),
                metadata={'coast': 'true', 'project_id': export.project_id,
                          'stack_id': stack.id, 'export_id': export.id,
                          'volume_id': volume.id})
            entry = [clone, server, False]
            self.clones.append(entry)
            self.block.wait_for_status(
                clone, status='available', failures=['error'], interval=1,
                wait=self.conf.conductor.build_timeout)
            entry[2] = True  # Unknown attachment outcome must also detach.
            attachment = self.compute.create_volume_attachment(
                server.compute_id, volume_id=clone.id)
            self.block.wait_for_status(
                clone, status='in-use', failures=['error'], interval=1,
                wait=self.conf.conductor.build_timeout)
            device = attachment.device
            if not device:
                raise exception.CoastException('Export device is missing.')
            manifest = {'consistency': 'quiesced-snapshot',
                        'services': self.manifest(ctx, stack.id)}
            expires = int(time.time()) + self.conf.export.archive_timeout
            result = archive_volume(
                self.conf, server, export, volume, device, 'ext4',
                storage.url(export, volume.id, 'PUT', expires), manifest)
            storage.retain(export, volume.id)
            artifacts.append({'volume_id': volume.id,
                              'name': volume.id + '.tar.gz',
                              'size_bytes': result['size'],
                              'sha256': result['sha256']})
        export.artifacts = artifacts

    @staticmethod
    def manifest(ctx, stack_id):
        from coast.conductor import manager
        result = []
        for service in manager.rows(objects.ServiceList, ctx,
                                    {'stack_id': stack_id}):
            # Spec paths only; no source credentials or plaintext values.
            source = objects.Source.get_by_id(ctx, service.source_id)
            result.append({
                'id': service.id, 'name': service.name,
                'spec': {'type': source.type,
                         'compose_path': source.compose_path},
                'env_names': [item.key for item in manager.rows(
                    objects.SecretList, ctx, {'service_id': service.id})],
                'mounts': [{'volume_id': item.volume_id,
                            'container_path': item.container_path,
                            'sub_path': item.sub_path}
                           for item in manager.rows(
                               objects.AppVolumeMountList, ctx,
                               {'service_id': service.id})],
                'domains': [item.fqdn for item in manager.rows(
                    objects.DomainList, ctx, {'service_id': service.id})],
                'deployments': [item.number for item in manager.rows(
                    objects.DeploymentList, ctx, {'service_id': service.id})]})
        return result

    def cleanup(self):
        failed = False
        for clone, server, attached in reversed(self.clones):
            try:
                if attached:
                    self.compute.delete_volume_attachment(
                        server.compute_id, clone.id, ignore_missing=True)
                    self.block.wait_for_status(
                        clone, status='available', failures=['error'],
                        interval=1, wait=self.conf.conductor.build_timeout)
                self.block.delete_volume(clone, ignore_missing=True)
                self.block.wait_for_delete(
                    clone, interval=1, wait=self.conf.conductor.build_timeout)
            except Exception:
                failed = True
                LOG.warning('Export clone cleanup requires recovery.')
        for snapshot in reversed(self.snapshots):
            try:
                self.block.delete_snapshot(snapshot, ignore_missing=True)
                self.block.wait_for_delete(
                    snapshot, interval=1,
                    wait=self.conf.conductor.build_timeout)
            except Exception:
                failed = True
                LOG.warning('Export snapshot cleanup requires recovery.')
        return failed


def housekeep(manager):
    """Delete expired objects, including uncertain PUTs, on the shared timer.

    Only the control-plane scan is all-projects. Every object operation and
    DB write uses the recorded owner project and deterministic object names.
    RGW does not need a Swift object-expirer process for this cleanup.
    """
    admin = context_module.RequestContext(is_admin=True)
    marker = None
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    while True:
        with db_api.reader.using(admin):
            query = admin.session.query(models.Export.id,
                                        models.Export.project_id).filter(
                models.Export.object_expires_at <= now)
            if marker:
                query = query.filter(models.Export.id > marker)
            batch = query.order_by(models.Export.id).limit(100).all()
        if not batch:
            return
        for identifier, project_id in batch:
            ctx = context_module.RequestContext(project_id=project_id)
            try:
                export = objects.Export.get_by_id(ctx, identifier)
                # The stack row is soft-deleted. Retention still applies.
                stack = objects.Stack.get_by_id(
                    ctx, export.stack_id, show_deleted=True)
                if stack.driver == 'noop':
                    continue
                # The other backend is cleaned only while it is configured.
                if export.backend != manager.conf.export.backend:
                    continue
                storage = storage_for(manager.conf, ctx, export)
                storage.delete(export)
                if export.status == states.ACTIVE:
                    flow = ExportFlow(manager)
                    with db_api.writer.using(ctx):
                        current = objects.Export.obj_from_db(
                            ctx, db_api.exports._get(
                                ctx, identifier, lock=True))
                        if current.status == states.ACTIVE:
                            flow.owner._event(ctx, current, stack,
                                              'expire', 'start')
                            flow.owner._save(current, status=states.EXPIRED)
                            flow.owner._event(ctx, current, stack,
                                              'expire', 'success')
                    flow.flush()
            except Exception:
                LOG.warning('Export object cleanup requires another attempt.')
        marker = batch[-1][0]


INTERRUPTED = (states.PENDING, states.SNAPSHOTTING, states.UPLOADING,
               states.DELETING)
RECOVERY_FAULT = 'Export interrupted by conductor restart; archive discarded.'


def _recovery_revision(export):
    return export.status + ":" + rpcapi.revision(export)


def prepare_recovery(manager):
    """Capture startup work before stack recovery clears host ownership.

    No cross-host takeover. Unclaimed PENDING exports have no cloud effects.
    The captured revision excludes casts accepted after this scan.
    """
    admin = context_module.RequestContext(is_admin=True)
    unclaimed = (models.Stack.conductor_host.is_(None) &
                 models.Export.status.in_((states.PENDING, states.DELETING)))
    local = models.Stack.conductor_host == manager.conf.host
    with db_api.reader.using(admin):
        rows = admin.session.query(models.Export).join(models.Stack).filter(
            models.Export.status.in_(INTERRUPTED),
            models.Export.backend == manager.conf.export.backend,
            models.Stack.driver != 'noop', local | unclaimed).order_by(
                models.Export.id).all()
        manager._export_recovery_stacks = {row.stack_id for row in rows}
        manager._export_recovery = deque(
            (row.id, row.project_id, _recovery_revision(
                objects.Export.obj_from_db(admin, row))) for row in rows)
    # Old guests may still upload and old cloud creates may still complete.
    manager._export_recovery_after = time.monotonic() + max(
        manager.conf.export.archive_timeout,
        manager.conf.conductor.build_timeout)


def recover(manager):
    """Attempt at most 100 startup exports per shared timer run."""
    pending = getattr(manager, '_export_recovery', ())
    for unused in range(min(100, len(pending))):
        identifier, project_id, revision = pending.popleft()
        ctx = context_module.RequestContext(project_id=project_id)
        try:
            if not recover_one(manager, ctx, identifier, revision):
                pending.append((identifier, project_id, revision))
        except exception.NotFound:
            continue
        except Exception:
            pending.append((identifier, project_id, revision))
            LOG.warning('Interrupted export cleanup requires another attempt.')


def recover_one(manager, ctx, identifier, revision):
    export = objects.Export.get_by_id(ctx, identifier)
    if (export.status not in INTERRUPTED or
            _recovery_revision(export) != revision):
        return True
    stack = objects.Stack.get_by_id(ctx, export.stack_id, show_deleted=True)
    if stack.conductor_host not in (None, manager.conf.host):
        return True
    # Claim an unstarted cast before cloud I/O. Its queued RPC can otherwise
    # start uploading between this read and the final ERROR publication.
    if export.status == states.PENDING:
        claim = ExportFlow(manager)
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            current = objects.Export.obj_from_db(
                ctx, db_api.exports._get(ctx, identifier, lock=True))
            if _recovery_revision(current) != revision:
                return True
            receipt = rpcapi.receipt_id('create_export', current)
            if not objects.EventList.get_all(
                    ctx, filters={'id': receipt}, limit=1):
                claim.owner._event(ctx, current, stack, 'create', 'start',
                                   receipt)
        claim.flush()
    # Source rows can disappear; try every possible owner in this stack.
    from coast.conductor import manager as manager_module
    servers = list(manager_module.rows(
        objects.ServerList, ctx, {'stack_id': stack.id}))
    owners = [server for server in servers
              if server.role in ('control', 'worker', 'build')]
    failed = resume_each(AgentClient(manager.conf), owners, export.id)
    failed = failed or not owners
    if export.status != states.PENDING and (
            time.monotonic() < manager._export_recovery_after):
        return False
    flow = ExportFlow(manager)
    flow.block = clients.get_connection('cinder', manager.conf).block_storage
    flow.compute = clients.get_sensitive_proxy('nova', manager.conf)
    failed = _discover_recovery_resources(flow, export, owners) or failed
    failed = flow.cleanup() or failed
    try:
        storage_for(manager.conf, ctx, export).delete(export)
    except Exception:
        failed = True
        LOG.warning('Interrupted export objects require another attempt.')
    if failed:
        return False
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        current = objects.Export.obj_from_db(
            ctx, db_api.exports._get(ctx, identifier, lock=True))
        if (current.status not in INTERRUPTED or
                _recovery_revision(current) != revision):
            return True
        stack = objects.Stack.obj_from_db(
            ctx, db_api.stacks._get(ctx, export.stack_id, lock=True,
                                    show_deleted=True))
        flow.owner._save(current, status=states.ERROR, fault=RECOVERY_FAULT,
                         artifacts=[])
        receipts = objects.EventList.get_all(ctx, filters={
            'resource_id': identifier, 'resource_type': 'export',
            'outcome': 'start'})
        for receipt in receipts:
            flow.owner._event(ctx, current, stack, receipt.action,
                              'failure', receipt.id)
            flow.owner._save(receipt, outcome='failure',
                             message=RECOVERY_FAULT)
        if not receipts:
            flow.owner._event(ctx, current, stack, 'create', 'failure')
        if stack.task_state == states.RECONCILING:
            flow._release_export_lock(stack)
        elif (stack.task_state == states.DELETING and
              stack.conductor_host == manager.conf.host):
            flow.owner._save(stack, status=states.ERROR,
                             fault='Interrupted by conductor restart')
            stack.release_task(states.DELETING)
            flow.owner._event(ctx, stack, stack, 'update', 'failure')
    flow.flush()
    return True


def _discover_recovery_resources(flow, export, owners):
    failed = False
    # Never delete a cloud resource based on its name alone.
    expected = {'coast': 'true', 'project_id': export.project_id,
                'stack_id': export.stack_id, 'export_id': export.id}

    def owned(resource):
        metadata = resource.metadata or {}
        return (all(metadata.get(key) == value
                    for key, value in expected.items()) and
                metadata.get('volume_id') in export.volume_ids)

    for clone in flow.block.volumes(details=True):
        if not owned(clone):
            continue
        attachments = clone.attachments or []
        if len(attachments) > 1:
            failed = True
            continue
        server = None
        if attachments:
            server = next((item for item in owners if item.compute_id ==
                           attachments[0].get('server_id')), None)
            if server is None:
                failed = True
                continue
        flow.clones.append((clone, server, bool(attachments)))
    flow.snapshots = [snapshot for snapshot in
                      flow.block.snapshots(details=True) if owned(snapshot)]
    return failed
