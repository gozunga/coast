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

"""Quota limits. Project show also returns engine usage counters."""

import pecan

from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers as common
from coast.api.v1 import validation
from coast.common import exception
from coast.common import policy
from coast.common import quota


QUOTA = schemas.document({name: {'type': ['integer', 'null'], 'minimum': -1}
                          for name in quota.RESOURCES})


def authorize(action, project_id=None):
    ctx = common.context()
    policy.authorize(ctx, 'coast:quota:' + action,
                     {'project_id': project_id or ctx.project_id})
    try:
        policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
        ctx.is_admin = True
    except exception.NotAuthorized:
        ctx.is_admin = False
    validation.parameters(pecan.request.params, ())
    return ctx


class Quotas:
    @pecan.expose()
    def _lookup(self, project_id, *remainder):
        return Quota(project_id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        if pecan.request.method != 'GET':
            common.context()
            pecan.abort(405)
        ctx = authorize('get_all')
        return {'quotas': quota.get_all(ctx)}

    @pecan.expose('json')
    def defaults(self, **kwargs):
        if pecan.request.method != 'GET':
            common.context()
            pecan.abort(405)
        authorize('get_defaults')
        return {'quota': quota.defaults()}


class Quota:
    def __init__(self, project_id):
        self.project_id = project_id

    @pecan.expose('json')
    def index(self, **kwargs):
        method = pecan.request.method
        if method not in ('GET', 'PUT', 'DELETE'):
            common.context()
            pecan.abort(405)
        ctx = authorize({'GET': 'get_one', 'PUT': 'update',
                         'DELETE': 'delete'}[method], self.project_id)
        if not self.project_id or len(self.project_id) > 36:
            raise exception.Invalid()
        if method == 'GET':
            project_id = self.project_id
            return {'quota': quota.get(ctx, project_id),
                    'usage': quota.usage(ctx, project_id)}
        if method == 'PUT':
            values = schemas.body(QUOTA, 'quota')
            return {'quota': quota.update(ctx, self.project_id, values)}
        if pecan.request.body:
            raise exception.Invalid()
        quota.reset(ctx, self.project_id)
        return pecan.Response(status=204)
