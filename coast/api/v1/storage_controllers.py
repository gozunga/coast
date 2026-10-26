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

"""Named storage and service-owned host binds. API 1.10 and newer."""

import copy
from urllib import parse

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import timeutils
import pecan

from coast.api import microversions
from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers as stacks
from coast.api.v1 import validation
from coast.common import exception
from coast.common import file_mounts
from coast.common import notifications
from coast.common import policy
from coast.common import quota
from coast.common import states
from coast.common import storage as storage_rules
from coast.conductor import rpcapi
from coast.conductor import volume_delete
from coast.db import api as db_api
from coast import objects


LOG = logging.getLogger(__name__)


def _typed():
    return microversions.at_least(
        pecan.request.version, microversions.STORAGE_TYPES_VERSION)


def _gate():
    if not microversions.at_least(pecan.request.version, '1.10'):
        pecan.abort(404)


def _open_stack(ctx, stack_id):
    stack = volume_delete.lock_stack(ctx, stack_id)
    if stack.task_state != states.NONE:
        raise exception.Conflict()
    return stack


def _same_service(storage, service):
    if storage.service_id and storage.service_id != service.id:
        raise exception.Conflict()
    if storage.origin == 'template' or (
            service.is_engine_service and not _typed()):
        raise exception.Conflict('Template-defined volumes are read-only.')
    if service.stack_id != storage.stack_id:
        raise exception.Conflict()


def _worker(ctx, service):
    if not service.server_id:
        raise exception.Conflict()
    server = objects.Server.get_by_id(ctx, service.server_id)
    if (server.role != 'worker' or server.status != states.ACTIVE or
            server.id != service.server_id):
        raise exception.Conflict()
    return server


def _path_free(ctx, service_id, path, storage_id):
    if any(file_mounts.overlaps(path, row.container_path)
           for row in file_mounts.rows(ctx, service_id)):
        raise exception.Conflict()
    rows = objects.StorageList.get_all(
        ctx, filters={'service_id': service_id, 'status': states.IN_USE})
    for other in rows:
        if other.id != storage_id and other.container_path == path:
            raise exception.Conflict()


def _revert(ctx, storage_id, service_id, previous, changed_at):
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        fresh = objects.Storage.get_by_id(ctx, storage_id)
        for key, value in previous.items():
            setattr(fresh, key, value)
        fresh.save()
        service = objects.Service.get_by_id(ctx, service_id)
        service.settings_changed_at = changed_at
        service.save()


def _push(ctx, service, storage_id, previous, changed_at):
    if not service.engine_ref or service.is_engine_service:
        return
    try:
        rpcapi.ConductorAPI().apply_mounts(ctx, service.id)
    except Exception:
        try:
            _revert(ctx, storage_id, service.id, previous, changed_at)
        except Exception:
            LOG.warning('Storage mount revert failed for %s.', storage_id)
        raise


