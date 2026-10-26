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

"""Rebuild one Coolify controller VM on its recorded image.

Fence the old guest before any detach. A confirmed SHUTOFF guest cannot
write the volumes that move with the same ports and floating IP. See the
Compute proxy:
https://docs.openstack.org/openstacksdk/latest/user/proxies/compute.html
"""

import time
import uuid

from oslo_log import log as logging
from oslo_utils import timeutils
from taskflow import engines
from taskflow.patterns import linear_flow

from coast.common import exception
from coast.common import notifications
from coast.common import safe_log
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import deployments
from coast.conductor import engine_upgrade
from coast.conductor import rpcapi
from coast.conductor import server_failover
from coast.conductor import stack_create
from coast.db import api as db_api
from coast.db import models
from coast.health_manager import failover
from coast.health_manager import stale
from coast import objects

LOG = logging.getLogger(__name__)
IMAGE_FAULT = (
    'Operator action needed: controller image is not the recorded '
    'engine image, Q122.')
REFUSED = 'Controller failover refused.'
MOUNTS = {'data': '/data', 'docker': '/var/lib/docker',
          'registry': '/var/lib/registry'}


class ImageRefused(exception.Conflict):
    """The recorded controller image is missing or is not version N."""


def recorded_image(conf, stack, server):
    """True when Glance still has this server's version N controller image.

    Invalid means the image is missing or not a Coolify controller image.
    Other errors propagate so a Glance outage is not treated as Q122.
    """
    if not server.image_id:
        return False
    try:
        found = engine_upgrade.eligible(
            conf, stack, server.image_id, newer=False)
    except exception.Invalid:
        return False
    return found == stack.engine_version


def _image_identifier(ctx, server):
    previous = failover.episode(ctx, server)
    if previous:
        name = 'image:' + (previous.message or '')
        source = previous.id
    else:
        name = 'image:' + (server.image_id or '')
        source = server.id
    return str(uuid.uuid5(uuid.UUID(str(source)), name))


def note_image(ctx, server):
    """Record one Q122 event. Return True only when the row is new."""
    with db_api.writer.using(ctx):
        # The admin path has no session until this writer starts.
        identifier = _image_identifier(ctx, server)
        if db_api.events._query(ctx).filter_by(id=identifier).first():
            return False
        objects.Event(
            context=ctx, id=identifier, stack_id=server.stack_id,
            resource_type='server', resource_id=server.id, action='failover',
            outcome='failure', message=IMAGE_FAULT,
            detail={'server_name': server.name,
                    'old_compute_id': server.compute_id,
                    'new_compute_id': None, 'reason': IMAGE_FAULT}).create()
    return True


def ensure_image(ctx, stack, server, conf):
    """Refuse an admin failover whose recorded image cannot boot version N."""
    if recorded_image(conf, stack, server):
        return
    if note_image(ctx, server):
        notifications.emit(
            ctx, notifications.API, 'server', 'failover', 'error',
            notifications.payload_for(server, ctx), conf=conf)
    failover.warn_once(ctx, server, LOG, 'image_unavailable')
    raise exception.Conflict()


def _note_refused(ctx, server):
    identifier = str(uuid.uuid5(uuid.UUID(str(server.id)),
                                'refused:' + (server.image_id or '')))
    with db_api.writer.using(ctx):
        if db_api.events._query(ctx).filter_by(id=identifier).first():
            return False
        objects.Event(
            context=ctx, id=identifier, stack_id=server.stack_id,
            resource_type='server', resource_id=server.id, action='failover',
            outcome='failure', message=REFUSED,
            detail={'server_name': server.name,
                    'old_compute_id': server.compute_id,
                    'new_compute_id': None, 'reason': REFUSED}).create()
    return True


def lock_workloads(ctx, stack_id):
    # Take these before infrastructure rows: synchronous service completion
    # can validate FK parents / insert events after its engine write.
    return db_api.services._query(ctx).filter_by(
        stack_id=stack_id).order_by(models.Service.id).limit(
            engine_upgrade.MAX_SERVICES + 1).with_for_update().all()


