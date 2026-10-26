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

"""Open one short-lived shell session for an app or a service."""

import datetime
import secrets
import urllib.parse

from oslo_config import cfg
from oslo_utils import timeutils
import pecan

from coast.api import microversions
from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers as common
from coast.common import exception
from coast.console import access
from coast.console import tokens
from coast import objects


def _public_host():
    host = (cfg.CONF.console.public_endpoint or '').strip()
    if not host or '://' in host or '/' in host:
        return None
    return host


def _session_url(host, token):
    scheme = 'wss' if cfg.CONF.console.tls else 'ws'
    quoted = urllib.parse.quote(token, safe='')
    return '%s://%s/?token=%s' % (scheme, host, quoted)


class Session:
    def __init__(self, resource_id, kind):
        self.resource_id = resource_id
        self.kind = kind

    def _service(self, ctx):
        try:
            service = objects.Service.get_by_id(ctx, self.resource_id)
        except exception.NotFound:
            pecan.abort(404)
        kind = service.kind
        if kind != self.kind:
            pecan.abort(404)
        return service

    def _server(self, ctx, service):
        if not service.server_id:
            return None
        try:
            return objects.Server.get_by_id(ctx, service.server_id)
        except exception.NotFound:
            return None

    def _issue(self, ctx, service, command):
        token = secrets.token_urlsafe(32)
        expires = timeutils.utcnow() + datetime.timedelta(
            seconds=cfg.CONF.console.token_ttl)
        objects.ConsoleToken(
            context=ctx, token_hash=tokens.digest(token),
            stack_id=service.stack_id, resource_type=self.kind,
            resource_id=service.id, command=command, user_id=ctx.user_id,
            expires_at=expires).create()
        return token, expires

    @pecan.expose('json')
    def index(self):
        if not microversions.at_least(pecan.request.version, '1.9'):
            pecan.abort(404)
        if pecan.request.method != 'POST':
            pecan.abort(405)
        ctx = common.context()
        service = self._service(ctx)
        common.authorize(self.kind, 'console', service)
        body = schemas.body(schemas.CONSOLE, optional=True)
        if not access.workload_available(service, self._server(ctx, service)):
            raise exception.Conflict('The app is not available.')
        host = _public_host()
        if host is None:
            pecan.abort(503)
        token, expires = self._issue(ctx, service, body.get('command'))
        pecan.response.status = 201
        return {
            'url': _session_url(host, token),
            'expires_at': expires.isoformat().replace('+00:00', 'Z'),
        }


class App:
    def __init__(self, app_id):
        from coast.api.v1 import file_mount_controllers
        self.file_mounts = file_mount_controllers.FileMounts(app_id, 'app')
        self.console = Session(app_id, 'app')

    @pecan.expose('json')
    def index(self):
        pecan.abort(404)


class Apps:
    @pecan.expose('json')
    def index(self):
        pecan.abort(404)

    @pecan.expose()
    def _lookup(self, app_id, *remainder):
        return App(app_id), remainder
