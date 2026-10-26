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

"""Prune superseded app secrets only after their last DB reference commits."""

from oslo_log import log as logging
from sqlalchemy import event

from coast.api import secret_store
from coast.common import app_secrets
from coast.common import context as context_module
from coast.common import states
from coast.db import models


LOG = logging.getLogger(__name__)
_KEY = 'coast_secret_pruning'


def track(session, row, values=None):
    fields = {models.Secret: {'value_ref'},
              models.FileMount: {'content_ref'},
              models.Service: {'compose_ref', 'dockerfile_ref',
                               'file_mount_secret_refs'}}
    if values is not None and not fields.get(type(row), set()) & values.keys():
        return
    if isinstance(row, models.Secret):
        refs = {row.value_ref}
    elif isinstance(row, models.FileMount):
        refs = {row.content_ref}
    elif isinstance(row, models.Deployment):
        refs = app_secrets.spec_refs(row.spec)
    elif isinstance(row, models.Service):
        refs = set(row.file_mount_secret_refs or [])
        if row.compose_ref:
            refs.add(row.compose_ref)
        if row.dockerfile_ref:
            refs.add(row.dockerfile_ref)
    else:
        return
    service_id = row.id if isinstance(row, models.Service) else row.service_id
    if not session.info.get(_KEY + '_listeners'):
        session.info[_KEY + '_listeners'] = True
        event.listen(session, 'before_commit', _prepare)
        event.listen(session, 'after_commit', _delete)
        event.listen(session, 'after_rollback', _rollback)
    pending = session.info.setdefault(_KEY, {})
    pending.setdefault((row.project_id, service_id), set()).update(refs)


def _prepare(session):
    # Savepoint commits must not delete anything before the outer commit.
    if session.in_nested_transaction():
        return
    session.flush()
    pending = session.info.get(_KEY, {})
    for (project_id, service_id), candidates in pending.items():
        def query(model):
            return session.query(model).filter_by(
                project_id=project_id, service_id=service_id)

        service = session.query(models.Service).filter_by(
            project_id=project_id, id=service_id, deleted_at=None).first()
        if service is None or service.status == states.DELETING:
            # Whole-app teardown owns cleanup; never race its retries.
            candidates.clear()
            continue
        stack = session.query(models.Stack).filter_by(
            project_id=project_id, id=service.stack_id).one()
        if stack.task_state == states.DELETING:
            candidates.clear()
            continue
        live = {row.value_ref for row in query(models.Secret)}
        mounts = {row.content_ref for row in query(models.FileMount)}
        for deployment in query(models.Deployment).filter_by(deleted_at=None):
            live.update(app_secrets.spec_refs(deployment.spec))
        source = session.query(models.Source).filter_by(
            project_id=project_id, id=service.source_id).first()
        if source:
            if source.credentials_ref:
                live.add(source.credentials_ref)
            pending = source.deploy_key_rotation or {}
            live.update(pending[key] for key in ('reference', 'previous')
                        if pending.get(key))
        live.update(mounts)
        if service.compose_ref:
            live.add(service.compose_ref)
        if service.dockerfile_ref:
            live.add(service.dockerfile_ref)
        # This list is an inventory for masking/cleanup, not a rollback pin.
        inventory = service.file_mount_secret_refs or []
        kept = [ref for ref in inventory if ref in live]
        if kept != inventory:
            service.file_mount_secret_refs = kept
        candidates.difference_update(live)
    session.flush()


def _delete(session):
    if session.in_nested_transaction():
        return
    pending = session.info.pop(_KEY, {})
    for (project_id, _service_id), refs in pending.items():
        ctx = context_module.RequestContext(project_id=project_id)
        for ref in sorted(refs):
            try:
                secret_store.call('delete', ctx, ref)
            except Exception:
                # Commit succeeded. Leave an orphan for the janitor, without
                # logging backend exceptions, refs or payloads.
                LOG.warning('Superseded app secret cleanup failed.')


def _rollback(session):
    if not session.in_nested_transaction():
        session.info.pop(_KEY, None)
