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

"""Real stack delete. Cloud calls stay outside DB transactions.

A missing resource (404) is already gone. Unreachable engine cleanup is
recorded and skipped. Other errors set ERROR and stop teardown.
"""

import datetime
import json
import uuid

from openstack import exceptions as sdk_exceptions
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import strutils

from coast.api import secret_store
from coast.common import app_secrets
from coast.common import context as context_module
from coast.common import exception
from coast.common import notifications
from coast.common import quota
from coast.common import stack_delete
from coast.common import states
from coast.common import storage as storage_rules
from coast.conductor import backups
from coast.conductor import controller
from coast.conductor import dns as managed_dns
from coast.conductor import exports
from coast.conductor import volume_delete
from coast.db import api as db_api
from coast.db import models
from coast.drivers import base as driver_base
from coast.network import fip
from coast.network import stack as network_stack
from coast import objects


LOG = logging.getLogger(__name__)
SNAPSHOT_CLEANUP_BATCH_SIZE = 100
_SECRET_REFS = (
    'engine_token_ref', 'heartbeat_key_ref', 'engine_token_previous_ref',
    'ssh_key_ref', 'registry_secret_ref')
_ENGINE_REFS = ('engine_ref',)


class Clients(object):
    """Lazy production wrappers. Tests pass their own object instead."""

    def __init__(self, manager, stack, options):
        self.manager = manager
        self.stack = stack
        self.options = options or {}
        self.notifier = getattr(manager, 'notifier', None)
        self._cache = {}

    def _once(self, name, factory):
        if name not in self._cache:
            self._cache[name] = factory()
        return self._cache[name]

    @property
    def driver(self):
        return self._once(
            'driver', lambda: self.manager._driver(self.stack))

    @property
    def endpoint(self):
        return self._once(
            'endpoint', lambda: self.manager._endpoint(self.stack))

    @property
    def dns(self):
        return self._once(
            'dns', lambda: managed_dns.Cleanup(self.manager.conf))

    @property
    def floating(self):
        return self._once(
            'floating', lambda: fip.FloatingIP(self.manager.conf))

    @property
    def cloud(self):
        return self._once(
            'cloud', lambda: controller.ControllerCloud(
                self.manager.conf, self.stack))

    @property
    def block(self):
        return self._once('block', lambda: self.cloud.proxy('cinder'))

    @property
    def network(self):
        return self._once(
            'network', lambda: network_stack.EnvironmentNetwork(
                self.manager.conf))

    @property
    def secrets(self):
        return secret_store


def teardown(context, stack, clients):
    """Delete one real stack. Never re-raise a cloud or cleanup error."""
    try:
        _teardown(context, stack, clients)
    except Exception as exc:
        _fail(context, stack, exc)


def _teardown(ctx, stack, clients):
    current = _prepare(ctx, stack, clients)
    if current is None:
        return
    identifier = str(uuid.uuid5(uuid.UUID(current.id), 'delete-timeline'))
    with db_api.writer.using(ctx):
        if not objects.EventList.get_all(ctx, filters={'id': identifier}):
            counts = {
                'servers': sum(1 for _ in _rows(
                    objects.ServerList, ctx, {'stack_id': current.id})),
                'volumes': sum(1 for _ in _rows(
                    objects.VolumeList, ctx, {'stack_id': current.id})),
                'networks': int(bool(current.network_id))}
            objects.Event(
                context=ctx, id=identifier, stack_id=current.id,
                resource_type='stack', resource_id=current.id,
                action='delete', outcome='start', detail=counts,
                request_id=ctx.request_id).create()
    _notify(ctx, current, 'start', clients)
    steps = (_export_before_delete, _backups, _coolify, _dns, _floating,
             _servers, _ports, _snapshot_volumes, _upgrade_storage, _volumes,
             _network, _secrets,
             _finish)
    for step in steps:
        if not _step(ctx, current, lambda step=step: step(
                ctx, current, clients)):
            return
    try:
        done = objects.Stack.get_by_id(
            ctx, current.id, show_deleted=True)
    except exception.NotFound:
        done = current
    _notify(ctx, done, 'end', clients)


