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

"""Durable enqueue checkpoints and reference-only deployment recovery."""

from collections import deque
import time
from time import sleep

from oslo_utils import strutils
from oslo_utils import timeutils

from coast.common import context
from coast.common import exception
from coast.common import safe_log
from coast.common import states
from coast.conductor import database_faults
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.conductor import runtime_logs
from coast.db import api as db_api
from coast.drivers import base
from coast import objects


ENV_SYNC_FAILED = ('Environment variable sync failed; deployment was not '
                   'started. Check the engine connection and variable '
                   'settings.')
COMPOSE_SYNC_FAILED = ('Compose sync failed; deployment was not started. '
                       'Check the saved YAML and engine connection.')
DOCKERFILE_CHANGE_UNSUPPORTED = (
    "This app can't use a changed Dockerfile. "
    "Create a new app from the new Dockerfile, "
    "or deploy from a Git repository.")
ENQUEUE = 'Deployment enqueue is in progress.'
NO_ANSWER = ('App is running but nothing answered on port %s. '
             'Check the port setting.')
# Deployment id -> first unanswered HTTP check after engine ACTIVE.
PROXY_ERRORS_SINCE = {}
CRASH_SINCE = {}
RUNNING_SINCE = {}
RUNTIME_LOGS = {}
CRASH_LOOP = ('The rollout finished but the app container keeps %s. It '
              'probably exits right after start; check its runtime logs '
              'and environment variables.')
UNKNOWN = ('Engine deployment outcome is unknown; '
           'the operator must check the engine before another deployment.')
TASKS = (states.DEPLOYING, states.ROLLING_BACK, states.PROVISIONING)


class EnvSyncFailed(base.DriverError):
    """Environment sync failed before engine deployment work started."""


def enqueue_fault(error, fallback):
    if isinstance(error, base.DockerfileChangeUnsupported):
        return DOCKERFILE_CHANGE_UNSUPPORTED
    if isinstance(error, base.EngineRateLimited):
        safe_log.log_failure(manager.LOG, 'Engine enqueue rate limited: '
                             '%(cause)s at %(where)s', error)
        return manager.RATE_LIMIT_FAULT
    return fallback


def stable(key, running, seconds):
    """A failed observation resets the continuous running window."""
    if not running or not seconds:
        RUNNING_SINCE.pop(key, None)
        return running
    now = time.monotonic()
    first = RUNNING_SINCE.setdefault(key, now)
    return now - first >= seconds


def health_spec(service, deployment):
    """Use the enqueued settings while later edits wait for another deploy."""
    service = service.obj_clone()
    spec = deployment.spec or {}
    service.health_check = spec.get('health_check', 'http')
    service.health_check_port = spec.get('health_check_port')
    service.healthcheck_path = spec.get('healthcheck_path')
    service.ports = spec.get('ports', service.ports)
    return service


def prepare(owner, ctx, deployment, env, receipt, show=False):
    """Called inside the claim transaction, before any enqueue request."""
    # Match whole-second MySQL/MariaDB storage for the ownership fence.
    owner._save(deployment, status=states.DEPLOYING, fault=ENQUEUE,
                started_at=timeutils.utcnow().replace(microsecond=0))
    owner._event(ctx, deployment, env, 'create', 'start', receipt)
    # show: user deployments expose DEPLOYING on the service. Internal
    # failover/upgrade redeploys keep the service status they verify.
    service = objects.Service.get_by_id(ctx, deployment.service_id)
    if show and service.status != states.DEPLOYING:
        owner._save(service, status=states.DEPLOYING)
    return deployment.obj_clone()


