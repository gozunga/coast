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

"""Write-only file content, scoped to one workload and project."""

from oslo_utils import timeutils
import pecan

from coast.api import microversions
from coast.api import secret_store
from coast.api.v1 import schemas
from coast.api.v1 import service_controllers as services
from coast.api.v1 import stack_controllers as common
from coast.api.v1 import validation
from coast.common import exception
from coast.common import file_mounts
from coast.common import notifications
from coast.common import secret_limits
from coast.common import states
from coast.db import api as db_api
from coast import objects


_FIELDS = ('id', 'project_id', 'service_id', 'container_path',
           'resource_name', 'created_at', 'updated_at')


def _view(row):
    return services.values(row, _FIELDS)


def _body(create=False):
    schema = schemas.document({
        'container_path': schemas.text(255),
        'content': {'type': 'string'},
        'resource_name': schemas.text(255),
    }, ('container_path', 'content') if create else ())
    values = schemas.body(schema, 'file_mount')
    if 'content' in values:
        secret_limits.validate(values['content'], 'File mount content',
                               file_mounts.MAX_BYTES)
    if not values:
        raise exception.Invalid()
    file_mounts.validate(values)
    return values


class FileMounts:
    def __init__(self, service_id, kind=None):
        self.service_id = service_id
        self.kind = kind

    def parent(self):
        if not microversions.at_least(pecan.request.version, '1.15'):
            pecan.abort(404)
        service = objects.Service.get_by_id(common.context(), self.service_id)
        if self.kind and service.kind != self.kind:
            raise exception.NotFound()
        return service

    @pecan.expose()
    def _lookup(self, id, *remainder):
        self.parent()
        return FileMountItem(self, id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        service = self.parent()
        method = pecan.request.method
        if method not in ('GET', 'POST'):
            pecan.abort(405)
        ctx = common.authorize(
            'file_mount', 'get_all' if method == 'GET' else 'create', service)
        validation.parameters(pecan.request.params, ())
        if method == 'GET':
            return {'file_mounts': [_view(row) for row in
                                    file_mounts.rows(ctx, service.id)]}
        values = _body(create=True)
        if 'resource_name' in values and not service.is_engine_service:
            raise exception.Invalid()
        with services.secret_writes(ctx) as refs, db_api.writer.using(ctx):
            service = self.lock(ctx, service)
            file_mounts.check_free(ctx, service.id, values['container_path'])
            ref = secret_store.call(
                'put', ctx, values.pop('content'),
                secret_store.SecretIdentity.for_resource(
                    service, 'file-mount'))
            refs.append(ref)
            row = objects.FileMount(context=ctx, service_id=service.id,
                                    content_ref=ref, **values)
            row.create()
            self.changed(service, refs)
        notifications.completed(ctx, row, 'create')
        pecan.response.status = 201
        return {'file_mount': _view(row)}

    def lock(self, ctx, service):
        db_api._lock_project(ctx.session, ctx.project_id)
        services.stack_open(ctx, service.stack_id)
        service = objects.Service.get_by_id(ctx, service.id)
        if service.task_state != states.NONE:
            raise exception.Conflict()
        services.settings_lock(ctx, service)
        return service

    def changed(self, service, refs):
        service.file_mount_secret_refs = (
            service.file_mount_secret_refs or []) + list(refs)
        service.settings_changed_at = timeutils.utcnow()
        service.save()
        pecan.response.headers['X-Coast-Redeploy-Required'] = 'true'


class FileMountItem:
    def __init__(self, collection, id):
        self.collection = collection
        self.id = id

    def get(self, ctx, service):
        row = objects.FileMount.get_by_id(ctx, self.id)
        if row.service_id != service.id:
            raise exception.NotFound()
        return row

    @pecan.expose('json')
    def index(self, **kwargs):
        service = self.collection.parent()
        ctx = common.context()
        row = self.get(ctx, service)
        method = pecan.request.method
        action = {'GET': 'get_one', 'PATCH': 'update', 'DELETE': 'delete'}
        if method not in action:
            pecan.abort(405)
        common.authorize('file_mount', action[method], service)
        validation.parameters(pecan.request.params, ())
        if method == 'GET':
            return {'file_mount': _view(row)}
        values = _body() if method == 'PATCH' else {}
        if 'resource_name' in values and not service.is_engine_service:
            raise exception.Invalid()
        if method == 'DELETE' and pecan.request.body:
            schemas.body(schemas.document({}))
        with services.secret_writes(ctx) as refs, db_api.writer.using(ctx):
            service = self.collection.lock(ctx, service)
            row = self.get(ctx, service)
            if method == 'DELETE':
                row.destroy()
            else:
                path = values.get('container_path', row.container_path)
                file_mounts.check_free(ctx, service.id, path, row.id)
                if 'content' in values:
                    ref = secret_store.call(
                        'put', ctx, values.pop('content'),
                        secret_store.SecretIdentity.for_resource(
                            service, 'file-mount'))
                    refs.append(ref)
                    values['content_ref'] = ref
                for key, value in values.items():
                    setattr(row, key, value)
                row.save()
            self.collection.changed(service, refs)
        notifications.completed(ctx, row, action[method])
        if method == 'DELETE':
            return pecan.Response(status=204, headers={
                'X-Coast-Redeploy-Required': 'true'})
        return {'file_mount': _view(row)}
