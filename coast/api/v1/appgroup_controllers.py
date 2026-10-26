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

"""Appgroup and Coolify environment endpoints.

Create writes the row, then casts the engine sync. Delete sets DELETING.
"""

import datetime
from urllib import parse

from oslo_config import cfg
import pecan

from coast.api import handlers
from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers
from coast.api.v1 import validation
from coast.common import exception
from coast.common import policy
from coast import objects


PUBLIC = {
    'appgroup': ('id', 'project_id', 'stack_id', 'name', 'description',
                 'status', 'created_at', 'updated_at'),
    'environment': ('id', 'project_id', 'appgroup_id', 'name',
                    'description', 'status', 'created_at', 'updated_at'),
}
_QUERY = {'limit', 'marker', 'sort_key', 'sort_dir', 'fields',
          'all_projects', 'project_id'}


def url(path):
    endpoint = cfg.CONF.api.public_endpoint or pecan.request.host_url
    return endpoint.rstrip('/') + path


def render(row, resource):
    names = PUBLIC[resource]
    result = {}
    for name in names:
        value = getattr(row, name)
        if isinstance(value, datetime.datetime):
            value = value.isoformat().replace('+00:00', 'Z')
        result[name] = value
    result['links'] = [{'rel': 'self', 'href': url(
        '/v1/%ss/%s' % (resource, row.id))}]
    fields = (pecan.request.params.get('fields')
              if pecan.request.method == 'GET' else None)
    if fields:
        requested = fields.split(',')
        for field in requested:
            validation.choice('field', field, set(names) | {'links'})
        result = {key: value for key, value in result.items()
                  if key in requested or key == 'links'}
    return result


class ChildCollection:
    """Nested list and create. Item routes stay at the top level."""

    resource = 'appgroup'
    parent_kind = 'stack'
    parent_field = 'stack_id'
    object_class = objects.Appgroup
    list_class = objects.AppgroupList

    def __init__(self, parent_id, handler=None):
        self.parent_id = parent_id
        self.handler = handler or handlers.Handler()

    def parent(self):
        ctx = stack_controllers.context()
        all_projects = stack_controllers.read_all_projects(self.resource)
        if self.parent_kind == 'stack':
            return objects.Stack.get_by_id(
                ctx, self.parent_id, all_projects=all_projects)
        return objects.Appgroup.get_by_id(
            ctx, self.parent_id, all_projects=all_projects)

    def collection_path(self):
        if self.resource == 'appgroup':
            return '/v1/stacks/%s/appgroups' % self.parent_id
        return '/v1/appgroups/%s/environments' % self.parent_id

    def create_row(self, ctx, parent, values):
        if self.resource == 'appgroup':
            return self.handler.create_appgroup(ctx, parent, values)
        return self.handler.create_environment(ctx, parent, values)

    @pecan.expose('json')
    def index(self, **kwargs):
        parent = self.parent()
        if pecan.request.method == 'GET':
            return self.list()
        if pecan.request.method != 'POST':
            pecan.abort(405)
        ctx = stack_controllers.authorize(self.resource, 'create')
        values = schemas.body(schemas.APPGROUP, self.resource)
        row = self.create_row(ctx, parent, values)
        pecan.response.status = 202
        return {self.resource: render(row, self.resource)}

    def list(self):
        ctx = stack_controllers.authorize(self.resource, 'get_all')
        params = dict(pecan.request.params)
        names = PUBLIC[self.resource]
        validation.parameters(params, set(names) | _QUERY)
        all_projects = params.pop('all_projects', 'false')
        validation.choice('all_projects', all_projects, ('true', 'false'))
        all_projects = all_projects == 'true'
        if all_projects or 'project_id' in params:
            policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
            all_projects = True
            ctx.is_admin = True
        limit = validation.limit(params.pop('limit', cfg.CONF.api.max_limit))
        limit = min(limit, cfg.CONF.api.max_limit)
        marker = params.pop('marker', None)
        if marker:
            marker_row = self.object_class.get_by_id(
                ctx, marker, all_projects=all_projects)
            if getattr(marker_row, self.parent_field) != self.parent_id:
                raise exception.NotFound()
        sort_key = params.pop('sort_key', 'created_at')
        sort_dir = params.pop('sort_dir', 'asc')
        fields = params.pop('fields', None)
        if fields:
            for field in fields.split(','):
                validation.choice('field', field, set(names) | {'links'})
        validation.choice('sort key', sort_key, names)
        validation.choice('sort direction', sort_dir, ('asc', 'desc'))
        for key in ('created_at', 'updated_at'):
            if key in params:
                try:
                    params[key] = datetime.datetime.fromisoformat(
                        params[key].replace('Z', '+00:00'))
                except ValueError:
                    raise validation.filter_value(
                        key, params[key], 'an ISO 8601 timestamp') from None
        params[self.parent_field] = self.parent_id
        rows = self.list_class.get_all(
            ctx, filters=params, limit=limit, marker=marker,
            sort_key=sort_key, sort_dir=sort_dir, all_projects=all_projects)
        links = []
        if limit and len(rows) == limit and self.list_class.get_all(
                ctx, filters=params, limit=1, marker=rows[-1].id,
                sort_key=sort_key, sort_dir=sort_dir,
                all_projects=all_projects):
            query = dict(pecan.request.params)
            query.update(marker=rows[-1].id, limit=limit)
            links = [{'rel': 'next', 'href': url(self.collection_path()) +
                      '?' + parse.urlencode(query)}]
        return {self.resource + 's': [render(row, self.resource)
                                      for row in rows],
                'links': links}