def _prepare(ctx, stack, clients):
    try:
        current = objects.Stack.get_by_id(
            ctx, stack.id, show_deleted=True)
    except exception.NotFound:
        return None
    if current.status == states.DELETED or current.deleted_at:
        return None
    if current.status not in (states.ERROR, states.DELETING):
        return None
    stack_delete.require_empty(ctx, current)
    if current.status == states.ERROR:
        current.status = states.DELETING
        current.fault = None
        current.save()
        if current.task_state == states.NONE:
            current = current.acquire_task(states.DELETING)
    current.conductor_host = clients.manager.conf.host
    current.save()
    return current


def _step(ctx, stack, fn):
    try:
        fn()
    except Exception as exc:
        _fail(ctx, stack, exc)
        return False
    return True


def _fail(ctx, stack, exc):
    """Persist ERROR and the exception text. Do not re-raise."""
    reason = strutils.mask_password(str(exc)) or type(exc).__name__
    # Keep the original frames without logging an unmasked exception chain.
    safe = RuntimeError(reason)
    LOG.error('Stack teardown failed for %s (%s): %s',
              getattr(stack, 'id', None), type(exc).__name__, reason,
              exc_info=(type(safe), safe, exc.__traceback__))
    try:
        with db_api.writer.using(ctx):
            current = objects.Stack.get_by_id(ctx, stack.id)
            if current.status == states.DELETED or current.deleted_at:
                return
            if current.status != states.ERROR:
                current.status = states.ERROR
            current.fault = reason
            current.save()
            if current.task_state == states.DELETING:
                current.release_task(states.DELETING)
            objects.Event(
                context=ctx, stack_id=current.id,
                resource_type=current.repository.resource,
                resource_id=current.id, action='delete',
                outcome='failure',
                message=stack_delete.public_fault(reason)[:1024],
                request_id=getattr(ctx, 'request_id', None)).create()
        _notify(ctx, current, 'error', None)
    except Exception as err:
        LOG.warning('Could not record stack delete failure for %s (%s).',
                    getattr(stack, 'id', None), type(err).__name__)


def _notify(ctx, stack, phase, clients):
    try:
        notifier = getattr(clients, 'notifier', None)
        notifications.emit(
            ctx, notifications.CONDUCTOR, 'environment', 'delete', phase,
            notifications.payload_for(stack, ctx), notifier=notifier)
    except Exception:
        LOG.warning('Stack delete notification failed for %s.', stack.id)


def _gone(exc):
    if isinstance(exc, (sdk_exceptions.ResourceNotFound,
                        exception.NotFound,
                        driver_base.EngineNotFound)):
        return True
    for name in ('status_code', 'http_status', 'code'):
        if getattr(exc, name, None) == 404:
            return True
    return False


def _call(fn):
    try:
        return fn()
    except Exception as exc:
        if _gone(exc):
            return None
        raise


def _rows(list_class, ctx, filters):
    marker = None
    while True:
        batch = list_class.get_all(
            ctx, filters=filters, marker=marker, sort_key='id')
        if not batch:
            return
        yield from batch
        marker = batch[-1].id


def _accounted(ctx, resource):
    kind = resource.repository.resource
    if not kind:
        return False
    found = objects.EventList.get_all(ctx, filters={
        'resource_id': resource.id, 'resource_type': kind,
        'action': 'create', 'outcome': 'failure'}, limit=1)
    return not found


def _stack_create_failed(ctx, stack):
    found = objects.EventList.get_all(ctx, filters={
        'resource_id': stack.id, 'resource_type': 'environment',
        'action': 'create', 'outcome': 'failure'}, limit=1)
    return bool(found)


def _delete_export_id(stack_id):
    """One export id per stack delete. A retry loads this row.

    The id is the key. The fault column stays empty until a real failure.
    """
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, 'stack-delete-export:' + stack_id))


def _export_fault(export):
    detail = (getattr(export, 'fault', None) or '').strip()
    if not detail:
        return 'Export before delete did not complete'
    if detail.startswith('Export before delete'):
        return detail
    return 'Export before delete failed. ' + detail


