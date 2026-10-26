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

"""Bounded settings reconciliation, including database runtime recreation."""

from collections import OrderedDict
import random
import time

from oslo_log import log as logging
from oslo_utils import timeutils

from coast.common import context
from coast.common import exception
from coast.common import notifications
from coast.common import safe_log
from coast.common import states
from coast.conductor import database_faults
from coast.conductor import deployments
from coast.conductor import runtime_logs
from coast.db import api as db_api
from coast.network import stack as network_stack
from coast import objects


LOG = logging.getLogger(__name__)
LIMIT = 100
BUDGET = 30


class Reconciler:
    def __init__(self, manager):
        self.manager = manager
        self.marker = None
        self.due = 0
        self.retries = OrderedDict()

    def sweep(self):
        now = time.monotonic()
        if now < self.due:
            return
        admin = context.RequestContext(is_admin=True)
        try:
            batch = objects.ServiceList.get_all(
                admin, marker=self.marker,
                limit=LIMIT, sort_key='id', all_projects=True)
        except exception.NotFound:
            # A deleted cursor is not a reason to get stuck on that page.
            self.marker = None
            return
        interval = self.manager.conf.conductor.service_reconcile_interval
        for listed in batch:
            if time.monotonic() - now >= BUDGET:
                return
            self.marker = listed.id
            due, failures = self.retries.get(listed.id, (0, 0))
            if time.monotonic() < due:
                continue
            try:
                failed = self.run(listed)
            except Exception:
                LOG.warning('Service reconcile pass failed.')
                failed = True
            if failed:
                delay = min(interval * 2 ** min(failures, 6), 3600)
                self.retries[listed.id] = (
                    time.monotonic() + delay + random.uniform(0, delay / 10),
                    failures + 1)
                self.retries.move_to_end(listed.id)
                if len(self.retries) > LIMIT:
                    self.retries.popitem(last=False)
            else:
                self.retries.pop(listed.id, None)
        if len(batch) < LIMIT:
            self.marker = None
            self.due = time.monotonic() + interval + random.uniform(
                0, interval / 10)

    def event(self, ctx, service, stack, phase, message):
        resource = ('database' if getattr(service, 'kind', None) == 'database'
                    else 'service')
        objects.Event(context=ctx, stack_id=stack.id,
                      resource_type=resource, resource_id=service.id,
                      action='reconcile', outcome={
                          'start': 'start', 'end': 'success',
                          'error': 'failure'}[phase], message=message,
                      request_id=ctx.request_id).create()
        notifications.emit(ctx, notifications.CONDUCTOR, resource,
                           'reconcile', phase,
                           notifications.payload_for(service, ctx),
                           self.manager.conf, notifier=self.manager.notifier)

    def capture_logs(self, ctx, service, observed, driver, ep, stack):
        """Read runtime output without holding the service task."""
        deployment, lines = None, []
        database = service.kind == 'database'
        if (observed in (states.DEPLOYING, states.ERROR) or
                (database and observed == states.STOPPED and
                 service.status != states.STOPPED)):
            # TODO(Q105): exited logs need a new worker endpoint and image.
            try:
                deployment = runtime_logs.latest(ctx, service.id)
                if deployment or database:
                    lines = runtime_logs.capture(
                        driver, ep, stack, service,
                        masks=self.manager._log_masks(ctx, service))
            except Exception:
                LOG.debug('Runtime output unavailable during reconcile.')
        return deployment, lines

    def persist_logs(self, deployment, lines):
        if deployment and lines:
            try:
                runtime_logs.persist(deployment, lines)
            except Exception:
                LOG.debug('Runtime output unavailable during reconcile.')

    def update_status(self, ctx, service, observed, driver, ep, stack):
        """Confirm runtime failures and recovery; keep desired settings."""
        if service.status == states.ERROR and not service.reconcile_fault:
            # Deployment-owned errors get capture, but no status recovery.
            return
        now = timeutils.utcnow()
        crash = timeutils.normalize_time(service.unhealthy_since) if (
            service.unhealthy_since) else None
        running = timeutils.normalize_time(service.running_since) if (
            service.running_since) else None
        new = service.status
        fault = service.reconcile_fault
        if observed == states.ACTIVE:
            running = now if running is None else running
            if (now - running).total_seconds() >= (
                    self.manager.conf.conductor.stable_seconds):
                crash = running = None
                new, fault = states.ACTIVE, None
        else:
            running = None
            if observed == states.ERROR:
                new = states.ERROR
                fault = 'App container failed; check the runtime logs.'
            elif observed == states.STOPPED:
                new, fault = states.STOPPED, None
            elif observed == states.DEPLOYING:
                crash = now if crash is None else crash
                window = self.manager.conf.conductor.deploy_unreachable_timeout
                if self.failed_crash_loop(ctx, service) or (
                        window and (now - crash).total_seconds() >= window):
                    new = states.ERROR
                    fault = ('App container keeps restarting; check the last '
                             'deployment and runtime logs.')
            else:
                # An unknown read cannot confirm continuous recovery.
                crash = None
        service.unhealthy_since = crash
        service.running_since = running
        if new != service.status or fault != service.reconcile_fault:
            self.event(ctx, service, stack, 'start', None)
            service.status, service.reconcile_fault = new, fault
            if service.kind == 'database':
                service.fault = (database_faults.public_fault()
                                 if new == states.ERROR else None)
            service.save()
            self.event(ctx, service, stack,
                       'error' if new == states.ERROR else 'end', fault)
        elif service.obj_what_changed():
            service.save()

    def sync_database_firewall(self, ctx, stack, service, previous):
        if service.status != previous.status:
            self.manager.network_reconciler.defer_cleanup(ctx, stack, service)

    def failed_crash_loop(self, ctx, service):
        latest = runtime_logs.latest(ctx, service.id)
        if not latest or latest.status != states.FAILED:
            return False
        # Match the conductor's fixed fault prefix, never runtime output.
        if not (latest.fault or '').startswith((
                'The rollout finished but the app container keeps restarting.',
                'The rollout finished but the app container keeps exited.')):
            return False
        if not service.current_deployment_id:
            return True
        current = objects.Deployment.get_by_id(
            ctx, service.current_deployment_id)
        return latest.number > current.number

    def reset_confirmation(self, service):
        service._from_db(db_api.services.update(
            service._context, service.id,
            {'unhealthy_since': None, 'running_since': None}))

    def claim(self, ctx, service, stack):
        with db_api._write(ctx):
            db_api._lock_project(ctx.session, ctx.project_id)
            current = objects.Service.obj_from_db(
                ctx, db_api.services._get(ctx, service.id, lock=True))
            fresh_stack = objects.Stack.obj_from_db(
                ctx, db_api.stacks._get(ctx, stack.id, lock=True))
            if (current.updated_at != service.updated_at or
                    current.task_state != states.NONE or
                    fresh_stack.updated_at != stack.updated_at or
                    fresh_stack.task_state != states.NONE):
                return False
            try:
                service.acquire_task(states.RECONCILING, owner='settings_sync')
            except exception.Conflict:
                return False
        return True

    def prepare_mounts(self, ctx, driver, ep, stack, service,
                       deployment_error):
        if deployment_error:
            return lambda: None
        return driver.prepare_volume_mounts(
            ep, stack, service, self.manager._mounts(ctx, service)) or (
                lambda: None)

    def run(self, listed):
        ctx = context.RequestContext(project_id=listed.project_id)
        manager = self.manager
        with db_api._write(ctx):
            db_api._lock_project(ctx.session, ctx.project_id)
            stack = objects.Stack.obj_from_db(
                ctx, db_api.stacks._get(ctx, listed.stack_id, lock=True))
            service = objects.Service.get_by_id(ctx, listed.id)
            if (stack.status != states.ACTIVE or
                    stack.task_state != states.NONE or
                    service.stack_id != stack.id or
                    service.status not in (
                        (states.ACTIVE, states.ERROR, states.STOPPED)
                        if service.kind == 'database' else
                        (states.ACTIVE, states.ERROR)) or
                    not service.engine_ref or
                    service.task_state != states.NONE or
                    not service.environment_id):
                # Busy services can belong to another conductor.
                if service.task_state == states.NONE:
                    self.reset_confirmation(service)
                return False
            environment = objects.Environment.get_by_id(
                ctx, service.environment_id)
            appgroup = objects.Appgroup.get_by_id(
                ctx, environment.appgroup_id)
            if appgroup.stack_id != stack.id:
                return False
        started = False
        claimed = False
        try:
            stack = objects.Stack.get_by_id(ctx, stack.id)
            if (stack.status != states.ACTIVE or
                    stack.task_state != states.NONE):
                return False
            driver = manager._driver(stack)
            ep = manager._endpoint(stack)
            deployment_error = (service.status == states.ERROR and
                                not service.reconcile_fault)
            if getattr(service, 'kind', None) == 'database':
                observed = driver.get_database_status(ep, stack, service)
                deployment, lines = self.capture_logs(
                    ctx, service, observed.status, driver, ep, stack)
                if not self.claim(ctx, service, stack):
                    return False
                claimed = True
                restarting = driver.update_database(ep, stack, service)
                with manager._service_write(ctx, service):
                    self.persist_logs(deployment, lines)
                    if restarting:
                        manager._save(service, settings_changed_at=None)
                    if restarting:
                        deployments.checkpoint_database_start(ctx, service)
                        self.event(ctx, service, stack, 'start', None)
                        manager._save(service, status=states.STARTING,
                                      fault=None)
                        self.event(ctx, service, stack, 'end', None)
                    else:
                        self.update_status(
                            ctx, service, observed.status, driver, ep, stack)
                self.sync_database_firewall(ctx, stack, service, listed)
                return False
            variables = manager._variables(ctx, service)
            fqdns = manager._fqdns(ctx, service)
            drift = ({} if deployment_error else driver.compare_settings(
                ep, stack, service, variables, fqdns))
            observed = driver.get_app_status(ep, stack, service)
            apply_mounts = self.prepare_mounts(
                ctx, driver, ep, stack, service, deployment_error)
            deployment, lines = self.capture_logs(
                ctx, service, observed.status, driver, ep, stack)
            if not self.claim(ctx, service, stack):
                return False
            claimed = True
            self.persist_observation(
                ctx, service, stack, driver, ep, deployment, lines, observed)
            if not self.apply_settings(
                    ctx, service, stack, driver, ep, apply_mounts, drift,
                    observed, variables, fqdns, deployment_error):
                return False
            started = True
            if any(value is True for value in drift.values()):
                drift = driver.compare_settings(ep, stack, service,
                                                variables, fqdns)
            if observed.status != states.ACTIVE:
                message = ('Expected running; engine reports ' +
                           (observed.status if observed.status in (
                               states.STOPPED, states.ERROR, states.DEPLOYING)
                            else 'unknown') + '.')
            elif drift != {'env': False, 'domains': False}:
                message = 'Service settings drift or unknown observation.'
            else:
                message = None
            self.event(ctx, service, stack,
                       'error' if message else 'end', message)
            return bool(message)
        except Exception:
            if not claimed:
                claimed = self.claim(ctx, service, stack)
                if not claimed:
                    return False
            try:
                with db_api.writer.using(ctx) as session:
                    db_api._lock_project(session, ctx.project_id)
                    self.record_failure(ctx, service, stack, started)
                return True
            except exception.Conflict:
                return False
        finally:
            self.release(ctx, service, claimed)

    def record_failure(self, ctx, service, stack, started):
        with self.manager._service_write(ctx, service):
            self.reset_confirmation(service)
            if not started:
                self.event(ctx, service, stack, 'start', None)
            self.event(ctx, service, stack, 'error',
                       'Service reconcile failed; settings may be partial.')

    def persist_observation(self, ctx, service, stack, driver, ep,
                            deployment, lines, observed):
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            with self.manager._service_write(ctx, service):
                self.persist_logs(deployment, lines)
                self.update_status(
                    ctx, service, observed.status, driver, ep, stack)

    def apply_settings(self, ctx, service, stack, driver, ep, apply_mounts,
                       drift, observed, variables, fqdns, deployment_error):
        started = (not deployment_error and (
            drift != {'env': False, 'domains': False} or
            observed.status != states.ACTIVE))
        if started:
            self.event(ctx, service, stack, 'start', None)
        with self.manager._service_write(ctx, service):
            try:
                driver.disable_builtin_terminals(ep, stack)
            except Exception as exc:
                safe_log.log_failure(
                    LOG,
                    'Built-in terminal disable failed for %(stack)s: '
                    '%(cause)s at %(where)s',
                    exc, stack=stack.id)
            if deployment_error:
                return False
            apply_mounts()
            if drift.get('env') is True:
                driver.set_env(ep, stack, service, variables)
            if drift.get('domains') is True:
                driver.set_domains(ep, stack, service, fqdns)
        return started

    def release(self, ctx, service, claimed):
        if claimed:
            try:
                with self.manager._service_write(ctx, service):
                    service.release_task(states.RECONCILING)
            except exception.Conflict:
                pass


