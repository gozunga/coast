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

"""Durable deploy-key delivery, binding recovery and retryable cleanup.

Only references, public keys and engine identifiers are checkpointed. Each
external operation runs under the service row lock. Preparation must adopt
its deterministic registration after an uncertain reply. A binding with an
uncertain DB commit is restored to the old key on recovery, never guessed.
"""

import contextlib
import datetime

from oslo_utils import timeutils

from coast.api import secret_store
from coast.common import context
from coast.common import exception
from coast.common import notifications
from coast.common import states
from coast.db import api as db_api
from coast.drivers import base as driver_base
from coast import objects

OWNER = 'deploy_key_rotation'
RECOVERY_DELAY = 300
PAGE_SIZE = 100


@contextlib.contextmanager
def locked(manager, ctx, service_id):
    with db_api.writer.using(ctx):
        service = objects.Service.get_by_id(ctx, service_id)
        stack = objects.Stack.obj_from_db(
            ctx, db_api.stacks._get(ctx, service.stack_id, lock=True))
        service = manager._locked_service(ctx, service_id)
        source = objects.Source.get_by_id(ctx, service.source_id)
        yield service, source, stack


def checkpoint(source, **changes):
    pending = dict(source.deploy_key_rotation)
    pending.update(changes, updated_at=timeutils.utcnow().isoformat())
    source.deploy_key_rotation = pending
    source.save()


def _release(service):
    if (service.task_state == states.RECONCILING and
            service.task_owner == OWNER):
        service.release_task(states.RECONCILING)


def _cleanup(manager, ctx, service_id):
    # Each successful delete has its own durable receipt. Failures keep the
    # remaining references and are retried; committed rotation stays success.
    with locked(manager, ctx, service_id) as (service, source, stack):
        pending = source.deploy_key_rotation
        if not pending:
            return
        old = pending['phase'] == 'cleanup_old'
        key = 'old_engine_key' if old else 'new_engine_key'
        shared = (pending.get('old_engine_key') ==
                  pending.get('new_engine_key'))
        if pending.get(key) and not shared:
            manager._driver(stack).delete_deploy_key(
                manager._endpoint(stack), pending[key])
            checkpoint(source, **{key: None})
    with locked(manager, ctx, service_id) as (service, source, stack):
        pending = source.deploy_key_rotation
        ref = pending['previous'] if old else pending['reference']
        if ref:
            secret_store.call('delete', ctx, ref)
        source.deploy_key_rotation = None
        source.save()
        _release(service)


def _claim(manager, ctx, service_id, reference, public_key, recover):
    with locked(manager, ctx, service_id) as (service, source, stack):
        pending = source.deploy_key_rotation
        if not pending:
            raise exception.Conflict()
        if not recover and (pending['reference'] != reference or
                            pending['public_key'] != public_key):
            raise exception.Conflict()
        if recover:
            stamp = datetime.datetime.fromisoformat(pending['updated_at'])
            age = (timeutils.utcnow() - stamp).total_seconds()
            if age < RECOVERY_DELAY:
                return False
        if recover and service.task_state != states.NONE and (
                service.task_owner != OWNER):
            return False
        phase = pending['phase']
        if phase == 'queued':
            if (service.task_owner != OWNER or
                    service.task_state != states.RECONCILING):
                raise exception.Conflict()
            if stack.task_state not in (states.NONE, states.RECONCILING):
                # No engine call has happened. Retain cleanup ownership even
                # if Barbican is unavailable, but free the service claim.
                checkpoint(source, phase='cleanup_new')
                _release(service)
            else:
                checkpoint(source, phase='preparing')
        elif not recover:
            return False
        elif phase == 'binding':
            checkpoint(source, phase='rollback')
        return True


def _prepare(manager, ctx, service_id):
    with locked(manager, ctx, service_id) as (service, source, stack):
        pending = source.deploy_key_rotation
        abort = pending['phase'] == 'abort_preparing'
        if pending['phase'] not in ('preparing', 'abort_preparing'):
            return
        driver = manager._driver(stack)
        if driver.SUPPORTS_DEPLOY_KEY_ROTATION is False:
            checkpoint(source, phase='cleanup_new')
            _release(service)
            return
        private = secret_store.call('get', ctx, pending['reference'])
        try:
            old, new = driver.prepare_deploy_key(
                manager._endpoint(stack), stack, service, private,
                pending['reference'])
        except driver_base.EngineNotFound:
            checkpoint(source, phase='cleanup_new')
            _release(service)
            return
        checkpoint(source, phase='cleanup_new' if abort else 'prepared',
                   old_engine_key=old,
                   new_engine_key=new)