def _export_before_delete(ctx, stack, clients):
    """Run the POST /exports path before any cloud delete.

    Continue only when that export is ACTIVE. Failure or timeout sets
    the stack to ERROR through the caller and deletes nothing. The
    export row and its Swift objects stay for normal retention.
    """
    options = getattr(clients, 'options', None) or {}
    if not options.get('export_before_delete'):
        return
    export = _delete_export(ctx, stack)
    if export is None:
        export = _create_delete_export(ctx, stack)
    if export.status == states.ACTIVE:
        return
    if export.status == states.ERROR:
        raise RuntimeError(_export_fault(export))
    if export.status not in (
            states.PENDING, states.SNAPSHOTTING, states.UPLOADING):
        raise RuntimeError('Export before delete did not complete')
    exports.ExportFlow(clients.manager).execute(
        ctx, 'create_export', export)
    fresh = objects.Export.get_by_id(ctx, export.id)
    if fresh.status != states.ACTIVE:
        raise RuntimeError(_export_fault(fresh))


def _delete_export(ctx, stack):
    try:
        return objects.Export.get_by_id(ctx, _delete_export_id(stack.id))
    except exception.NotFound:
        return None


def _create_delete_export(ctx, stack):
    # Same volume set as POST /v1/stacks/{id}/exports with volume_ids null.
    volumes = [volume for volume in _rows(
        objects.VolumeList, ctx, {'stack_id': stack.id})
        if volume.purpose in ('data', 'docker')]
    if not volumes:
        raise RuntimeError(
            'Export before delete found no data or docker volume')
    for volume in volumes:
        if volume.status not in (states.AVAILABLE, states.IN_USE):
            raise RuntimeError('Export before delete volume is not ready')
    with db_api.writer.using(ctx):
        quota.check_export(
            ctx, sum(volume.size_gb for volume in volumes))
        export = objects.Export(
            context=ctx, id=_delete_export_id(stack.id),
            stack_id=stack.id, status=states.PENDING,
            backend=cfg.CONF.export.backend,
            volume_ids=[volume.id for volume in volumes])
        export.create()
    return export


def _coolify(ctx, stack, clients):
    # No token means bootstrap did not run. Coolify has nothing to delete.
    if not stack.engine_token_ref:
        return
    completed = objects.EventList.get_all(ctx, filters={
        'resource_id': stack.id, 'resource_type': 'stack',
        'action': 'delete.engine', 'outcome': 'success'}, limit=1)
    if completed:
        return
    for server in _rows(objects.ServerList, ctx, {'stack_id': stack.id}):
        if server.role == 'control' and server.compute_id:
            observed = _call(lambda: clients.cloud.proxy('nova').get_server(
                server.compute_id))
            if observed is None:
                _clear_engine_refs(ctx, stack)
                return
    try:
        _delete_engine(ctx, stack, clients)
    except driver_base.EngineUnreachable:
        with db_api.writer.using(ctx):
            skipped = objects.EventList.get_all(ctx, filters={
                'resource_id': stack.id, 'resource_type': 'stack',
                'action': 'delete.engine.skip', 'outcome': 'failure'},
                limit=1)
            if not skipped:
                objects.Event(
                    context=ctx, stack_id=stack.id, resource_type='stack',
                    resource_id=stack.id, action='delete.engine.skip',
                    outcome='failure',
                    message='Engine cleanup skipped: engine unreachable.',
                    request_id=getattr(ctx, 'request_id', None)).create()
        LOG.warning('Engine cleanup skipped for stack %s: unreachable.',
                    stack.id)


def _delete_engine(ctx, stack, clients):
    endpoint = clients.endpoint
    driver = clients.driver
    for service in _rows(objects.ServiceList, ctx, {'stack_id': stack.id}):
        _call(lambda service=service: driver.delete_app(
            endpoint, stack, service))
    apps = list(_rows(objects.AppgroupList, ctx, {'stack_id': stack.id}))
    for app in apps:
        envs = list(_rows(
            objects.EnvironmentList, ctx, {'appgroup_id': app.id}))
        for env in envs:
            _call(lambda app=app, env=env: driver.delete_environment(
                endpoint, stack, app, env))
        _call(lambda app=app: driver.delete_project(endpoint, stack, app))
    for server in _rows(objects.ServerList, ctx, {'stack_id': stack.id}):
        if server.role == 'control' and not server.engine_ref:
            continue
        _call(lambda server=server: driver.unregister_server(
            endpoint, stack, server))
    _call(lambda: driver.delete_stack_project(endpoint, stack))
    _clear_engine_refs(ctx, stack)