class NetworkReconciler:
    """Bounded periodic security group repair, outside service reconcile.

    It takes no stack task: the repair is idempotent and creates rules
    before deleting stale ones, so a crash or restart cannot strand a
    stack in a busy state. Each sweep and each stack has a time budget.
    """

    def __init__(self, manager):
        self.manager = manager
        self.marker = None
        self.cleanup_marker = None

    def defer_cleanup(self, ctx, stack, database):
        """Checkpoint cleanup before Neutron calls can fail."""
        pending = objects.EventList.get_all(ctx, filters={
            'resource_type': 'database', 'resource_id': database.id,
            'action': 'firewall_cleanup', 'outcome': 'start'}, limit=1)
        if pending:
            checkpoint = pending[0]
        else:
            checkpoint = objects.Event(
                context=ctx, stack_id=stack.id, resource_type='database',
                resource_id=database.id, action='firewall_cleanup',
                outcome='start', request_id=ctx.request_id)
            checkpoint.create()
        try:
            self.apply(ctx, stack)
        except Exception:
            # Preserve failure bookkeeping and task release. The durable
            # checkpoint is retried by the always-enabled settle timer.
            LOG.warning('Database firewall cleanup pending for %s',
                        database.id)
        else:
            checkpoint.outcome = 'success'
            checkpoint.save()

    def retry_cleanup(self):
        """Recover durable cleanup work independently of optional SG repair."""
        admin = context.RequestContext(is_admin=True)
        deadline = time.monotonic() + BUDGET
        limit = min(LIMIT, self.manager.conf.api.max_limit)
        try:
            batch = objects.EventList.get_all(
                admin, filters={'resource_type': 'database',
                                'action': 'firewall_cleanup',
                                'outcome': 'start'},
                marker=self.cleanup_marker, limit=limit, sort_key='id',
                all_projects=True)
        except exception.NotFound:
            self.cleanup_marker = None
            return
        for listed in batch:
            if time.monotonic() >= deadline:
                return
            self.cleanup_marker = listed.id
            ctx = context.RequestContext(project_id=listed.project_id)
            try:
                with db_api.writer.using(ctx) as session:
                    # Serialize with create/delete and re-read desired state;
                    # a delayed cleanup must never close a retried database.
                    db_api._lock_project(session, ctx.project_id)
                    checkpoint = objects.Event.obj_from_db(
                        ctx, db_api.events._get(ctx, listed.id, lock=True))
                    if checkpoint.outcome != 'start':
                        continue
                    stack = objects.Stack.get_by_id(ctx, checkpoint.stack_id)
                    if stack.task_state != states.NONE:
                        continue
                    self.apply(ctx, stack, deadline=deadline)
                    checkpoint.outcome = 'success'
                    checkpoint.save()
            except Exception:
                LOG.warning('Database firewall cleanup retry pending for %s',
                            listed.resource_id)
        if len(batch) < limit:
            self.cleanup_marker = None

    def sweep(self):
        options = self.manager.conf.controller_worker
        start = time.monotonic()
        budget = options.network_repair_timeout
        admin = context.RequestContext(is_admin=True)
        try:
            batch = objects.StackList.get_all(
                admin, marker=self.marker, limit=LIMIT,
                sort_key='id', all_projects=True)
        except exception.NotFound:
            self.marker = None
            return
        for listed in batch:
            if time.monotonic() - start >= budget:
                return
            self.marker = listed.id
            try:
                self.run(listed, deadline=start + budget)
            except Exception as exc:
                cause, where = safe_log.describe(exc)
                LOG.warning('Stack security group reconcile failed for '
                            '%(stack)s: %(cause)s at %(where)s',
                            {'stack': listed.id, 'cause': cause,
                             'where': where})
        if len(batch) < LIMIT:
            self.marker = None

    def run(self, listed, deadline=None):
        ctx = context.RequestContext(project_id=listed.project_id)
        stack = objects.Stack.get_by_id(ctx, listed.id)
        # Skip stacks another task is changing; the next sweep retries.
        if (stack.driver == 'noop' or
                stack.status not in (states.ACTIVE, states.DEGRADED) or
                stack.task_state != states.NONE):
            return
        self.apply(ctx, stack, deadline)

    def apply(self, ctx, stack, deadline=None):
        """Apply firewall desired state during a conductor operation too."""
        if stack.driver == 'noop':
            return
        servers = list(objects.ServerList.get_all(
            ctx, filters={'stack_id': stack.id}))
        controllers = [s for s in servers if s.role == 'control']
        if len(controllers) != 1 or not controllers[0].private_ip:
            raise ValueError('Stack controller address unavailable')
        # Match server add/remove: a build row counts only after Nova
        # returns an instance id. An unbooted row must not open registry.
        has_build_servers = any(
            server.role == 'build' and server.compute_id
            for server in servers)
        network_stack.EnvironmentNetwork(self.manager.conf).reconcile(
            ctx.project_id, stack.id, controllers[0].private_ip,
            has_build_servers, deadline=deadline, ctx=ctx)
