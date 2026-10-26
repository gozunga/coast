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

"""Project-scoped reads from packaged driver metadata; no engine calls."""

import hashlib
import json

from oslo_config import cfg
import pecan

from coast.api import microversions
from coast.api.v1 import stack_controllers as common
from coast.api.v1 import validation
from coast.common import exception
from coast.common import policy
from coast.common import template_catalog
from coast.drivers import base
from coast.drivers import loader
from coast import objects


def get_template(ctx, stack, template_id):
    policy.authorize(ctx, 'coast:template:get',
                     {'project_id': stack.project_id,
                      'template_id': template_id})
    try:
        return loader.load_driver(stack.driver, cfg.CONF).get_template(
            None, stack, template_id)
    except base.EngineNotFound:
        raise exception.NotFound() from None
    except base.UnsupportedEngineVersion:
        raise exception.Conflict('Template catalog is unavailable for this '
                                 'stack engine version.') from None
    except base.DriverError:
        raise exception.CoastException() from None


def stack_for_request():
    ctx = common.context()
    params = dict(pecan.request.params)
    validation.parameters(params, {'stack_id', 'all_projects', 'project_id'})
    if not params.get('stack_id'):
        raise exception.Invalid(
            "Invalid stack_id '%s'. Specify a stack ID." %
            params.get('stack_id', ''))
    stack = objects.Stack.get_by_id(
        ctx, params['stack_id'],
        all_projects=common.read_all_projects('template'))
    if 'project_id' in params and params['project_id'] != stack.project_id:
        raise exception.NotFound()
    return ctx, stack


def render(item):
    item = dict(item)
    if not microversions.at_least(pecan.request.version, '1.7'):
        item.pop('http_port', None)
    return item


class Templates:
    @pecan.expose()
    def _lookup(self, id, *remainder):
        return Template(id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        ctx, stack = stack_for_request()
        if pecan.request.method != 'GET':
            pecan.abort(405)
        target = {'project_id': stack.project_id}
        policy.authorize(ctx, 'coast:template:list', target)
        try:
            items = template_catalog.read(stack)
        except base.UnsupportedEngineVersion:
            raise exception.Conflict('Template catalog is unavailable for '
                                     'this stack engine version.') from None
        except base.DriverError:
            raise exception.CoastException() from None
        visible = []
        for item in items:
            try:
                policy.authorize(ctx, 'coast:template:get',
                                 dict(target, template_id=item['id']))
            except exception.NotAuthorized:
                continue
            visible.append(render(item))
        result = {'templates': visible}
        digest = hashlib.sha256(json.dumps(
            result, sort_keys=True, separators=(',', ':'),
            ensure_ascii=True).encode('utf-8')).hexdigest()
        # A weak validator describes the JSON value across renderers.
        pecan.response.headers['ETag'] = 'W/"%s"' % digest
        pecan.response.headers['Cache-Control'] = 'private, no-cache'
        if digest in pecan.request.if_none_match:
            pecan.response.status = 304
            pecan.response.body = b''
            return pecan.response
        return result


class Template:
    def __init__(self, id):
        self.id = id

    @pecan.expose('json')
    def index(self, **kwargs):
        ctx, stack = stack_for_request()
        if pecan.request.method != 'GET':
            pecan.abort(405)
        item = get_template(ctx, stack, self.id)
        return {'template': render(item)}