def _clear_engine_refs(ctx, stack):
    with db_api.writer.using(ctx):
        current = objects.Stack.get_by_id(ctx, stack.id)
        _blank(current, _ENGINE_REFS)
        for app in _rows(objects.AppgroupList, ctx,
                         {'stack_id': stack.id}):
            _blank(app, _ENGINE_REFS)
            for env in _rows(objects.EnvironmentList, ctx,
                             {'appgroup_id': app.id}):
                _blank(env, _ENGINE_REFS)
        for service in _rows(objects.ServiceList, ctx,
                             {'stack_id': stack.id}):
            _blank(service, _ENGINE_REFS)
        for server in _rows(objects.ServerList, ctx, {'stack_id': stack.id}):
            _blank(server, _ENGINE_REFS)
        objects.Event(
            context=ctx, stack_id=stack.id, resource_type='stack',
            resource_id=stack.id, action='delete.engine',
            outcome='success').create()


def _blank(obj, names):
    changed = False
    for name in names:
        if getattr(obj, name, None):
            setattr(obj, name, None)
            changed = True
    if changed:
        obj.save()


def _dns(ctx, stack, clients):
    for service in _rows(objects.ServiceList, ctx, {'stack_id': stack.id}):
        _call(lambda service=service: clients.dns.delete(
            stack.project_id, stack.id, service.id))


def _floating(ctx, stack, clients):
    for server in _rows(objects.ServerList, ctx, {'stack_id': stack.id}):
        _call(lambda server=server: clients.floating.delete(
            stack.project_id, stack.id, server.id))


def _ports(ctx, stack, clients):
    for server in _rows(objects.ServerList, ctx, {'stack_id': stack.id}):
        _call(lambda server=server: clients.cloud.delete_ports(server))


def _servers(ctx, stack, clients):
    for server in _rows(objects.ServerList, ctx, {'stack_id': stack.id}):
        _call(lambda server=server: clients.cloud.delete_server(server))


def _network(ctx, stack, clients):
    _call(lambda: clients.network.delete(stack.project_id, stack.id))


def _secrets(ctx, stack, clients):
    store = clients.secrets
    for ref in _secret_refs(ctx, stack):
        if hasattr(store, 'call'):
            _call(lambda ref=ref: store.call('delete', ctx, ref))
        else:
            _call(lambda ref=ref: store.delete(ctx, ref))


def _secret_refs(ctx, stack):
    refs = []
    seen = set()

    def add(ref):
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)

    for name in _SECRET_REFS:
        add(getattr(stack, name, None))
    for service in _rows(objects.ServiceList, ctx, {'stack_id': stack.id}):
        for ref in app_secrets.references(ctx, service):
            add(ref)
        try:
            source = objects.Source.get_by_id(ctx, service.source_id)
        except exception.NotFound:
            continue
        add(source.credentials_ref)
    return refs


def _snapshot_volumes(ctx, stack, clients):
    """Create one kept Cinder snapshot per volume. Retry reuses the row."""
    options = getattr(clients, 'options', None) or {}
    if not options.get('snapshot_volumes'):
        return
    for volume in list(_rows(objects.VolumeList, ctx,
                             {'stack_id': stack.id})):
        _snapshot_one(ctx, stack, clients, volume)


def _snapshot_record(ctx, stack, volume):
    found = objects.StackDeleteSnapshotList.get_all(
        ctx, filters={'stack_id': stack.id, 'volume_id': volume.id},
        limit=1)
    return found[0] if found else None


def _snapshot_volume_id(ctx, clients, volume):
    if volume.cinder_volume_id:
        return volume.cinder_volume_id
    found = _call(lambda: clients.cloud.find_volume(volume))
    if found is None:
        if _accounted(ctx, volume):
            raise RuntimeError('Volume snapshot failed; retry delete')
        return None
    volume.cinder_volume_id = found.id
    volume.save()
    return found.id