class Recovery:
    def __init__(self, owner):
        # RPC and timer threads must not share a notification buffer.
        self.owner = manager.Manager(owner.conf, owner.cloud, owner.notifier)
        self.owner.drivers = owner.drivers
        self.owner._pending = []
        self.owner._seen = set()

    def flush(self):
        pending = self.owner._pending
        self.owner._pending = []
        self.owner._seen = set()
        self.owner._flush(pending)

    def locked(self, ctx, deployment_id):
        snapshot = objects.Deployment.get_by_id(ctx, deployment_id)
        service = self.owner._locked_service(ctx, snapshot.service_id)
        deployment = objects.Deployment.obj_from_db(
            ctx, db_api.deployments._get(ctx, deployment_id, lock=True))
        env = objects.Stack.get_by_id(ctx, service.stack_id)
        # A current locking read matters after waiting for the service row:
        # repeatable-read snapshots may predate a newer deployment claim.
        latest = (db_api.deployments._query(ctx).filter_by(
            service_id=service.id).order_by(
                deployment.repository.model.number.desc()).limit(1).
            populate_existing().with_for_update().all())
        # Q145: a deployment locks only its service, never the stack.
        if (not latest or latest[0].id != deployment.id or
                service.task_state not in TASKS):
            return None
        db_api.cas_task_state(ctx, service.repository.model, service.id,
                              expected=service.task_state,
                              new=service.task_state)
        return deployment, service, env

    def execute(self, ctx, operation, resource, revision=None,
                task_claim=None):
        if ctx.project_id != resource.project_id:
            raise exception.NotFound()
        receipt = rpcapi.receipt_id(operation, resource, revision,
                                    task_claim=task_claim)

        @db_api.retry_transaction
        def prepare(context):
            return self._prepare(context, resource, receipt, task_claim)

        try:
            claimed = prepare(ctx)
        except Exception as error:
            safe_log.log_failure(
                manager.LOG,
                'Conductor %(operation)s for app %(resource)s failed '
                'request_id=%(request_id)s: %(cause)s at %(where)s',
                error, operation=operation, resource=resource.service_id,
                request_id=ctx.request_id)
            self.owner._pending = []
            self.owner._seen = set()
            self.owner._failed_operation(
                ctx, operation, resource, 'deployment',
                operation.partition('_')[0], {}, receipt, task_claim)
            self.flush()
            raise
        self.flush()
        if claimed:
            self.enqueue(ctx, claimed)
        else:
            self.reconcile(ctx, resource.id)

    def _prepare(self, ctx, resource, receipt, task_claim):
        self.owner._pending = []
        self.owner._seen = set()
        claimed = None
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            if (task_claim is not None and self.owner._claim_lock(
                    ctx, resource, 'deployment', task_claim) is None):
                return
            deployment = objects.Deployment.obj_from_db(
                ctx, db_api.deployments._get(ctx, resource.id, lock=True))
            service = self.owner._locked_service(ctx, deployment.service_id)
            env = self.owner._stack(ctx, deployment)
            expected = (states.ROLLING_BACK if deployment.rollback_of
                        else states.DEPLOYING)
            if (deployment.status == states.PENDING and
                    not deployment.started_at and
                    service.task_state == expected):
                db_api.cas_task_state(ctx, service.repository.model,
                                      service.id,
                                      expected=expected, new=expected)
                if (env.status != states.ACTIVE or
                        env.task_state not in manager.SHARED_STACK_TASKS):
                    # This request has not enqueued. Do not release another
                    # operation's stack lock or leave this service stuck.
                    self.owner._save(deployment, status=states.FAILED,
                                     fault=manager.FAULT,
                                     finished_at=timeutils.utcnow())
                    service.release_task(expected)
                    self.owner._event(ctx, deployment, env, 'create',
                                      'failure', receipt)
                else:
                    claimed = prepare(self.owner, ctx, deployment, env,
                                      receipt, show=True)
        return claimed

    def enqueue(self, ctx, checkpoint):
        # Keep the service row lock from claim validation through every
        # engine write and reference persistence. Age is not proof of runner
        # death: failover must wait for a paused live enqueue to finish. A
        # crashed runner releases these database locks automatically.
        with db_api.writer.using(ctx):
            outcome = self._enqueue_locked(ctx, checkpoint)
        if outcome is None:
            return
        enqueued, fault = outcome
        # Observations and notifications must run after the fence commits.
        if enqueued:
            self.reconcile(ctx, checkpoint.id)
        else:
            self.finish_unknown(ctx, checkpoint.id, checkpoint.started_at,
                                fault=fault)

    def _enqueue_locked(self, ctx, checkpoint):
        with db_api.writer.using(ctx):
            held = self.locked(ctx, checkpoint.id)
            if not held:
                return
            current, service, env = held
            if (current.started_at != checkpoint.started_at or
                    current.engine_ref or current.fault != ENQUEUE):
                return
            abandon = (current.status == states.CANCELLED or
                       self.expired(current))
        if abandon:
            return False, UNKNOWN
        sync_fault = ENV_SYNC_FAILED
        try:
            driver = self.owner._driver(env)
            endpoint = self.owner._endpoint(env)
            sync_fault = COMPOSE_SYNC_FAILED
            self.owner._push_compose(
                ctx, driver, endpoint, env, service, checkpoint)
            sync_fault = ENV_SYNC_FAILED
            driver.set_env(endpoint, env, service,
                           self.owner._variables(ctx, service))
        except Exception as exc:
            return False, enqueue_fault(exc, sync_fault)
        try:
            driver.set_volume_mounts(
                endpoint, env, service, self.owner._mounts(ctx, service))
        except Exception as exc:
            return False, enqueue_fault(
                exc, 'Container storage settings could not be applied.')
        try:
            self.owner._apply_file_mounts(ctx, endpoint, env, driver, service)
        except Exception as exc:
            return False, enqueue_fault(
                exc, 'Container file settings could not be applied.')
        try:
            if checkpoint.rollback_of:
                target = objects.Deployment.get_by_id(
                    ctx, checkpoint.rollback_of)
                if target.resolved_ref:
                    target.source_ref = target.resolved_ref
                result = driver.rollback(
                    endpoint, env, service, target, checkpoint)
            else:
                result = driver.deploy_app(endpoint, env, service, checkpoint)
            if not result.engine_ref:
                raise base.DriverError('Deployment reference is missing')
        except Exception as exc:
            # Driver exceptions can describe failures after the request was
            # sent. Never repeat an uncertain POST outcome or persist text.
            return False, enqueue_fault(exc, UNKNOWN)
        with db_api.writer.using(ctx):
            held = self.locked(ctx, checkpoint.id)
            if not held:
                return
            deployment, service, env = held
            if (deployment.started_at != checkpoint.started_at or
                    deployment.status not in (states.DEPLOYING,
                                              states.CANCELLED)):
                return
            # These scalar fields do not change any parent relationship.
            # Generic repository saves also lock FK parents (server/stack),
            # reversing admission's lock order while we hold the service.
            row = db_api.deployments._get(ctx, deployment.id, lock=True)
            row.engine_ref = result.engine_ref
            row.resolved_ref = result.extra.get('resolved_ref')
            row.fault = None
            ctx.session.flush()
        return True, None

    def cancel(self, ctx, deployment_id):
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            deployment = objects.Deployment.get_by_id(ctx, deployment_id)
            service = self.owner._locked_service(ctx, deployment.service_id)
            # No checkpoint means this conductor has not sent an enqueue.
            # Initial service setup still owns PROVISIONING and its service
            # quota.
            if (deployment.status == states.CANCELLED and
                    deployment.started_at is None and
                    service.task_state in (
                        states.DEPLOYING, states.ROLLING_BACK)):
                held = self.locked(ctx, deployment_id)
                if held:
                    self.finish(ctx, *held, states.CANCELLED, None)
        self.flush()
        self.reconcile(ctx, deployment_id)

    def probe(self, ctx, deployment, service, engine_ready=True):
        """Gate engine ACTIVE on HTTP readiness; return (terminal, fault)."""
        if service.health_check == 'none':
            return states.ACTIVE, None
        try:
            code = self.owner._probe(ctx, service) if engine_ready else 0
        except Exception:
            code = 0
        if code is None or 200 <= code < 500:
            PROXY_ERRORS_SINCE.pop(deployment.id, None)
            return states.ACTIVE, None
        if self.unreachable(deployment, code):
            return states.FAILED, NO_ANSWER % (service.health_check_port or
                                               service.ports or 'the app port')
        return states.DEPLOYING, None  # Up, but not routed yet.

    def unreachable(self, deployment, code):
        """True once HTTP kept failing with no answer or 502/503.

        The engine already reports the app running, so a 502/503 from the
        proxy means nothing answers on the configured port. The clock is per
        conductor process; a restart only restarts the window, and
        deploy_timeout remains the backstop.
        """
        limit = self.owner.conf.conductor.deploy_unreachable_timeout
        if code not in (0, 502, 503) or not limit:
            PROXY_ERRORS_SINCE.pop(deployment.id, None)
            return False
        first = PROXY_ERRORS_SINCE.setdefault(deployment.id, time.monotonic())
        return time.monotonic() - first >= limit

    def confirm_stable(self, service, env):
        """Sample the container a few times before calling it stable.

        The settle poll runs every settle_poll_interval seconds, so a
        container that crashes and restarts within that interval can look
        running at every poll. Sample it quickly before ACTIVE.
        """
        samples = self.owner.conf.conductor.stable_samples
        if samples <= 1:
            return True
        try:
            driver = self.owner._driver(env)
            endpoint = self.owner._endpoint(env)
            for index in range(samples):
                if index:
                    sleep(self.owner.conf.conductor.stable_sample_interval)
                observed = driver.get_app_status(endpoint, env, service)
                if observed.status != states.ACTIVE:
                    return False
        except base.EngineRateLimited:
            raise
        except Exception:
            return False
        return True

    def crashing(self, deployment):
        """True once a finished rollout kept crash-looping for the window.

        Uses deploy_unreachable_timeout (per conductor process, like the
        proxy check); deploy_timeout stays the backstop.
        """
        limit = self.owner.conf.conductor.deploy_unreachable_timeout
        if not limit:
            return False
        first = CRASH_SINCE.setdefault(deployment.id, time.monotonic())
        return time.monotonic() - first >= limit

    def runtime_logs(self, deployment, service, env, attempts=10, delay=0.5):
        """Catch short running intervals and append only sanitized output."""
        try:
            lines = runtime_logs.capture(
                self.owner._driver(env), self.owner._endpoint(env), env,
                service, attempts=attempts, delay=delay, sleep=sleep,
                masks=self.owner._log_masks(deployment._context, service))
            if lines:
                runtime_logs.persist(deployment, lines)
                text = '\n'.join(line.line for line in deque(lines, maxlen=50))
                RUNTIME_LOGS[deployment.id] = strutils.mask_password(
                    text)[-6000:]
        except Exception:
            pass

    def expired(self, deployment):
        timeout = self.owner.conf.conductor.deploy_timeout
        return (deployment.started_at is not None and
                timeutils.is_older_than(deployment.started_at, timeout))

    def failed_service_status(self, service, env, crash_loop=False):
        """Keep ACTIVE only for a previous release verified outside a lock."""
        if crash_loop or not service.current_deployment_id:
            return states.ERROR
        try:
            driver = self.owner._driver(env)
            observed = driver.get_app_status(
                self.owner._endpoint(env), env, service)
        except Exception:
            return states.ERROR
        return (states.ACTIVE if observed.status == states.ACTIVE
                else states.ERROR)

    def finish_unknown(self, ctx, deployment_id, started_at=None,
                       fault=UNKNOWN):
        checkpoint = objects.Deployment.get_by_id(ctx, deployment_id)
        service = objects.Service.get_by_id(ctx, checkpoint.service_id)
        env = objects.Stack.get_by_id(ctx, service.stack_id)
        service_status = self.failed_service_status(service, env)
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            held = self.locked(ctx, deployment_id)
            if not held:
                return
            deployment, service, env = held
            if (deployment.engine_ref or deployment.fault != ENQUEUE or
                    deployment.status not in (states.DEPLOYING,
                                              states.CANCELLED) or
                    (started_at is not None and
                     deployment.started_at != started_at)):
                return
            self.finish(ctx, deployment, service, env, states.FAILED, fault,
                        service_status=service_status)
        self.flush()

    def readiness(self, ctx, deployment, service, env, observed):
        terminal = observed.status
        fault = None
        if (terminal in (states.DEPLOYING, states.ERROR, states.FAILED) or
                deployment.id in CRASH_SINCE):
            self.runtime_logs(deployment, service, env)
        if terminal == states.DEPLOYING and observed.container in (
                'restarting', 'exited'):
            stable(deployment.id, False, 0)
            if self.crashing(deployment):
                terminal = states.FAILED
                fault = CRASH_LOOP % (
                    'restarting' if observed.container == 'restarting'
                    else 'exiting')
                logs = RUNTIME_LOGS.get(deployment.id)
                if logs:
                    fault += '\n\nLast output:\n' + logs
        if (terminal == states.ACTIVE or
                terminal == states.DEPLOYING and
                observed.container == 'running'):
            ready = stable(
                deployment.id, observed.container in (None, 'running'),
                self.owner.conf.conductor.stable_seconds)
            if ready and not self.confirm_stable(service, env):
                # Up at two polls but not in between: a crash loop.
                stable(deployment.id, False, 0)
                ready = False
                if self.crashing(deployment):
                    terminal = states.FAILED
                    fault = CRASH_LOOP % 'restarting'
                    logs = RUNTIME_LOGS.get(deployment.id)
                    if logs:
                        fault += '\n\nLast output:\n' + logs
                    return terminal, fault
            if ready:
                terminal, fault = self.probe(
                    ctx, deployment, service,
                    engine_ready=observed.status == states.ACTIVE)
            else:
                terminal = states.DEPLOYING
        elif terminal != states.CANCELLED:
            stable(deployment.id, False, 0)
        return terminal, fault

    def cancel_engine(self, ctx, deployment, driver, endpoint):
        with db_api.writer.using(ctx):
            held = self.locked(ctx, deployment.id)
            if not held:
                return False
            current, service, env = held
            if (current.status != deployment.status or
                    current.started_at != deployment.started_at or
                    current.engine_ref != deployment.engine_ref):
                return False
            driver.cancel_deployment(endpoint, env, service, current)
        return True

    def observe(self, ctx, deployment, driver, endpoint, env, service):
        if deployment.status == states.CANCELLED:
            if not self.cancel_engine(ctx, deployment, driver, endpoint):
                raise exception.Conflict()
            return base.ObservedStatus(states.CANCELLED)
        return driver.get_deployment_status(endpoint, env, service, deployment)

    def reconcile(self, ctx, deployment_id):
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            held = self.locked(ctx, deployment_id)
            if not held:
                return
            deployment, service, env = held
            if deployment.status not in (states.DEPLOYING, states.CANCELLED):
                return
        if not deployment.engine_ref:
            if self.expired(deployment):
                self.finish_unknown(ctx, deployment_id)
            return
        service = objects.Service.get_by_id(ctx, deployment.service_id)
        env = objects.Stack.get_by_id(ctx, service.stack_id)
        rate_limit_error = None
        terminal, fault = states.FAILED, None
        service = health_spec(service, deployment)
        try:
            driver = self.owner._driver(env)
            endpoint = self.owner._endpoint(env)
            observed = self.observe(ctx, deployment, driver, endpoint,
                                    env, service)
            terminal, fault = self.readiness(
                ctx, deployment, service, env, observed)
        except base.EngineRateLimited as exc:
            stable(deployment.id, False, 0)
            if not self.expired(deployment):
                return
            rate_limit_error = exc
            observed = base.ObservedStatus(states.FAILED)
            fault = manager.RATE_LIMIT_FAULT
        except base.EngineUnreachable:
            stable(deployment.id, False, 0)
            if not self.expired(deployment):
                return
            observed = base.ObservedStatus(states.FAILED)
        except Exception:
            observed = base.ObservedStatus(states.FAILED)
        if terminal not in (states.ACTIVE, states.FAILED, states.CANCELLED):
            if not self.expired(deployment):
                return
            terminal = states.FAILED
        service_status = states.ERROR
        if terminal == states.FAILED:
            service_status = self.failed_service_status(
                service, env, crash_loop=(
                    deployment.id in CRASH_SINCE or
                    observed.container in ('restarting', 'exited')))
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            held = self.locked(ctx, deployment_id)
            if not held:
                return
            current, service, env = held
            if (current.status != deployment.status or
                    current.engine_ref != deployment.engine_ref or
                    current.started_at != deployment.started_at):
                return
            if current.status == states.DEPLOYING:
                resolved = observed.resolved_ref or current.resolved_ref
                self.owner._save(current, resolved_ref=resolved)
            if terminal == states.FAILED:
                fault = fault or manager.FAULT
            else:
                fault = None
            PROXY_ERRORS_SINCE.pop(current.id, None)
            CRASH_SINCE.pop(current.id, None)
            RUNNING_SINCE.pop(current.id, None)
            RUNTIME_LOGS.pop(current.id, None)
            self.finish(ctx, current, service, env, terminal, fault,
                        service_status=service_status)
        if rate_limit_error:
            safe_log.log_failure(manager.LOG, 'Engine polling rate limited: '
                                 '%(cause)s at %(where)s', rate_limit_error)
        self.flush()

    def finish(self, ctx, deployment, service, env, terminal, fault,
               service_status=states.ERROR):
        if terminal == states.ACTIVE:
            if service.current_deployment_id:
                previous = objects.Deployment.get_by_id(
                    ctx, service.current_deployment_id)
                follow = (states.ROLLED_BACK if deployment.rollback_of
                          else states.SUPERSEDED)
                if follow in states.TRANSITIONS['deployment'][previous.status]:
                    self.owner._save(previous, status=follow)
            self.owner._save(service, status=states.ACTIVE,
                             current_deployment_id=deployment.id)
        elif (terminal == states.FAILED and
              deployment.status != states.CANCELLED):
            states.check_transition('service', service.status, service_status)
            self.owner._save(service, status=service_status)
        elif service.status == states.DEPLOYING:
            # A cancelled deployment leaves the previous release
            # in place, if there was one.
            self.owner._save(service, status=(
                states.ACTIVE if service.current_deployment_id
                else states.STOPPED))
        # Cancellation is already committed by the API. It stays CANCELLED
        # even when no engine reference was returned.
        status = (states.CANCELLED if deployment.status == states.CANCELLED
                  else terminal)
        self.owner._save(deployment, status=status, fault=fault,
                         finished_at=timeutils.utcnow())
        service.release_task(service.task_state)
        receipts = objects.EventList.get_all(ctx, filters={
            'resource_type': 'deployment', 'resource_id': deployment.id,
            'outcome': 'start', 'action': 'create'}, limit=1)
        receipt = receipts[0].id if receipts else None
        outcome = 'success' if status == states.ACTIVE else 'failure'
        self.owner._event(ctx, deployment, env, 'create', outcome, receipt)
        if status == states.CANCELLED:
            self.owner._event(ctx, deployment, env, 'cancel', 'success')
        if fault and receipts:
            event = objects.Event.get_by_id(ctx, receipt)
            self.owner._save(event, message=fault[:1024])