def idle_workloads(ctx, stack, conf):
    """Check claims under the caller's project/stack writer locks.

    Return services, expired task holders and expired deployments without
    changing any rows. Both API and conductor admission use this check.
    """
    # Like Manager._recover_domain_task, use age plus row locks: services
    # have no conductor_host or runner heartbeat. Never infer death from
    # app status. Wait out the durable deployment/start operation budget.
    # Service row locks serialize engine writers. Enqueue/cancellation and
    # background sync revalidate claims under that lock through their writes;
    # callbacks recheck the task claim before persisting observations. These
    # HTTP fences take one service, then children, without project/stack locks.
    # Service-first admission also protects synchronous FK/event completion.
    # Age alone never permits admission past a live writer.
    rows = lock_workloads(ctx, stack.id)
    if len(rows) > engine_upgrade.MAX_SERVICES:
        raise exception.Conflict('Too many apps to lock for failover.')
    services = [objects.Service.obj_from_db(ctx, row) for row in rows]
    abandoned = []
    pending_deployments = []
    now = timeutils.utcnow()
    budget = max(300, conf.conductor.deploy_timeout,
                 conf.conductor.start_timeout)

    def expired(stamp):
        return (stamp is not None and
                (now - timeutils.normalize_time(stamp)).total_seconds()
                >= budget)

    for service in services:
        if db_api.backup_jobs._query(ctx).filter_by(
                service_id=service.id).filter(
                    models.BackupJob.status.in_((
                        states.PENDING, states.BUILDING))).first():
            raise exception.Conflict('App %s has an unfinished backup job.'
                                     % service.id)
        query = db_api.deployments._query(ctx).filter_by(
            service_id=service.id)
        pending = [objects.Deployment.obj_from_db(ctx, row)
                   for row in query.filter(
                           models.Deployment.status.in_((
                               states.PENDING, states.BUILDING,
                               states.DEPLOYING))).all()]
        if any(not expired(item.started_at or item.created_at)
               for item in pending):
            raise exception.Conflict('App %s has an unexpired deployment.'
                                     % service.id)
        if service.task_state != states.NONE:
            if not expired(service.task_started_at or service.updated_at):
                raise exception.Conflict('App %s has an unexpired task.'
                                         % service.id)
            abandoned.append(service)
        pending_deployments.extend(pending)
    return services, abandoned, pending_deployments