def _snapshot_one(ctx, stack, clients, volume):
    # Persist the snapshot id before the wait so a retry does not create
    # another snapshot. force=False matches the openstacksdk snapshot
    # attribute (block_storage create_snapshot). The server step already
    # detached the volume.
    row = _snapshot_record(ctx, stack, volume)
    if row is not None:
        snap = _call(lambda: clients.block.get_snapshot(
            row.cinder_snapshot_id))
        if snap is None:
            raise RuntimeError('Volume snapshot is missing; retry delete')
    else:
        ref = _snapshot_volume_id(ctx, clients, volume)
        if ref is None:
            return
        snap = clients.block.create_snapshot(
            volume_id=ref, force=False, name='coast-delete-' + volume.id)
        row = objects.StackDeleteSnapshot(
            context=ctx, stack_id=stack.id, volume_id=volume.id,
            cinder_volume_id=ref, cinder_snapshot_id=snap.id,
            status=getattr(snap, 'status', None) or 'creating',
            retention_days=clients.options.get(
                'snapshot_retention_days',
                clients.manager.conf.export.snapshot_retention_days))
        row.create()
    if getattr(snap, 'status', None) != 'available':
        opts = cfg.CONF.controller_worker
        snap = clients.block.wait_for_status(
            snap, status='available', failures=['error'],
            interval=opts.volume_delete_poll_interval,
            wait=opts.volume_delete_timeout)
    if getattr(snap, 'status', None) != 'available':
        raise RuntimeError('Volume snapshot did not become available')
    if row.status != 'available':
        row.status = 'available'
        row.save()


def _kept_snapshot_ids(ctx, stack, volume):
    found = objects.StackDeleteSnapshotList.get_all(
        ctx, filters={'stack_id': stack.id, 'volume_id': volume.id})
    return {item.cinder_snapshot_id for item in found
            if item.cinder_snapshot_id}


def _upgrade_storage(ctx, stack, clients):
    """Remove owned recovery storage after fencing every stack guest."""
    kept = {row.cinder_snapshot_id for row in _rows(
        objects.StackDeleteSnapshotList, ctx, {'stack_id': stack.id})}
    for snap in _listing(lambda: clients.block.snapshots(details=True)):
        if snap.id in kept or not _owned(snap, stack):
            continue
        observed = _call(lambda: clients.block.get_snapshot(snap.id))
        if observed is None:
            continue
        if not _owned(observed, stack):
            raise RuntimeError('Snapshot ownership differs')
        _call(lambda: clients.block.delete_snapshot(
            observed.id, ignore_missing=True))
        opts = cfg.CONF.controller_worker
        _call(lambda: clients.block.wait_for_delete(
            observed, interval=opts.volume_delete_poll_interval,
            wait=opts.volume_delete_timeout))
    tracked = {volume.cinder_volume_id for volume in _rows(
        objects.VolumeList, ctx, {'stack_id': stack.id})}
    for volume in _listing(lambda: clients.block.volumes()):
        if volume.id in tracked or not _owned(volume, stack):
            continue
        observed = _call(lambda: clients.block.get_volume(volume.id))
        if observed is None:
            continue
        if not _owned(observed, stack) or observed.attachments:
            raise RuntimeError('Recovery volume cleanup failed; retry delete')
        if _listing(lambda: clients.block.snapshots(
                details=True, volume_id=observed.id)):
            raise RuntimeError(
                'Recovery volume snapshots remain; retry delete')
        _call(lambda: clients.block.delete_volume(
            observed.id, ignore_missing=False, force=False, cascade=False))
        opts = cfg.CONF.controller_worker
        _call(lambda: clients.block.wait_for_delete(
            observed, interval=opts.volume_delete_poll_interval,
            wait=opts.volume_delete_timeout))