def checkpoint_database_start(ctx, service, action='restart'):
    """Persist a new readiness window alongside the STARTING status write."""
    objects.Event(context=ctx, stack_id=service.stack_id,
                  resource_type='database', resource_id=service.id,
                  action=action, outcome='start',
                  request_id=ctx.request_id).create()
    RUNNING_SINCE.pop(('start', service.id), None)


def _start_request(ctx, service):
    """Return the time and action of the current readiness request.

    updated_at is not usable: any later write (an env save, a rename)
    bumps it and would keep a stuck start alive forever.
    """
    started = []
    actions = ('start', 'restart')
    if getattr(service, 'kind', None) == 'database':
        actions += ('create',)
    for action in actions:
        try:
            rows = objects.EventList.get_all(
                ctx, filters={'resource_id': service.id, 'action': action},
                sort_key='created_at', sort_dir='desc', limit=1)
        except Exception:
            rows = []
        started.extend((row.created_at, action) for row in rows)
    if started:
        return max(started, key=lambda item: item[0])
    return service.updated_at, 'start'


def _start_requested_at(ctx, service):
    return _start_request(ctx, service)[0]


def settle_starting(owner, ctx, service_id):
    """Move one STARTING service to ACTIVE or, after start_timeout, ERROR."""
    service = objects.Service.get_by_id(ctx, service_id)
    if service.status != states.STARTING or not service.engine_ref:
        return
    env = objects.Stack.get_by_id(ctx, service.stack_id)
    running = False
    failure = None
    try:
        driver = owner._driver(env)
        observed = driver.get_app_status(owner._endpoint(env), env, service)
        if service.kind == 'database':
            owner.service_reconciler.capture_logs(
                ctx, service, observed.status, driver, owner._endpoint(env),
                env)
        running = stable(('start', service.id),
                         observed.status == states.ACTIVE,
                         owner.conf.conductor.stable_seconds)
        if running and service.health_check == 'http':
            running = owner._serving(ctx, service)
    except Exception as error:
        failure = error
        stable(('start', service.id), False, 0)
        running = False
    requested_at, action = _start_request(ctx, service)
    expired = timeutils.is_older_than(
        requested_at, owner.conf.conductor.start_timeout)
    if not running and not expired:
        return
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        current = objects.Service.get_by_id(ctx, service_id)
        # Short lock: re-check, then one status write. Never while polling.
        if (current.status != states.STARTING or
                current.task_state != states.NONE):
            return
        RUNNING_SINCE.pop(('start', service.id), None)
        values = {}
        if current.kind == 'database':
            values['fault'] = (None if running else
                               database_faults.public_fault(failure, action))
        owner._save(current, status=(states.ACTIVE if running
                                     else states.ERROR), **values)
        if current.kind == 'database' and not running:
            owner.network_reconciler.defer_cleanup(ctx, env, current)
        if not running:
            manager.LOG.warning('Service %s did not report running within '
                                'start_timeout', service_id)


