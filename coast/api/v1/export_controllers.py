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

"""Nested exports with M1 acceptance and ephemeral URL seams."""

import copy
import datetime

from oslo_config import cfg
import pecan

from coast.api.v1 import schemas
from coast.api.v1 import service_controllers
from coast.api.v1 import stack_controllers as common
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.db import api as db_api
from coast import objects


ARTIFACT_FIELDS = ('volume_id', 'name', 'size_bytes', 'sha256')


def utc(value):
    return (value.astimezone(datetime.timezone.utc) if value.tzinfo else
            value.replace(tzinfo=datetime.timezone.utc))


def future(value):
    return value is not None and utc(value) > datetime.datetime.now(
        datetime.timezone.utc)


class Exports(common.Collection):
    resource = 'export'
    nest = 'exports'
    object_class = objects.Export
    list_class = objects.ExportList

    @pecan.expose()
    def _lookup(self, id, *remainder):
        return ExportItem(self, id), remainder

    def render(self, row):
        result = service_controllers.values(row, common.EXPORT_FIELDS)
        # Never expose backend object metadata, stored URLs or fault text.
        artifacts = [{key: item[key] for key in ARTIFACT_FIELDS if key in item}
                     for item in row.artifacts or []]
        try:
            common.authorize('export', 'renew_url', row)
            allowed = True
        except exception.NotAuthorized:
            allowed = False
        if (allowed and row.status == states.ACTIVE and
                future(row.object_expires_at)):
            ctx = common.context()
            if common.read_all_projects(self.resource):
                ctx = copy.copy(ctx)
                ctx.project_id = row.project_id
            stack = objects.Stack.get_by_id(ctx, row.stack_id)
            urls = (self.handler.export_urls(ctx, row)
                    if stack.driver == 'noop' else
                    self.handler.rpc.get_export_urls(ctx, row.id))
            by_volume = {item['volume_id']: item for item in urls}
            for item in artifacts:
                signed = by_volume.get(item.get('volume_id'), {})
                expires = signed.get('expires_at')
                if isinstance(expires, str):
                    try:
                        expires = datetime.datetime.fromisoformat(
                            expires.replace('Z', '+00:00'))
                    except ValueError:
                        expires = None
                if (future(expires) and
                        utc(expires) <= utc(row.object_expires_at)):
                    item.update(download_url=signed['download_url'],
                                expires_at=expires.isoformat().replace(
                                    '+00:00', 'Z'))
        result['artifacts'] = artifacts
        result['links'] = [{'rel': 'self',
                            'href': common.url(self.path(row.id))}]
        fields = (pecan.request.params.get('fields')
                  if pecan.request.method == 'GET' else None)
        if fields:
            self.validate_fields(fields)
            result = {key: value for key, value in result.items()
                      if key in fields.split(',') or key == 'links'}
        return result

    @pecan.expose('json')
    def index(self, **kwargs):
        stack = self.parent()
        if pecan.request.method == 'GET':
            return self.list()
        if pecan.request.method != 'POST':
            pecan.abort(405)
        ctx = common.authorize('export', 'create', stack)
        options = schemas.body(schemas.EXPORT, 'export')
        with db_api.writer.using(ctx):
            quota.reserve(ctx)  # Project sentinel before parent locks.
            stack.acquire_task(states.RECONCILING)
            if stack.status != states.ACTIVE:
                raise exception.Conflict()
            ids = options.get('volume_ids')
            if ids is None:
                volumes = [row for row in service_controllers.all_rows(
                    objects.VolumeList, ctx,
                    {'stack_id': stack.id})
                    if row.purpose in common.SNAPSHOT_PURPOSES]
            else:
                volumes = [objects.Volume.get_by_id(ctx, id) for id in ids]
            if not volumes:
                raise exception.Invalid()
            for volume in volumes:
                if (volume.stack_id != stack.id or
                        volume.purpose not in common.SNAPSHOT_PURPOSES):
                    raise exception.Invalid()
                if volume.status not in (states.AVAILABLE, states.IN_USE):
                    raise exception.Conflict()
            common.quota_hook(ctx, 'export', {
                'size_gb': sum(volume.size_gb for volume in volumes)})
            row = objects.Export(
                context=ctx, stack_id=stack.id,
                status=states.PENDING, backend=cfg.CONF.export.backend,
                volume_ids=[volume.id for volume in volumes])
            row.create()
        self.handler.create_export(ctx, row, options)
        pecan.response.status = 202
        return {'export': self.render(row)}


class ExportItem(common.Item):
    def response(self, row):
        return {'export': self.collection.render(row)}

    @pecan.expose('json')
    def index(self, **kwargs):
        row = self.get()
        if pecan.request.method == 'GET':
            common.authorize('export', 'get_one', row)
            return self.response(row)
        if pecan.request.method != 'DELETE':
            pecan.abort(405)
        ctx = common.authorize('export', 'delete', row)
        if pecan.request.body:
            schemas.body(schemas.document({}))
        if row.status == states.DELETING:
            raise exception.Conflict()
        with db_api.writer.using(ctx):
            row = objects.Export.obj_from_db(ctx, db_api.cas_status(
                ctx, objects.Export.repository.model, row.id,
                new=states.DELETING, expected=row.status))
        self.collection.handler.delete_export(ctx, row)
        pecan.response.status = 202
        return self.response(row)

    @pecan.expose('json')
    def action(self, **kwargs):
        if pecan.request.method != 'POST':
            pecan.abort(405)
        row = self.get()
        common.authorize('export', 'renew_url', row)
        schemas.body(schemas.document(
            {'renew_url': schemas.document({})}, ('renew_url',)))
        if row.status != states.ACTIVE or not future(row.object_expires_at):
            raise exception.Conflict()
        result = self.response(row)
        stack = objects.Stack.get_by_id(common.context(), row.stack_id)
        artifacts = result['export']['artifacts']
        if (stack.driver != 'noop' and
                (not artifacts or any('download_url' not in item
                                      for item in artifacts))):
            raise exception.Conflict()
        return result