def _volumes(ctx, stack, clients):
    for volume in list(_rows(objects.VolumeList, ctx,
                             {'stack_id': stack.id})):
        receipt = _ensure_receipt(ctx, stack, volume)
        if receipt.outcome == 'success':
            continue
        ref = volume.cinder_volume_id
        if not ref:
            found = _call(lambda volume=volume: clients.cloud.find_volume(
                volume))
            if found is None:
                accounted = _accounted(ctx, volume)
                # A live stack must retry until the cloud volume is gone.
                # A failed create can leave an unmarked row after revert
                # already deleted that Cinder volume. Release its size once.
                if accounted and not _stack_create_failed(ctx, stack):
                    raise RuntimeError(
                        'Volume cleanup failed; retry delete')
                _complete_volume(ctx, stack, volume, accounted)
                continue
            ref = found.id
            volume.cinder_volume_id = ref
            volume.save()
        try:
            observed = clients.block.get_volume(ref)
        except Exception as exc:
            if _gone(exc):
                _complete_volume(ctx, stack, volume, True)
                continue
            raise
        if not _owned(observed, stack) or getattr(
                observed, 'attachments', None):
            raise RuntimeError('Volume cleanup failed; retry delete')
        kept = _kept_snapshot_ids(ctx, stack, volume)
        snaps = _listing(lambda ref=ref: clients.block.snapshots(
            details=True, volume_id=ref))
        if any(getattr(item, 'id', None) not in kept for item in snaps):
            raise RuntimeError('Volume cleanup failed; retry delete')
        for item in _listing(lambda: clients.block.volumes()):
            source = getattr(item, 'source_volume_id', None)
            meta = getattr(item, 'metadata', None) or {}
            if source == ref or meta.get('source_volume_id') == ref:
                raise RuntimeError('Volume cleanup failed; retry delete')
        if getattr(observed, 'status', None) != 'deleting':
            _call(lambda ref=ref: clients.block.delete_volume(
                ref, ignore_missing=False, force=False, cascade=False))
        opts = cfg.CONF.controller_worker
        _call(lambda observed=observed: clients.block.wait_for_delete(
            observed, interval=opts.volume_delete_poll_interval,
            wait=opts.volume_delete_timeout))
        try:
            clients.block.get_volume(ref)
        except Exception as exc:
            if not _gone(exc):
                raise
        else:
            raise RuntimeError('Volume cleanup failed; retry delete')
        _complete_volume(ctx, stack, volume, True)


def _owned(observed, stack):
    meta = getattr(observed, 'metadata', None) or {}
    return (meta.get('coast') == 'true' and
            meta.get('project_id') == stack.project_id and
            meta.get('stack_id') == stack.id)


def _listing(fn):
    try:
        found = fn()
    except Exception as exc:
        if _gone(exc):
            return []
        raise
    return list(found or [])


def _receipt_id(volume):
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, 'stack-volume:' + volume.id))


def _ensure_receipt(ctx, stack, volume):
    ident = _receipt_id(volume)
    try:
        return objects.Event.get_by_id(ctx, ident)
    except exception.NotFound:
        message = json.dumps({
            'reference': volume.cinder_volume_id,
            'amount': volume.size_gb, 'steps': [], 'running': False})
        event = objects.Event(
            context=ctx, id=ident, stack_id=stack.id,
            resource_type='volume', resource_id=volume.id,
            action=volume_delete.RECEIPT_ACTION, outcome='start',
            message=message, request_id=getattr(ctx, 'request_id', None))
        event.create()
        return event


def _complete_volume(ctx, stack, volume, release):
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        db_api.stacks._get(ctx, stack.id, lock=True)
        ident = _receipt_id(volume)
        try:
            receipt = objects.Event.obj_from_db(
                ctx, db_api.events._get(ctx, ident, lock=True))
        except exception.NotFound:
            receipt = None
        if receipt is not None and receipt.outcome == 'success':
            return
        try:
            current = objects.Volume.obj_from_db(
                ctx, db_api.volumes._get(ctx, volume.id, lock=True))
        except exception.NotFound:
            if receipt is not None:
                receipt.outcome = 'success'
                receipt.save()
            return
        for mount in list(_rows(objects.AppVolumeMountList, ctx,
                                {'volume_id': current.id})):
            mount.destroy()
        accounted = _accounted(ctx, current)
        quota_released = bool(release and accounted)
        if quota_released:
            quota.release(ctx, volume_gb=current.size_gb)
        ref = current.cinder_volume_id
        amount = current.size_gb
        current.destroy()
        if receipt is None:
            return
        receipt.outcome = 'success'
        receipt.message = json.dumps({
            'reference': ref, 'amount': amount,
            'steps': ['gone'], 'running': False,
            'quota_released': quota_released})
        receipt.save()