class ControllerFailover(server_failover.FailoverServer):
    action = 'failover'
    task_state = states.FAILING_OVER
    steps = ('fence', 'move', 'boot', 'active', 'health', 'engine',
             'services', 'retire', 'complete')

    def execute(self, ctx, resource, options=None, reservation=None,
                revision=None):
        if resource.role != 'control':
            failover.warn_once(ctx, resource, LOG, 'unsupported_resource')
            return
        self.target_flavor = (options or {}).get('target_flavor')
        self.admission_status = (options or {}).get('admission_status')
        self.target_plan = (options or {}).get('target_plan')
        self.succeeded = False
        self.ctx = ctx
        self.server_id = resource.id
        self.stack_id = resource.stack_id
        self.revision = revision or rpcapi.revision(resource)
        self.receipt = rpcapi.receipt_id(
            'failover_server', resource, self.revision)
        self.old_receipt = str(uuid.uuid5(
            uuid.UUID(self.receipt), 'old-guest'))
        self.boot_attempted = self.fenced = self.moved = self.retired = False
        self.locked_services = []
        self.new_deployments = []
        self.server = None
        self.admin = False
        self.image_ok = False
        try:
            stack = objects.Stack.get_by_id(ctx, resource.stack_id)
        except exception.NotFound:
            failover.warn_once(ctx, resource, LOG, 'stack_unavailable')
            return
        if stack.driver == 'noop':
            failover.warn_once(ctx, resource, LOG, 'driver_disabled')
            return
        if resource.kind != 'vm':
            self._baremetal()
            return
        if not self.secrets_available(ctx, stack, resource):
            return
        self.checked_image = resource.image_id
        self.checked_version = stack.engine_version
        try:
            # Catalog reads stay outside the project and global locks.
            self.image_ok = recorded_image(self.conf, stack, resource)
        except Exception:
            # A Glance outage is not Q122. Release only an admin claim.
            failover.warn_once(ctx, resource, LOG, 'image_lookup_failed')
            self._refuse_unclaimed(False)
            self.flush()
            raise
        if not self._admit():
            self.flush()
            return
        self.flush()
        started = time.monotonic()
        safe_log.log_failover(
            LOG, 'info', self.server, 'start', 'admitted',
            old_compute_id=self.old_id)
        try:
            self.prepare()
            engines.run(linear_flow.Flow('controller-failover').add(*[
                stack_create.Step(self, name) for name in self.steps]),
                engine='serial')
        except ImageRefused:
            failover.warn_once(ctx, self.server, LOG, 'image_unavailable')
            self.owner._pending = []
            self.owner._seen = set()
            self.finish(False, image=True)
        except Exception as exc:
            safe_log.log_failure(
                LOG, 'Controller failover failed: %(cause)s at %(where)s '
                'request_id=%(request_id)s', exc, request_id=ctx.request_id)
            self.owner._pending = []
            self.owner._seen = set()
            self.finish(False)
        finally:
            safe_log.log_failover(
                LOG, 'info', self.server, 'end', 'completed',
                outcome='success' if self.succeeded else 'failure',
                duration='%.3f' % (time.monotonic() - started),
                old_compute_id=self.old_id,
                new_compute_id=self.server.compute_id)

    def _accepted(self, session):
        server = self.server
        if (server.stack_id != self.stack.id or
                rpcapi.revision(server) != self.revision or
                objects.EventList.get_all(self.ctx, filters={
                    'id': self.receipt}, limit=1)):
            failover.warn_once(self.ctx, server, LOG, 'stale_request')
            return False
        self.admin = (server.status == states.FAILOVER and
                      server.task_state == states.FAILING_OVER)
        automatic = failover.due(
            self.ctx, server, self.conf, timeutils.utcnow())
        if not self.admin and not automatic:
            failover.warn_once(self.ctx, server, LOG, 'not_due')
            return False
        drifted = (server.image_id != self.checked_image or
                   self.stack.engine_version != self.checked_version)
        if drifted or not self.image_ok:
            failover.warn_once(self.ctx, server, LOG, 'image_unavailable')
            if self.admin:
                self._release_admin(server, image=not drifted)
            elif not drifted:
                if note_image(self.ctx, server):
                    self.owner._queue(
                        self.ctx, server, 'failover', 'error')
            return False
        capacity = server_failover.room(session, self.conf, server.id)
        ready = (self.stack.driver != 'noop' and
                 self.stack.task_state == (states.FAILING_OVER
                                           if self.target_flavor else
                                           states.NONE) and
                 self.stack.status in (states.ACTIVE, states.DEGRADED) and
                 all((server.image_id, server.compute_id, server.flavor_id,
                      server.mgmt_ip)) and
                 capacity and
                 not self._peer_busy())
        if not ready:
            reason = ('global_capacity' if not capacity else
                      'stack_busy' if self.stack.task_state != (
                          states.FAILING_OVER if self.target_flavor else
                          states.NONE) or self._peer_busy() else
                      'server_unavailable')
            failover.warn_once(self.ctx, server, LOG, reason)
            if self.admin:
                self._release_admin(server)
            return False
        try:
            self.locked_services = (engine_upgrade.idle(self.ctx, self.stack)
                                    if self.target_flavor else
                                    self._idle_workloads())
        except exception.Conflict:
            failover.warn_once(self.ctx, server, LOG, 'services_busy')
            if self.admin:
                self._release_admin(server)
            return False
        self.workloads = self.locked_services
        return True

    def _idle_workloads(self):
        services, abandoned, self.stale_deployments = idle_workloads(
            self.ctx, self.stack, self.conf)
        # Validate every row before changing any claim. Only ownership changes
        # here; engine observations and deployment outcomes wait for health.
        for service in abandoned:
            service.release_task(service.task_state)
        return services

    def _peer_busy(self):
        return db_api.servers._query(self.ctx).filter(
            models.Server.stack_id == self.stack.id,
            models.Server.id != self.server.id,
            models.Server.task_state != states.NONE).first()

    def _admit(self):
        try:
            with db_api.writer.using(self.ctx) as session:
                db_api._lock_project(session, server_failover.GLOBAL_LOCK)
                db_api._lock_project(session, self.ctx.project_id)
                lock_workloads(self.ctx, self.stack_id)
                self.stack = objects.Stack.obj_from_db(
                    self.ctx, db_api.stacks._get(
                        self.ctx, self.stack_id, lock=True))
                self.server = objects.Server.obj_from_db(
                    self.ctx, db_api.servers._get(
                        self.ctx, self.server_id, lock=True))
                if not self._accepted(session):
                    return False
                self._claim()
                return True
        except exception.Conflict:
            # The API commit survives this rollback. Release it here.
            # Persist the warning after the claim transaction rolls back.
            failover.warn_once(self.ctx, self.server, LOG, 'claim_conflict')
            self._refuse_unclaimed()
            return False

    def _claim(self):
        if self.server.task_state == states.NONE:
            self.server.acquire_task(states.FAILING_OVER)
        for service in self.locked_services:
            service.acquire_task(states.FAILING_OVER)
        if not self.target_flavor:
            self.stack.acquire_task(states.FAILING_OVER)
        self.save(self.stack, conductor_host=self.conf.host)
        if self.server.status != states.FAILOVER:
            self.save(self.server, status=states.FAILOVER)
        self.old_id = self.server.compute_id
        self.old_cert = (self.server.agent_cert_serial,
                         self.server.agent_cert_expires_at)
        self.owner._event(self.ctx, self.server, self.stack, 'failover',
                          'start', self.receipt)
        objects.Event(
            context=self.ctx, id=self.old_receipt, stack_id=self.stack.id,
            resource_type='server', resource_id=self.server.id,
            action='delete.cleanup', outcome='start',
            message=self.old_id).create()

    def _release_admin(self, server, image=False):
        # FAILOVER cannot return to UNHEALTHY. ACTIVE lets the next stale
        # check restore UNHEALTHY when the heartbeat is still old.
        self.save(server, status=states.ACTIVE, fault=None)
        if server.task_state == states.FAILING_OVER:
            server.release_task(states.FAILING_OVER)
        created = note_image(self.ctx, server) if image else _note_refused(
            self.ctx, server)
        if created:
            self.owner._queue(self.ctx, server, 'failover', 'error')

    def _refuse_unclaimed(self, image=False):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            try:
                server = objects.Server.obj_from_db(
                    self.ctx, db_api.servers._get(
                        self.ctx, self.server_id, lock=True))
            except exception.NotFound:
                return
            if (rpcapi.revision(server) != self.revision or
                    server.status != states.FAILOVER or
                    server.task_state != states.FAILING_OVER or
                    objects.EventList.get_all(self.ctx, filters={
                        'id': self.receipt}, limit=1)):
                return
            self._release_admin(server, image=image)

    def _mark_baremetal(self, server):
        if failover.note_baremetal(self.ctx, server):
            return True
        if failover.episode(self.ctx, server):
            return False
        identifier = str(uuid.uuid5(uuid.UUID(str(server.id)), 'baremetal'))
        if db_api.events._query(self.ctx).filter_by(id=identifier).first():
            return False
        objects.Event(
            context=self.ctx, id=identifier, stack_id=server.stack_id,
            resource_type='server', resource_id=server.id, action='failover',
            outcome='failure', message=failover.BAREMETAL_FAULT).create()
        return True

    def _baremetal(self):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            self.stack = objects.Stack.obj_from_db(
                self.ctx, db_api.stacks._get(
                    self.ctx, self.stack_id, lock=True))
            server = objects.Server.obj_from_db(
                self.ctx, db_api.servers._get(
                    self.ctx, self.server_id, lock=True))
            if (rpcapi.revision(server) != self.revision or
                    server.kind == 'vm' or server.role != 'control'):
                failover.warn_once(self.ctx, server, LOG, 'stale_request')
                return
            admin = (server.status == states.FAILOVER and
                     server.task_state == states.FAILING_OVER)
            if not admin and not failover.past_grace(
                    self.ctx, server, self.conf, timeutils.utcnow()):
                failover.warn_once(self.ctx, server, LOG, 'not_due')
                return
            if server.task_state == states.NONE:
                server.acquire_task(states.FAILING_OVER)
            elif server.task_state != states.FAILING_OVER:
                failover.warn_once(self.ctx, server, LOG, 'server_unavailable')
                return
            self.save(server, status=states.ERROR,
                      fault=failover.BAREMETAL_FAULT)
            if self._mark_baremetal(server):
                failover.warn_once(self.ctx, server, LOG, 'bare_metal')
                self.owner._queue(self.ctx, server, 'failover', 'error')
            self.server = server
            self._rollup(session)
            server.release_task(states.FAILING_OVER)
        self.flush()

    def prepare(self):
        if self.conf.coolify.insecure_direct_url:
            raise exception.Invalid()
        try:
            found = engine_upgrade.eligible(
                self.conf, self.stack, self.server.image_id, newer=False)
        except exception.Invalid:
            raise ImageRefused() from None
        if found != self.stack.engine_version:
            raise ImageRefused()
        self.target_version = self.stack.engine_version
        self.driver = self.owner._driver(self.stack)
        if self.endpoint is None:
            self.endpoint = self.owner._endpoint(self.stack)
        self.cloud = engine_upgrade.RebuildCloud(self.conf, self.stack)
        self.ports = self.cloud.retained_ports(self.server)
        with db_api.reader.using(self.ctx):
            self.volumes = [objects.Volume.obj_from_db(self.ctx, row)
                            for row in db_api.volumes._query(self.ctx)
                            .filter_by(server_id=self.server.id).limit(4)
                            .all()]
        mounts = {volume.purpose: volume.mount_path for volume in self.volumes}
        if (len(self.volumes) != 3 or mounts != MOUNTS or
                any(volume.status != states.IN_USE or
                    not volume.cinder_volume_id for volume in self.volumes)):
            raise exception.Invalid()
        metadata = dict(tag.split('=', 1) if '=' in tag else (tag, 'true')
                        for tag in self.cloud.tags)
        for volume in self.volumes:
            found = self.cloud.proxy('cinder').get_volume(
                volume.cinder_volume_id)
            if (not found or not all(found.metadata.get(key) == value
                                     for key, value in metadata.items()) or
                    any(item['server_id'] != self.old_id
                        for item in (found.attachments or []))):
                raise exception.Invalid()
        if self.server.floating_ip_id:
            floating = self.cloud.proxy('neutron').get_ip(
                self.server.floating_ip_id)
            if floating.port_id != self.ports[1].id:
                raise exception.Invalid()
        self.agent = agent_client.AgentClient(self.conf, self.server.mgmt_ip)

    def boot(self):
        self.deadline = time.monotonic() + self.conf.conductor.deploy_timeout
        server_failover.FailoverServer.boot(self)

    def active(self):
        engine_upgrade.Upgrade.active(self)

    def health(self):
        engine_upgrade.Upgrade.health(self)

    def engine(self):
        engine_upgrade.Upgrade.engine(self)

    def services(self):
        # Worker health belongs to the tenant workload, not controller
        # readiness. A failed or unreachable worker must not roll back a
        # healthy replacement controller. The health rollup retains DEGRADED.
        if self.target_flavor:
            self._verify_workers()
        for deployment in getattr(self, 'stale_deployments', []):
            self.save(deployment, status=states.FAILED,
                      fault=deployments.UNKNOWN,
                      finished_at=timeutils.utcnow())
            self.owner._event(self.ctx, deployment, self.stack, 'create',
                              'failure')
            self.owner._queue(self.ctx, deployment, 'create', 'error')
        for service in self.workloads:
            try:
                self._recover_service(service)
            except Exception:
                if self.target_flavor:
                    raise
                # Do not let tenant workload failures undo a healthy control
                # plane. Keep desired state and leave recovery to the tenant.
                LOG.warning('Workload recovery deferred for service %s after '
                            'controller recovery.', service.id)

    def _verify_workers(self):
        with db_api.reader.using(self.ctx):
            servers = [objects.Server.obj_from_db(self.ctx, row)
                       for row in db_api.servers._query(self.ctx).filter_by(
                           stack_id=self.stack.id).limit(
                               engine_upgrade.MAX_SERVICES + 1).all()]
        if len(servers) > engine_upgrade.MAX_SERVICES:
            raise exception.Conflict()
        for server in servers:
            if time.monotonic() >= self.deadline:
                raise exception.Conflict()
            if server.role not in ('worker', 'build'):
                continue
            if server.status not in (states.ACTIVE, states.UNHEALTHY):
                raise exception.Conflict()
            observed = self.driver.get_server_status(
                self.endpoint, self.stack, server).status
            if observed != server.status:
                raise exception.Conflict()

    def _recover_service(self, service):
        if time.monotonic() >= self.deadline:
            raise exception.Conflict()
        observed = self.driver.get_app_status(
            self.endpoint, self.stack, service).status
        if service.status == states.ACTIVE and observed == states.STOPPED:
            if service.is_engine_service:
                raise exception.Conflict()
            self.redeploy(service, self.deadline)
            observed = self.driver.get_app_status(
                self.endpoint, self.stack, service).status
        if not self.target_flavor and observed in (
                states.ACTIVE, states.STOPPED, states.ERROR):
            if observed != service.status:
                self.save(service, status=observed)
                self.owner._queue(self.ctx, service, 'update', 'end')
        elif observed != service.status:
            raise exception.Conflict()

    def _rollup(self, session):
        target = stale.apply_health_rollup(
            session, self.ctx.project_id, self.stack)
        if target is not None:
            self.save(self.stack, status=target)

    def finish(self, success, image=False):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            self.server = objects.Server.get_by_id(self.ctx, self.server_id)
            self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            if self.server.task_state != states.FAILING_OVER:
                return
            for item in self.new_deployments:
                deployment = objects.Deployment.get_by_id(self.ctx, item.id)
                if deployment.status == states.DEPLOYING:
                    self.save(deployment, status=states.FAILED,
                              fault=deployments.UNKNOWN,
                              finished_at=timeutils.utcnow())
                    self.owner._event(
                        self.ctx, deployment, self.stack, 'create', 'failure',
                        str(uuid.uuid5(uuid.UUID(deployment.id), 'failover')))
            self.succeeded = success
            if success:
                self.save(self.server, status=states.ACTIVE, fault=None)
                if self.target_flavor:
                    self.save(self.server, flavor_id=self.target_flavor)
                    self.save(self.stack, plan=self.target_plan)
            elif image:
                self.save(self.server, status=states.ACTIVE, fault=None)
                if note_image(self.ctx, self.server):
                    self.owner._queue(
                        self.ctx, self.server, 'failover', 'error')
            else:
                self.save(self.server, status=states.ERROR,
                          fault=server_failover.CONTROLLER_FAULT)
            self._rollup(session)
            self.server.release_task(states.FAILING_OVER)
            for service in self.locked_services:
                service = objects.Service.get_by_id(self.ctx, service.id)
                if service.task_state == states.FAILING_OVER:
                    service.release_task(states.FAILING_OVER)
            if (not self.target_flavor and
                    self.stack.task_state == states.FAILING_OVER):
                self.stack.release_task(states.FAILING_OVER)
            self.owner._event(
                self.ctx, self.server, self.stack, 'failover',
                'success' if success else 'failure', self.receipt)
        self.flush()
