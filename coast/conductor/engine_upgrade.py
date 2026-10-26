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

"""Controller rebuilds with durable rollback points and bounded recovery."""

import copy
import re
import time
import uuid

from oslo_log import log as logging
from oslo_utils import timeutils
from taskflow import engines
from taskflow.patterns import linear_flow

from coast.common import exception
from coast.common import naming
from coast.common import safe_log
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import controller
from coast.conductor import deployments
from coast.conductor import rpcapi
from coast.conductor import server_failover
from coast.conductor import stack_create
from coast.conductor import worker
from coast.db import api as db_api
from coast.db import models
from coast.drivers import base
from coast.drivers import loader
from coast import objects

LOG = logging.getLogger(__name__)
PURPOSES = ('data', 'docker', 'registry')
MAX_SERVICES = 100
REFUSED = 'Engine upgrade refused.'
FAILED = 'Engine upgrade failed.'


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise exception.Invalid('Invalid engine image version')
    return tuple(int(part) for part in value.split('.'))


def eligible(conf, stack, image_id, supported=None, newer=True):
    if stack.driver != 'coolify_v4':
        raise exception.Invalid('An App engine server VM stack is required')
    try:
        image = controller.ControllerCloud(conf, stack).select_image(
            'controller', image_id)
    except ValueError:
        raise exception.Invalid(
            'Published controller image is required') from None
    properties = image.properties or {}
    target = properties.get('coast_engine_version')
    if (image.id != image_id or properties.get('coast_engine') != 'coolify'
            or (newer and version(target) < version(stack.engine_version))):
        raise exception.Invalid('A newer App engine server image is required')
    if newer and version(target) == version(stack.engine_version):
        ctx = stack._context
        servers = objects.ServerList.get_all(ctx, filters={
            'stack_id': stack.id, 'role': 'control'}, limit=2)
        if len(servers) != 1:
            raise exception.Invalid('One App engine server is required')
        try:
            current = controller.ControllerCloud(conf, stack).select_image(
                'controller', servers[0].image_id)
            published = timeutils.parse_isotime(image.created_at)
            previous = timeutils.parse_isotime(current.created_at)
            if image.id == current.id or published <= previous:
                raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise exception.Invalid(
                'A newer App engine server image is required') from None
    version(target)
    if supported is None:
        supported = loader.load_driver(
            stack.driver, conf).SUPPORTED_ENGINE_VERSIONS
    if target not in supported:
        raise exception.Invalid('Engine image version is not supported')
    return target


def idle(ctx, stack):
    services = [objects.Service.obj_from_db(ctx, row)
                for row in db_api.services._query(ctx).filter_by(
                    stack_id=stack.id).limit(MAX_SERVICES + 1).all()]
    if len(services) > MAX_SERVICES or any(
            item.task_state != states.NONE for item in services):
        raise exception.Conflict()
    if db_api.deployments._query(ctx).join(
            models.Service, models.Deployment.service_id == models.Service.id
    ).filter(models.Service.stack_id == stack.id,
             models.Deployment.status.in_(
                 (states.PENDING, states.BUILDING, states.DEPLOYING))).first():
        raise exception.Conflict()
    return services


def rollback_point(stack):
    checkpoint = stack.upgrade_checkpoint or {}
    if checkpoint.get('boot_attempted') or checkpoint.get('rollback'):
        image = checkpoint.get('previous_image_id')
        engine = checkpoint.get('previous_engine_version')
        snapshots = checkpoint.get('snapshots')
    else:
        image, engine, snapshots = (stack.previous_image_id,
                                    stack.previous_engine_version,
                                    stack.upgrade_snapshots)
    if not image or not engine or set(snapshots or {}) != set(PURPOSES):
        raise exception.Conflict('No complete engine rollback point')
    return image, engine, snapshots


class RebuildCloud(worker.WorkerCloud):
    """Reuse guest operations with a controller config drive."""

    config_drive = controller.ControllerCloud.config_drive

    def wait_server(self, identifier, timeout=None):
        if timeout is None:
            return super().wait_server(identifier)
        nova = self.proxy('nova')
        return nova.wait_for_server(
            nova.get_server(identifier), status='ACTIVE', failures=['ERROR'],
            interval=self.conf.controller_worker.build_poll_interval,
            wait=timeout)


