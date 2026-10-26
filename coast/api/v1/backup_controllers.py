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

"""Database backup settings, history, restore and decrypted downloads."""

import base64

from oslo_config import cfg
from oslo_utils import timeutils
import pecan

from coast.agent import backup as wire
from coast.api import handlers
from coast.api import microversions
from coast.api import secret_store
from coast.api.v1 import schemas
from coast.api.v1 import service_controllers as services
from coast.api.v1 import stack_controllers as api
from coast.api.v1 import validation
from coast.common import context
from coast.common import database_backups as common
from coast.common import exception
from coast.common import notifications
from coast.common import policy as rbac
from coast.common import states
from coast.conductor import backup_storage
from coast.db import api as db_api
from coast import objects


FIELDS = ('id', 'service_id', 'kind', 'status', 'size_bytes', 'started_at',
          'finished_at', 'duration_seconds', 'error', 'engine',
          'engine_version')
POLICY_FIELDS = tuple(common.POLICY_DEFAULTS) + ('engine', 'container')
SELECTION = {
    'engine': {'type': 'string', 'enum': [
        'postgres', 'mysql', 'mariadb', 'mongodb']},
    'container': {'type': 'string', 'pattern': r'^[A-Za-z0-9][A-Za-z0-9_.-]*$',
                  'maxLength': 128},
}


def gate():
    if not microversions.at_least(pecan.request.version,
                                  microversions.BACKUP_VERSION):
        pecan.abort(404)
    validation.parameters(pecan.request.params, ())


def lookup(cls, identifier, action):
    gate()
    ctx = api.context()
    # An admin grant is explicit; ordinary tenant reads never widen scope.
    admin = rbac.get_enforcer().authorize(
        'ADMIN', {}, ctx.to_policy_values(), do_raise=False)
    if admin:
        ctx.is_admin = True
    row = cls.get_by_id(ctx, identifier, all_projects=bool(admin))
    if cls is objects.Backup:
        service = objects.Service.get_by_id(ctx, row.service_id,
                                            all_projects=bool(admin))
        if service.kind == 'database':
            from coast.api.v1 import database_controllers
            database_controllers.gate()
    api.authorize('backup', action, row)
    owner = context.RequestContext(project_id=row.project_id,
                                   user_id=ctx.user_id, roles=ctx.roles,
                                   is_admin=bool(admin),
                                   request_id=ctx.request_id)
    return owner, row


def resource(identifier, action, database):
    if database:
        from coast.api.v1 import database_controllers
        database_controllers.gate()
    ctx, row = lookup(objects.Service, identifier, action)
    if (row.kind == 'database') != database:
        raise exception.NotFound()
    if database:
        common.selection(row)
    return ctx, row


def view(row):
    return services.values(row, FIELDS)


def available(ctx):
    # Catalog check before accepting any operation requiring object storage.
    backup_storage.proxy_for(cfg.CONF, ctx.project_id)


def lock_stack(ctx, service):
    """Order API backup claims without changing conductor acquisition."""
    db_api._lock_project(ctx.session, ctx.project_id)
    db_api.stacks._get(ctx, service.stack_id, lock=True)


class BackupPolicy:
    def __init__(self, service_id, database=False):
        self.service_id = service_id
        self.database = database

    @pecan.expose('json')
    def index(self, **kwargs):
        method = pecan.request.method
        if method not in ('GET', 'PUT'):
            pecan.abort(405)
        action = 'get_policy' if method == 'GET' else 'set_policy'
        ctx, service = resource(self.service_id, action, self.database)
        row = common.policy_for(ctx, service.id)
        fields = (tuple(common.POLICY_DEFAULTS) if self.database else
                  POLICY_FIELDS)
        if method == 'GET':
            return {'backup_policy': services.values(row, fields) if row else (
                    dict(common.POLICY_DEFAULTS) if self.database else
                    dict(common.POLICY_DEFAULTS, engine=None, container=None))}
        if not self.database and not service.is_engine_service:
            raise exception.Conflict('Backups require a compose service.')
        selection = {} if self.database else SELECTION
        schema = schemas.document(dict(selection, **{
            'enabled': {'type': 'boolean'},
            'hour': {'type': 'integer', 'minimum': 0, 'maximum': 23},
            'minute': {'type': 'integer', 'minimum': 0, 'maximum': 59},
            'keep_daily': {'type': 'integer', 'minimum': 0, 'maximum': 365},
            'keep_weekly': {'type': 'integer', 'minimum': 0, 'maximum': 104},
        }), () if row or self.database else ('engine', 'container'))
        values = schemas.body(schema, 'backup_policy')
        if self.database:
            values.update(common.selection(service))
        if values.get('enabled', row.enabled if row else False):
            available(ctx)
        with db_api.writer.using(ctx):
            db_api._lock_project(ctx.session, ctx.project_id)
            services.stack_open(ctx, service.stack_id)
            service = objects.Service.get_by_id(ctx, service.id)
            if service.task_state != states.NONE:
                raise exception.Conflict()
            row = common.policy_for(ctx, service.id)
            if row is None:
                row = objects.BackupPolicy(context=ctx, service_id=service.id,
                                           **dict(common.POLICY_DEFAULTS,
                                                  **values))
                row.create()
            else:
                for key, value in values.items():
                    setattr(row, key, value)
                row.save()
        notifications.completed(ctx, row, 'update', name='backup_policy')
        return {'backup_policy': services.values(row, fields)}


