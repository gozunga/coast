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

"""Project-scoped backup requests, durable locks and retention rules."""

import datetime

from oslo_utils import timeutils

from coast.common import engine_versions
from coast.common import exception
from coast.common import notifications
from coast.common import states
from coast.db import api as db_api
from coast import objects


POLICY_DEFAULTS = dict(enabled=False, hour=2, minute=0,
                       keep_daily=14, keep_weekly=8)


def rows(cls, ctx, **filters):
    # These are small per-service settings/history collections. Scan all
    # pages for internal cleanup; public lists use an explicit bounded page.
    marker = None
    while True:
        page = cls.get_all(ctx, filters=filters, marker=marker, limit=100)
        yield from page
        if len(page) < 100:
            return
        marker = page[-1].id


def policy_for(ctx, service_id):
    return next(rows(objects.BackupPolicyList, ctx, service_id=service_id),
                None)


def selection(service):
    info = engine_versions.DATABASES[service.engine]
    if not info['backups']:
        raise exception.Conflict('Backups are not available for this engine.')
    return dict(engine=info.get('backup_engine', service.engine),
                container=service.engine_ref or service.id)


def lock(ctx, service):
    db_api._lock_project(ctx.session, ctx.project_id)
    service = objects.Service.get_by_id(ctx, service.id)
    stack = objects.Stack.get_by_id(ctx, service.stack_id)
    if (stack.task_state != states.NONE or stack.status != states.ACTIVE or
            service.status != states.ACTIVE or not service.engine_ref or
            not service.server_id):
        raise exception.Conflict('Database service must be active and idle.')
    service.acquire_task(states.RECONCILING)
    return service


def new_backup(ctx, service, policy, kind):
    row = objects.Backup(
        context=ctx, service_id=service.id, kind=kind, status=states.PENDING,
        engine=policy.engine, container=policy.container)
    row.create()
    notifications.accepted(ctx, row, 'create')
    return row


def queue(ctx, backup, service, operation):
    job = objects.BackupJob(context=ctx, backup_id=backup.id,
                            service_id=service.id, operation=operation,
                            status=states.PENDING)
    job.create()
    notifications.accepted(ctx, job, operation)
    return job


def submit(ctx, service, kind='manual', scheduled_at=None):
    with db_api.writer.using(ctx):
        service = lock(ctx, service)
        policy = policy_for(ctx, service.id)
        if not policy:
            raise exception.Conflict('Set the database backup policy first.')
        if service.kind == 'database':
            for key, value in selection(service).items():
                setattr(policy, key, value)
            policy.save()
        if scheduled_at is not None:
            if (not policy.enabled or
                    (policy.last_scheduled_at is not None and
                     naive(policy.last_scheduled_at) >= naive(scheduled_at))):
                raise exception.Conflict()
            policy.last_scheduled_at = scheduled_at
            policy.save()
        backup = new_backup(ctx, service, policy, kind)
        queue(ctx, backup, service, 'backup')
    return backup


def change(ctx, row, status, action, **values):
    states.check_transition(row.repository.resource, row.status, status)
    row.status = status
    for key, value in values.items():
        setattr(row, key, value)
    row.save()
    notifications.emit(
        ctx, notifications.CONDUCTOR, row.repository.resource, action,
        'error' if status == states.ERROR else
        'start' if status in (states.BUILDING, states.DELETING) else 'end',
        notifications.payload_for(row, ctx))


def naive(value):
    return timeutils.normalize_time(value)


def due(policy, now=None):
    now = now or timeutils.utcnow()
    scheduled = now.replace(hour=policy.hour, minute=policy.minute,
                            second=0, microsecond=0)
    return (scheduled if policy.enabled and now >= scheduled and
            (policy.last_scheduled_at is None or
             naive(policy.last_scheduled_at) < naive(scheduled)) else None)


def expired(backups, policy):
    """Keep newest N UTC dates plus newest N ISO weeks; scheduled only.

    Manual and safety backups require explicit deletion. A failed backup
    never displaces a successful restore point.
    """
    daily, weekly, keep = set(), set(), set()
    candidates = sorted((row for row in backups if row.kind == 'scheduled'
                         and row.status == states.ACTIVE),
                        key=lambda row: (row.started_at, row.id), reverse=True)
    for row in candidates:
        date = row.started_at.date()
        week = date.isocalendar()[:2]
        if date not in daily and len(daily) < policy.keep_daily:
            daily.add(date)
            keep.add(row.id)
        if week not in weekly and len(weekly) < policy.keep_weekly:
            weekly.add(week)
            keep.add(row.id)
    return [row for row in candidates if row.id not in keep]


def stale(job, conf, now=None):
    now = now or timeutils.utcnow()
    timeout = 2 * max(conf.controller_worker.build_timeout,
                      conf.export.archive_timeout) + 300
    return (job.started_at is not None and
            naive(job.started_at) < naive(now) -
            datetime.timedelta(seconds=timeout))


def in_use(ctx, backup_id):
    return any(job.status in (states.PENDING, states.BUILDING) for job in
               rows(objects.BackupJobList, ctx, backup_id=backup_id))
