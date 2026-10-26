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

"""Customer-project Swift/RGW containers and temporary segment links."""

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from urllib import parse

from keystoneauth1 import exceptions as ks_exceptions
from openstack import connection
from openstack import exceptions as sdk_exceptions

from coast.api import secret_store
from coast.common import clients
from coast.common import database_backups as common
from coast.common import exception
from coast.db import api as db_api
from coast import objects


class StorageUnavailable(exception.Conflict):
    msg_fmt = 'Object storage not available.'


def proxy_for(conf, project_id):
    """Use service auth plus Swift reseller rights, not tenant credentials.

    Swift accounts use the standard AUTH_<project UUID> catalog suffix.
    No request token or agent credential is used or forwarded.
    """
    if not re.fullmatch(r'[a-fA-F0-9]{32}|[a-fA-F0-9-]{36}', project_id):
        raise exception.Invalid('Invalid object storage project.')
    session = clients.get_session(conf)
    try:
        endpoint = session.get_endpoint(
            service_type='object-store', interface=conf.backup.interface,
            region_name=conf.backup.region_name or conf.nova.region_name)
        if not endpoint:
            raise StorageUnavailable()
    except ks_exceptions.catalog.EndpointNotFound:
        raise StorageUnavailable() from None
    parts = parse.urlsplit(endpoint.rstrip('/'))
    prefix, sep, account = parts.path.rpartition('/AUTH_')
    if (not sep or not account or '/' in account or
            parts.scheme != 'https' or not parts.hostname or
            parts.username or parts.password or parts.query or parts.fragment):
        raise StorageUnavailable()
    endpoint = parse.urlunsplit((parts.scheme, parts.netloc,
                                prefix + '/AUTH_' + project_id, '', ''))
    proxy = connection.Connection(
        session=session, service_types=['object-store'],
        object_store_endpoint_override=endpoint, api_timeout=60).object_store
    quiet = logging.Logger('coast.backup.storage')
    quiet.disabled = True
    proxy.logger = quiet
    return proxy


class SwiftStorage:
    def __init__(self, conf, ctx, service_id, proxy=None):
        self.conf = conf
        self.ctx = ctx
        self.proxy = proxy or proxy_for(conf, ctx.project_id)
        self.endpoint = self.proxy.get_endpoint().rstrip('/')
        self.container = 'coast-backups-' + service_id

    def prepare(self, policy):
        self.proxy.create_container(self.container, metadata={
            'coast': 'true', 'project_id': self.ctx.project_id,
            'service_id': policy.service_id})
        # References come only from Coast's DB. Account metadata is editable
        # by the customer and must never select a control-plane secret.
        reference, key = account_key(self.ctx)
        account = self.proxy.get_account_metadata()
        metadata = {'temp-url-key': account.meta_temp_url_key,
                    'temp-url-key-2': account.meta_temp_url_key_2}
        slots = ('temp-url-key-2', 'temp-url-key')
        if not any(metadata.get(slot) == key for slot in slots):
            slot = next((slot for slot in slots if not metadata.get(slot)),
                        None)
            if slot is None:
                raise exception.Conflict(
                    'Object storage temporary URL key slots are occupied.')
            self.proxy.set_account_metadata(**{slot: key})
        policy.signing_key_ref = reference
        policy.save()

    def name(self, backup, index=None):
        return (backup.id + '/manifest' if index is None else
                '%s/%06d' % (backup.id, index))

    def urls(self, policy, backup, method, count):
        key = secret_store.call('get', self.ctx, policy.signing_key_ref)
        expires = int(time.time()) + self.conf.backup.url_ttl
        origin = parse.urlsplit(self.endpoint)
        result = []
        for index in range(count):
            path = (origin.path + '/' + parse.quote(self.container, safe='') +
                    '/' + self.name(backup, index))
            signature = hmac.new(key.encode(),
                                 ('%s\n%s\n%s' % (
                                     method, expires, path)).encode(),
                                 hashlib.sha1).hexdigest()  # nosec B324
            result.append(parse.urlunsplit((
                origin.scheme, origin.netloc, path, parse.urlencode({
                    'temp_url_sig': signature,
                    'temp_url_expires': expires}), '')))
        return result

    def complete(self, backup, segments):
        manifest = []
        for index, segment in enumerate(segments):
            observed = self.proxy.get_object_metadata(
                self.name(backup, index), container=self.container)
            if (int(observed.content_length) != segment['size_bytes'] or
                    not re.fullmatch(r'[a-fA-F0-9]{32}', observed.etag)):
                raise ValueError('Invalid uploaded object')
            manifest.append({'path': '/' + self.container + '/' +
                             self.name(backup, index),
                             'etag': observed.etag,
                             'size_bytes': segment['size_bytes']})
        # Use the documented SLO PUT API; no SDK automatic segmentation.
        response = self.proxy.put(
            parse.quote(self.container) + '/' + self.name(backup),
            params={'multipart-manifest': 'put'},
            data=json.dumps(manifest),
            headers={'Content-Type': 'application/json'})
        sdk_exceptions.raise_from_response(response)

    def delete(self, backup):
        # Include uncertain PUT outcomes, not only reported successful uploads.
        try:
            for obj in self.proxy.objects(self.container,
                                          prefix=backup.id + '/'):
                self.proxy.delete_object(obj.name, container=self.container,
                                         ignore_missing=True)
        except sdk_exceptions.ResourceNotFound:
            return


def account_key(ctx):
    """Persist one signer per owner project before any Swift metadata write.

    Keep it after the last service is deleted, for safe reuse by new services.
    This also permits retry after an uncertain account metadata response.
    """
    reference = None
    try:
        with db_api.writer.using(ctx):
            db_api._lock_project(ctx.session, ctx.project_id)
            row = next(common.rows(objects.BackupAccountList, ctx), None)
            if row is None:
                reference = secret_store.call(
                    'put', ctx, secrets.token_hex(32),
                    secret_store.SecretIdentity(
                        ctx.project_id, 'backup-signing-key', ctx.project_id,
                        ctx.project_id))
                row = objects.BackupAccount(context=ctx, key_ref=reference)
                row.create()
    except Exception:
        if reference:
            secret_store.call('delete', ctx, reference)
        raise
    return row.key_ref, secret_store.call('get', ctx, row.key_ref)