class Upgrade(server_failover.FailoverServer):
    steps = ('snapshots', 'fence', 'move', 'restore_snapshots', 'boot',
             'active', 'health', 'engine', 'services', 'retire',
             'roll_servers', 'complete')

    def execute(self, ctx, resource, options=None, reservation=None,
                revision=None, rollback=False):
        self.ctx = ctx
        revision = revision or rpcapi.revision(resource)
        self.rollback = rollback
        self.action = 'upgrade_rollback' if rollback else 'upgrade'
        self.task_state = states.ROLLING_BACK if rollback else states.UPGRADING
        self.receipt = rpcapi.receipt_id(
            self.action + '_stack', resource, revision)
        self.boot_attempted = self.fenced = self.moved = self.retired = False
        self.quiesced = False
        self.compensated = True
        self.locked_services = []
        self.new_deployments = []
        self.server = None
        self.controller_done = False
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            self.stack = objects.Stack.obj_from_db(
                ctx, db_api.stacks._get(ctx, resource.id, lock=True))
            if (self.stack.status != self.task_state or
                    self.stack.task_state != self.task_state or
                    rpcapi.revision(self.stack) != revision or
                    objects.EventList.get_all(ctx, filters={
                        'id': self.receipt}, limit=1)):
                return
            self.from_version = self.stack.engine_version
            checkpoint = copy.deepcopy(self.stack.upgrade_checkpoint or {})
            checkpoint['operation_id'] = self.receipt
            self.save(self.stack, conductor_host=self.conf.host,
                      upgrade_checkpoint=checkpoint)
            self.owner._event(ctx, self.stack, self.stack, self.action,
                              'start', self.receipt, detail={
                                  'from_engine_version': self.from_version,
                                  'to_engine_version': (
                                      self.stack.previous_engine_version
                                      if rollback else None),
                                  'image_id': (self.stack.previous_image_id
                                               if rollback else
                                               checkpoint.get('image_id'))})
        self.flush()
        try:
            controllers = objects.ServerList.get_all(ctx, filters={
                'stack_id': self.stack.id, 'role': 'control'}, limit=2)
            if len(controllers) == 1 and not self.secrets_available(
                    ctx, self.stack, controllers[0]):
                raise exception.SecretStoreUnavailable()
            self.prepare()
            engines.run(linear_flow.Flow('controller-rebuild').add(*[
                stack_create.Step(self, name) for name in self.steps]),
                engine='serial')
        except exception.SecretStoreUnavailable:
            LOG.warning('Engine upgrade secret storage unavailable for '
                        'stack %s.', self.stack.id)
            self.finish(False, transient=not self.fenced)
        except Exception as exc:
            safe_log.log_failure(LOG, 'Engine rebuild failed: %(cause)s at '
                                 '%(where)s request_id=%(request_id)s', exc,
                                 request_id=ctx.request_id)
            self.owner._pending = []
            self.owner._seen = set()
            self.finish(False)

    def checkpoint(self, **changes):
        value = copy.deepcopy(self.stack.upgrade_checkpoint or {})
        value.update(changes)
        self.save(self.stack, upgrade_checkpoint=value)

    def prepare(self):
        if self.conf.coolify.insecure_direct_url:
            raise exception.Invalid()
        self.driver = self.owner._driver(self.stack)
        if getattr(self, 'endpoint', None) is None:
            self.endpoint = self.owner._endpoint(self.stack)
        checkpoint = self.stack.upgrade_checkpoint or {}
        if self.rollback:
            self.target_image, self.target_version, snapshots = rollback_point(
                self.stack)
        else:
            self.target_image = checkpoint.get('image_id')
            snapshots = {}
        recorded_version = self.target_version if self.rollback else None
        self.target_version = eligible(
            self.conf, self.stack, self.target_image,
            self.driver.SUPPORTED_ENGINE_VERSIONS, newer=not self.rollback)
        if self.rollback and self.target_version != recorded_version:
            raise exception.Conflict()
        if not self.rollback and self.driver.get_engine_version(
                self.endpoint) != self.stack.engine_version:
            raise exception.Conflict()
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            current = db_api.stacks._get(self.ctx, self.stack.id, lock=True)
            if (current.task_state != self.task_state or
                    current.conductor_host != self.conf.host or
                    (current.upgrade_checkpoint or {}).get('operation_id') !=
                    self.receipt):
                raise exception.Conflict()
            self.locked_services = idle(self.ctx, self.stack)
            controllers = objects.ServerList.get_all(self.ctx, filters={
                'stack_id': self.stack.id, 'role': 'control'}, limit=2)
            if len(controllers) != 1:
                raise exception.Conflict()
            self.server = controllers[0]
            allowed = (states.ACTIVE, states.ERROR) if self.rollback else (
                states.ACTIVE,)
            if (self.server.kind != 'vm' or self.server.status not in allowed
                    or self.server.task_state != states.NONE or
                    not all((self.server.image_id, self.server.compute_id,
                             self.server.flavor_id, self.server.mgmt_ip))):
                raise exception.Conflict()
            self.server.acquire_task(self.task_state)
            for item in self.locked_services:
                item.acquire_task(self.task_state)
            self.workloads = self.locked_services
        if not self.rollback and eligible(
                self.conf, self.stack, self.server.image_id,
                self.driver.SUPPORTED_ENGINE_VERSIONS, newer=False) != (
                self.stack.engine_version):
            raise exception.Conflict()
        self.old_id = self.server.compute_id
        self.old_cert = (self.server.agent_cert_serial,
                         self.server.agent_cert_expires_at)
        self.cloud = RebuildCloud(self.conf, self.stack)
        self.ports = self.cloud.retained_ports(self.server)
        with db_api.reader.using(self.ctx):
            self.volumes = [objects.Volume.obj_from_db(self.ctx, row)
                            for row in db_api.volumes._query(self.ctx)
                            .filter_by(server_id=self.server.id).limit(
                                4).all()]
        if (len(self.volumes) != 3 or
                {v.purpose for v in self.volumes} != set(PURPOSES) or
                {v.purpose: v.mount_path for v in self.volumes} != {
                    'data': '/data', 'docker': '/var/lib/docker',
                    'registry': '/var/lib/registry'}):
            raise exception.Conflict()
        for volume in self.volumes:
            if not volume.cinder_volume_id:
                raise exception.Conflict()
            found = self.cloud.proxy('cinder').get_volume(
                volume.cinder_volume_id)
            if (not found or volume.status != states.IN_USE or
                    not all(found.metadata.get(k) == v for k, v in
                            self.metadata().items()) or
                    any(a['server_id'] != self.old_id
                        for a in (found.attachments or []))):
                raise exception.Conflict()
        if self.server.floating_ip_id:
            floating = self.cloud.proxy('neutron').get_ip(
                self.server.floating_ip_id)
            if floating.port_id != self.ports[1].id:
                raise exception.Conflict()
        self.agent = agent_client.AgentClient(self.conf, self.server.mgmt_ip)
        self.original_volumes = [v.obj_clone() for v in self.volumes]
        self.previous_snapshots = set(
            (self.stack.upgrade_snapshots or {}).values())
        self.previous_snapshots.update(
            checkpoint.get('retained_snapshot_ids', []))
        if self.rollback:
            self.checkpoint(prior_attempt=checkpoint)
        self.checkpoint(operation_id=self.receipt, image_id=self.target_image,
                        rollback=self.rollback, boot_attempted=False,
                        new_compute_id=None,
                        previous_image_id=(self.target_image if self.rollback
                                           else self.server.image_id),
                        previous_engine_version=(self.target_version if
                                                 self.rollback else
                                                 self.stack.engine_version),
                        snapshots=snapshots, old_compute_id=self.old_id,
                        server_id=self.server.id,
                        old_volumes={v.id: v.cinder_volume_id
                                     for v in self.volumes}, clones={})

    def rolling_targets(self):
        servers = objects.ServerList.get_all(self.ctx, filters={
            'stack_id': self.stack.id}, limit=MAX_SERVICES + 1)
        if len(servers) > MAX_SERVICES or any(
                server.kind != 'vm' or server.status != states.ACTIVE or
                server.task_state != states.NONE for server in servers
                if server.id != self.server.id):
            raise exception.Conflict()
        self.targets = sorted((server for server in servers
                               if server.role in ('worker', 'build')),
                              key=lambda server: (server.role, server.id))
        if not self.targets:
            return
        try:
            cloud = controller.ControllerCloud(self.conf, self.stack)
            image = cloud.select_image('worker')
        except ValueError:
            raise exception.Invalid(
                'Published App engine server worker image is required'
            ) from None
        if (image.properties or {}).get('coast_engine_version') != (
                self.target_version):
            raise exception.Invalid(
                'App engine server worker image version must match')
        self.worker_image = image.id

    def roll_servers(self):
        if self.rollback:
            return
        # The controller is already verified and retired. Keep its successful
        # image/version even if a later worker replacement fails.
        self.save(self.server, status=states.ACTIVE,
                  image_id=self.target_image, fault=None)
        self.server.release_task(self.task_state)
        self.save(self.stack, engine_version=self.target_version)
        self.checkpoint(controller_done=True)
        self.controller_done = True
        self.owner._queue(self.ctx, self.server, 'update', 'end')
        self.flush()
        for target in self.targets:
            flow = server_failover.FailoverServer(self.owner)
            flow.execute(self.ctx, target, upgrade=self)
            if not flow.succeeded:
                raise exception.Conflict()

    def metadata(self):
        return dict(tag.split('=', 1) if '=' in tag else (tag, 'true')
                    for tag in self.cloud.tags)

    def snapshots(self):
        if self.rollback:
            return
        self.rolling_targets()
        self.compensated = False
        self.quiesced = True  # A lost reply still requires a resume attempt.
        result = self.agent.request('POST', '/v1/engine/quiesce', {
            'export_id': self.receipt,
            'volume_ids': [v.id for v in self.volumes]})
        if result.get('status') != 'quiesced':
            raise exception.Conflict()
        lease = result['deadline']
        block = self.cloud.proxy('cinder')
        for volume in self.volumes:
            name = 'coast-upgrade-%s-%s' % (self.stack.id, volume.purpose)
            if self.previous_snapshots:
                name += '-' + self.receipt
            if list(block.snapshots(name=name)):
                raise exception.Conflict('Duplicate upgrade snapshot')
            snap = block.create_snapshot(
                name=name, volume_id=volume.cinder_volume_id,
                force=True, metadata=self.metadata())
            snapshots = dict(self.stack.upgrade_checkpoint['snapshots'])
            snapshots[volume.purpose] = snap.id
            self.checkpoint(snapshots=snapshots)
            remaining = min(self.conf.conductor.deploy_timeout,
                            int(lease - time.time()) - 1)
            if remaining <= 0:
                raise exception.Conflict()
            block.wait_for_status(
                snap, status='available', failures=['error'],
                interval=self.conf.controller_worker.build_poll_interval,
                wait=remaining)
        if time.time() >= lease:
            raise exception.Conflict()

    def revert_snapshots(self):
        if self.quiesced and not self.boot_attempted:
            self.agent.request('POST', '/v1/engine/resume', {
                'export_id': self.receipt})
            self.quiesced = False
            if not self.fenced:
                self.compensated = True

    def fence(self):
        self.compensated = False
        self.fenced = True
        self.cloud.stopped(self.old_id, receipt=self.receipt)
        self.save(self.server, status=states.FAILOVER)
        self.owner._queue(self.ctx, self.server, 'update', 'start')
        self.flush()

    def restore_snapshots(self):
        if not self.rollback:
            return
        block = self.cloud.proxy('cinder')
        for volume in self.volumes:
            snap = block.get_snapshot(
                self.stack.upgrade_checkpoint['snapshots'][volume.purpose])
            if (snap.status != 'available' or not all(
                    snap.metadata.get(k) == v
                    for k, v in self.metadata().items())):
                raise exception.Conflict()
            name = naming.resource_name(volume.id, 'rollback', self.receipt,
                                        conf=self.conf)
            clone = block.create_volume(
                name=name, snapshot_id=snap.id, size=volume.size_gb,
                volume_type=volume.volume_type, metadata=self.metadata())
            clones = dict(self.stack.upgrade_checkpoint['clones'])
            clones[volume.id] = clone.id
            self.checkpoint(clones=clones)
            self.cloud.wait_volume(clone.id)
            self.save(volume, cinder_volume_id=clone.id, name=name)

    def boot(self):
        key = self.secret_value(self.stack.heartbeat_key_ref)
        data, serial, expires = self.cloud.config_drive(
            self.server, self.volumes, key)
        self.save(self.server, agent_cert_serial=serial,
                  agent_cert_expires_at=expires)
        # A lost create reply may already have started database migrations.
        self.checkpoint(boot_attempted=True)
        self.boot_attempted = True
        self.deadline = time.monotonic() + self.conf.conductor.deploy_timeout
        guest = self.server.obj_clone()
        guest.image_id = self.target_image
        result = self.cloud.boot(guest, self.ports, self.volumes, data,
                                 attempt=self.receipt)
        self.checkpoint(new_compute_id=result.id)
        self.save(self.server, compute_id=result.id)

    def retire(self):
        self.cloud.delete_guest(self.old_id)
        self.retired = True

    def revert_boot(self):
        # Never restart N with disks that may have been migrated by N+1.
        if self.boot_attempted and not self.controller_done:
            guest = self.cloud.replacement(self.receipt)
            if guest:
                self.cloud.stopped(guest.id)

    def revert_fence(self):
        if not self.boot_attempted:
            for original in self.original_volumes:
                volume = objects.Volume.get_by_id(self.ctx, original.id)
                self.save(volume, cinder_volume_id=original.cinder_volume_id,
                          name=original.name)
            self.volumes = self.original_volumes
            super().revert_fence()
            self.save(self.server, agent_cert_serial=self.old_cert[0],
                      agent_cert_expires_at=self.old_cert[1])
            self.compensated = True

    def active(self):
        remaining = int(self.deadline - time.monotonic())
        if remaining <= 0:
            raise exception.Conflict()
        self.cloud.wait_server(self.server.compute_id, timeout=remaining)

    def health(self):
        # All readiness calls use the deploy budget, not an unbounded poll.
        while True:
            if time.monotonic() >= self.deadline:
                raise exception.Conflict()
            try:
                health = self.agent.request('GET', '/health')
                if health.get('status') == 'ok' and health.get(
                        'egress', {}).get('status') == 'ok':
                    return
            except base.DriverError:
                pass
            self.pause(self.deadline)

    def engine(self):
        while True:
            if time.monotonic() >= self.deadline:
                raise exception.Conflict(
                    message='App engine did not reach the target version '
                            'before the upgrade deadline')
            try:
                if self.driver.get_engine_version(
                        self.endpoint) == self.target_version:
                    return
            except base.EngineUnreachable:
                pass
            self.pause(self.deadline)

    def services(self):
        with db_api.reader.using(self.ctx):
            servers = [objects.Server.obj_from_db(self.ctx, row)
                       for row in db_api.servers._query(self.ctx).filter_by(
                           stack_id=self.stack.id).limit(
                               MAX_SERVICES + 1).all()]
        if len(servers) > MAX_SERVICES:
            raise exception.Conflict()
        for server in servers:
            if time.monotonic() >= self.deadline:
                raise exception.Conflict()
            if server.role in ('worker', 'build') and (
                    server.status != states.ACTIVE or
                    self.driver.get_server_status(
                        self.endpoint, self.stack, server).status !=
                    states.ACTIVE):
                raise exception.Conflict()
        for service in self.workloads:
            if time.monotonic() >= self.deadline:
                raise exception.Conflict()
            observed = self.driver.get_app_status(
                self.endpoint, self.stack, service).status
            if service.status == states.ACTIVE and observed == states.STOPPED:
                # The public driver reports an absent running container as
                # STOPPED. Do not touch stopped services or healthy containers.
                if service.is_engine_service:
                    raise exception.Conflict()
                self.redeploy(service, self.deadline)
                observed = self.driver.get_app_status(
                    self.endpoint, self.stack, service).status
            if observed != service.status:
                raise exception.Conflict()

    def complete(self):
        if not self.finish(True):
            return
        # The old point is unused only after durable success. Cleanup errors
        # never turn a completed upgrade into a failed migration.
        if not self.rollback:
            try:
                block = self.cloud.proxy('cinder')
            except Exception:
                LOG.warning('Superseded upgrade snapshots retained.')
                return
            for identifier in self.previous_snapshots:
                try:
                    block.delete_snapshot(identifier, ignore_missing=True)
                except Exception:
                    LOG.warning('Superseded upgrade snapshot retained: %s',
                                identifier)

    def record_success(self, stack, checkpoint):
        values = dict(engine_version=self.target_version, status=states.ACTIVE,
                      fault=None, upgrade_checkpoint=None)
        if not self.rollback or not stack.previous_image_id:
            values.update(previous_image_id=checkpoint['previous_image_id'],
                          previous_engine_version=checkpoint[
                              'previous_engine_version'],
                          upgrade_snapshots=checkpoint['snapshots'])
        if self.rollback:
            point = values.get('upgrade_snapshots', stack.upgrade_snapshots)
            retained = set(checkpoint.get('retained_snapshot_ids', []))
            retained.update(set(checkpoint['snapshots'].values()) -
                            set((point or {}).values()))
            if retained:
                values['upgrade_checkpoint'] = {
                    'retained_snapshot_ids': sorted(retained)}
        self.save(stack, **values)

    def finish(self, success, transient=False):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            stack = objects.Stack.obj_from_db(self.ctx, db_api.stacks._get(
                self.ctx, self.stack.id, lock=True))
            if (stack.task_state != self.task_state or
                    stack.conductor_host != self.conf.host or
                    (stack.upgrade_checkpoint or {}).get('operation_id') !=
                    self.receipt):
                return False
            if self.server:
                self.server = objects.Server.get_by_id(
                    self.ctx, self.server.id)
            checkpoint = stack.upgrade_checkpoint or {}
            for item in self.new_deployments:
                deployment = objects.Deployment.get_by_id(self.ctx, item.id)
                if deployment.status == states.DEPLOYING:
                    self.save(deployment, status=states.FAILED,
                              fault=deployments.UNKNOWN)
                    self.owner._event(self.ctx, deployment, stack, 'create',
                                      'failure', str(uuid.uuid5(
                                          uuid.UUID(deployment.id),
                                          'failover')))
            if success:
                self.save(self.server, status=states.ACTIVE,
                          image_id=self.target_image, fault=None)
                self.record_success(stack, checkpoint)
            else:
                unsafe = (not transient and (
                    self.boot_attempted or self.rollback or
                    not self.compensated))
                status = states.ERROR if unsafe else states.ACTIVE
                if transient:
                    status = checkpoint.get(
                        'admission_status',
                        states.ERROR if self.rollback else states.ACTIVE)
                self.save(stack, status=status,
                          fault=(stack.fault if transient else
                                 FAILED if self.boot_attempted else REFUSED))
                if (not transient and not self.controller_done and
                        self.server and
                        self.server.task_state == self.task_state):
                    self.server = objects.Server.get_by_id(
                        self.ctx, self.server.id)
                    self.save(self.server, status=states.ERROR if unsafe
                              else states.ACTIVE,
                              fault=FAILED if unsafe else None)
            if self.server and self.fenced and not self.controller_done:
                self.owner._queue(self.ctx, self.server, 'update',
                                  'end' if success else 'error')
            if self.server and self.server.task_state == self.task_state:
                self.server.release_task(self.task_state)
            for item in self.locked_services:
                item = objects.Service.get_by_id(self.ctx, item.id)
                if item.task_state == self.task_state:
                    item.release_task(self.task_state)
            stack.release_task(self.task_state)
            self.owner._event(self.ctx, stack, stack, self.action,
                              'success' if success else 'failure',
                              self.receipt, detail={
                                  'from_engine_version': self.from_version,
                                  'to_engine_version': getattr(
                                      self, 'target_version', None),
                                  'image_id': getattr(
                                      self, 'target_image', None),
                                  'server_id': (self.server.id
                                                if self.server else None),
                                  'old_compute_id': getattr(
                                      self, 'old_id', None),
                                  'new_compute_id': checkpoint.get(
                                      'new_compute_id')})
        self.flush()
        return True


def interrupted(owner, ctx, stack):
    action = ('upgrade_rollback' if stack.task_state == states.ROLLING_BACK
              else 'upgrade')
    for klass in (objects.Server, objects.Service):
        rows = klass.repository._query(ctx).filter_by(
            stack_id=stack.id, task_state=stack.task_state).limit(
                MAX_SERVICES + 1).all()
        for row in rows:
            item = klass.obj_from_db(ctx, row)
            if isinstance(item, objects.Server):
                owner._save(item, status=states.ERROR, fault=FAILED)
                owner._queue(ctx, item, 'update', 'error')
            item.release_task(stack.task_state)
    starts = objects.EventList.get_all(ctx, filters={
        'resource_id': stack.id, 'action': action, 'outcome': 'start'},
        sort_key='created_at', sort_dir='desc', limit=1)
    owner._event(ctx, stack, stack, action, 'failure',
                 starts[0].id if starts else None, detail={
                     key: (stack.upgrade_checkpoint or {}).get(key)
                     for key in ('server_id', 'old_compute_id',
                                 'new_compute_id')})
