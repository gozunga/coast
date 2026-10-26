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

"""Retained data deletion with durable activity receipts and no takeover."""

import json
from types import SimpleNamespace

from openstack import exceptions as sdk_exceptions
from oslo_log import log as logging
from taskflow import engines
from taskflow.patterns import linear_flow
from taskflow import task

from coast.common import exception
from coast.common import notifications
from coast.common import quota
from coast.common import safe_log
from coast.common import states
from coast.conductor import controller
from coast.conductor import rpcapi
from coast.db import api as db_api
from coast import objects


LOG = logging.getLogger(__name__)
RECEIPT_ACTION = 'delete.cleanup'
FAULT = 'Volume cleanup failed; operator recovery may be required.'


def rows(cls, ctx, filters):
    marker = None
    while True:
        page = cls.get_all(ctx, filters=filters, marker=marker)
        if not page:
            return
        yield from page
        marker = page[-1].id


def eligible(ctx, volume):
    if volume.purpose != 'data':
        raise exception.Invalid()
    if (volume.server_id or volume.status in (
            states.CREATING, states.SNAPSHOTTING, states.DELETING) or
            objects.AppVolumeMountList.get_all(
                ctx, filters={'volume_id': volume.id}, limit=1)):
        raise exception.Conflict()
    # In-flight extend receipts hold quota even when status is AVAILABLE.
    for event in rows(objects.EventList, ctx, {
            'resource_type': 'volume', 'resource_id': volume.id,
            'outcome': 'start'}):
        if event.action in ('create', 'extend', 'snapshot'):
            raise exception.Conflict()
    for server in rows(objects.ServerList, ctx,
                       {'stack_id': volume.stack_id}):
        if server.task_state != states.NONE:
            raise exception.Conflict()
    for other in rows(objects.VolumeList, ctx,
                      {'stack_id': volume.stack_id}):
        if other.status == states.SNAPSHOTTING:
            raise exception.Conflict()
    for event in rows(objects.EventList, ctx, {
            'stack_id': volume.stack_id, 'resource_type': 'volume',
            'action': 'extend', 'outcome': 'start'}):
        raise exception.Conflict()
    dependencies(ctx, volume)


def dependencies(ctx, volume):
    for export in rows(objects.ExportList, ctx, {'stack_id': volume.stack_id}):
        if (volume.id in export.volume_ids and
                export.status not in (states.ACTIVE, states.EXPIRED)):
            raise exception.Conflict()


def lock_stack(ctx, stack_id):
    # Call only after the project sentinel, before any volume/receipt lock.
    return objects.Stack.obj_from_db(
        ctx, db_api.stacks._get(ctx, stack_id, lock=True))


def accept(ctx, volume):
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        stack = lock_stack(ctx, volume.stack_id)
        current = objects.Volume.obj_from_db(
            ctx, db_api.volumes._get(ctx, volume.id, lock=True))
        if current.stack_id != stack.id:
            raise exception.NotFound()
        eligible(ctx, current)
        if stack.status not in (states.ACTIVE, states.DEGRADED, states.ERROR):
            raise exception.Conflict()
        stack.acquire_task(states.RECONCILING)
        volume.transition_status(states.DELETING)
        data = dict(revision=rpcapi.revision(volume),
                    reference=current.cinder_volume_id, amount=current.size_gb,
                    steps=[], running=False, pending=['start'])
        # A retry inherits only previously verified ownership, not a name.
        for previous in rows(objects.EventList, ctx, {
                'resource_id': volume.id, 'resource_type': 'volume',
                'action': RECEIPT_ACTION, 'outcome': 'failure'}):
            old = json.loads(previous.message)
            if (old['reference'] == data['reference'] and
                    'owned' in old['steps']):
                data['steps'] = [step for step in ('owned', 'intent')
                                 if step in old['steps']]
        objects.Event(
            context=ctx, id=rpcapi.receipt_id('delete_volume', volume),
            stack_id=stack.id, resource_type='volume', resource_id=volume.id,
            action=RECEIPT_ACTION, outcome='start',
            message=json.dumps(data), request_id=ctx.request_id).create()


class Step(task.Task):
    def __init__(self, flow, name):
        super().__init__(name=name)
        self.flow = flow

    def execute(self):
        try:
            getattr(self.flow, self.name)()
        except Exception as exc:
            ctx = getattr(self.flow, 'ctx', None)
            safe_log.log_failure(
                LOG,
                'Volume %(volume)s stack %(stack)s step %(step)s failed: '
                '%(cause)s at %(where)s request_id=%(request_id)s',
                exc,
                volume=getattr(
                    getattr(self.flow, 'volume', None), 'id', None),
                stack=getattr(getattr(self.flow, 'stack', None), 'id', None),
                step=self.name,
                request_id=getattr(ctx, 'request_id', None))
            # SDK errors can include response bodies and credentials.
            raise exception.CoastException(FAULT) from None

    def revert(self, **kwargs):
        # Deletion is irreversible. finish(False) retains quota/references.
        pass


