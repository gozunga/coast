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

"""CAS-claimed disposable worker/build guests; retained storage and ingress."""

import copy
import time
import uuid

from oslo_log import log as logging
from oslo_utils import timeutils
from taskflow import engines
from taskflow.patterns import linear_flow

from coast.api import secret_store
from coast.common import exception
from coast.common import safe_log
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import deployments
from coast.conductor import rpcapi
from coast.conductor import secret_retry
from coast.conductor import server_add
from coast.conductor import stack_create
from coast.conductor import worker
from coast.db import api as db_api
from coast.db import models
from coast.drivers import base
from coast.health_manager import failover
from coast import objects

LOG = logging.getLogger(__name__)

GLOBAL_LOCK = 'coast-worker-failover'
FAULT = 'Worker failover failed; operator recovery is required.'
CONTROLLER_FAULT = (
    'Controller failover failed; operator recovery is required.')
MAX_SERVICES = 100
ROLES = ('worker', 'build', 'control')


def room(session, conf, server_id):
    """True when another claimed failover leaves room for this server.

    Worker, build and controller claims share one cap. The caller's own
    row is excluded so an admin task already in FAILING_OVER can proceed.
    """
    held = session.query(models.Server.id).filter(
        models.Server.task_state == states.FAILING_OVER,
        models.Server.role.in_(ROLES),
        models.Server.id != server_id,
    ).with_for_update().limit(
        conf.health_manager.failover_max_concurrent).all()
    return len(held) < conf.health_manager.failover_max_concurrent