def _finish(ctx, stack, clients):
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        current = objects.Stack.obj_from_db(
            ctx, db_api.stacks._get(ctx, stack.id, lock=True))
        if current.status != states.DELETING or current.deleted_at:
            return
        completed_at = datetime.datetime.now(
            datetime.timezone.utc).replace(tzinfo=None)
        for snapshot in _rows(objects.StackDeleteSnapshotList, ctx,
                              {'stack_id': current.id}):
            if snapshot.expires_at is None:
                snapshot.expires_at = completed_at + datetime.timedelta(
                    days=snapshot.retention_days)
                snapshot.save()
        _drop_children(ctx, current)
        for name in _SECRET_REFS:
            if getattr(current, name):
                setattr(current, name, None)
        current.status = states.DELETED
        current.fault = None
        current.save()
        if current.task_state == states.DELETING:
            current.release_task(states.DELETING)
        if _accounted(ctx, current):
            quota.release(ctx, environments=1)
        current.destroy()
        timeline = objects.Event.get_by_id(
            ctx, str(uuid.uuid5(uuid.UUID(current.id), 'delete-timeline')))
        timeline.outcome = 'success'
        timeline.save()
        # Hard-deleted volumes have no deleted_at. Recount after the rows
        # are gone so a missed per-row release cannot keep volume_gb.
        # A held reservation still skips volume_gb. Release that counter
        # once when it is exactly the volumes that did not release quota.
        quota.reconcile(ctx)
        _release_failed_volume_quota(ctx, current)
        _release_removed_failed_volume_charge(ctx, current)


def _release_failed_volume_quota(ctx, stack):
    """Release volume_gb that cleanup left charged, once.

    A failed create does not get a per-row release. The receipt records
    that explicitly. Invalid receipts block teardown completion.
    """
    pending = []
    amount = 0
    for event in _rows(objects.EventList, ctx, {
            'stack_id': stack.id,
            'action': volume_delete.RECEIPT_ACTION}):
        if event.outcome != 'success' or not event.message:
            continue
        try:
            payload = json.loads(event.message)
        except (TypeError, ValueError):
            continue
        if (not isinstance(payload, dict) or
                not isinstance(payload.get('quota_released'), bool)):
            raise ValueError('Invalid volume cleanup quota receipt')
        if payload['quota_released']:
            continue
        value = payload.get('amount')
        if (isinstance(value, bool) or not isinstance(value, int) or
                value < 1):
            continue
        amount += value
        pending.append((event, payload))
    if not pending:
        return
    if not quota.release_exact_failed_volume_gb(ctx, amount):
        return
    for event, payload in pending:
        payload['quota_released'] = True
        event.message = json.dumps(payload)
        event.save()


def _release_removed_failed_volume_charge(ctx, stack):
    """Lower volume_gb when a failed create left no rows and no receipts.

    Cleanup receipts own the exact-match decision. A receipt that does
    not match leaves that different charge. This does not clear a
    reservation. It does nothing while another create is in flight.
    """
    if not _stack_create_failed(ctx, stack):
        return
    if any(_rows(objects.VolumeList, ctx, {'stack_id': stack.id})):
        return
    if any(_rows(objects.EventList, ctx, {
            'stack_id': stack.id,
            'action': volume_delete.RECEIPT_ACTION})):
        return
    quota.lower_volume_gb_to_live(ctx)