def _bind(manager, ctx, service_id):
    with locked(manager, ctx, service_id) as (service, source, stack):
        if source.deploy_key_rotation['phase'] == 'prepared':
            checkpoint(source, phase='binding')
    with locked(manager, ctx, service_id) as (service, source, stack):
        pending = source.deploy_key_rotation
        if pending['phase'] == 'binding':
            driver = manager._driver(stack)
            if driver.DEPLOY_KEY_IN_PLACE is True:
                private = secret_store.call('get', ctx, pending['reference'])
                driver.write_deploy_key(manager._endpoint(stack), stack,
                                        service, pending['new_engine_key'],
                                        private)
            else:
                driver.restore_deploy_key(
                    manager._endpoint(stack), stack, service,
                    pending['new_engine_key'])
            source.credentials_ref = pending['reference']
            source.deploy_public_key = pending['public_key']
            checkpoint(source, phase='cleanup_old')
            _release(service)
        return source.deploy_key_rotation['phase'] == 'cleanup_old'


def _rollback(manager, ctx, service_id):
    with locked(manager, ctx, service_id) as (service, source, stack):
        pending = source.deploy_key_rotation
        if pending and pending['phase'] == 'rollback':
            driver = manager._driver(stack)
            if driver.DEPLOY_KEY_IN_PLACE is True:
                private = secret_store.call('get', ctx, pending['previous'])
                driver.write_deploy_key(manager._endpoint(stack), stack,
                                        service, pending['old_engine_key'],
                                        private)
            else:
                driver.restore_deploy_key(
                    manager._endpoint(stack), stack, service,
                    pending['old_engine_key'])
            checkpoint(source, phase='cleanup_new')
            _release(service)


def mark_failure(service, source):
    """Checkpoint failure and free the claim while holding the service lock."""
    pending = source.deploy_key_rotation
    if pending and pending['phase'] in ('queued', 'prepared'):
        checkpoint(source, phase='cleanup_new')
    elif pending and pending['phase'] == 'preparing':
        # Adopt any uncertain registration before deleting the new key.
        checkpoint(source, phase='abort_preparing')
    elif pending and pending['phase'] == 'binding':
        checkpoint(source, phase='rollback')
    _release(service)


def _recover_failure(manager, ctx, service_id, reference=None,
                     public_key=None):
    with locked(manager, ctx, service_id) as (service, source, stack):
        pending = source.deploy_key_rotation
        if reference is not None and (
                not pending or pending['reference'] != reference or
                pending['public_key'] != public_key):
            return
        mark_failure(service, source)
    _rollback(manager, ctx, service_id)
    with locked(manager, ctx, service_id) as (service, source, stack):
        cleanup = (source.deploy_key_rotation or {}).get('phase')
    if cleanup == 'cleanup_new':
        _cleanup(manager, ctx, service_id)


def run(manager, ctx, service_id, reference=None, public_key=None,
        recover=False):
    """Consume only an admitted checkpoint; never trust RPC secret refs."""
    try:
        claimed = _claim(
            manager, ctx, service_id, reference, public_key, recover)
    except Exception:
        try:
            _recover_failure(manager, ctx, service_id, reference, public_key)
        except Exception:
            pass
        raise exception.Conflict('Deploy key update failed.') from None
    if not claimed:
        return False
    committed = False
    try:
        _prepare(manager, ctx, service_id)
        committed = _bind(manager, ctx, service_id)
        if committed:
            service = objects.Service.get_by_id(ctx, service_id)
            notifications.completed(ctx, service, 'update')
        _rollback(manager, ctx, service_id)
        _cleanup(manager, ctx, service_id)
    except Exception:
        if committed:
            # Cleanup has a durable retry checkpoint; the new key stays live.
            return True
        try:
            _recover_failure(manager, ctx, service_id)
        except Exception:
            # Never lose rollback refs or expose the restoration exception.
            pass
        raise exception.Conflict('Deploy key update failed.') from None
    if not committed and not recover:
        raise exception.Conflict('Deploy key update failed.')
    return committed


def sweep(manager):
    """Bounded, project-scoped retries for lost RPC and unfinished cleanup."""
    admin = context.RequestContext(is_admin=True)
    marker = getattr(manager, '_deploy_key_marker', None)
    try:
        page = objects.ServiceList.get_all(
            admin, marker=marker, limit=PAGE_SIZE, sort_key='id',
            all_projects=True)
    except Exception:
        manager._deploy_key_marker = None
        return
    for service in page:
        manager._deploy_key_marker = service.id
        ctx = context.RequestContext(project_id=service.project_id,
                                     is_admin=True)
        try:
            source = objects.Source.get_by_id(ctx, service.source_id)
            pending = source.deploy_key_rotation
            if not pending:
                continue
            stamp = datetime.datetime.fromisoformat(pending['updated_at'])
            if (timeutils.utcnow() - stamp).total_seconds() < RECOVERY_DELAY:
                continue
            run(manager, ctx, service.id, recover=True)
        except Exception:
            # Errors and their payloads stay out of logs and periodic RPC.
            continue
    if len(page) < PAGE_SIZE:
        manager._deploy_key_marker = None