class ServiceBackups:
    def __init__(self, service_id, database=False):
        self.service_id = service_id
        self.database = database

    @pecan.expose('json')
    def index(self, **kwargs):
        method = pecan.request.method
        if method not in ('GET', 'POST'):
            pecan.abort(405)
        ctx, service = resource(self.service_id,
                                'get_all' if method == 'GET' else 'create',
                                self.database)
        if method == 'GET':
            return {'backups': [view(row) for row in common.rows(
                objects.BackupList, ctx, service_id=service.id)
                if row.status != states.DELETED]}
        if pecan.request.body:
            schemas.body(schemas.document({}))
        available(ctx)
        with db_api.writer.using(ctx):
            lock_stack(ctx, service)
            row = common.submit(ctx, service)
        pecan.response.status = 202
        return {'backup': view(row)}


class Backups:
    @pecan.expose()
    def _lookup(self, id, *remainder):
        gate()
        return BackupItem(id), remainder


class BackupItem:
    def __init__(self, identifier):
        self.id = identifier

    def get(self, action):
        ctx, row = lookup(objects.Backup, self.id, action)
        if row.status == states.DELETED:
            raise exception.NotFound()
        return ctx, row

    @pecan.expose('json')
    def index(self, **kwargs):
        method = pecan.request.method
        if method not in ('GET', 'DELETE'):
            pecan.abort(405)
        ctx, row = self.get('get_one' if method == 'GET' else 'delete')
        if method == 'GET':
            result = view(row)
            result['restores'] = [services.values(job, (
                'id', 'service_id', 'operation', 'status', 'started_at',
                'finished_at', 'error', 'safety_backup_id')) for job in
                common.rows(objects.BackupJobList, ctx, backup_id=row.id)
                if job.operation in ('new_copy', 'overwrite')]
            return {'backup': result}
        if pecan.request.body:
            schemas.body(schemas.document({}))
        available(ctx)
        with db_api.writer.using(ctx):
            service = objects.Service.get_by_id(ctx, row.service_id)
            lock_stack(ctx, service)
            service = common.lock(ctx, service)
            if common.in_use(ctx, row.id):
                raise exception.Conflict('Backup is in use.')
            row = objects.Backup.get_by_id(ctx, row.id)
            common.change(ctx, row, states.DELETING, 'delete')
            common.queue(ctx, row, service, 'delete')
        pecan.response.status = 202
        return {'backup': view(row)}

    @pecan.expose('json')
    def restore(self, **kwargs):
        if pecan.request.method != 'POST':
            pecan.abort(405)
        ctx, row = self.get('restore')
        options = schemas.body(schemas.document({
            'mode': {'type': 'string', 'enum': ['new_copy', 'overwrite']},
            'name': schemas.SERVICE['properties']['name'],
            'confirmation': schemas.text(63),
        }))
        mode = options.get('mode', 'new_copy')
        service = objects.Service.get_by_id(ctx, row.service_id)
        if row.status != states.ACTIVE:
            raise exception.Conflict('Backup is not available.')
        available(ctx)
        if mode == 'overwrite':
            if options.get('confirmation') != service.name:
                raise exception.Invalid('Type the service name to overwrite.')
            if 'name' in options:
                raise exception.Invalid()
            with db_api.writer.using(ctx):
                lock_stack(ctx, service)
                service = common.lock(ctx, service)
                job = common.queue(ctx, row, service, mode)
        else:
            if not options.get('name') or 'confirmation' in options:
                raise exception.Invalid('A new service name is required.')
            # Normal service create owns quota, template storage accounting,
            # deployment and failure recovery. It also chooses fresh secrets.
            if service.kind != 'database' and not service.is_engine_service:
                raise exception.Conflict(
                    'New copy requires a compose service.')
            environment = objects.Environment.get_by_id(
                ctx, service.environment_id)
            if service.kind == 'database':
                values = dict(name=options['name'], engine=service.engine,
                              version=service.engine_version,
                              appgroup_id=environment.appgroup_id,
                              environment_id=environment.id,
                              server_id=service.server_id,
                              database=service.db_name,
                              username=service.db_username)
            else:
                values = dict(name=options['name'],
                              appgroup_id=environment.appgroup_id,
                              environment_id=environment.id,
                              server_id=service.server_id,
                              source=({'type': 'template',
                                       'template_id': service.template_id}
                                      if service.template_id else {
                                          'type': 'compose',
                                          'compose': secret_store.call(
                                              'get', ctx,
                                              service.compose_ref)}),
                              kind='service')
            deferred = DeferredCreate()
            with db_api.writer.using(ctx):
                db_api._lock_project(ctx.session, ctx.project_id)
                row = objects.Backup.get_by_id(ctx, row.id)
                if row.status != states.ACTIVE:
                    raise exception.Conflict()
                if service.kind == 'database':
                    from coast.api.v1 import database_controllers
                    result = database_controllers.RestoreCollection(
                        handler=deferred).create_database(ctx, values)
                    target_id = result['database']['id']
                else:
                    result = RestoreServices(handler=deferred).create_service(
                        ctx, values)
                    target_id = result['service']['id']
                target = objects.Service.get_by_id(ctx, target_id)
                job = common.queue(ctx, row, target, mode)
                job.started_at = timeutils.utcnow()  # Provisioning deadline.
                job.save()
            deferred.send()
        pecan.response.status = 202
        return {'restore': services.values(job, (
            'id', 'service_id', 'operation', 'status', 'safety_backup_id'))}

    @pecan.expose()
    def download(self, **kwargs):
        if pecan.request.method != 'GET':
            pecan.abort(405)
        ctx, row = self.get('download')
        if row.status != states.ACTIVE:
            raise exception.Conflict('Backup is not available.')
        available(ctx)
        policy = common.policy_for(ctx, row.service_id)
        storage = backup_storage.SwiftStorage(cfg.CONF, ctx, row.service_id)
        urls = storage.urls(policy, row, 'GET', len(row.segments))
        key = base64.b64decode(secret_store.call('get', ctx, row.key_ref),
                               validate=True)
        encrypted = download_chunks(urls)
        frames = wire.decrypt(wire.Input(encrypted), key)
        # Authenticate the header and first frame before returning HTTP 200.
        try:
            metadata, first = next(frames)
            if (metadata['engine'] != row.engine or
                    metadata['engine_version'] != row.engine_version):
                raise exception.CoastException('Backup metadata mismatch.')
        except Exception:
            frames.close()
            close = getattr(encrypted, 'close', None)
            if close:
                close()
            raise

        def stream():
            try:
                yield first
                for metadata, chunk in frames:
                    if chunk:
                        yield chunk
            finally:
                frames.close()
                close = getattr(encrypted, 'close', None)
                if close:
                    close()
        notifications.completed(ctx, row, 'download')
        return pecan.Response(
            app_iter=stream(), content_type='application/gzip',
            headers={'Cache-Control': 'no-store',
                     'Content-Disposition':
                     'attachment; filename="%s.dump.gz"' %
                     row.id})


def download_chunks(urls):
    transfer = wire.Transfer()
    for url in urls:
        with transfer.get(url) as response:
            if response.status_code != 200:
                raise exception.CoastException('Backup download failed.')
            yield from response.iter_content(65536)


class DeferredCreate:
    """Send the normal create RPC only after target and restore job commit."""

    def create_service(self, ctx, service, options):
        self.args = (ctx, service, options)

    def send(self):
        handlers.Handler().create_service(*self.args)


class RestoreServices(services.Collection):
    def render(self, row):
        # The clone and job share a transaction. Do not read uncommitted
        # source metadata through the response renderer's separate context.
        return {'id': row.id}