class FailoverServer(server_add.AddServer):
    action = 'failover'
    task_state = states.FAILING_OVER
    steps = ('fence', 'move', 'boot', 'active', 'health', 'register',
             'services', 'retire', 'complete')

    def execute(self, ctx, resource, options=None, reservation=None,
                revision=None, upgrade=None):
        self.target_flavor = (options or {}).get('target_flavor')
        self.admission_status = (options or {}).get('admission_status')
        self.revision = revision or rpcapi.revision(resource)
        self.upgrade = upgrade
        self.succeeded = False
        if upgrade:
            self.task_state = states.UPGRADING
        if resource.role not in ('worker', 'build') or resource.kind != 'vm':
            failover.warn_once(ctx, resource, LOG, 'unsupported_resource')
            return
        stack = objects.Stack.get_by_id(ctx, resource.stack_id)
        if stack.driver == 'noop':
            failover.warn_once(ctx, resource, LOG, 'driver_disabled')
            return
        if not self.secrets_available(ctx, stack, resource):
            return
        self.ctx = ctx
        revision = self.revision
        self.receipt = (str(uuid.uuid5(
            uuid.UUID(upgrade.receipt), resource.id)) if upgrade else
            rpcapi.receipt_id('failover_server', resource, revision))
        self.old_receipt = str(uuid.uuid5(
            uuid.UUID(self.receipt), 'old-guest'))
        self.boot_attempted = False
        self.fenced = False
        self.moved = False
        self.retired = False
        self.new_deployments = []
        try:
            admitted = self.admit(ctx, resource, revision)
        except exception.Conflict:
            failover.warn_once(ctx, resource, LOG, 'claim_conflict')
            raise
        if not admitted:
            self.flush()
            return
        self.flush()
        started = time.monotonic()
        safe_log.log_failover(
            LOG, 'info', self.server, 'start', 'admitted',
            old_compute_id=self.old_id)
        try:
            self.prepare()
            engines.run(linear_flow.Flow('worker-failover').add(*[
                stack_create.Step(self, name) for name in self.steps]),
                engine='serial')
        except Exception as exc:
            safe_log.log_failure(server_add.LOG,
                                 'Worker failover failed: %(cause)s at '
                                 '%(where)s request_id=%(request_id)s', exc,
                                 request_id=ctx.request_id)
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

    def admit(self, ctx, resource, revision):
        upgrade = self.upgrade
        resizing = bool(self.target_flavor)
        with db_api.writer.using(ctx) as session:
            # All admissions take this sentinel before tenant/stack locks.
            # Count with a current read under MySQL REPEATABLE READ too.
            db_api._lock_project(session, GLOBAL_LOCK)
            db_api._lock_project(session, ctx.project_id)
            self.stack = objects.Stack.obj_from_db(ctx, db_api.stacks._get(
                ctx, resource.stack_id, lock=True))
            self.server = objects.Server.obj_from_db(ctx, db_api.servers._get(
                ctx, resource.id, lock=True))
            admin = (self.server.status == states.FAILOVER and
                     self.server.task_state == states.FAILING_OVER)
            stack_task = (states.UPGRADING if upgrade else
                          states.FAILING_OVER if resizing else states.NONE)
            stack_status = ((states.UPGRADING,) if upgrade else
                            (states.ACTIVE, states.DEGRADED))
            if (self.server.stack_id != self.stack.id or
                    rpcapi.revision(self.server) != revision or
                    objects.EventList.get_all(ctx, filters={
                        'id': self.receipt}, limit=1)):
                failover.warn_once(ctx, self.server, LOG, 'stale_request')
                return
            if (self.stack.driver == 'noop' or
                    self.stack.task_state != stack_task or
                    self.stack.status not in stack_status or
                    not (admin or upgrade or failover.due(
                        ctx, self.server, self.conf, timeutils.utcnow()))):
                reason = ('stack_busy' if
                          self.stack.task_state != stack_task else
                          'stack_unavailable_or_not_due')
                failover.warn_once(ctx, self.server, LOG, reason)
                if admin:
                    self.refuse_admin()
                return
            if (not (admin or upgrade) and self.server.role == 'worker' and
                    failover.templates(ctx, self.server)):
                failover.warn_once(ctx, self.server, LOG, 'template_worker')
                return
            controllers = objects.ServerList.get_all(ctx, filters={
                'stack_id': self.stack.id, 'role': 'control'}, limit=2)
            if (len(controllers) != 1 or controllers[0].status != states.ACTIVE
                    or controllers[0].task_state not in (
                        states.NONE, stack_task) or
                    controllers[0].compute_id == self.server.compute_id):
                failover.warn_once(
                    ctx, self.server, LOG, 'controller_unavailable')
                if admin:
                    self.refuse_admin()
                return
            busy = db_api.services._query(ctx).filter(
                models.Service.stack_id == self.stack.id,
                models.Service.task_state != (
                    states.UPGRADING if upgrade else states.NONE)).first()
            self.locked_services = [objects.Service.obj_from_db(ctx, row)
                                    for row in db_api.services._query(ctx)
                                    .filter_by(stack_id=self.stack.id)
                                    .limit(MAX_SERVICES + 1).all()]
            self.workloads = [service for service in self.locked_services
                              if service.server_id == self.server.id]
            if (busy or len(self.locked_services) > MAX_SERVICES or
                    not room(session, self.conf, self.server.id)):
                reason = ('stack_busy' if busy else
                          'service_limit' if len(self.locked_services) >
                          MAX_SERVICES else 'global_capacity')
                failover.warn_once(ctx, self.server, LOG, reason)
                if admin:
                    self.refuse_admin()
                return
            if not upgrade:
                for service in self.locked_services:
                    service.acquire_task(states.FAILING_OVER)
                if not resizing:
                    self.stack.acquire_task(states.FAILING_OVER)
            if not admin:
                self.server.acquire_task(self.task_state)
            self.save(self.stack, conductor_host=self.conf.host)
            self.old_id = self.server.compute_id
            self.old_cert = (self.server.agent_cert_serial,
                             self.server.agent_cert_expires_at)
            self.save(self.server, status=states.FAILOVER)
            self.owner._event(ctx, self.server, self.stack, 'failover',
                              'start', self.receipt)
            # Save the old reference before overwriting compute_id at boot.
            objects.Event(context=ctx, id=self.old_receipt,
                          stack_id=self.stack.id, resource_type='server',
                          resource_id=self.server.id, action='delete.cleanup',
                          outcome='start', message=self.old_id).create()
        return True

    def refuse_admin(self):
        self.save(self.server, status=states.ACTIVE, fault=FAULT)
        self.server.release_task(states.FAILING_OVER)
        self.owner._event(self.ctx, self.server, self.stack, 'failover',
                          'failure', self.receipt)

    def secrets_available(self, ctx, stack, server):
        self.endpoint = None
        self.secret_values = {}
        if not secret_retry.ready(server.id):
            self.release_deferred_claim(ctx, server)
            return False
        try:
            self.endpoint = self.owner._endpoint(stack)
            refs = [stack.heartbeat_key_ref]
            if server.role in ('worker', 'build'):
                refs.append(stack.ssh_key_ref)
            for reference in refs:
                self.secret_values[reference] = secret_store.call(
                    'get', ctx, reference)
        except exception.SecretStoreUnavailable:
            self.secret_values.clear()
            self.endpoint = None
            secret_retry.defer(server.id, stack.id)
            self.release_deferred_claim(ctx, server)
            return False
        except Exception:
            # Existing validation and non-secret errors belong to prepare,
            # after admission. Only secret-store failures defer admission.
            self.endpoint = None
        else:
            secret_retry.clear(server.id)
        return True

    def release_deferred_claim(self, ctx, resource):
        # The claim did not verify or change the VM. Restore the accepted
        # status and leave the existing fault intact.
        if (resource.status != states.FAILOVER or
                resource.task_state != states.FAILING_OVER):
            return
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            server = objects.Server.obj_from_db(
                ctx, db_api.servers._get(ctx, resource.id, lock=True))
            if (rpcapi.revision(server) == self.revision and
                    server.status == states.FAILOVER and
                    server.task_state == states.FAILING_OVER):
                status = getattr(self, 'admission_status', None)
                if status not in (states.ACTIVE, states.UNHEALTHY,
                                  states.ERROR):
                    status = states.ERROR
                self.save(server, status=status)
                server.release_task(states.FAILING_OVER)
                self.owner._queue(ctx, server, 'failover', 'error')
        self.flush()

    def secret_value(self, reference):
        values = getattr(self, 'secret_values', {})
        if reference in values:
            return values[reference]
        return secret_store.call('get', self.ctx, reference)

    def prepare(self):
        if (self.conf.coolify.insecure_direct_url or
                not self.server.compute_id or not self.server.engine_ref or
                not self.server.image_id or not self.server.flavor_id):
            raise exception.Invalid()
        self.driver = self.owner._driver(self.stack)
        if self.endpoint is None:
            self.endpoint = self.owner._endpoint(self.stack)
        self.cloud = worker.WorkerCloud(self.conf, self.stack)
        # Ordinary failover keeps its image; upgrades use the selected pin.
        image_id = (self.upgrade.worker_image if self.upgrade else
                    self.server.image_id)
        self.cloud.select_image('worker', image_id)
        self.target_image = image_id
        self.ports = self.cloud.retained_ports(self.server)
        with db_api.reader.using(self.ctx):
            self.volumes = [objects.Volume.obj_from_db(self.ctx, row)
                            for row in db_api.volumes._query(self.ctx)
                            .filter_by(server_id=self.server.id)
                            .limit(MAX_SERVICES + 1).all()]
        if (len(self.volumes) > MAX_SERVICES or
                sum(v.mount_path == '/data' and v.purpose == 'data'
                    for v in self.volumes) != 1 or
                sum(v.mount_path == '/var/lib/docker' and v.purpose == 'docker'
                    for v in self.volumes) != 1):
            raise exception.Invalid()
        for volume in self.volumes:
            found = self.cloud.find_volume(volume)
            if (not volume.cinder_volume_id or not found or
                    found.id != volume.cinder_volume_id or
                    volume.status != states.IN_USE or
                    any(a['server_id'] != self.old_id
                        for a in (found.attachments or []))):
                raise exception.Invalid()
        if self.server.floating_ip_id:
            floating = self.cloud.proxy('neutron').get_ip(
                self.server.floating_ip_id)
            if floating.port_id != self.ports[1].id:
                raise exception.Invalid()
        self.agent = agent_client.AgentClient(self.conf, self.server.mgmt_ip)

    def fence(self):
        self.fenced = True
        self.cloud.stopped(self.old_id, receipt=self.receipt)

    def move(self):
        self.moved = True  # Partial detach must also be reverted.
        self.cloud.detach(self.old_id, self.ports, self.volumes)

    def boot(self):
        key = self.secret_value(self.stack.heartbeat_key_ref)
        data, serial, expires = self.cloud.config_drive(
            self.server, self.volumes, key)
        self.save(self.server, agent_cert_serial=serial,
                  agent_cert_expires_at=expires)
        self.boot_attempted = True
        # Clone only to select the replacement image; identity stays saved.
        guest = self.server.obj_clone()
        if guest.role in ('worker', 'build'):
            guest.image_id = self.target_image
        if getattr(self, 'target_flavor', None):
            guest.flavor_id = self.target_flavor
        result = self.cloud.boot(guest, self.ports, self.volumes, data,
                                 attempt=self.receipt)
        self.save(self.server, compute_id=result.id)

    def revert_boot(self):
        if not self.boot_attempted or self.retired:
            return
        guest = self.cloud.replacement(self.receipt)
        if not guest:
            # A lost create reply may still be in flight. Do not permit the
            # old VM to write until replacement deletion is confirmed.
            raise exception.Conflict()
        self.cloud.delete_guest(guest.id)
        self.boot_attempted = False
        self.save(self.server, compute_id=self.old_id,
                  agent_cert_serial=self.old_cert[0],
                  agent_cert_expires_at=self.old_cert[1])

    def revert_move(self):
        # Revert fence restores attachments after replacement cleanup.
        pass

    def revert_fence(self):
        if self.fenced and not self.boot_attempted and not self.retired:
            if self.moved:
                self.cloud.restore(self.old_id, self.ports, self.volumes)
            elif self.cloud.owned_guest(self.old_id):
                self.cloud.proxy('nova').unlock_server(self.old_id)
                self.cloud.proxy('nova').start_server(self.old_id)
                self.cloud.wait_server(self.old_id)

    def active(self):
        self.cloud.wait_server(self.server.compute_id)

    def register(self):
        result = self.driver.register_server(
            self.endpoint, self.stack, self.server,
            self.secret_value(self.stack.ssh_key_ref))
        if (result.engine_ref != self.server.engine_ref or
                self.driver.get_server_status(
                    self.endpoint, self.stack, self.server).status !=
                states.ACTIVE):
            raise exception.Conflict()

    def revert_register(self):
        # Registration belongs to the retained Coast server, not the VM.
        pass

    def services(self):
        deadline = time.monotonic() + self.conf.conductor.deploy_timeout
        for service in self.workloads:
            if time.monotonic() >= deadline:
                raise exception.Conflict()
            if service.status not in (states.ACTIVE, states.STOPPED):
                raise exception.Conflict()
            observed = self.driver.get_app_status(
                self.endpoint, self.stack, service).status
            if service.status == states.STOPPED:
                if observed != states.STOPPED:
                    self.driver.stop_app(self.endpoint, self.stack, service)
                    observed = self.driver.get_app_status(
                        self.endpoint, self.stack, service).status
                expected = states.STOPPED
            else:
                expected = states.ACTIVE
                if observed == states.STOPPED:
                    self.redeploy(service, deadline)
            while observed != expected:
                allowed = (states.DEPLOYING, states.STOPPED)
                if expected == states.STOPPED:
                    allowed += (states.ACTIVE,)
                if observed not in allowed:
                    raise exception.Conflict()
                self.pause(deadline)
                observed = self.driver.get_app_status(
                    self.endpoint, self.stack, service).status

    def pause(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise exception.Conflict()
        time.sleep(min(self.conf.controller_worker.build_poll_interval,
                       remaining))

    def redeploy(self, service, deadline):
        if time.monotonic() >= deadline:
            raise exception.Conflict()
        if getattr(service, 'kind', None) == 'database':
            self.driver.start_app(self.endpoint, self.stack, service)
            while True:
                observed = self.driver.get_database_status(
                    self.endpoint, self.stack, service).status
                if observed == states.ACTIVE:
                    return
                if observed not in (states.DEPLOYING, states.STOPPED):
                    raise exception.Conflict()
                self.pause(deadline)
        if not service.current_deployment_id:
            raise exception.Conflict()
        target = objects.Deployment.get_by_id(
            self.ctx, service.current_deployment_id)
        if target.status != states.ACTIVE:
            raise exception.Conflict()
        with db_api.writer.using(self.ctx):
            source_ref = target.resolved_ref or target.source_ref
            deployment = objects.Deployment(
                context=self.ctx, service_id=service.id,
                server_id=service.server_id, trigger='reconcile',
                status=states.PENDING, source_ref=source_ref,
                image_digest=target.image_digest,
                spec=copy.deepcopy(target.spec))
            deployment.create()
            deployments.prepare(self.owner, self.ctx, deployment, self.stack,
                                str(uuid.uuid5(uuid.UUID(deployment.id),
                                               'failover')))
        self.new_deployments.append(deployment)
        self.flush()
        variables = {key: base.EnvVar(
            value=secret_store.call('get', self.ctx, item['value_ref']),
            is_secret=item['secret'], build_time=item['build_time'])
            for key, item in target.spec.get('env', {}).items()}
        self.driver.set_env(self.endpoint, self.stack, service, variables)
        result = self.driver.deploy_app(
            self.endpoint, self.stack, service, deployment)
        if not result.engine_ref:
            raise exception.Conflict()
        self.save(deployment, engine_ref=result.engine_ref, fault=None)
        while True:
            observed = self.driver.get_deployment_status(
                self.endpoint, self.stack, service, deployment)
            if observed.status == states.ACTIVE:
                break
            if observed.status not in (states.PENDING, states.BUILDING,
                                       states.DEPLOYING):
                raise exception.Conflict()
            self.pause(deadline)
        with db_api.writer.using(self.ctx):
            self.save(deployment, status=states.ACTIVE,
                      resolved_ref=observed.resolved_ref,
                      finished_at=timeutils.utcnow())
            self.save(target, status=states.SUPERSEDED)
            self.save(service, current_deployment_id=deployment.id)
            self.owner._event(self.ctx, deployment, self.stack, 'create',
                              'success', str(uuid.uuid5(
                                  uuid.UUID(deployment.id), 'failover')))
        self.flush()

    def retire(self):
        self.cloud.delete_guest(self.old_id)
        self.retired = True
        self.save(objects.Event.get_by_id(self.ctx, self.old_receipt),
                  outcome='success')

    def finish(self, success):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            self.server = objects.Server.get_by_id(self.ctx, self.server.id)
            self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            for item in self.new_deployments:
                deployment = objects.Deployment.get_by_id(self.ctx, item.id)
                if deployment.status == states.DEPLOYING:
                    self.save(deployment, status=states.FAILED,
                              fault=deployments.UNKNOWN,
                              finished_at=timeutils.utcnow())
                    self.owner._event(self.ctx, deployment, self.stack,
                                      'create', 'failure', str(uuid.uuid5(
                                          uuid.UUID(deployment.id),
                                          'failover')))
            self.save(self.server, status=states.ACTIVE if success else
                      states.ERROR, fault=None if success else FAULT)
            if success:
                self.save(self.server, image_id=self.target_image)
                if self.target_flavor:
                    self.save(self.server, flavor_id=self.target_flavor)
            self.server.release_task(self.task_state)
            if not self.upgrade:
                for service in self.locked_services:
                    service = objects.Service.get_by_id(self.ctx, service.id)
                    service.release_task(states.FAILING_OVER)
                if not self.target_flavor:
                    self.stack.release_task(states.FAILING_OVER)
            self.succeeded = success
            self.owner._event(self.ctx, self.server, self.stack, 'failover',
                              'success' if success else 'failure',
                              self.receipt)
        self.flush()


def interrupted(owner, ctx, stack):
    """Fail closed on same-host restart; never enqueue or move resources."""
    from coast.conductor import manager
    # Startup recovery must emit only after the surrounding DB commit.
    owner = FailoverServer(owner).owner
    for service in manager.rows(objects.ServiceList, ctx, {
            'stack_id': stack.id, 'task_state': states.FAILING_OVER}):
        service.release_task(states.FAILING_OVER)
    for server in manager.rows(objects.ServerList, ctx, {
            'stack_id': stack.id, 'task_state': states.FAILING_OVER}):
        if server.role in ('worker', 'build'):
            fault = FAULT
        elif server.role == 'control' and server.status == states.FAILOVER:
            # An ACTIVE controller with a stray task is not this attempt.
            fault = CONTROLLER_FAULT
        else:
            continue
        owner._save(server, status=states.ERROR, fault=fault)
        server.release_task(states.FAILING_OVER)
        starts = objects.EventList.get_all(ctx, filters={
            'resource_id': server.id, 'action': 'failover',
            'outcome': 'start'},
            sort_key='created_at', sort_dir='desc', limit=1)
        owner._event(ctx, server, stack, 'failover', 'failure',
                     starts[0].id if starts else None)
        for deployment in manager.rows(objects.DeploymentList, ctx, {
                'server_id': server.id, 'trigger': 'reconcile',
                'status': states.DEPLOYING}):
            receipt = str(uuid.uuid5(uuid.UUID(deployment.id), 'failover'))
            if not objects.EventList.get_all(
                    ctx, filters={'id': receipt}, limit=1):
                continue
            owner._save(deployment, status=states.FAILED,
                        fault=deployments.UNKNOWN,
                        finished_at=timeutils.utcnow())
            owner._event(ctx, deployment, stack, 'create', 'failure', receipt)
    for action in ('resize', 'resize_workers'):
        starts = objects.EventList.get_all(ctx, filters={
            'stack_id': stack.id, 'action': action, 'outcome': 'start',
            'resource_type': 'environment' if action == 'resize_workers'
            else 'server'})
        for event in starts:
            resource = (stack if action == 'resize_workers' else
                        objects.Server.get_by_id(ctx, event.resource_id))
            owner._event(ctx, resource, stack, action, 'failure', event.id)
    return owner._pending