class StackAppgroups(ChildCollection):
    pass


class EnvironmentCollection(ChildCollection):
    # Policy resource is environment. Delete uses the app_environment
    # state key in the handler. ``environment`` is the stack state alias.
    resource = 'environment'
    parent_kind = 'appgroup'
    parent_field = 'appgroup_id'
    object_class = objects.Environment
    list_class = objects.EnvironmentList


class Record:
    resource = 'appgroup'
    object_class = objects.Appgroup

    def __init__(self, id, handler):
        self.id = id
        self.handler = handler

    def get(self):
        return self.object_class.get_by_id(
            stack_controllers.context(), self.id,
            all_projects=stack_controllers.read_all_projects(self.resource))

    def update_row(self, ctx, row, values):
        if self.resource == 'appgroup':
            return self.handler.update_appgroup(ctx, row, values)
        return self.handler.update_environment(ctx, row, values)

    def delete_row(self, ctx, row):
        if self.resource == 'appgroup':
            return self.handler.delete_appgroup(ctx, row)
        return self.handler.delete_environment(ctx, row)

    @pecan.expose('json')
    def index(self, **kwargs):
        row = self.get()
        method = pecan.request.method
        if method == 'GET':
            stack_controllers.authorize(self.resource, 'get_one', row)
            return {self.resource: render(row, self.resource)}
        if method == 'PATCH':
            ctx = stack_controllers.authorize(self.resource, 'update', row)
            values = schemas.body(schemas.APPGROUP_UPDATE, self.resource)
            row = self.update_row(ctx, row, values)
            return {self.resource: render(row, self.resource)}
        if method != 'DELETE':
            pecan.abort(405)
        ctx = stack_controllers.authorize(self.resource, 'delete', row)
        if pecan.request.body:
            schemas.body(schemas.document({}))
        row = self.delete_row(ctx, row)
        pecan.response.status = 202
        return {self.resource: render(row, self.resource)}


class AppgroupItem(Record):
    def __init__(self, id, handler):
        super().__init__(id, handler)
        self.environments = EnvironmentCollection(id, handler)


class EnvironmentItem(Record):
    resource = 'environment'
    object_class = objects.Environment


class Appgroups:
    """Top-level ``/v1/appgroups/{appgroup_id}`` routes."""

    def __init__(self, handler=None):
        self.handler = handler or handlers.Handler()

    @pecan.expose()
    def _lookup(self, appgroup_id, *remainder):
        return AppgroupItem(appgroup_id, self.handler), remainder


class Environments:
    """Top-level ``/v1/environments/{environment_id}`` routes."""

    def __init__(self, handler=None):
        self.handler = handler or handlers.Handler()

    @pecan.expose()
    def _lookup(self, environment_id, *remainder):
        return EnvironmentItem(environment_id, self.handler), remainder
