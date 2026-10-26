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

"""Two-phase token rotation with durable checkpoints and fenced claims."""

import datetime
import uuid

from oslo_utils import timeutils

from coast.api import secret_store
from coast.common import context
from coast.common import exception
from coast.common import notifications
from coast.common import states
from coast.conductor import agent_client
from coast.db import api
from coast import objects

BOOTSTRAP_NAME = 'coast-agent-bootstrap-v1'
TASK = states.UPGRADING
ACTION = 'rotate_engine_token'


def lease_seconds(conf):
    # Larger than any bounded guest request. Renew before each external step.
    return 3 * conf.controller_worker.build_timeout + 60


def _busy_children(ctx, stack_id):
    for repo in (api.servers, api.services, api.appgroups):
        if repo._query(ctx).filter(
                repo.model.stack_id == stack_id,
                repo.model.task_state != states.NONE).first():
            return True
    if api.environments._query(ctx).join(
            api.appgroups.model,
            api.environments.model.appgroup_id ==
            api.appgroups.model.id).filter(
                api.appgroups.model.stack_id == stack_id,
                api.environments.model.task_state != states.NONE).first():
        return True
    return False


def claim(manager, ctx, stack_id):
    now = timeutils.utcnow()
    cutoff = now - datetime.timedelta(seconds=lease_seconds(manager.conf))
    owner = str(uuid.uuid4())
    with api.writer.using(ctx):
        # Serialize the fleet-wide slot count, then follow normal project and
        # stack lock ordering. No database lock spans an external request.
        api._lock_project(ctx.session, 'coast-engine-token-rotation')
        api._lock_project(ctx.session, ctx.project_id)
        stack = objects.Stack.obj_from_db(
            ctx, api.stacks._get(ctx, stack_id, lock=True))
        if not stack.engine_token_name:
            # Migrated unnamed tokens cannot be rotated or backfilled. Refuse
            # before taking the task so recovery never inherits a stuck flow.
            raise exception.Conflict('Engine token has no managed name')
        if (stack.status != states.ACTIVE or not stack.engine_token_ref or
                stack.task_state not in (states.NONE, TASK) or
                (stack.task_state == TASK and
                 not stack.engine_token_rotation_id) or
                (stack.task_state == TASK and stack.task_started_at and
                 stack.task_started_at.replace(tzinfo=None) > cutoff) or
                _busy_children(ctx, stack.id)):
            raise exception.Conflict(
                'Stack has work in progress or is not active')
        active = ctx.session.query(api.stacks.model).filter(
            api.stacks.model.task_state == TASK,
            api.stacks.model.engine_token_rotation_id.isnot(None),
            api.stacks.model.task_started_at > cutoff).count()
        if active >= manager.conf.secrets.rotation_max_concurrent:
            raise exception.Conflict('Engine token rotation limit reached')
        if stack.task_state == states.NONE:
            stack.acquire_task(TASK)
        stack.engine_token_rotation_id = (
            stack.engine_token_rotation_id or str(uuid.uuid4()))
        stack.save()
        row = api.stacks._get(ctx, stack_id, lock=True)
        row.task_owner = owner
        row.task_started_at = now
        ctx.session.flush()
        stack = objects.Stack.obj_from_db(ctx, row)
    return stack, owner


def checkpoint(ctx, stack_id, owner, **values):
    with api.writer.using(ctx):
        stack = objects.Stack.obj_from_db(
            ctx, api.stacks._get(ctx, stack_id, lock=True))
        if stack.task_state != TASK or stack.task_owner != owner:
            raise exception.Conflict('Engine token rotation claim changed')
        # Repository updates deliberately cannot change task ownership.
        row = api.stacks._get(ctx, stack_id, lock=True)
        for name in ('task_owner', 'task_started_at'):
            if name in values:
                setattr(row, name, values.pop(name))
        ctx.session.flush()
        for name, value in values.items():
            setattr(stack, name, value)
        stack.save()
        return objects.Stack.obj_from_db(ctx, row)


def rotate(manager, ctx, stack_id):
    try:
        controllers = objects.ServerList.get_all(ctx, filters={
            'stack_id': stack_id, 'role': 'control'})
        if (len(controllers) != 1 or controllers[0].status != states.ACTIVE or
                not controllers[0].mgmt_ip or controllers[0].kind != 'vm'):
            raise exception.Conflict(
                'A healthy managed controller is required')
        client = agent_client.AgentClient(manager.conf, controllers[0].mgmt_ip)
        if client.request('GET', '/health').get('status') != 'ok':
            raise exception.Conflict(
                'A healthy managed controller is required')
    except Exception:
        raise exception.CoastException(
            'A healthy managed controller is required') from None
    stack, owner = claim(manager, ctx, stack_id)

    def save(**values):
        return checkpoint(ctx, stack.id, owner, **values)

    def renew():
        save(task_started_at=timeutils.utcnow())

    def notify(phase):
        notifications.emit(
            ctx, notifications.CONDUCTOR, 'stack', ACTION, phase,
            notifications.payload_for(stack, ctx), manager.conf,
            notifier=manager.notifier)

    try:
        notify('start')
        identifier = stack.engine_token_rotation_id
        if not stack.engine_token_previous_ref:
            previous_name = stack.engine_token_name
            renew()
            token = client.rotate_token(stack.engine_version, identifier,
                                        previous_name)
            renew()
            reference = secret_store.call(
                'put', ctx, token,
                secret_store.SecretIdentity.for_resource(
                    stack, 'engine-token'))
            # A crash after put can leave an attributed orphan, but the old
            # token remains stored and valid. Never revoke before this commit.
            stack = save(engine_token_ref=reference,
                         engine_token_previous_ref=stack.engine_token_ref,
                         engine_token_previous_name=previous_name,
                         engine_token_name=(
                             'coast-agent-rotation-' + identifier),
                         engine_token_created_at=timeutils.utcnow())
        renew()
        client.revoke_token(stack.engine_version, identifier,
                            stack.engine_token_previous_name)
        renew()
        secret_store.call('delete', ctx, stack.engine_token_previous_ref)
        with api.writer.using(ctx):
            stack = save(engine_token_previous_ref=None,
                         engine_token_previous_name=None,
                         engine_token_rotation_id=None)
            stack.release_task(TASK)
        notify('end')
    except Exception:
        # Keep the exclusive stack task and durable protocol state for retry;
        # release only the execution claim. A killed process expires instead.
        try:
            save(task_owner=None, task_started_at=None)
        except exception.Conflict:
            pass
        notify('error')
        raise exception.CoastException(
            'Engine token rotation failed') from None


def tick(manager):
    days = manager.conf.secrets.engine_token_rotation_days
    if not days:
        return
    admin = context.RequestContext(is_admin=True)
    cutoff = timeutils.utcnow() - datetime.timedelta(days=days)
    marker = None
    while True:
        batch = objects.StackList.get_all(
            admin, filters={'status': states.ACTIVE}, all_projects=True,
            sort_key='id', marker=marker)
        if not batch:
            return
        for stack in batch:
            stamp = stack.engine_token_created_at or stack.created_at
            if (stack.driver == 'noop' or stack.plan == 'adopted' or
                    (not stack.engine_token_rotation_id and
                     stamp.replace(tzinfo=None) > cutoff)):
                continue
            ctx = context.RequestContext(project_id=stack.project_id)
            try:
                rotate(manager, ctx, stack.id)
            except exception.CoastException:
                # Notifications carry only identifiers and a fixed outcome.
                # A later tick resumes the same rotation, without secret logs.
                continue
        marker = batch[-1].id