class Storages(stacks.Collection):
    resource = 'storage'
    nest = 'storages'
    object_class = objects.Storage
    list_class = objects.StorageList

    def read_context(self, ctx):
        if stacks.read_all_projects(self.resource):
            ctx = copy.copy(ctx)
            ctx.project_id = self.parent().project_id
        return ctx

    @pecan.expose()
    def _lookup(self, id, *remainder):
        _gate()
        return StorageItem(self, id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        _gate()
        self.parent()
        if pecan.request.method == 'GET':
            return self.list()
        if pecan.request.method != 'POST':
            pecan.abort(405)
        return self._create()

    def list(self):
        if not microversions.at_least(pecan.request.version, '1.16'):
            return super().list()
        ctx = stacks.authorize('storage', 'get_all')
        params = dict(pecan.request.params)
        names = set(stacks.STORAGE_FIELDS) | {'source'}
        if _typed():
            names |= {'type', 'host_path', 'read_only'}
        validation.parameters(params, names | {
            'fields', 'limit', 'marker', 'sort_key', 'sort_dir',
            'all_projects', 'project_id'})
        limit = min(validation.limit(params.pop(
            'limit', cfg.CONF.api.max_limit)), cfg.CONF.api.max_limit)
        all_projects = params.pop('all_projects', 'false')
        validation.choice('all_projects', all_projects, ('true', 'false'))
        if all_projects == 'true' or 'project_id' in params:
            policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
        marker = params.pop('marker', None)
        sort = params.pop('sort_key', 'created_at')
        direction = params.pop('sort_dir', 'asc')
        fields = params.pop('fields', None)
        validation.choice('sort key', sort, names)
        validation.choice('sort direction', direction, ('asc', 'desc'))
        if fields:
            for field in fields.split(','):
                validation.choice('field', field, names | {'links'})
        reader = (storage_rules.typed_rows if _typed() else
                  storage_rules.observed_rows)
        read_ctx = self.read_context(ctx)
        rows = reader(read_ctx, self.stack_id,
                      service_id=params.get('service_id'))
        if 'project_id' in params:
            rows = [row for row in rows
                    if row.project_id == params['project_id']]

        def value(row, key):
            if _typed() and key == 'source':
                return row.host_path if row.type == 'bind' else row.name
            if key == 'source':
                return 'coast' if row.origin == 'api' else 'template'
            if _typed() and key == 'origin':
                return 'user' if row.origin == 'api' else 'template'
            item = getattr(row, key)
            return item.isoformat() if hasattr(item, 'isoformat') else item
        rows.sort(key=lambda row: (value(row, sort) is not None,
                                   value(row, sort) or '', row.id),
                  reverse=direction == 'desc')
        if marker:
            position = next((i for i, row in enumerate(rows)
                             if row.id == marker), None)
            if position is None:
                raise exception.NotFound()
            rows = rows[position + 1:]

        def matches(row, key, item):
            field = value(row, key)
            return (str(field).lower() if isinstance(field, bool) else
                    str(field)) == item
        rows = [row for row in rows if all(
            matches(row, key, item) for key, item in params.items())]
        links = []
        if limit == 0:
            rows = []
        elif len(rows) > limit:
            rows = rows[:limit]
            query = dict(pecan.request.params, marker=rows[-1].id, limit=limit)
            links = [{'rel': 'next', 'href': stacks.url(self.path()) + '?' +
                      parse.urlencode(query)}]
        return {'storages': [self.render(row) for row in rows], 'links': links}

    def _create(self):
        ctx = stacks.authorize(self.resource, 'create')
        if _typed():
            return self._create_mount(ctx)
        values = schemas.body(schemas.STORAGE, 'storage')
        with db_api.writer.using(ctx):
            held = quota.reserve(ctx, storage_gb=values['size_gb'])
            _open_stack(ctx, self.stack_id)
            row = self.object_class(
                context=ctx, stack_id=self.stack_id, name=values['name'],
                size_gb=values['size_gb'], status=states.AVAILABLE,
                origin='api')
            row.create()
            quota.commit(ctx, held)
        notifications.completed(ctx, row, 'create')
        pecan.response.status = 201
        return {self.resource: self.render(row)}

    def _create_mount(self, ctx):
        values = schemas.body(schemas.STORAGE_MOUNT, 'storage')
        _validate_mount(values)
        service = objects.Service.get_by_id(ctx, values['service_id'])
        stacks.authorize('storage', 'attach')
        if service.stack_id != self.stack_id:
            raise exception.NotFound()
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            _open_stack(ctx, self.stack_id)
            service = objects.Service.get_by_id(ctx, service.id)
            server = _worker(ctx, service)
            _idle(service)
            _path_free(ctx, service.id, values['container_path'], None)
            size = values.get('size_gb', 0)
            if values['type'] == 'volume':
                storage_rules.ensure_capacity(ctx, server.id, size)
            held = quota.reserve(ctx, storage_gb=size)
            row = self.object_class(
                context=ctx, stack_id=self.stack_id,
                name=values.get('name'), size_gb=values.get('size_gb'),
                type=values['type'], host_path=values.get('host_path'),
                read_only=values.get('read_only', False), origin='api',
                status=states.IN_USE, service_id=service.id,
                server_id=server.id, container_path=values['container_path'])
            row.create()
            service.settings_changed_at = timeutils.utcnow()
            service.save()
            quota.commit(ctx, held)
        notifications.completed(ctx, row, 'create')
        pecan.response.headers['X-Coast-Redeploy-Required'] = 'true'
        pecan.response.status = 201
        return {self.resource: self.render(row)}


class StorageItem(stacks.Item):
    def get(self):
        if not microversions.at_least(pecan.request.version, '1.16'):
            return super().get()
        self.collection.parent()
        ctx = stacks.authorize('storage', 'get_one')
        # A desired-state write must not depend on engine availability.
        try:
            row = super().get()
            if pecan.request.method != 'GET' or row.origin == 'api':
                return row
        except exception.NotFound:
            pass
        rows = storage_rules.observed_rows(
            self.collection.read_context(ctx), self.collection.stack_id)
        row = next((row for row in rows if row.id == self.id), None)
        if row is None:
            raise exception.NotFound() from None
        if pecan.request.method != 'GET':
            action = ('delete' if pecan.request.method == 'DELETE' else
                      'attach' if pecan.request.method == 'PATCH' else
                      pecan.request.path_info.rsplit('/', 1)[-1])
            if action in ('delete', 'attach', 'detach'):
                stacks.authorize('storage', action, row)
            raise exception.Conflict(
                'Template-defined volumes are read-only.')
        return row

    @pecan.expose('json')
    def index(self, **kwargs):
        _gate()
        row = self.get()
        method = pecan.request.method
        if method == 'GET':
            stacks.authorize('storage', 'get_one', row)
            return self.response(row)
        if method == 'PATCH' and _typed() and row.origin == 'api':
            return self._edit_mount(row)
        if method == 'PATCH' and row.origin == 'template':
            stacks.authorize('storage', 'attach', row)
            raise exception.Conflict(
                'Template-defined volumes are read-only.')
        if method != 'DELETE':
            pecan.abort(405)
        return self._delete(row)

    @pecan.expose('json')
    def attach(self, **kwargs):
        _gate()
        if pecan.request.method != 'POST':
            pecan.abort(405)
        row = self.get()
        ctx = stacks.authorize('storage', 'attach', row)
        values = schemas.body(schemas.ATTACH, 'attach')
        if row.type == 'bind':
            raise exception.Conflict()
        if not file_mounts.path_ok(values['container_path']):
            raise exception.Invalid()
        service = objects.Service.get_by_id(ctx, values['service_id'])
        _same_service(row, service)
        saved, previous, changed_at = self._attach(
            ctx, row, service, values['container_path'])
        _push(ctx, service, saved.id, previous, changed_at)
        notifications.completed(ctx, saved, 'attach')
        return self.response(saved)

    def _attach(self, ctx, row, service, path):
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            _open_stack(ctx, row.stack_id)
            current = objects.Storage.get_by_id(ctx, row.id)
            service = objects.Service.get_by_id(ctx, service.id)
            _same_service(current, service)
            server = _worker(ctx, service)
            _path_free(ctx, service.id, path, current.id)
            storage_rules.ensure_capacity(
                ctx, server.id, current.size_gb, ignore_id=current.id)
            previous = {
                'status': current.status,
                'service_id': current.service_id,
                'server_id': current.server_id,
                'container_path': current.container_path,
            }
            changed_at = service.settings_changed_at
            current.status = states.IN_USE
            current.service_id = service.id
            current.server_id = server.id
            current.container_path = path
            current.save()
            service.settings_changed_at = timeutils.utcnow()
            service.save()
        return current, previous, changed_at

    @pecan.expose('json')
    def detach(self, **kwargs):
        _gate()
        if pecan.request.method != 'POST':
            pecan.abort(405)
        row = self.get()
        ctx = stacks.authorize('storage', 'detach', row)
        schemas.body(schemas.document({}))
        if row.type == 'bind':
            raise exception.Conflict('Bind mounts use PATCH or DELETE.')
        if row.origin == 'template':
            raise exception.Conflict(
                'Template-defined volumes are read-only.')
        saved, previous, changed_at, service = self._detach(ctx, row)
        if service is not None:
            _push(ctx, service, saved.id, previous, changed_at)
        notifications.completed(ctx, saved, 'detach')
        return self.response(saved)

    def _detach(self, ctx, row):
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            _open_stack(ctx, row.stack_id)
            current = objects.Storage.get_by_id(ctx, row.id)
            if current.origin == 'template':
                raise exception.Conflict()
            previous = {
                'status': current.status,
                'service_id': current.service_id,
                'server_id': current.server_id,
                'container_path': current.container_path,
            }
            service = None
            changed_at = None
            if current.service_id:
                service = objects.Service.get_by_id(ctx, current.service_id)
                changed_at = service.settings_changed_at
                service.settings_changed_at = timeutils.utcnow()
                service.save()
            current.status = states.AVAILABLE
            current.container_path = None
            current.save()
        return current, previous, changed_at, service

    def _delete(self, row):
        ctx = stacks.authorize('storage', 'delete', row)
        if pecan.request.body:
            schemas.body(schemas.document({}))
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            _open_stack(ctx, row.stack_id)
            current = objects.Storage.get_by_id(ctx, row.id)
            if current.origin == 'template':
                raise exception.Conflict(
                    'Template-defined volumes are read-only.')
            if current.status == states.IN_USE and not _typed():
                raise exception.Conflict()
            if _typed() and current.service_id:
                service = objects.Service.get_by_id(ctx, current.service_id)
                _idle(service)
                service.settings_changed_at = timeutils.utcnow()
                service.save()
                pecan.response.headers['X-Coast-Redeploy-Required'] = 'true'
            quota.release(ctx, storage_gb=current.size_gb or 0)
            current.destroy()
        notifications.completed(ctx, current, 'delete')
        headers = ({'X-Coast-Redeploy-Required': 'true'}
                   if _typed() and current.service_id else {})
        return pecan.Response(status=204, headers=headers)

    def _edit_mount(self, row):
        ctx = stacks.authorize('storage', 'attach', row)
        values = schemas.body(schemas.STORAGE_EDIT, 'storage')
        if not values:
            raise exception.Invalid()
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            _open_stack(ctx, row.stack_id)
            row = objects.Storage.get_by_id(ctx, row.id)
            if not row.service_id or row.status != states.IN_USE:
                raise exception.Conflict()
            service = objects.Service.get_by_id(ctx, row.service_id)
            _idle(service)
            merged = {key: getattr(row, key) for key in
                      ('type', 'name', 'size_gb', 'host_path',
                       'container_path', 'read_only')}
            merged.update(values)
            _validate_mount(merged)
            _path_free(ctx, service.id, merged['container_path'], row.id)
            for key, value in values.items():
                setattr(row, key, value)
            row.save()
            service.settings_changed_at = timeutils.utcnow()
            service.save()
        notifications.completed(ctx, row, 'update')
        pecan.response.headers['X-Coast-Redeploy-Required'] = 'true'
        return self.response(row)


def _idle(service):
    if service.task_state != states.NONE:
        raise exception.Conflict()


def _validate_mount(values):
    if not file_mounts.path_ok(values['container_path']):
        raise exception.Invalid()
    if values['type'] == 'bind':
        if (not storage_rules.host_path_ok(values.get('host_path')) or
                values.get('name') is not None or
                values.get('size_gb') is not None):
            raise exception.Invalid()
    elif (not values.get('name') or not values.get('size_gb') or
          values.get('host_path') is not None):
        raise exception.Invalid()
