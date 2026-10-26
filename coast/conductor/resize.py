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

"""Serial resize orchestration using the retained-resource failover flows."""

from coast.common import states
from coast.conductor import controller_failover
from coast.conductor import rpcapi
from coast.conductor import server_failover
from coast.db import api as db_api
from coast import objects


def execute(owner, ctx, resource, options, revision):
    emitter = server_failover.FailoverServer(owner)
    owner = emitter.owner
    rolling = states.kind(resource.repository.resource) == 'stack'
    stack_id = resource.id if rolling else resource.stack_id
    action = 'resize_workers' if rolling else 'resize'
    receipt = rpcapi.receipt_id(action + ('_stack' if rolling else '_server'),
                                resource, revision)
    targets = (options or {}).get('targets', {})
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        stack = objects.Stack.obj_from_db(ctx, db_api.stacks._get(
            ctx, stack_id, lock=True))
        current = (stack if rolling else objects.Server.get_by_id(
            ctx, resource.id))
        if (stack.task_state != states.FAILING_OVER or
                rpcapi.revision(current) != (revision or rpcapi.revision(
                    resource)) or objects.EventList.get_all(ctx, filters={
                        'id': receipt}, limit=1)):
            return
        owner._save(stack, conductor_host=owner.conf.host)
        owner._event(ctx, resource, stack, action, 'start', receipt)
    emitter.flush()
    success = False
    completed = []
    failed = None
    try:
        success = bool(targets)
        for identifier in sorted(targets):
            success = False
            failed = identifier
            target = dict(targets[identifier], rolling=rolling,
                          admission_status=(options or {}).get(
                              'admission_status'))
            with db_api.writer.using(ctx):
                server = objects.Server.get_by_id(ctx, identifier)
                if server.stack_id != stack.id:
                    success = False
                    break
                if rolling:
                    if (server.status != states.ACTIVE or
                            server.task_state != states.NONE):
                        success = False
                        break
                    target['admission_status'] = server.status
                    server.acquire_task(states.FAILING_OVER)
                    owner._save(server, status=states.FAILOVER)
            flow = (controller_failover.ControllerFailover(owner)
                    if server.role == 'control' else
                    server_failover.FailoverServer(owner))
            if stack.driver == 'noop':
                with db_api.writer.using(ctx):
                    owner._save(server, flavor_id=target['target_flavor'],
                                status=states.ACTIVE)
                    if server.role == 'control':
                        owner._save(stack, plan=target['target_plan'])
                    server.release_task(states.FAILING_OVER)
                flow.succeeded = True
            else:
                flow.execute(ctx, server, target,
                             revision=rpcapi.revision(server))
            if not flow.succeeded:
                break
            success = True
            completed.append(identifier)
            failed = None
    finally:
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            stack = objects.Stack.get_by_id(ctx, stack_id)
            if failed:
                server = objects.Server.get_by_id(ctx, failed)
                if server.task_state == states.FAILING_OVER:
                    owner._save(server, status=states.ERROR,
                                fault='Resize failed; operator recovery '
                                      'may be required.')
                    server.release_task(states.FAILING_OVER)
            if stack.task_state == states.FAILING_OVER:
                stack.release_task(states.FAILING_OVER)
            current = (stack if rolling else objects.Server.get_by_id(
                ctx, resource.id))
            owner._event(ctx, current, stack, action,
                         'success' if success else 'failure', receipt,
                         detail={'completed_server_ids': completed,
                                 'failed_server_id': failed})
        emitter.flush()