class DeleteVolume:
    def __init__(self, owner):
        self.owner = owner
        self.conf = owner.conf

    def receipt(self):
        return objects.Event.obj_from_db(self.ctx, db_api.events._get(
            self.ctx, self.receipt_id, lock=True))

    def checkpoint(self, name=None, **values):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            self.stack = lock_stack(self.ctx, self.resource.stack_id)
            receipt = self.receipt()
            data = json.loads(receipt.message)
            if receipt.outcome != 'start':
                raise exception.Conflict()
            if name and name not in data['steps']:
                data['steps'].append(name)
            data.update(values)
            receipt.message = json.dumps(data)
            receipt.save()
            self.data = data

    def execute(self, ctx, resource, revision=None, replay=False):
        if ctx.project_id != resource.project_id:
            raise exception.NotFound()
        self.ctx, self.resource = ctx, resource
        self.receipt_id = rpcapi.receipt_id(
            'delete_volume', resource, revision)
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            self.stack = lock_stack(ctx, resource.stack_id)
            try:
                receipt = self.receipt()
            except exception.NotFound:
                return  # No acceptance receipt: no cloud operation permitted.
            self.data = json.loads(receipt.message)
            if (receipt.resource_id != resource.id or
                    receipt.stack_id != resource.stack_id or
                    self.data['revision'] != (revision or
                                              rpcapi.revision(resource))):
                return
            if receipt.outcome != 'start':
                terminal = True
            else:
                terminal = False
                if self.data['running'] and not replay:
                    return  # Crash recovery requires an operator check.
                self.volume = objects.Volume.obj_from_db(
                    ctx, db_api.volumes._get(ctx, resource.id, lock=True))
                if (self.volume.stack_id != self.stack.id or
                        self.volume.status != states.DELETING or
                        self.stack.task_state != states.RECONCILING):
                    return
                self.data['running'] = True
                receipt.message = json.dumps(self.data)
                receipt.save()
        self.publish()
        if terminal:
            return
        try:
            self.cloud = controller.ControllerCloud(self.conf, self.stack)
            self.block = (NoopCinder(self.owner.cloud, self.volume.id)
                          if self.stack.driver == 'noop' else
                          self.cloud.proxy('cinder'))
            flow = linear_flow.Flow('volume-delete').add(*[
                Step(self, name) for name in (
                    'verify', 'check_dependencies', 'delete', 'wait',
                    'complete')])
            engines.run(flow, engine='serial')
        except Exception as exc:
            safe_log.log_failure(
                LOG,
                'Volume %(volume)s stack %(stack)s delete failed: '
                '%(cause)s at %(where)s request_id=%(request_id)s',
                exc,
                volume=getattr(getattr(self, 'volume', None), 'id', None),
                stack=getattr(self.stack, 'id', None),
                request_id=getattr(self.ctx, 'request_id', None))
            self.fail()
        self.publish()

    def verify(self):
        volume = objects.Volume.get_by_id(self.ctx, self.volume.id)
        if (volume.purpose != 'data' or volume.server_id or
                volume.stack_id != self.stack.id or
                volume.cinder_volume_id != self.data['reference'] or
                volume.size_gb != self.data['amount'] or
                objects.AppVolumeMountList.get_all(
                    self.ctx, filters={'volume_id': volume.id}, limit=1)):
            raise exception.Conflict()
        identifier = self.data['reference']
        if not identifier:
            found = self.cloud.find_volume(volume)
            if found is None:
                # No authoritative owned reference, never release quota.
                raise exception.Conflict()
            identifier = found.id
            with db_api.writer.using(self.ctx) as session:
                db_api._lock_project(session, self.ctx.project_id)
                lock_stack(self.ctx, volume.stack_id)
                volume.cinder_volume_id = identifier
                volume.save()
                self.checkpoint(reference=identifier)
        try:
            self.observed = self.block.get_volume(identifier)
        except sdk_exceptions.ResourceNotFound:
            if 'owned' not in self.data['steps']:
                raise exception.Conflict() from None
            self.observed = None
            self.checkpoint('gone')
            return
        metadata = self.observed.metadata or {}
        expected = dict(tag.split('=', 1) if '=' in tag else (tag, 'true')
                        for tag in self.cloud.tags)
        if (self.observed.id != identifier or
                any(metadata.get(k) != v for k, v in expected.items()) or
                self.observed.attachments != [] or
                self.observed.status not in (
                    'available', 'error', 'deleting')):
            raise exception.Conflict()
        if (self.observed.status == 'deleting' and
                'intent' not in self.data['steps']):
            raise exception.Conflict()
        self.checkpoint('owned')
        self.checkpoint('detached')

    def check_dependencies(self):
        dependencies(self.ctx, self.volume)
        identifier = self.data['reference']
        # SDK generators follow all pages. Never use a name as ownership.
        snapshots = list(self.block.snapshots(
            details=True, volume_id=identifier))
        if any(snapshot.volume_id == identifier for snapshot in snapshots):
            raise exception.Conflict()
        for volume in self.block.volumes(details=True):
            if (getattr(volume, 'source_volume_id', None) == identifier or
                    (volume.metadata or {}).get('source_volume_id') ==
                    identifier or
                    ((volume.metadata or {}).get('coast') == 'true' and
                     (volume.metadata or {}).get('project_id') ==
                     self.ctx.project_id and
                     (volume.metadata or {}).get('stack_id') == self.stack.id
                     and (volume.metadata or {}).get('volume_id') ==
                     self.volume.id)):
                raise exception.Conflict()
        self.checkpoint('dependencies', dependencies=[])

    def delete(self):
        if 'gone' in self.data['steps']:
            return
        self.checkpoint('intent')
        if self.observed.status != 'deleting':
            self.block.delete_volume(self.data['reference'],
                                     ignore_missing=False,
                                     force=False, cascade=False)
        self.checkpoint('acknowledged')

    def wait(self):
        if 'gone' not in self.data['steps']:
            opts = self.conf.controller_worker
            self.block.wait_for_delete(
                self.observed, interval=opts.volume_delete_poll_interval,
                wait=opts.volume_delete_timeout)
            try:
                self.block.get_volume(self.data['reference'])
            except sdk_exceptions.ResourceNotFound:
                self.checkpoint('gone')
            else:
                raise exception.Conflict()

    def complete(self):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            stack = lock_stack(self.ctx, self.stack.id)
            receipt = self.receipt()
            data = json.loads(receipt.message)
            if receipt.outcome == 'success':
                return
            if 'gone' not in data['steps'] or receipt.outcome != 'start':
                raise exception.Conflict()
            volume = objects.Volume.obj_from_db(
                self.ctx, db_api.volumes._get(
                    self.ctx, self.volume.id, lock=True))
            if (volume.status != states.DELETING or
                    volume.cinder_volume_id != data['reference'] or
                    volume.size_gb != data['amount'] or
                    stack.task_state != states.RECONCILING):
                raise exception.Conflict()
            if self.owner._accounted(self.ctx, volume):
                quota.release(self.ctx, volume_gb=data['amount'])
            volume.destroy()
            data.update(running=False, pending=data['pending'] + ['end'])
            receipt.outcome = 'success'
            receipt.message = json.dumps(data)
            receipt.save()
            stack.release_task(states.RECONCILING)

    def fail(self):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            stack = lock_stack(self.ctx, self.stack.id)
            receipt = self.receipt()
            if receipt.outcome != 'start':
                return
            data = json.loads(receipt.message)
            volume = objects.Volume.get_by_id(self.ctx, self.volume.id)
            volume.transition_status(states.ERROR)
            data.update(running=False, pending=data['pending'] + ['error'],
                        fault=FAULT)
            receipt.outcome = 'failure'
            receipt.message = json.dumps(data)
            receipt.save()
            stack.release_task(states.RECONCILING)

    def publish(self):
        # Receipt survives hard deletion. Delivery retry does no cloud I/O.
        receipt = objects.Event.get_by_id(self.ctx, self.receipt_id)
        data = json.loads(receipt.message)
        for phase in data['pending']:
            payload = notifications.payload_for(self.resource, self.ctx)
            payload['operation_id'] = self.receipt_id
            if receipt.outcome == 'failure':
                payload['status'] = states.ERROR
            if not notifications.emit(
                    self.ctx, notifications.CONDUCTOR, 'volume', 'delete',
                    phase, payload, self.conf, self.owner.notifier):
                return
        if data['pending']:
            with db_api.writer.using(self.ctx) as session:
                db_api._lock_project(session, self.ctx.project_id)
                lock_stack(self.ctx, self.resource.stack_id)
                receipt = self.receipt()
                current = json.loads(receipt.message)
                if current['pending'] == data['pending']:
                    current['pending'] = []
                    receipt.message = json.dumps(current)
                    receipt.save()


class NoopCinder:
    """M1 cloud adapter. Production drivers always use the service SDK."""

    def __init__(self, cloud, volume_id):
        self.cloud = cloud
        self.volume_id = volume_id

    def get_volume(self, identifier):
        row = self.cloud.resources.get(identifier)
        if not row:
            raise sdk_exceptions.ResourceNotFound()
        return SimpleNamespace(
            id=identifier, metadata=dict(
                tag.split('=', 1) if '=' in tag else (tag, 'true')
                for tag in row['tags']), status='available', attachments=[])

    def volumes(self, **kwargs):
        return [self.get_volume(key) for key, value in
                self.cloud.resources.items() if value['kind'] == 'volume']

    def snapshots(self, **kwargs):
        identifier = self.cloud.reference('snapshot', self.volume_id)
        return ([SimpleNamespace(volume_id=kwargs['volume_id'])]
                if identifier in self.cloud.resources else [])

    def delete_volume(self, identifier, **kwargs):
        self.cloud.delete(identifier)

    def wait_for_delete(self, volume, **kwargs):
        if volume.id in self.cloud.resources:
            raise sdk_exceptions.ResourceTimeout()
