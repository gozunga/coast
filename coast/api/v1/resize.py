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

"""Admission for project-owned VM growth (API 1.26)."""

import pecan

from oslo_config import cfg

from coast.api import microversions
from coast.api.v1 import plan_controllers
from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers as common
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import controller
from coast.conductor import engine_upgrade
from coast.conductor import rpcapi
from coast.conductor import volume_delete
from coast.db import api as db_api
from coast import objects


def requested():
    try:
        body = pecan.request.json
    except ValueError:
        return False
    return isinstance(body, dict) and bool(
        set(body) & {'resize', 'resize_workers'})


def selection(server, flavor):
    role = server.role
    values = ({'plan': flavor} if role == 'control' else
              {'build_server_plan': flavor} if role == 'build' else
              {'workers': [{'plan': flavor}]})
    common.flavor_to_plan(values)
    name = (values.get('plan') if role == 'control' else
            values.get('build_server_plan') if role == 'build' else
            values['workers'][0]['plan'])
    plans = [p for p in plan_controllers.load_plans()
             if p['name'] == name and p['kind'] == 'vm' and role in p['roles']]
    if server.kind != 'vm' or len(plans) != 1:
        raise exception.Invalid('Choose an available VM flavor for this role.')
    nova = controller.ControllerCloud(cfg.CONF).proxy('nova')
    old = nova.find_flavor(server.flavor_id)
    new = nova.find_flavor(plans[0]['flavor'])
    try:
        dimensions = ('vcpus', 'ram', 'disk', 'ephemeral')
        before = tuple(int(getattr(old, key)) for key in dimensions)
        after = tuple(int(getattr(new, key)) for key in dimensions)
        larger = all(a >= b for a, b in zip(after, before)) and after != before
    except (AttributeError, TypeError, ValueError):
        larger = False
    if not larger:
        raise exception.Invalid(
            'Choose a strictly larger flavor; to go smaller, add a new, '
            'smaller worker and move your applications instead.')
    return {'target_flavor': new.id, 'target_plan': name}


def accept(item, row):
    if not microversions.at_least(pecan.request.version,
                                  microversions.RESIZE_VERSION):
        raise exception.Invalid('Resize requires API microversion 1.26.')
    rolling = item.collection.resource == 'stack'
    action = 'resize_workers' if rolling else 'resize'
    values = schemas.body(schemas.document({action: schemas.document(
        {'flavor': schemas.text(255)}, ('flavor',))}, (action,)))
    ctx = common.authorize(item.collection.resource, action, row)
    if ctx.project_id != row.project_id:
        raise exception.NotFound()
    stack_id = row.id if rolling else row.stack_id
    servers = (objects.ServerList.get_all(ctx, filters={
        'stack_id': stack_id, 'role': 'worker'}) if rolling else [row])
    if not servers:
        raise exception.Invalid('This stack has no workers to resize.')
    targets = {s.id: selection(s, values[action]['flavor']) for s in servers}
    options = {'targets': targets}
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        stack = volume_delete.lock_stack(ctx, stack_id)
        if stack.status != states.ACTIVE or stack.task_state != states.NONE:
            raise exception.Conflict()
        engine_upgrade.idle(ctx, stack)
        current = [objects.Server.obj_from_db(ctx, s) for s in
                   db_api.servers._query(ctx).filter_by(stack_id=stack_id)
                   .with_for_update().all()]
        if any(s.task_state != states.NONE for s in current):
            raise exception.Conflict()
        chosen = [s for s in current if s.id in targets]
        if (len(chosen) != len(servers) or
                (rolling and {s.id for s in current if s.role == 'worker'} !=
                 set(targets)) or any(s.status != states.ACTIVE or
                                      s.flavor_id != next(
                                         old.flavor_id for old in servers
                                         if old.id == s.id) for s in chosen)):
            raise exception.Conflict()
        # Resize adds no servers or storage. Check current capacity against
        # operator limits; there is no positive quota delta to reserve.
        usage = quota.usage(ctx)
        for key in quota.RESOURCES:
            if not (usage['in_use'][key] or usage['reserved'][key]):
                usage['in_use'][key] = quota._existing(
                    session, ctx.project_id, key)
        limits = quota.get(ctx)
        exceeded = [key for key in quota.RESOURCES if limits[key] != -1 and
                    usage['in_use'][key] + usage['reserved'][key] >
                    limits[key]]
        if exceeded:
            raise exception.OverQuota(resources=exceeded)
        ctx.quota_reservation = quota.reserve(ctx)
        stack.acquire_task(states.FAILING_OVER)
        if not rolling:
            row = chosen[0]
            options['admission_status'] = row.status
            item.transition(row, states.FAILOVER, states.FAILING_OVER)
        else:
            row = stack
    operation = 'resize_workers_stack' if rolling else 'resize_server'
    revision = rpcapi.revision(row)
    try:
        item.collection.handler.rpc.cast(ctx, operation, row, options)
    except Exception:
        # An uncertain cast can be released only if no conductor has claimed
        # this exact revision. A later delivery is fenced by the changed row.
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            stack = volume_delete.lock_stack(ctx, stack_id)
            current = (stack if rolling else objects.Server.get_by_id(
                ctx, row.id))
            started = objects.EventList.get_all(ctx, filters={
                'id': rpcapi.receipt_id(operation, row, revision)}, limit=1)
            if rpcapi.revision(current) == revision and not started:
                if not rolling:
                    current.status = states.ACTIVE
                    current.save()
                    current.release_task(states.FAILING_OVER)
                stack.release_task(states.FAILING_OVER)
        raise
    pecan.response.status = 202
    return item.response(row)