def sweep_starting(owner):
    admin = context.RequestContext(is_admin=True)
    marker = None
    while True:
        batch = objects.ServiceList.get_all(
            admin, filters={'status': states.STARTING}, all_projects=True,
            sort_key='id', marker=marker)
        if not batch:
            break
        for service in batch:
            ctx = context.RequestContext(project_id=service.project_id)
            try:
                settle_starting(owner, ctx, service.id)
            except Exception:
                manager.LOG.warning('Service start check failed for %s',
                                    service.id)
        marker = batch[-1].id


def sweep(owner):
    sweep_starting(owner)
    admin = context.RequestContext(is_admin=True)
    for status in (states.DEPLOYING, states.CANCELLED):
        marker = None
        while True:
            batch = objects.DeploymentList.get_all(
                admin, filters={'status': status}, all_projects=True,
                sort_key='id', marker=marker)
            if not batch:
                break
            for deployment in batch:
                ctx = context.RequestContext(project_id=deployment.project_id)
                try:
                    recovery = Recovery(owner)
                    if status == states.CANCELLED:
                        recovery.cancel(ctx, deployment.id)
                    else:
                        recovery.reconcile(ctx, deployment.id)
                except Exception:
                    # A missing row, lock conflict or DB failure must not
                    # stop recovery of other projects. Never log exceptions.
                    manager.LOG.warning('Deployment recovery failed for %s',
                                        deployment.id)
            marker = batch[-1].id
