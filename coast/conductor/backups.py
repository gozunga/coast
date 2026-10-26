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

"""Durable database backup work on the existing conductor timer."""

import base64
import contextlib
import math
import os
import re

from oslo_utils import timeutils

from coast.api import secret_store
from coast.common import context
from coast.common import database_backups as common
from coast.common import engine_resources
from coast.common import exception
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import backup_storage
from coast.db import api as db_api
from coast.db import models
from coast import objects


FAULT = 'Database backup operation failed. Check storage and worker health.'


def _server(ctx, service):
    server = objects.Server.get_by_id(ctx, service.server_id)
    if (server.stack_id != service.stack_id or server.role != 'worker' or
            not server.mgmt_ip or not service.engine_ref):
        raise exception.Conflict('Database worker is not available.')
    return server


def _receipt(result, conf):
    if not isinstance(result, dict):
        raise ValueError('Invalid backup receipt')
    segments = result.get('segments')
    if (not isinstance(segments, list) or not segments or
            len(segments) > conf.backup.max_segments or
            type(result.get('size_bytes')) is not int or
            result['size_bytes'] <= 0 or
            not isinstance(result.get('engine_version'), str) or
            not re.fullmatch(r'[0-9][0-9A-Za-z ._+-]{0,127}',
                             result['engine_version'])):
        raise ValueError('Invalid backup receipt')
    for index, segment in enumerate(segments):
        if (not isinstance(segment, dict) or
                type(segment.get('size_bytes')) is not int or
                not 0 < segment['size_bytes'] <= conf.backup.segment_bytes or
                (index < len(segments) - 1 and
                 segment['size_bytes'] != conf.backup.segment_bytes)):
            raise ValueError('Invalid backup segment')
    for item in [result] + segments:
        if (not isinstance(item.get('sha256'), str) or
                not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
            raise ValueError('Invalid backup checksum')
    if sum(segment['size_bytes'] for segment in segments) != result[
            'size_bytes']:
        raise ValueError('Invalid backup size')
    duration(result)
    return segments


def duration(result):
    value = result.get('duration_seconds')
    if (type(value) not in (int, float) or not math.isfinite(value) or
            value < 0):
        raise ValueError('Invalid duration')
    return value


@contextlib.contextmanager
def engine_write(ctx, service, job):
    # Job recovery takes this same service lock before retiring a job.
    with db_api.writer.using(ctx):
        current = objects.Service.obj_from_db(
            ctx, db_api.services._get(ctx, service.id, lock=True))
        current_job = objects.BackupJob.get_by_id(ctx, job.id)
        if (current_job.service_id != service.id or
                current_job.status != states.BUILDING or
                current_job.started_at != job.started_at):
            raise exception.Conflict('Backup operation was recovered.')
        db_api.cas_task_state(ctx, current.repository.model, current.id,
                              expected=states.RECONCILING,
                              new=states.RECONCILING)
        yield


def capture(conf, ctx, service, policy, backup, job):
    storage = backup_storage.SwiftStorage(conf, ctx, service.id)
    common.change(ctx, backup, states.BUILDING, 'create',
                  started_at=timeutils.utcnow())
    try:
        storage.prepare(policy)
        if not policy.key_ref:
            reference = secret_store.call(
                'put', ctx, base64.b64encode(os.urandom(32)).decode('ascii'),
                secret_store.SecretIdentity(
                    service.stack_id, 'backup-key', policy.id,
                    service.project_id))
            try:
                policy.key_ref = reference
                policy.save()
            except Exception:
                secret_store.call('delete', ctx, reference)
                raise
        backup.key_ref = policy.key_ref
        backup.save()
        server = _server(ctx, service)
        key = secret_store.call('get', ctx, backup.key_ref)
        upload_urls = storage.urls(
            policy, backup, 'PUT', conf.backup.max_segments)
        with engine_write(ctx, service, job):
            result = agent_client.AgentClient(conf, server.mgmt_ip).request(
                'POST', '/v1/backup_database', dict(
                    service_id=service.engine_ref,
                    resource_type=engine_resources.resource_type(service),
                    container=(service.engine_ref if service.kind == 'database'
                               else backup.container),
                    engine=backup.engine, key=key,
                    upload_urls=upload_urls,
                    segment_bytes=conf.backup.segment_bytes))
        del key
        segments = _receipt(result, conf)
        # Save the receipt before publishing the manifest, for recovery.
        backup.segments = segments
        backup.save()
        storage.complete(backup, segments)
        common.change(ctx, backup, states.ACTIVE, 'create',
                      finished_at=timeutils.utcnow(),
                      size_bytes=result['size_bytes'], sha256=result['sha256'],
                      duration_seconds=duration(result),
                      engine_version=result['engine_version'])
    except Exception:
        common.change(ctx, backup, states.ERROR, 'create', error=FAULT,
                      finished_at=timeutils.utcnow())
        try:
            storage.delete(backup)
        except Exception:
            # ERROR records retain deterministic prefixes for cleanup retry.
            pass
        raise exception.CoastException(FAULT) from None


def restore(conf, ctx, job, backup, service):
    if backup.status != states.ACTIVE:
        raise exception.Conflict()
    policy = common.policy_for(ctx, backup.service_id)
    storage = backup_storage.SwiftStorage(conf, ctx, backup.service_id)
    if job.operation == 'overwrite':
        target_policy = common.policy_for(ctx, service.id)
        if (not target_policy or target_policy.engine != backup.engine or
                target_policy.container != backup.container):
            raise exception.Conflict('Database policy no longer matches.')
        with db_api.writer.using(ctx):
            safety = common.new_backup(ctx, service, target_policy, 'safety')
            job.safety_backup_id = safety.id
            job.save()
        # A failure here must prevent the first restore write.
        capture(conf, ctx, service, target_policy, safety, job)
    if objects.BackupJob.get_by_id(ctx, job.id).status != states.BUILDING:
        raise exception.Conflict('Restore operation was recovered.')
    server = _server(ctx, service)
    download_urls = storage.urls(
        policy, backup, 'GET', len(backup.segments))
    with engine_write(ctx, service, job):
        result = agent_client.AgentClient(conf, server.mgmt_ip).request(
            'POST', '/v1/restore_database', dict(
                service_id=service.engine_ref,
                resource_type=engine_resources.resource_type(service),
                container=(service.engine_ref if service.kind == 'database'
                           else backup.container),
                engine=backup.engine,
                target=(service.engine_ref if service.kind == 'database'
                        else backup.container),
                key=secret_store.call('get', ctx, backup.key_ref),
                download_urls=download_urls))
    duration(result)


def run_job(conf, ctx, identifier):
    with db_api.writer.using(ctx):
        job = objects.BackupJob.get_by_id(ctx, identifier)
        if job.status != states.PENDING:
            return
        service = objects.Service.get_by_id(ctx, job.service_id)
        # A new copy is provisioned by the normal service flow first.
        if job.operation == 'new_copy':
            if (service.status in (states.ERROR, states.DELETING) or
                    common.stale(job, conf)):
                common.change(ctx, job, states.ERROR, job.operation,
                              error='New database service is not ready.',
                              finished_at=timeutils.utcnow())
                return
            if service.status in (states.PENDING, states.STARTING,
                                  states.DEPLOYING, states.STOPPED):
                return
            service = common.lock(ctx, service)
        db_api.cas_status(ctx, models.BackupJob, job.id,
                          new=states.BUILDING, expected=states.PENDING)
        job = objects.BackupJob.get_by_id(ctx, job.id)
        common.change(ctx, job, states.BUILDING, job.operation,
                      started_at=timeutils.utcnow())
    backup = objects.Backup.get_by_id(ctx, job.backup_id)
    try:
        if job.operation == 'backup':
            capture(conf, ctx, service, common.policy_for(ctx, service.id),
                    backup, job)
        elif job.operation == 'delete':
            backup_storage.SwiftStorage(
                conf, ctx, backup.service_id).delete(backup)
            common.change(ctx, backup, states.DELETED, 'delete')
        else:
            restore(conf, ctx, job, backup, service)
        outcome = states.ACTIVE
    except Exception:
        outcome = states.ERROR
        # Recovery can already have failed this record. Refresh before
        # checking the legal transition and never revive a recovered backup.
        backup = objects.Backup.get_by_id(ctx, job.backup_id)
        if backup.status in (states.PENDING, states.BUILDING,
                             states.DELETING):
            common.change(ctx, backup, states.ERROR, job.operation,
                          error=FAULT, finished_at=timeutils.utcnow())
    finish_job(ctx, job, outcome)


def finish_job(ctx, job, outcome):
    try:
        with db_api.writer.using(ctx):
            db_api._lock_project(ctx.session, ctx.project_id)
            db_api.cas_status(ctx, models.BackupJob, job.id,
                              new=outcome, expected=states.BUILDING)
            job = objects.BackupJob.get_by_id(ctx, job.id)
            common.change(ctx, job, outcome, job.operation,
                          error=FAULT if outcome == states.ERROR else None,
                          finished_at=timeutils.utcnow())
            service = objects.Service.get_by_id(ctx, job.service_id)
            if service.task_state == states.RECONCILING:
                service.release_task(states.RECONCILING)
    except exception.Conflict:
        # Recovery owns completion. An old executor must not release a new
        # operation's service lock after its own lease was recovered.
        return


def recover(conf, ctx, job):
    if not common.stale(job, conf):
        return
    # Never repeat an uncertain destructive restore. The safety backup stays.
    with db_api.writer.using(ctx):
        db_api._lock_project(ctx.session, ctx.project_id)
        db_api.services._get(ctx, job.service_id, lock=True)
        db_api.cas_status(ctx, models.BackupJob, job.id,
                          new=states.ERROR, expected=states.BUILDING)
        job = objects.BackupJob.get_by_id(ctx, job.id)
        common.change(ctx, job, states.ERROR, job.operation,
                      error=FAULT, finished_at=timeutils.utcnow())
        if job.safety_backup_id:
            safety = objects.Backup.get_by_id(ctx, job.safety_backup_id)
            if safety.status in (states.PENDING, states.BUILDING):
                common.change(ctx, safety, states.ERROR, 'create',
                              error=FAULT, finished_at=timeutils.utcnow())
        backup = objects.Backup.get_by_id(ctx, job.backup_id)
        if backup.status in (states.PENDING, states.BUILDING,
                             states.DELETING):
            common.change(ctx, backup, states.ERROR, job.operation,
                          error=FAULT, finished_at=timeutils.utcnow())
        service = objects.Service.get_by_id(ctx, job.service_id)
        if service.task_state == states.RECONCILING:
            service.release_task(states.RECONCILING)


def tick(conf):
    admin = context.RequestContext(is_admin=True)
    # Keyset paging bounds scan memory. Only this internal inventory is global.
    for cls, action in ((objects.BackupPolicyList, 'schedule'),
                        (objects.BackupJobList, 'job')):
        marker = None
        while True:
            batch = cls.get_all(admin, all_projects=True, marker=marker,
                                limit=100)
            for item in batch:
                ctx = context.RequestContext(project_id=item.project_id)
                try:
                    if action == 'schedule':
                        schedule(conf, ctx, item.id)
                    elif item.status == states.PENDING:
                        run_job(conf, ctx, item.id)
                    elif item.status == states.BUILDING:
                        recover(conf, ctx, item)
                except Exception:
                    # Per-resource failures must not stop other projects.
                    continue
            if len(batch) < 100:
                break
            marker = batch[-1].id


def schedule(conf, ctx, identifier):
    policy = objects.BackupPolicy.get_by_id(ctx, identifier)
    service = objects.Service.get_by_id(ctx, policy.service_id)
    when = common.due(policy)
    for failed in common.rows(objects.BackupList, ctx,
                              service_id=service.id, status=states.ERROR):
        backup_storage.SwiftStorage(conf, ctx, service.id).delete(failed)
    if when:
        common.submit(ctx, service, 'scheduled', scheduled_at=when)
        return
    # Cleanup also runs with scheduling disabled. Never evict manual/safety.
    for row in common.expired(list(common.rows(
            objects.BackupList, ctx, service_id=service.id)), policy):
        with db_api.writer.using(ctx):
            service = common.lock(ctx, service)
            row = objects.Backup.get_by_id(ctx, row.id)
            if common.in_use(ctx, row.id):
                raise exception.Conflict('Backup is in use.')
            common.change(ctx, row, states.DELETING, 'delete')
            common.queue(ctx, row, service, 'delete')
        return  # One cleanup per service per pass, bounded and resumable.


def drop_service(conf, ctx, service):
    records = list(common.rows(objects.BackupList, ctx,
                               service_id=service.id))
    policy = common.policy_for(ctx, service.id)
    target_jobs = list(common.rows(objects.BackupJobList, ctx,
                                   service_id=service.id))
    if any(job.status in (states.PENDING, states.BUILDING)
           for job in target_jobs):
        raise exception.Conflict(
            'A database backup or restore is in progress.')
    if not records and not policy and not target_jobs:
        return
    for row in records:
        if any(job.status in (states.PENDING, states.BUILDING) for job in
               common.rows(objects.BackupJobList, ctx, backup_id=row.id)):
            raise exception.Conflict(
                'A database backup operation is in progress.')
    if records or (policy and policy.signing_key_ref):
        storage = backup_storage.SwiftStorage(conf, ctx, service.id)
        for row in records:
            storage.delete(row)
        storage.proxy.delete_container(storage.container, ignore_missing=True)
    references = {row.key_ref for row in records if row.key_ref}
    if policy and policy.key_ref:
        references.add(policy.key_ref)
    for reference in references:
        secret_store.call('delete', ctx, reference)
    for row in records:
        for job in list(common.rows(objects.BackupJobList, ctx,
                                    backup_id=row.id)):
            job.destroy()
        row.destroy()
    # New-copy jobs point to this service and a backup owned by the source.
    for job in list(common.rows(objects.BackupJobList, ctx,
                                service_id=service.id)):
        job.destroy()
    if policy:
        policy.destroy()