def _drop_children(ctx, stack):
    # Stack soft deletion does not trigger the ingress rule FK cascade.
    with db_api.writer.using(ctx) as session:
        for rule in db_api.ingress_rules.list_for_stack(ctx, stack.id):
            session.delete(rule)
    storage_rules.drop_stack_storages(ctx, stack)
    for service in list(_rows(objects.ServiceList, ctx,
                              {'stack_id': stack.id})):
        for domain in list(_rows(objects.DomainList, ctx,
                                 {'service_id': service.id})):
            if _accounted(ctx, domain):
                quota.release(ctx, domains=1)
            domain.destroy()
        for mount in list(_rows(objects.FileMountList, ctx,
                                {'service_id': service.id})):
            mount.destroy()
        for secret in list(_rows(objects.SecretList, ctx,
                                 {'service_id': service.id})):
            secret.destroy()
        for mount in list(_rows(objects.AppVolumeMountList, ctx,
                                {'service_id': service.id})):
            mount.destroy()
        # Save while the current deployment is still a live FK parent.
        service.status = states.DELETING
        service.current_deployment_id = None
        service.save()
        for deploy in list(_rows(objects.DeploymentList, ctx,
                                 {'service_id': service.id})):
            deploy.destroy()
        if _accounted(ctx, service):
            quota.release(ctx, apps=1)
        service.destroy()
    for app in list(_rows(objects.AppgroupList, ctx,
                          {'stack_id': stack.id})):
        for env in list(_rows(objects.EnvironmentList, ctx,
                              {'appgroup_id': app.id})):
            _retire(env)
        _retire(app)
    for server in list(_rows(objects.ServerList, ctx,
                             {'stack_id': stack.id})):
        _to_deleting(server)
        held = {key: value for key, value in quota.deltas(
            'server', {'role': server.role, 'kind': server.kind}).items()
                if value}
        if held and _accounted(ctx, server):
            quota.release(ctx, **held)
        server.destroy()


def _to_deleting(obj):
    if obj.status != states.DELETING:
        obj.status = states.DELETING
        obj.save()


def _retire(obj):
    _to_deleting(obj)
    if obj.status == states.DELETING:
        obj.status = states.DELETED
        obj.save()
    obj.destroy()


def housekeep(manager):
    """Remove at most 100 expired snapshots; NULL expiry is never selected."""
    admin = context_module.RequestContext(is_admin=True)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    with db_api.reader.using(admin):
        batch = admin.session.query(
            models.StackDeleteSnapshot.id,
            models.StackDeleteSnapshot.project_id).join(
                models.Stack, models.Stack.id ==
                models.StackDeleteSnapshot.stack_id).filter(
                models.StackDeleteSnapshot.expires_at < now,
                models.Stack.status == states.DELETED,
                models.Stack.driver != 'noop').order_by(
                    models.StackDeleteSnapshot.expires_at,
                    models.StackDeleteSnapshot.id).limit(
                        SNAPSHOT_CLEANUP_BATCH_SIZE).all()
    for identifier, project_id in batch:
        ctx = context_module.RequestContext(project_id=project_id)
        try:
            row = objects.StackDeleteSnapshot.get_by_id(ctx, identifier)
            if (row.expires_at is None or
                    row.expires_at.replace(tzinfo=None) >= now):
                continue
            stack = objects.Stack.get_by_id(
                ctx, row.stack_id, show_deleted=True)
            if stack.status != states.DELETED or stack.driver == 'noop':
                continue
            block = controller.ControllerCloud(manager.conf, stack).proxy(
                'cinder')
            snap = _call(lambda: block.get_snapshot(row.cinder_snapshot_id))
            if snap is not None:
                if snap.status != 'deleting':
                    _call(lambda: block.delete_snapshot(
                        row.cinder_snapshot_id, ignore_missing=False))
                # Do not block the recovery timer waiting for Cinder.
                # Later runs observe an asynchronous delete's completion.
                # Keep the receipt until Cinder confirms absence.
                if _call(lambda: block.get_snapshot(
                        row.cinder_snapshot_id)) is not None:
                    continue
            row.destroy()
        except exception.NotFound:
            continue
        except Exception:
            LOG.warning('Stack snapshot cleanup requires another attempt.')


def _backups(ctx, stack, clients):
    for service in _rows(objects.ServiceList, ctx, {'stack_id': stack.id}):
        backups.drop_service(cfg.CONF, ctx, service)
