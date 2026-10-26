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

"""Bounded conductor flows with atomic CAS completion and explicit revert.

Noop cloud provisioning stays in this module. Real stack create is in
stack_create and real stack delete is in stack_teardown. Neither holds
a DB transaction across cloud calls.
"""

import contextlib
import contextvars
import dataclasses
import datetime
import functools
import ssl
import threading
import uuid

from oslo_config import cfg
from oslo_db import exception as db_exception
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_utils import strutils
from oslo_utils import timeutils

from coast.api import secret_store
from coast.common import app_secrets
from coast.common import context as context_module
from coast.common import endpoints
from coast.common import exception
from coast.common import file_mounts
from coast.common import naming
from coast.common import notifications
from coast.common import policy
from coast.common import quota
from coast.common import safe_log
from coast.common import stack_delete
from coast.common import states
from coast.common import storage as storage_rules
from coast.conductor import appgroups
from coast.conductor import backups
from coast.conductor import certificates
from coast.conductor import cloud
from coast.conductor import containers
from coast.conductor import controller_failover
from coast.conductor import database_faults
from coast.conductor import deploy_keys
from coast.conductor import deployments
from coast.conductor import dns as managed_dns
from coast.conductor import engine_upgrade
from coast.conductor import exports
from coast.conductor import reconcile
from coast.conductor import registry
from coast.conductor import rpcapi
from coast.conductor import runtime_logs
from coast.conductor import server_add
from coast.conductor import server_failover
from coast.conductor import server_remove
from coast.conductor import stack_create
from coast.conductor import stack_teardown
from coast.conductor import volume_delete
from coast.db import api as db_api
from coast.drivers import base
from coast.drivers import loader
from coast.network import domain_check
from coast import objects


def retry_operation(fn):
    """Let nested repository writes defer deadlocks to the whole operation."""
    @functools.wraps(fn)
    def wrapped(self, ctx, *args, **kwargs):
        @db_api.retry_transaction
        def attempt(context):
            return fn(self, context, *args, **kwargs)
        return attempt(ctx)
    return wrapped


def _reraise_deadlock(exc):
    """Recognize deadlocks masked by savepoint cleanup failures."""
    if not isinstance(exc, db_exception.DBError):
        return
    pending = [exc]
    seen = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        if isinstance(error, db_exception.DBDeadlock):
            raise db_exception.DBDeadlock() from None
        args = getattr(error, 'args', ())
        message = str(error).lower()
        if (args and args[0] == 1305 and 'savepoint' in message and
                'does not exist' in message):
            raise db_exception.DBDeadlock() from None
        for attr in ('__cause__', '__context__', 'inner_exception', 'orig'):
            nested = getattr(error, attr, None)
            if isinstance(nested, BaseException):
                pending.append(nested)


LOG = logging.getLogger(__name__)
ACTION = contextvars.ContextVar('coast_action', default='update')
FAULT = 'Conductor operation failed; see the resource activity log.'
RATE_LIMIT_FAULT = ('Engine rate limit (HTTP 429) persisted; '
                    'retry budget exhausted. Try again later.')
CLEANUP_FAILED = 'Engine cleanup failed; retry delete'
OPERATIONS = {
    'stack': ('create', 'delete', 'upgrade', 'upgrade_rollback',
              'resize_workers'),
    'server': ('create', 'delete', 'failover', 'rotate', 'resize'),
    'volume': ('create', 'delete', 'extend', 'snapshot'),
    'service': ('create', 'delete', 'start', 'stop', 'restart', 'update',
                'rename'),
    'deployment': ('create', 'rollback', 'cancel'),
    'domain': ('create', 'delete', 'verify', 'update'),
    'export': ('create', 'delete'),
    'appgroup': ('create', 'delete'),
    'app_environment': ('create', 'delete'),
}
# API locks that are not columns on the cast resource.
PARENT_TASK = {
    'create_deployment': states.DEPLOYING,
    'rollback_deployment': states.ROLLING_BACK,
    'delete_domain': states.RECONCILING,
    'verify_domain': states.RECONCILING,
    'update_domain': states.RECONCILING,
}
# Q145: deploys and service actions lock only their service. They run while
# the stack is idle or under a shared RECONCILING hold (appgroup, volume,
# export work); exclusive stack tasks (delete, upgrade, failover) 409 them.
SERVICE_SCOPED = ('service', 'deployment', 'domain')
SHARED_STACK_TASKS = (states.NONE, states.RECONCILING)
# No quota change: the service row lock is enough, no project lock.
UNLOCKED_PROJECT = ('start_service', 'stop_service', 'restart_service',
                    'update_service', 'rename_service')
PUBLISHED = (states.VERIFIED, states.PENDING_DNS, states.ACTIVE)
# Creates whose reservation is not settled by a volume-style start receipt.
UNSETTLED_CREATES = ('create_service', 'create_domain')


def coalesced_volume(kind, action):
    return kind == 'volume' and action in ('create', 'extend')


def superseded(current, resource):
    return (current.status != resource.status or
            getattr(current, 'task_state', None) !=
            getattr(resource, 'task_state', None))


def _create_settled(ctx, resource):
    """True when create already recorded success or failure."""
    kind = resource.repository.resource
    for outcome in ('success', 'failure'):
        if objects.EventList.get_all(ctx, filters={
                'resource_id': resource.id, 'resource_type': kind,
                'action': 'create', 'outcome': outcome}, limit=1):
            return True
    return False


def _fail_unsettled_creates(ctx, stack):
    """Mark an interrupted create failed so its quota is not released twice.

    The event uses the repository resource name. ``environment`` is the
    stack. A later delete sees the failure and does not release again.
    """
    resources = [stack]
    resources.extend(rows(objects.ServerList, ctx, {'stack_id': stack.id}))
    resources.extend(rows(objects.VolumeList, ctx, {'stack_id': stack.id}))
    services = list(rows(objects.ServiceList, ctx, {'stack_id': stack.id}))
    resources.extend(services)
    for service in services:
        resources.extend(rows(objects.DomainList, ctx,
                              {'service_id': service.id}))
    for resource in resources:
        if _create_settled(ctx, resource):
            continue
        objects.Event(
            context=ctx, stack_id=stack.id,
            resource_type=resource.repository.resource,
            resource_id=resource.id, action='create', outcome='failure',
            message='Interrupted by conductor restart').create()


def rows(list_class, ctx, filters, show_deleted=False, all_projects=False):
    marker = None
    while True:
        batch = list_class.get_all(ctx, filters=filters, marker=marker,
                                   sort_key='id', show_deleted=show_deleted,
                                   all_projects=all_projects)
        yield from batch
        if not batch:
            return
        marker = batch[-1].id


def _seconds_since(now, started):
    if started is None:
        return None
    if started.tzinfo is not None:
        started = started.astimezone(
            datetime.timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return (now - started).total_seconds()


REAL_FLOWS = frozenset((
    'create_service', 'delete_service', 'create_deployment',
    'rollback_deployment', 'cancel_deployment', 'update_service',
    'rename_service', 'create_domain', 'verify_domain', 'delete_domain',
    'update_domain',
    # Start/restart go STARTING; the deployment poll settles them.
    'start_service', 'stop_service', 'restart_service'))


def real_flow(operation, resource):
    """Return whether a real (non-noop) driver may run this operation."""
    return operation in REAL_FLOWS


class Manager:
    RPC_API_VERSION = '1.0'
    target = messaging.Target(version=RPC_API_VERSION)

    def __init__(self, conf=cfg.CONF, fake_cloud=None, notifier=None):
        self.conf = conf
        self.cloud = fake_cloud or cloud.FakeCloud()
        self.drivers = {}
        self.notifier = notifier
        # RPC handlers and periodic flows share this manager. Buffer and
        # deduplication state belong to the current execution context only.
        self._pending_context = contextvars.ContextVar('pending', default=None)
        self._seen_context = contextvars.ContextVar('seen', default=None)
        self._settle_lock = threading.Lock()
        self.service_reconciler = reconcile.Reconciler(self)
        self.network_reconciler = reconcile.NetworkReconciler(self)

    @property
    def _pending(self):
        return self._pending_context.get()

    @_pending.setter
    def _pending(self, value):
        self._pending_context.set(value)

    @property
    def _seen(self):
        value = self._seen_context.get()
        if value is None:
            value = set()
            self._seen_context.set(value)
        return value

    @_seen.setter
    def _seen(self, value):
        self._seen_context.set(value)

    def _driver(self, env):
        if env.driver not in self.conf.controller_worker.enabled_drivers:
            raise base.DriverError('Engine driver is not enabled')
        if env.driver != 'noop':
            self._endpoint(env)
        if env.driver not in self.drivers:
            self.drivers[env.driver] = loader.load_driver(
                env.driver, self.conf)
        driver = self.drivers[env.driver]
        unbootstrapped = env.status == states.PENDING or (
            env.status == states.DELETING and not env.engine_ref)
        if (not unbootstrapped and
                env.engine_version not in driver.SUPPORTED_ENGINE_VERSIONS):
            raise base.UnsupportedEngineVersion(
                'Engine version is not supported')
        return driver

    def _engine_version(self, driver, env):
        version = driver.get_engine_version(self._endpoint(env))
        if (version not in driver.SUPPORTED_ENGINE_VERSIONS or
                (env.driver != 'noop' and version != env.engine_version)):
            raise base.UnsupportedEngineVersion(
                'Engine version is not supported')
        return version

    def _endpoint(self, env):
        if env.driver == 'noop':
            return base.EngineEndpoint('https://example.com/engine',
                                       '', '', '', token=None)
        # Never pass an empty reference to the secret store.
        if not env.engine_token_ref:
            raise base.EngineAuthError('Engine token reference is required')
        controllers = objects.ServerList.get_all(
            env._context, filters={
                'stack_id': env.id, 'role': 'control', 'kind': 'vm'},
            limit=2)
        controllers = [server for server in controllers if server.mgmt_ip]
        if controllers:
            # Local import: agent_client pulls the management namespace.
            from coast.conductor import agent_client
            if (len(controllers) != 1 or
                    self.conf.coolify.insecure_direct_url):
                raise base.DriverError(
                    'Controller agent requires production mTLS transport')
            return registry.attach(
                self.conf, agent_client.endpoint(
                    self.conf, controllers[0].mgmt_ip,
                    secret_store.call(
                        'get', env._context, env.engine_token_ref)),
                env, controllers[0])
        url = self.conf.coolify.adopted_endpoints.get(env.id)
        if not self.conf.coolify.insecure_direct_url or not url:
            raise base.DriverError('Engine endpoint is not configured')
        endpoints.validate_url(url)
        token = secret_store.call('get', env._context, env.engine_token_ref)
        if not isinstance(token, str) or not token:
            raise base.EngineAuthError('Engine token is unavailable')
        trust = ssl.get_default_verify_paths()
        ca_cert = trust.cafile or trust.capath
        if not ca_cert:
            raise base.DriverError('Engine CA trust path is unavailable')
        return base.EngineEndpoint(url, '', '', ca_cert, token=token,
                                   timeout=self.conf.coolify.api_timeout)

    def _save(self, resource, **values):
        if getattr(resource, 'kind', None) == 'database':
            status = values.get('status')
            if status in (states.ACTIVE, states.STOPPED):
                values['fault'] = None
            elif status == states.ERROR and 'fault' not in values:
                values['fault'] = database_faults.public_fault()
        for name, value in values.items():
            setattr(resource, name, value)
        resource.save()

    def _queue(self, ctx, resource, action, phase, name=None):
        """Queue one conductor phase. Send it only after the DB commits."""
        action = notifications.public_action(action)
        if getattr(resource, 'kind', None) == 'database':
            name = 'database'
        if not name:
            name = getattr(
                getattr(resource, 'repository', None), 'resource', None)
        if not name or not getattr(resource, 'id', None):
            return
        key = (resource.id, action, phase)
        if self._pending is not None and key in self._seen:
            return
        body = notifications.payload_for(resource, ctx)
        if self._pending is None:
            notifications.emit(
                ctx, notifications.CONDUCTOR, name, action, phase, body,
                self.conf, notifier=self.notifier)
            return
        self._seen.add(key)
        self._pending.append((ctx, name, action, phase, body))

    def _keep_parent_start(self, resource_id, action):
        """Drop child events from a rolled-back attempt. Keep its start."""
        action = notifications.public_action(action)
        kept = [item for item in self._pending
                if item[2] == action and item[3] == 'start' and
                item[4].get('resource_id') == resource_id]
        self._pending[:] = kept
        self._seen = {(item[4].get('resource_id'), item[2], item[3])
                      for item in kept}

    def _flush(self, pending):
        for ctx, name, action, phase, body in pending:
            notifications.emit(
                ctx, notifications.CONDUCTOR, name, action, phase, body,
                self.conf, notifier=self.notifier)

    def recover_stacks(self):
        """Release only interrupted stack tasks owned by this host."""
        exports.prepare_recovery(self)
        admin = context_module.RequestContext(is_admin=True)
        with db_api.reader.using(admin):
            model = db_api.stacks.model
            rows = db_api.stacks._query(admin, all_projects=True).filter(
                model.conductor_host == self.conf.host,
                model.task_state.notin_(
                    (states.NONE, states.RECONCILING))).all()
        for row in rows:
            if row.engine_token_rotation_id:
                continue  # Token rotation resumes its fenced claim on expiry.
            if row.id in self._export_recovery_stacks:
                continue  # Export cleanup must precede stack lock release.
            ctx = context_module.RequestContext(project_id=row.project_id)
            pending = []
            with db_api.writer.using(ctx):
                stack = objects.Stack.obj_from_db(
                    ctx, db_api.stacks._get(ctx, row.id, lock=True))
                if (stack.conductor_host != self.conf.host or
                        stack.task_state in (states.NONE, states.RECONCILING)):
                    continue
                task = stack.task_state
                self._save(stack, status=states.ERROR,
                           fault='Interrupted by conductor restart')
                if task in (states.UPGRADING, states.ROLLING_BACK):
                    self._pending = []
                    self._seen = set()
                    engine_upgrade.interrupted(self, ctx, stack)
                    pending = self._pending
                    self._pending = None
                if task == states.FAILING_OVER:
                    pending = server_failover.interrupted(self, ctx, stack)
                stack.release_task(task)
                objects.Event(
                    context=ctx, stack_id=stack.id, resource_type='stack',
                    resource_id=stack.id, action='update', outcome='failure',
                    message='Interrupted by conductor restart').create()
                # Only a create has committed quota before its rows exist.
                # A delete keeps real rows until the delete itself finishes.
                if task == states.PROVISIONING:
                    _fail_unsettled_creates(ctx, stack)
                if task in (states.PROVISIONING, states.DELETING):
                    quota.reconcile(ctx)
            self._flush(pending)
            LOG.warning('Stack %s interrupted by conductor restart', stack.id)
            self._queue(ctx, stack, 'update', 'error')

    def close(self):
        notifications.reset(self.conf)

    def get_export_urls(self, ctx, export_id):
        export = objects.Export.get_by_id(ctx, export_id)
        policy.authorize(ctx, 'coast:export:renew_url',
                         {'project_id': export.project_id})
        stack = objects.Stack.get_by_id(ctx, export.stack_id)
        if (stack.driver == 'noop' or export.status != states.ACTIVE or
                not export.object_expires_at):
            return []
        try:
            return exports.storage_for(self.conf, ctx, export).urls(export)
        except (base.EngineUnreachable, base.EngineAuthError) as error:
            # RPC copies exception text. Drop it and keep the type so the
            # API returns HTTP 503. Another fault stays a generic error.
            if isinstance(error, base.EngineAuthError):
                raise base.EngineAuthError() from None
            raise base.EngineUnreachable() from None
        except Exception:
            raise exception.CoastException(
                'Export URL request failed.') from None

    def rotate_deploy_key(self, ctx, service_id, reference, public_key):
        # RPC arguments cannot create ownership or select another secret.
        return deploy_keys.run(self, ctx, service_id, reference, public_key)

    def import_compose(self, ctx, service_id):
        """Adopt template YAML once; never return plaintext over RPC."""
        reference = None
        try:
            with db_api.writer.using(ctx):
                service = self._locked_service(ctx, service_id)
                if service.compose_ref:
                    return service.compose_ref
                if not service.is_engine_service or not service.engine_ref:
                    raise exception.Conflict('Compose content is unavailable.')
                service.acquire_task(states.RECONCILING)
                env = objects.Stack.obj_from_db(
                    ctx, db_api.stacks._get(ctx, service.stack_id, lock=True))
                if (service.status == states.DELETING or
                        env.task_state not in SHARED_STACK_TASKS):
                    raise exception.Conflict()
                content = self._driver(env).get_compose(
                    self._endpoint(env), env, service)
                # The existing document may need repair. Validate on PUT
                # and before deployment, so GET can show that document.
                size = len(content.encode('utf-8'))
                if size > self.conf.api.max_compose_bytes:
                    raise exception.Conflict(
                        'Engine compose exceeds size limit.')
                reference = secret_store.call(
                    'put', ctx, content,
                    secret_store.SecretIdentity.for_resource(
                        service, 'compose'))
                service.compose_ref = reference
                service.save()
                service.release_task(states.RECONCILING)
            return reference
        except Exception as error:
            if reference:
                secret_store.call('delete', ctx, reference)
            if isinstance(error, (exception.Conflict, base.EngineConflict)):
                return None
            raise

    def _push_compose(self, ctx, driver, endpoint, env, service, deployment):
        dockerfile_ref = (deployment.spec or {}).get('dockerfile_ref')
        if dockerfile_ref:
            driver.set_dockerfile(endpoint, env, service, secret_store.call(
                'get', ctx, dockerfile_ref))
        reference = (deployment.spec or {}).get('compose_ref')
        if reference:
            content = secret_store.call('get', ctx, reference)
            driver.set_compose(endpoint, env, service, content)

    def list_volume_mounts(self, ctx, service_id):
        """Read mount observations without changing desired state."""
        service = objects.Service.get_by_id(ctx, service_id)
        if not service.engine_ref:
            return []
        stack = objects.Stack.get_by_id(ctx, service.stack_id)
        return self._driver(stack).list_volume_mounts(
            self._endpoint(stack), stack, service)

    def apply_mounts(self, ctx, service_id):
        """Reapply user mounts for one service without restarting it."""
        service = objects.Service.get_by_id(ctx, service_id)
        if not service.engine_ref:
            return
        with self._service_write(ctx, service) as current:
            env = objects.Stack.get_by_id(ctx, current.stack_id)
            if (current.task_state != states.NONE or
                    env.task_state != states.NONE or
                    env.status != states.ACTIVE):
                raise exception.Conflict()
            driver = self._driver(env)
            driver.set_volume_mounts(
                self._endpoint(env), env, current, self._mounts(ctx, current))

    def get_containers(self, ctx, service_id, container=None, logs=False,
                       options=None):
        return containers.read(self, ctx, service_id, container, logs, options)

    def get_logs(self, ctx, resource, resource_id, options=None):
        """Prefer live logs; use durable failure output when unavailable."""
        options = options or {}
        if resource == 'deployment':
            deployment = objects.Deployment.get_by_id(ctx, resource_id)
            service = objects.Service.get_by_id(ctx, deployment.service_id)
        elif resource == 'service':
            service = objects.Service.get_by_id(ctx, resource_id)
            if options.get('tail') == 0:
                return {'lines': []}
            deployment = runtime_logs.latest(ctx, service.id)
        else:
            raise exception.Invalid()
        env = objects.Stack.get_by_id(ctx, service.stack_id)
        live = (env.driver != 'noop' and service.engine_ref and
                (resource == 'service' or deployment.engine_ref))
        attempts = 0

        def read():
            nonlocal attempts
            attempts += 1
            return self._live_logs(ctx, resource, resource_id, options)

        error = None
        try:
            result = runtime_logs.burst(
                read, attempts=10 if live else 1,
                nonempty=lambda value: bool(value['lines']) or
                value.get('complete', False))
        except base.DriverError as caught:
            error = caught
            result = {'lines': []}
        if result['lines']:
            if resource == 'service' and deployment:
                runtime_logs.persist(deployment, [
                    base.LogLine(**line) for line in result['lines']])
            if attempts > 1:
                result['note'] = ('Container is restarting; showing output '
                                  'from its recent runs.')
            return result
        # Reload after retries: a background capture may have completed.
        saved = (objects.Deployment.get_by_id(ctx, deployment.id)
                 if resource == 'deployment' else
                 runtime_logs.latest(ctx, service.id, with_output=True))
        stored = runtime_logs.stored(saved, options.get('tail', 200)) if (
            saved) else None
        if stored:
            if resource == 'deployment':
                stored['complete'] = saved.status not in (
                    states.PENDING, states.BUILDING, states.DEPLOYING)
            return stored
        if resource == 'service':
            held = runtime_logs.last(service.id)
            if held:
                return {'lines': [dataclasses.asdict(line) for line in held[1][
                    -options.get('tail', 200):]], 'captured_at': held[0],
                    'note': 'Showing output from the last run (captured %s).'
                            % held[0]}
            if live and not result.get('complete') and not isinstance(error, (
                    base.EngineUnreachable, base.EngineAuthError,
                    base.EngineNotFound)):
                env = objects.Stack.get_by_id(ctx, service.stack_id)
                reason = self._logs_unavailable(
                    self._driver(env), self._endpoint(env), env, service)
                if reason:
                    result['unavailable'] = reason
                    return result
        if error:
            raise error
        return result

    def _live_logs(self, ctx, resource, resource_id, options=None):
        """Read project-scoped engine logs, with an optional memory cache."""
        options = options or {}
        deployment = None
        if resource == 'deployment':
            deployment = objects.Deployment.get_by_id(ctx, resource_id)
            service = objects.Service.get_by_id(ctx, deployment.service_id)
        elif resource == 'service':
            service = objects.Service.get_by_id(ctx, resource_id)
        else:
            raise exception.Invalid()
        env = objects.Stack.get_by_id(ctx, service.stack_id)
        result = {'lines': []}
        if deployment:
            result.update(deployment_id=deployment.id, complete=False)
        # Retain Q32's M1 noop responses and pending-create behavior.
        if env.driver == 'noop' or not service.engine_ref:
            return result
        driver = self._driver(env)
        endpoint = self._endpoint(env)
        try:
            if deployment:
                if not deployment.engine_ref:
                    return result
                lines = list(driver.get_deployment_log(
                    endpoint, env, service, deployment,
                    masks=self._log_masks(ctx, service)))
                result['complete'] = bool(service.is_engine_service) or (
                    deployment.status not in (
                        states.PENDING, states.BUILDING, states.DEPLOYING))
            else:
                if options.get('tail') == 0:
                    return result
                lines = list(driver.get_app_logs(
                    endpoint, env, service, tail=options.get('tail', 200),
                    masks=self._log_masks(ctx, service)))
                runtime_logs.remember(service.id, lines)
        except base.EngineNotFound:
            result.update(lines=[], complete=True)
            return result
        since = options.get('since')
        if since:
            def timestamp(value):
                parsed = datetime.datetime.fromisoformat(
                    value.replace('Z', '+00:00'))
                return parsed.replace(tzinfo=datetime.timezone.utc) if (
                    parsed.tzinfo is None) else parsed
            cutoff = timestamp(since)
            lines = (line for line in lines if timestamp(line.ts) > cutoff)
        result['lines'] = [dataclasses.asdict(line) for line in lines]
        return result

    @staticmethod
    def _logs_unavailable(driver, endpoint, env, service):
        try:
            observed = driver.get_app_status(endpoint, env, service)
        except Exception:
            observed = None
        state = observed.status if observed else None
        if state in (states.DEPLOYING, states.ERROR):
            return ('No runtime output has been captured yet. The app '
                    'container is restarting or failing. Capture continues '
                    'in the background; refresh to check for output.')
        if state == states.STOPPED:
            return 'The app container is stopped. Start or redeploy it.'
        return None

    def _event(self, ctx, resource, env, action, outcome, id=None,
               detail=None, failure_message=FAULT):
        self._queue(ctx, resource, action, {'start': 'start', 'success': 'end',
                                            'failure': 'error'}[outcome])
        if resource.repository.resource == 'server' and action in (
                'create', 'delete', 'failover', 'resize'):
            detail = dict(detail or {})
            detail.update(server_name=resource.name, role=resource.role,
                          kind=resource.kind, flavor_id=resource.flavor_id,
                          image_id=resource.image_id)
            if action in ('create', 'delete'):
                detail['compute_id'] = resource.compute_id
            if action == 'failover':
                detail.update(new_compute_id=(resource.compute_id
                                              if outcome != 'start'
                                              else None),
                              reason={'start': 'Replacement started.',
                                      'success': 'Replacement completed.',
                                      'failure': 'Replacement failed.'}[
                                          outcome])
                if outcome == 'start':
                    detail['old_compute_id'] = resource.compute_id
        if id:
            previous = objects.EventList.get_all(
                ctx, filters={'id': id}, limit=1)
            if previous:
                values = dict(outcome=outcome,
                              message=(failure_message
                                       if outcome == 'failure' else None))
                if detail is not None:
                    values['detail'] = dict(previous[0].detail or {}, **detail)
                    if (action == 'failover' and
                            values['detail'].get('new_compute_id') ==
                            values['detail'].get('old_compute_id')):
                        values['detail']['new_compute_id'] = None
                self._save(previous[0], **values)
                self._timeline_stack(ctx, resource, previous[0])
                return
        event = objects.Event(
            context=ctx, stack_id=env.id,
            resource_type=resource.repository.resource,
            resource_id=resource.id, action=action, outcome=outcome,
            message=failure_message if outcome == 'failure' else None,
            request_id=ctx.request_id, detail=detail)
        if id:
            event.id = id
        event.create()
        self._timeline_stack(ctx, resource, event)

    @staticmethod
    def _timeline_stack(ctx, resource, event):
        # Keep Q47 environment receipts for quota and existing consumers.
        if resource.repository is not db_api.stacks:
            return
        identifier = str(uuid.uuid5(uuid.UUID(event.id), 'stack-timeline'))
        values = {key: getattr(event, key) for key in (
            'stack_id', 'resource_id', 'action', 'outcome', 'message',
            'detail', 'request_id')}
        previous = objects.EventList.get_all(ctx, filters={'id': identifier})
        if previous:
            for key in ('outcome', 'message', 'detail'):
                setattr(previous[0], key, values[key])
            previous[0].save()
        else:
            objects.Event(context=ctx, id=identifier, resource_type='stack',
                          **values).create()

    @staticmethod
    def _committed(ctx, resource):
        """True when create completion already moved quota into in_use."""
        found = objects.EventList.get_all(ctx, filters={
            'resource_id': resource.id,
            'resource_type': resource.repository.resource,
            'action': 'create', 'outcome': 'success'}, limit=1)
        return bool(found)

    @staticmethod
    def _accounted(ctx, resource):
        # Failed creates have already rolled back their reservation. Do not
        # release another resource's capacity when deleting the ERROR row.
        failures = objects.EventList.get_all(ctx, filters={
            'resource_id': resource.id,
            'resource_type': resource.repository.resource, 'action': 'create',
            'outcome': 'failure'}, limit=1)
        return not failures

    def _execute_server(self, ctx, operation, resource, options, reservation,
                        revision):
        if operation == 'rotate_server':
            certificates.rotate(self, ctx, resource)
            return True
        if operation == 'failover_server':
            # Real controller replacement must not reach register_server.
            # Noop stacks keep the existing admin stub below.
            if resource.role == 'control':
                stack = objects.Stack.get_by_id(ctx, resource.stack_id)
                if stack.driver != 'noop':
                    controller_failover.ControllerFailover(self).execute(
                        ctx, resource, options, reservation, revision)
                    return True
            stack = objects.Stack.get_by_id(ctx, resource.stack_id)
            if resource.role in ('worker', 'build') and stack.driver != 'noop':
                server_failover.FailoverServer(self).execute(
                    ctx, resource, options, reservation, revision)
                return True
        if operation in ('create_server', 'delete_server'):
            stack = objects.Stack.get_by_id(ctx, resource.stack_id)
            if stack.driver != 'noop':
                flow = (server_add.AddServer if operation == 'create_server'
                        else server_remove.RemoveServer)
                flow(self).execute(
                    ctx, resource, options, reservation, revision)
                return True
        return False

    def execute(self, ctx, operation, resource, options=None, reservation=None,
                revision=None):
        kind = states.kind(resource.repository.resource)
        action, _, suffix = operation.partition('_')
        if operation == 'upgrade_rollback_stack':
            action, suffix = 'upgrade_rollback', 'stack'
        if operation == 'resize_workers_stack':
            action, suffix = 'resize_workers', 'stack'
        if suffix != kind or action not in OPERATIONS.get(kind, ()):
            raise exception.Invalid()
        if ctx.project_id != resource.project_id:
            raise exception.NotFound()
        if operation in ('resize_server', 'resize_workers_stack'):
            from coast.conductor import resize
            resize.execute(self, ctx, resource, options, revision)
            return
        if operation in ('upgrade_stack', 'upgrade_rollback_stack'):
            engine_upgrade.Upgrade(self).execute(
                ctx, resource, revision=revision,
                rollback=operation == 'upgrade_rollback_stack')
            return
        if operation == 'create_stack' and resource.driver != 'noop':
            stack_create.CreateStack(self).execute(
                ctx, resource, options, reservation, revision)
            return
        if operation == 'delete_stack' and resource.driver != 'noop':
            # Return before _run. The noop path still deletes FakeCloud rows.
            stack_teardown.teardown(
                ctx, resource, stack_teardown.Clients(
                    self, resource, options or {}))
            return
        if kind == 'server' and self._execute_server(
                ctx, operation, resource, options, reservation, revision):
            return
        if operation == 'delete_volume':
            volume_delete.DeleteVolume(self).execute(ctx, resource, revision)
            return
        if kind == 'export':
            stack = objects.Stack.get_by_id(ctx, resource.stack_id)
            if stack.driver != 'noop':
                exports.ExportFlow(self).execute(
                    ctx, operation, resource, revision)
                return
        if operation == 'delete_appgroup':
            self._delete_appgroup(ctx, resource)
            return
        if operation == 'delete_app_environment':
            self._delete_app_environment(ctx, resource)
            return
        if kind in ('appgroup', 'app_environment'):
            appgroups.activate(self, ctx, resource)
            return
        if kind == 'deployment':
            service = objects.Service.get_by_id(ctx, resource.service_id)
            env = objects.Stack.get_by_id(ctx, service.stack_id)
            if env.driver != 'noop':
                recovery = deployments.Recovery(self)
                if action == 'cancel':
                    recovery.cancel(ctx, resource.id)
                else:
                    recovery.execute(ctx, operation, resource, revision,
                                     task_claim=(options or {}).get(
                                         '_task_claim'))
                return
        self._execute_operation(ctx, operation, resource, kind, action,
                                options, reservation, revision)

    def _execute_operation(self, ctx, operation, resource, kind, action,
                           options, reservation, revision):
        options = dict(options or {})
        reservation = reservation or {}
        self._pending = []
        self._seen = set()
        claim = options.get('_task_claim')
        if kind == 'service' and claim is None and resource.task_started_at:
            # The service itself is the accepted RPC snapshot. Never read a
            # fresh claim here; delayed messages must retain their identity.
            claim = {'resource': kind, 'id': resource.id,
                     'state': resource.task_state,
                     'owner': resource.task_owner,
                     'started_at': timeutils.normalize_time(
                         resource.task_started_at).isoformat()}
            options['_task_claim'] = claim
        receipt = rpcapi.receipt_id(operation, resource, revision,
                                    task_claim=claim)
        token = ACTION.set(action)
        try:
            self._run(ctx, operation, resource, kind, action, options,
                      reservation, receipt)
        except Exception as exc:
            safe_log.log_failure(
                LOG, 'Conductor %(operation)s for app %(resource)s failed '
                'request_id=%(request_id)s: %(cause)s at %(where)s',
                exc, operation=operation,
                resource=getattr(resource, 'service_id', resource.id),
                request_id=ctx.request_id)
            try:
                self._pending = []
                self._seen = set()
                self._failed_operation(ctx, operation, resource, kind,
                                       action, reservation, receipt,
                                       claim, exc)
                self._flush(self._pending)
            finally:
                self._pending = None
                ACTION.reset(token)
            raise
        pending = self._pending
        self._pending = None
        ACTION.reset(token)
        self._flush(pending)
        if options.get('_enqueue'):
            deployments.Recovery(self).enqueue(ctx, options['_enqueue'])

    @retry_operation
    def _failed_operation(self, ctx, operation, resource, kind, action,
                          reservation, receipt, claim, error=None):
        # The API claim committed before RPC. Rollback cannot release it.
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            if kind in ('domain', 'service', 'deployment'):
                if claim is None:
                    # Domain creates release their API claim before casting;
                    # they must never release another domain operation's task.
                    if kind == 'domain' and action == 'create':
                        self._failure(ctx, resource, reservation, False)
                        self._record_receipt(
                            ctx, resource, action, 'failure', receipt)
                        self._queue(ctx, resource, action, 'error')
                    return
                parent = self._claim_lock(ctx, resource, kind, claim)
                if parent is None:
                    return
                try:
                    message = (database_faults.public_fault(error, action)
                               if getattr(resource, 'kind', None) == 'database'
                               else FAULT)
                    current = self._failure(
                        ctx, resource, reservation, False, message)
                    if kind == 'deployment':
                        self._save(current, finished_at=timeutils.utcnow())
                    if operation == 'create_service':
                        self._fail_pending_deployment(ctx, current)
                except exception.NotFound:
                    pass  # A concurrent delete already removed the resource.
                parent.release_task(claim['state'])
                self._record_receipt(ctx, resource, action, 'failure', receipt)
                self._queue(ctx, resource, action, 'error')
                return
            current = type(resource).obj_from_db(
                ctx, resource.repository._get(ctx, resource.id, lock=True))
            # Resolve the original task directly. Setup helpers may be the
            # persistent failure, so neither is safe to call during cleanup.
            task = PARENT_TASK.get(operation)
            if task:
                lock = self._locked_service(ctx, current.service_id)
            elif operation == 'create_export':
                lock = objects.Stack.obj_from_db(
                    ctx, db_api.stacks._get(ctx, current.stack_id, lock=True))
                task = states.RECONCILING
            else:
                lock, task = current, getattr(resource, 'task_state', None)
            if task and lock.task_state != task:
                return
            current = self._failure(
                ctx, resource, reservation, coalesced_volume(kind, action))
            if task and task != states.NONE:
                lock.release_task(task)
            if kind == 'export':
                env = objects.Stack.get_by_id(ctx, current.stack_id)
                self._release_export_stack(ctx, operation, current, env)
            self._record_receipt(ctx, current, action, 'failure', receipt)
            self._queue(ctx, current, action, 'error')

    def _claim_lock(self, ctx, resource, kind, claim):
        service_id = resource.id if kind == 'service' else resource.service_id
        if (claim.get('resource') != 'service' or
                claim.get('id') != service_id):
            return None
        parent = self._locked_service(ctx, service_id)
        stamp = parent.task_started_at
        if (stamp is None or parent.task_state != claim.get('state') or
                parent.task_owner != claim.get('owner') or
                timeutils.normalize_time(stamp).isoformat() !=
                claim.get('started_at')):
            return None
        return parent

    def recover_domain_tasks(self):
        admin = context_module.RequestContext(is_admin=True)
        for listed in rows(objects.ServiceList, admin, {}, all_projects=True):
            if listed.task_state == states.NONE or not listed.task_owner:
                continue
            ctx = context_module.RequestContext(project_id=listed.project_id)
            recovered = self._recover_domain_task(ctx, listed.id)
            if recovered is not None:
                self._queue(ctx, recovered, 'update', 'error')

    @retry_operation
    def _recover_domain_task(self, ctx, service_id):
        # Live casts hold this row through completion. Wait, then recheck.
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            service = self._locked_service(ctx, service_id)
            age = _seconds_since(timeutils.utcnow(), service.task_started_at)
            if (service.task_state == states.NONE or not service.task_owner or
                    age is None or age < 300):
                return
            if service.task_owner == deploy_keys.OWNER:
                source = objects.Source.get_by_id(ctx, service.source_id)
                if source.deploy_key_rotation:
                    # Rotation recovery owns the old/new secret checkpoint.
                    return
            # Durable asynchronous jobs have their own recovery and may still
            # be running after the transaction that accepted them commits.
            for model, statuses in (
                    (objects.DeploymentList, (states.DEPLOYING,
                                              states.ROLLING_BACK)),
                    (objects.BackupJobList, (states.PENDING,
                                             states.BUILDING))):
                for status in statuses:
                    if model.get_all(ctx, filters={
                            'service_id': service.id, 'status': status},
                            limit=1):
                        return
            pending = objects.DeploymentList.get_all(ctx, filters={
                'service_id': service.id, 'status': states.PENDING})
            now = timeutils.utcnow()
            if any(row.started_at is not None or
                   _seconds_since(now, row.created_at) < 300
                   for row in pending):
                return
            # No enqueue checkpoint exists. Fail abandoned work before
            # releasing its claim, so delayed delivery cannot start it.
            for row in pending:
                self._save(row, status=states.FAILED, fault=FAULT,
                           finished_at=now)
                env = objects.Stack.get_by_id(ctx, service.stack_id)
                self._event(ctx, row, env, 'create', 'failure')
            if service.status == states.PENDING:
                self._save(service, status=states.ERROR, fault=FAULT)
            if service.kind == 'database':
                # Cloud rules can outlive a rolled-back create transaction.
                # Keep cleanup durable even when Neutron is unavailable.
                env = objects.Stack.get_by_id(ctx, service.stack_id)
                self.network_reconciler.defer_cleanup(ctx, env, service)
            owner = service.task_owner
            service.release_task(service.task_state)
            LOG.warning('Recovered abandoned %(owner)s for app %(app)s '
                        'request_id=%(request_id)s',
                        {'owner': owner, 'app': service.id,
                         'request_id': ctx.request_id})
            return service

    def _delete_appgroup(self, ctx, appgroup):
        self._delete_engine_row(
            ctx, appgroup, self._drop_project, 'appgroup')

    def _delete_app_environment(self, ctx, environment):
        # Public notifications use environment. The state key stays
        # app_environment.
        self._delete_engine_row(
            ctx, environment, self._drop_environment, 'environment')

    def _delete_engine_row(self, ctx, resource, drop, public_name):
        current = self._only_deleting(ctx, resource)
        if current is None:
            return
        self._queue(ctx, current, 'delete', 'start', public_name)
        try:
            drop(ctx, current)
        except Exception:
            self._engine_delete_failed(ctx, resource, public_name)
            return
        self._engine_delete_done(ctx, resource, public_name)

    @staticmethod
    def _only_deleting(ctx, resource):
        try:
            current = type(resource).get_by_id(ctx, resource.id)
        except exception.NotFound:
            return None
        if current.status != states.DELETING:
            return None
        return current

    def _drop_project(self, ctx, appgroup):
        if not appgroup.engine_ref:
            return
        stack = objects.Stack.get_by_id(ctx, appgroup.stack_id)
        self._drop(stack, 'delete_project', stack, appgroup)

    def _drop_environment(self, ctx, environment):
        if not environment.engine_ref:
            return
        appgroup = objects.Appgroup.get_by_id(
            ctx, environment.appgroup_id)
        stack = objects.Stack.get_by_id(ctx, appgroup.stack_id)
        self._drop(
            stack, 'delete_environment', stack, appgroup, environment)

    def _drop(self, stack, method, *args):
        def cleanup():
            driver = self._driver(stack)
            try:
                getattr(driver, method)(self._endpoint(stack), *args)
            except base.EngineNotFound:
                return
        self._delete_engine_cleanup(stack, cleanup)

    @staticmethod
    def _delete_engine_cleanup(stack, cleanup):
        if stack.status == states.ERROR:
            LOG.warning('Skipping engine cleanup for ERROR stack %s; '
                        'continuing record deletion.', stack.id)
            return
        try:
            cleanup()
        except base.EngineUnreachable:
            LOG.warning('Engine unreachable for stack %s; continuing '
                        'record deletion.', stack.id)

    def _engine_delete_failed(self, ctx, resource, public_name):
        # The engine message can contain secrets. Store only the fixed text.
        LOG.warning('Engine cleanup failed for %s', resource.id)
        current = self._only_deleting(ctx, resource)
        if current is None:
            return
        self._save(current, status=states.ERROR)
        objects.Event(
            context=ctx, stack_id=self._row_stack_id(ctx, current),
            resource_type=current.repository.resource,
            resource_id=current.id, action='delete', outcome='failure',
            message=CLEANUP_FAILED, request_id=ctx.request_id).create()
        self._queue(ctx, current, 'delete', 'error', public_name)

    def _engine_delete_done(self, ctx, resource, public_name):
        current = self._only_deleting(ctx, resource)
        if current is None:
            return
        self._save(current, status=states.DELETED)
        try:
            current.destroy()
        except exception.NotFound:
            return
        self._queue(ctx, current, 'delete', 'end', public_name)

    @staticmethod
    def _row_stack_id(ctx, resource):
        if resource.repository.resource == 'appgroup':
            return resource.stack_id
        appgroup = objects.Appgroup.get_by_id(
            ctx, resource.appgroup_id)
        return appgroup.stack_id

    def settle_deployments(self):
        """Check DEPLOYING/STARTING rows; skip if a sweep is running."""
        if not self._settle_lock.acquire(blocking=False):
            return
        try:
            self.network_reconciler.retry_cleanup()
            self.recover_domain_tasks()
            deployments.sweep(self)
        except Exception:
            LOG.warning('Deployment recovery sweep failed.')
        finally:
            self._settle_lock.release()

    def reconcile_deployments(self):
        try:
            exports.recover(self)
        except Exception:
            LOG.warning('Export recovery sweep failed.')
        self.settle_deployments()
        try:
            exports.housekeep(self)
        except Exception:
            LOG.warning('Export housekeeping sweep failed.')
        try:
            stack_teardown.housekeep(self)
        except Exception:
            LOG.warning('Stack snapshot housekeeping sweep failed.')

    def reconcile_networks(self):
        """Repair stack env security groups (own timer, own time budget)."""
        try:
            self.network_reconciler.sweep()
        except Exception as exc:
            cause, where = safe_log.describe(exc)
            LOG.warning('Stack security group reconcile sweep failed: '
                        '%(cause)s at %(where)s',
                        {'cause': cause, 'where': where})

    def reconcile_services(self):
        deploy_keys.sweep(self)
        try:
            self.service_reconciler.sweep()
        except Exception:
            LOG.warning('Service reconcile sweep failed.')

    def collect_registries(self):
        try:
            admin = context_module.RequestContext(is_admin=True)
            found = list(rows(objects.StackList, admin,
                              {'status': states.ACTIVE}, all_projects=True))
        except Exception as exc:
            safe_log.log_failure(
                LOG, 'Registry sweep failed: %(cause)s at %(where)s', exc)
            return
        for listed in found:
            if not listed.registry_secret_ref:
                continue
            try:
                ctx = context_module.RequestContext(
                    project_id=listed.project_id, is_admin=True)
                env = objects.Stack.get_by_id(ctx, listed.id)
                if env.status != states.ACTIVE or not env.registry_secret_ref:
                    continue
                self.collect_registry(ctx, env, [], strict=False)
            except base.EngineUnreachable as exc:
                cause, where = safe_log.describe(exc)
                LOG.warning('Registry sweep deferred for %s: %s at %s',
                            listed.id, cause, where)
            except Exception as exc:
                safe_log.log_failure(
                    LOG, 'Registry sweep failed for %(stack)s: '
                    '%(cause)s at %(where)s', exc, stack=listed.id)

    def collect_registry(self, ctx, env, repositories, strict):
        """Ask the controller agent to delete repositories and collect blobs.

        An empty repository list still runs garbage collection. ``strict``
        raises when the controller cannot run the collection. The periodic
        sweep logs that condition and continues.
        """
        if env.driver == 'noop' or not env.registry_secret_ref:
            return
        found = [server for server in objects.ServerList.get_all(
            ctx, filters={'stack_id': env.id, 'role': 'control'})
            if server.kind == 'vm' and server.status == states.ACTIVE and
            server.mgmt_ip]
        if len(found) != 1:
            error = base.DriverError('Registry controller is unavailable')
            if strict:
                raise error
            safe_log.log_failure(
                LOG, 'Registry sweep skipped for %(stack)s: '
                '%(cause)s at %(where)s', error, stack=env.id)
            return
        from coast.conductor import agent_client
        agent_client.AgentClient(self.conf, found[0].mgmt_ip).request(
            'POST', '/v1/registry/gc', {'repositories': list(repositories)})

    def drive_domains(self):
        try:
            admin = context_module.RequestContext(is_admin=True)
            for service in rows(objects.ServiceList, admin, {},
                                all_projects=True):
                if (naming.optional_attr(service, 'default_dns_zone_id') or
                        self.conf.dns.use_designate):
                    try:
                        self._drive_default_dns(service)
                    except Exception:
                        LOG.warning('Default DNS reconcile failed for %s.',
                                    service.id)
            statuses = (states.PENDING_VERIFICATION, states.VERIFIED,
                        states.PENDING_DNS, states.ACTIVE)
            for status in statuses:
                found = list(rows(objects.DomainList, admin,
                                  {'status': status}, all_projects=True))
                for domain in found:
                    try:
                        self._drive_domain(domain)
                    except Exception as exc:
                        # The exception text can contain a DNS token.
                        LOG.warning('Domain drive failed for %s (%s).',
                                    domain.id, type(exc).__name__)
        except Exception:
            LOG.warning('Domain drive sweep failed.')

    @contextlib.contextmanager
    def _domain_write(self, ctx, service, env, domain=None, engine=False):
        # The revision check and claim share the API's project sentinel.
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            current = objects.Service.obj_from_db(
                ctx, db_api.services._get(ctx, service.id, lock=True))
            stack = objects.Stack.obj_from_db(
                ctx, db_api.stacks._get(ctx, env.id, lock=True))
            valid = (current.updated_at == service.updated_at and
                     current.task_state == states.NONE and
                     current.status != states.DELETING and
                     stack.updated_at == env.updated_at and
                     stack.task_state == states.NONE and
                     stack.status == states.ACTIVE)
            if domain is not None:
                fresh = objects.Domain.obj_from_db(
                    ctx, db_api.domains._get(ctx, domain.id, lock=True))
                valid = (valid and fresh.updated_at == domain.updated_at and
                         fresh.status == domain.status)
            if valid:
                try:
                    current.acquire_task(states.RECONCILING,
                                         owner='domain_check')
                except exception.Conflict:
                    valid = False
            if not engine:
                if not valid:
                    raise exception.Conflict()
                try:
                    yield
                finally:
                    current.release_task(states.RECONCILING)
                    service._from_db(db_api.services.get(ctx, service.id))
                return
        if not valid:
            raise exception.Conflict()
        try:
            with self._service_write(ctx, current):
                yield
        finally:
            try:
                with self._service_write(ctx, current):
                    current.release_task(states.RECONCILING)
            except exception.Conflict:
                pass
            service._from_db(db_api.services.get(ctx, service.id))

    def _drive_default_dns(self, listed):
        ctx = context_module.RequestContext(project_id=listed.project_id)
        service = objects.Service.get_by_id(ctx, listed.id)
        env = objects.Stack.get_by_id(ctx, service.stack_id)
        if (service.task_state != states.NONE or
                service.status == states.DELETING or
                env.task_state != states.NONE or env.status != states.ACTIVE):
            return
        # Idempotent external DNS work has no service-state writes.
        managed_dns.ensure_default(
            self, ctx, service, env, self._advertised_host(ctx, service))

    def _drive_domain(self, listed):
        ctx = context_module.RequestContext(project_id=listed.project_id)
        try:
            domain = objects.Domain.get_by_id(ctx, listed.id)
            service = objects.Service.get_by_id(ctx, domain.service_id)
            env = objects.Stack.get_by_id(ctx, service.stack_id)
            if (service.task_state != states.NONE or
                    service.status == states.DELETING or
                    env.task_state != states.NONE or
                    env.status != states.ACTIVE):
                return
            self._check_domain(ctx, domain, service, env)
        except (exception.Conflict, exception.NotFound):
            # A user operation won the race; retry from fresh rows next pass.
            return

    def _check_domain(self, ctx, domain, service, env):
        if domain.status not in (states.PENDING_VERIFICATION, states.VERIFIED,
                                 states.PENDING_DNS, states.ACTIVE):
            return
        if domain.status == states.ACTIVE and not domain.zone_id:
            return

        def save_dns(row, **values):
            with self._domain_write(ctx, service, env, domain):
                self._save(row, **values)

        if not managed_dns.ensure_domain(
                self, ctx, service, env, domain, save=save_dns):
            return
        now = timeutils.utcnow()
        if domain.status == states.PENDING_VERIFICATION:
            started = self._verify_requested_at(ctx, domain)
            if domain.auto_dns and domain.zone_id:
                started = domain.verification_started_at or domain.created_at
            if started is None:
                return
            if self._past_limit(now, started,
                                self.conf.domains.verification_timeout):
                with self._domain_write(ctx, service, env, domain):
                    self._domain_fault(
                        ctx, domain, env, 'Domain verification timed out.')
                return
            resolver = domain_check.make_resolver(
                self.conf.domains.nameservers)
            if not domain_check.txt_matches(
                    domain.fqdn, domain.verification_token, resolver):
                return
            with self._domain_write(ctx, service, env, domain):
                self._domain_state(ctx, domain, states.VERIFIED,
                                   verified_at=now)
        if domain.status == states.VERIFIED:
            if self._past_limit(now, domain.verified_at,
                                self.conf.domains.activation_timeout):
                with self._domain_write(ctx, service, env, domain):
                    self._domain_fault(
                        ctx, domain, env, 'Domain activation timed out.')
                return
            driver = self._driver(env)
            if not self._known(driver, env, service):
                return
            with self._domain_write(ctx, service, env, domain, engine=True):
                driver.set_domains(
                    self._endpoint(env), env, service,
                    self._fqdns(ctx, service))
            with self._domain_write(ctx, service, env, domain):
                self._domain_state(ctx, domain, states.PENDING_DNS)
        if domain.status == states.PENDING_DNS:
            if self._past_limit(now, domain.verified_at,
                                self.conf.domains.activation_timeout):
                with self._domain_write(ctx, service, env, domain):
                    self._domain_fault(
                        ctx, domain, env, 'Domain activation timed out.')
                return
            if self._routing_ready(ctx, domain, service):
                with self._domain_write(ctx, service, env, domain):
                    self._domain_state(ctx, domain, states.ACTIVE)

    def _domain_state(self, ctx, domain, status, **values):
        self._queue(ctx, domain, 'verify', 'start')
        self._save(domain, status=status, **values)
        self._queue(ctx, domain, 'verify', 'end')

    def _past_limit(self, now, started, limit):
        age = _seconds_since(now, started)
        return age is not None and age >= limit

    def _verify_requested_at(self, ctx, domain):
        # _run writes this row when verify is accepted. It is the request.
        found = objects.EventList.get_all(
            ctx, filters={
                'resource_id': domain.id,
                'resource_type': domain.repository.resource,
                'action': 'verify', 'outcome': 'success'},
            sort_key='created_at', sort_dir='desc', limit=1)
        if not found:
            return None
        return found[0].created_at

    def _domain_fault(self, ctx, domain, env, message):
        self._queue(ctx, domain, 'verify', 'start')
        self._save(domain, status=states.ERROR, status_reason=message)
        self._queue(ctx, domain, 'verify', 'error')
        objects.Event(
            context=ctx, stack_id=env.id,
            resource_type=domain.repository.resource,
            resource_id=domain.id, action='verify', outcome='failure',
            message=message, request_id=ctx.request_id).create()

    def _routing_ready(self, ctx, domain, service):
        host = self._advertised_host(ctx, service)
        fip = None
        if service.server_id:
            try:
                server = objects.Server.get_by_id(ctx, service.server_id)
            except exception.NotFound:
                server = None
            if server is not None:
                fip = server.floating_ip
        resolver = domain_check.make_resolver(self.conf.domains.nameservers)
        try:
            routed = domain_check.routes_to(
                domain.fqdn, host or '', fip, resolver)
        except ValueError:
            routed = False
        if not routed:
            return False
        if domain.tls == 'none':
            return True
        return domain_check.tls_ready(domain.fqdn)

    @retry_operation
    def _run(self, ctx, operation, resource, kind, action, options,
             reservation, receipt):
        self._pending = []
        self._seen = set()
        options.pop('_enqueue', None)
        undo = []
        with db_api.writer.using(ctx) as session:
            if operation not in UNLOCKED_PROJECT or reservation:
                db_api._lock_project(session, ctx.project_id)
            receipts = objects.EventList.get_all(
                ctx, filters={'id': receipt}, limit=1)
            if receipts and receipts[0].outcome != 'start':
                return
            claim = options.get('_task_claim')
            if kind == 'domain' and action != 'create' and claim is None:
                return  # An unfenced cast cannot take a later API claim.
            if (claim is not None and kind in ('domain', 'deployment') and
                    self._claim_lock(ctx, resource, kind, claim) is None):
                return
            current = self._load_current(
                ctx, operation, resource, kind, action, reservation, receipt)
            if current is None:
                return
            if (claim is not None and kind == 'service' and
                    self._claim_lock(ctx, resource, kind, claim) is None):
                return
            env = self._stack(ctx, current)
            locked = self._operation_lock(ctx, operation, current, env)
            if locked is None:
                return  # A newer operation owns the parent task.
            lock, task = locked
            try:
                with session.begin_nested():
                    if task:
                        db_api.cas_task_state(ctx, lock.repository.model,
                                              lock.id, new=task,
                                              expected=task)
                    if (kind != 'stack' and
                            env.status != states.ACTIVE and not (
                                operation == 'delete_service' and
                                env.status in (states.ERROR,
                                               states.DEGRADED))):
                        raise exception.Conflict()
                    if (kind in SERVICE_SCOPED and
                            env.task_state not in SHARED_STACK_TASKS):
                        raise exception.Conflict()
                    held_env = self._hold_stack(kind, lock, env)
                    if (env.task_state != states.NONE and
                            kind not in SERVICE_SCOPED):
                        self._save(env, conductor_host=self.conf.host)
                    if env.driver != 'noop' and not real_flow(
                            operation, current):
                        raise base.DriverError(
                            'Real cloud flows are not implemented')
                    # App deletion can retire records without an engine.
                    # Resolve the driver only inside its cleanup guard.
                    driver = (None if operation == 'delete_service'
                              else self._driver(env))
                    self._queue(ctx, current, action, 'start')
                    getattr(self, '_' + operation)(
                        ctx, current, env, driver, options, undo)
                    if held_env and not options.get('_enqueue'):
                        env.release_task(states.RECONCILING)
                    if reservation and not coalesced_volume(kind, action):
                        quota.commit(ctx, reservation)
                    if not options.get('_enqueue'):
                        self._release_owned_task(ctx, action, kind, lock, task)
            except db_exception.DBDeadlock:
                raise db_exception.DBDeadlock() from None
            except Exception as exc:
                _reraise_deadlock(exc)
                safe_log.log_failure(
                    LOG,
                    'Conductor %(operation)s for %(kind)s %(resource)s '
                    'failed: %(cause)s at %(where)s '
                    'app=%(app)s request_id=%(request_id)s',
                    exc, operation=operation, kind=kind,
                    resource=getattr(resource, 'id', None),
                    app=getattr(resource, 'service_id', resource.id),
                    request_id=getattr(ctx, 'request_id', None))
                options.pop('_enqueue', None)
                self._abort(ctx, operation, resource, current, env, action,
                            lock, task, reservation, kind, receipt, undo,
                            failure_message=(database_faults.public_fault(
                                exc, action) if getattr(
                                    current, 'kind', None) == 'database' else
                                RATE_LIMIT_FAULT if isinstance(
                                exc, base.EngineRateLimited) else FAULT))
                if (kind == 'deployment' and
                        isinstance(exc, deployments.EnvSyncFailed)):
                    failed = objects.Deployment.get_by_id(ctx, current.id)
                    self._save(failed, fault=deployments.ENV_SYNC_FAILED,
                               finished_at=timeutils.utcnow())
                return
            self._event(ctx, current, env, action, 'success', receipt)

    def _load_current(self, ctx, operation, resource, kind, action,
                      reservation, receipt):
        try:
            if kind == 'domain':
                # Match API DELETE: service before domain. Loading the domain
                # first inverted the later parent task CAS and DNS saves.
                self._locked_service(ctx, resource.service_id)
            current = type(resource).obj_from_db(
                ctx, resource.repository._get(ctx, resource.id, lock=True))
        except exception.NotFound:
            # The row is gone. Volume delete settles its own holds.
            self._settle_absent(ctx, operation, resource, reservation,
                                receipt)
            return None
        if (coalesced_volume(kind, action) and
                current.status == states.DELETING):
            return None  # Delete completion settles the pending receipts.
        if (not coalesced_volume(kind, action) and
                superseded(current, resource)):
            self._settle_superseded(ctx, operation, current, reservation,
                                    receipt)
            return None
        return current

    def _settle_absent(self, ctx, operation, resource, reservation, receipt):
        if operation not in UNSETTLED_CREATES or not reservation:
            return
        quota.rollback(ctx, reservation)
        self._record_receipt(ctx, resource, 'create', 'failure', receipt)

    def _settle_superseded(self, ctx, operation, current, reservation,
                           receipt):
        """Finish quota when a newer status wins before this cast runs.

        Service create did not reach the driver, so the reservation comes back
        and the receipt stays a failure. The domain row still exists, so
        its reservation is committed.
        """
        if not reservation:
            return
        if operation == 'create_service':
            quota.rollback(ctx, reservation)
            self._record_receipt(ctx, current, 'create', 'failure', receipt)
        elif operation == 'create_domain':
            quota.commit(ctx, reservation)
            self._record_receipt(ctx, current, 'create', 'success', receipt)

    def _record_receipt(self, ctx, resource, action, outcome, receipt):
        if objects.EventList.get_all(ctx, filters={'id': receipt}, limit=1):
            return
        env_id = getattr(resource, 'stack_id', None)
        service_id = getattr(resource, 'service_id', None)
        if not env_id and service_id:
            try:
                env_id = objects.Service.get_by_id(ctx, service_id).stack_id
            except exception.NotFound:
                env_id = None
        event = objects.Event(
            context=ctx, stack_id=env_id,
            resource_type=resource.repository.resource,
            resource_id=resource.id, action=action, outcome=outcome,
            message=FAULT if outcome == 'failure' else None,
            request_id=ctx.request_id)
        event.id = receipt
        event.create()

    def _abort(self, ctx, operation, resource, current, env, action, lock,
               task, reservation, kind, receipt, undo, failure_message=FAULT):
        self._keep_parent_start(current.id, action)
        self._revert(undo, current.id)
        current = self._failure(
            ctx, resource, reservation, coalesced_volume(kind, action),
            failure_message=failure_message)
        if (kind == 'service' and current.kind == 'database' and
                operation in ('create_service', 'rename_service',
                              'delete_service', 'start_service',
                              'stop_service', 'restart_service')):
            self.network_reconciler.defer_cleanup(ctx, env, current)
        if operation == 'create_service':
            self._fail_pending_deployment(ctx, current, failure_message)
        if task and task != states.NONE:
            db_api.release_task_state(ctx, lock.repository.model, lock.id,
                                      expected=task)
        self._release_export_stack(ctx, operation, current, env)
        self._event(ctx, current, env, action, 'failure', receipt,
                    failure_message=failure_message)

    @staticmethod
    def _revert(undo, resource_id):
        for revert in reversed(undo):
            try:
                revert()
            except Exception:
                LOG.warning('Conductor revert failed for %s', resource_id)

    def _failure(self, ctx, resource, reservation, coalesced,
                 failure_message=FAULT):
        # Arbitrary error messages and traceback locals are not safe.
        current = type(resource).get_by_id(ctx, resource.id)
        if 'fault' in current.fields:
            self._save(current, fault=strutils.mask_password(failure_message))
        terminal = self._terminal(current)
        if terminal:
            values = {'status': terminal}
            if getattr(current, 'kind', None) == 'database':
                values['fault'] = failure_message
            self._save(current, **values)
        if coalesced:
            held = self._holds(ctx, current, commit=False)
            if current.size_gb > held:
                self._save(current, size_gb=current.size_gb - held)
        elif reservation:
            quota.rollback(ctx, reservation)
        return current

    def _cloud_create(self, kind, resource, env, undo):
        ref = self.cloud.reference(kind, resource.id)
        undo.append(lambda: self.cloud.delete(ref))
        return self.cloud.create(kind, resource, env.id)

    def _create_stack(self, ctx, env, unused, driver, options, undo):
        self._queue(ctx, env, 'create', 'start')
        self._event(ctx, env, env, 'create', 'start', detail={
            'plan': env.plan,
            'worker_count': len(options.get('workers', [])),
            'build_server': bool(options.get('build_server', False)),
            'controller_image_id': options.get('image')})
        self._save(env, status=states.BUILDING)
        network = self._cloud_create('network', env, env, undo)
        self._save(env, network_id=network)
        undo.append(lambda: driver.delete_stack_project(
            self._endpoint(env), env))
        result = driver.bootstrap_environment(self._endpoint(env), env)
        self._save(env, engine_ref=result.engine_ref,
                   engine_version=self._engine_version(driver, env))
        specs = [('control', 'vm')]
        specs.extend(('worker', item['kind'])
                     for item in options.get('workers', []))
        if options.get('build_server'):
            specs.append(('build', 'vm'))
        counts = {}
        for index, (role, kind) in enumerate(specs):
            counts[role] = counts.get(role, 0) + 1
            server = objects.Server(
                context=ctx, id=str(uuid.uuid5(uuid.UUID(env.id), str(index))),
                stack_id=env.id,
                name=('controller' if role == 'control' else
                      '%s-%s' % (role, counts[role])),
                role=role, kind=kind, status=states.PENDING,
                task_state=states.NONE)
            server.create()
            self._create_server(ctx, server, env, driver, {}, undo)
            self._event(ctx, server, env, 'create', 'success')
            if role == 'control':
                volume = objects.Volume(
                    context=ctx, stack_id=env.id, server_id=server.id,
                    name='controller-data', size_gb=options.get(
                        'data_volume_gb',
                        self.conf.controller_worker.data_volume_size_gb),
                    mount_path='/data', purpose='data', status=states.CREATING)
                volume.create()
                self._create_volume(ctx, volume, env, driver, {}, undo)
                self._event(ctx, volume, env, 'create', 'success')
        self._save(env, status=states.ACTIVE)
        self._queue(ctx, env, 'create', 'end')

    def _create_server(self, ctx, server, env, driver, options, undo):
        self._queue(ctx, server, 'create', 'start')
        self._save(server, status=states.BUILDING)
        compute = self._cloud_create('server', server, env, undo)
        self._cloud_create('mgmt-port', server, env, undo)
        self._cloud_create('env-port', server, env, undo)
        undo.append(lambda: driver.unregister_server(
            self._endpoint(env), env, server))
        result = driver.register_server(self._endpoint(env), env, server, '')
        self._save(server, compute_id=compute, engine_ref=result.engine_ref,
                   status=states.ACTIVE)
        self._queue(ctx, server, 'create', 'end')

    def _holds(self, ctx, resource, commit):
        held = 0
        for event in rows(objects.EventList, ctx, {
                'resource_type': resource.repository.resource,
                'resource_id': resource.id,
                'outcome': 'start'}):
            amount = int(event.message)
            held += amount
            finish = quota.commit if commit else quota.rollback
            finish(ctx, {'volume_gb': amount})
            self._save(event, outcome='success' if commit else 'failure',
                       message=None if commit else FAULT)
        return held

    def _create_volume(self, ctx, volume, env, driver, options, undo,
                       announce=True):
        if announce:
            self._queue(ctx, volume, 'create', 'start')
        ref = self._cloud_create('volume', volume, env, undo)
        self._save(volume, cinder_volume_id=ref)
        if volume.status == states.CREATING:
            self._save(volume, status=states.AVAILABLE)
            if volume.server_id:
                self._save(volume, status=states.IN_USE)
        self._holds(ctx, volume, commit=True)
        if announce:
            self._queue(ctx, volume, 'create', 'end')

    def _extend_volume(self, ctx, volume, env, driver, options, undo):
        self._queue(ctx, volume, 'extend', 'start')
        if (not volume.cinder_volume_id or
                volume.cinder_volume_id not in self.cloud.resources):
            self._create_volume(ctx, volume, env, driver, options, undo,
                                announce=False)
        else:
            self.cloud.extend(volume.cinder_volume_id, volume.size_gb)
            self._holds(ctx, volume, commit=True)
        self._queue(ctx, volume, 'extend', 'end')

    def _snapshot_volume(self, ctx, volume, env, driver, options, undo):
        self._queue(ctx, volume, 'snapshot', 'start')
        self.cloud.snapshot(volume)
        self._save(volume, status=states.IN_USE if volume.server_id else
                   states.AVAILABLE)
        self._queue(ctx, volume, 'snapshot', 'end')

    def _failover_server(self, ctx, server, env, driver, options, undo):
        self._queue(ctx, server, 'failover', 'start')
        driver.register_server(self._endpoint(env), env, server, '')
        self._save(server, status=states.ACTIVE)
        self._queue(ctx, server, 'failover', 'end')

    def _delete_volume(self, ctx, volume, env, driver, options, undo):
        self._queue(ctx, volume, 'delete', 'start')
        if objects.AppVolumeMountList.get_all(
                ctx, filters={'volume_id': volume.id}, limit=1):
            raise exception.Conflict()
        self.cloud.delete(volume.cinder_volume_id)
        held = self._holds(ctx, volume, commit=False)
        if self._accounted(ctx, volume):
            quota.release(ctx, volume_gb=volume.size_gb - held)
        volume.destroy()
        self._queue(ctx, volume, 'delete', 'end')

    def _delete_server(self, ctx, server, env, driver, options, undo):
        self._queue(ctx, server, 'delete', 'start')
        # A live placement still blocks removal of one server. Offboarding
        # keeps the server row, so service and deployment foreign keys stay
        # valid.
        if (env.status != states.DELETING and objects.ServiceList.get_all(
                ctx, filters={'server_id': server.id}, limit=1)):
            raise exception.Conflict()
        for volume in list(rows(objects.VolumeList, ctx,
                                {'server_id': server.id})):
            self._delete_volume(ctx, volume, env, driver, {}, undo)
        driver.unregister_server(self._endpoint(env), env, server)
        for kind in ('server', 'mgmt-port', 'env-port'):
            self.cloud.delete(self.cloud.reference(kind, server.id))
        if self._accounted(ctx, server):
            quota.release(ctx, **quota.deltas('server', server))
        if server.task_state == states.DELETING:
            server.release_task(states.DELETING)
        server.destroy()
        self._queue(ctx, server, 'delete', 'end')

    def _delete_stack(self, ctx, env, unused, driver, options, undo):
        stack_delete.require_empty(ctx, env)
        self._queue(ctx, env, 'delete', 'start')
        if options.get('export_before_delete'):
            # Real teardown exports first in stack_teardown. This
            # fake-cloud path still raises. Its tag sweep would delete
            # the fake export record, so it cannot keep an archive.
            # The API accepts the flag. This cast fails closed.
            raise base.DriverError(
                'Offboarding storage is not implemented')
        for service in rows(objects.ServiceList, ctx, {'stack_id': env.id}):
            managed_dns.delete_service(self.conf, ctx, service)
            driver.delete_app(self._endpoint(env), env, service)
            for reference in app_secrets.references(ctx, service):
                secret_store.call('delete', ctx, reference)
        servers = list(rows(objects.ServerList, ctx,
                            {'stack_id': env.id}))
        driver.delete_stack_project(self._endpoint(env), env)
        for volume in list(rows(objects.VolumeList, ctx,
                                {'stack_id': env.id})):
            self._delete_volume(ctx, volume, env, driver, {}, undo)
        for server in servers:
            self._save(server, status=states.DELETING)
            self._delete_server(ctx, server, env, driver, {}, undo)
        for ref, record in list(self.cloud.resources.items()):
            if 'stack_id=' + env.id in record['tags']:
                self.cloud.delete(ref)
        if self._accounted(ctx, env):
            quota.release(ctx, environments=1)
        self._save(env, status=states.DELETED)
        env.release_task(states.DELETING)
        env.destroy()
        quota.reconcile(ctx)
        self._queue(ctx, env, 'delete', 'end')

    def _stack(self, ctx, resource):
        kind = states.kind(resource.repository.resource)
        if kind == 'stack':
            return resource
        if kind == 'domain':
            service = objects.Service.get_by_id(ctx, resource.service_id)
            env_id = service.stack_id
        elif kind == 'deployment':
            service = objects.Service.get_by_id(ctx, resource.service_id)
            env_id = service.stack_id
        else:
            env_id = resource.stack_id
        if kind in SERVICE_SCOPED:
            return objects.Stack.get_by_id(ctx, env_id)
        return objects.Stack.obj_from_db(
            ctx, db_api.stacks._get(ctx, env_id, lock=True))

    def _operation_lock(self, ctx, operation, current, env):
        """Return the API lock row, or None when a newer operation owns it."""
        kind = states.kind(current.repository.resource)
        if operation == 'cancel_deployment':
            service = self._locked_service(ctx, current.service_id)
            if service.task_state == states.NONE:
                return None
            return service, service.task_state
        expected = PARENT_TASK.get(operation)
        if expected:
            parent = (env if kind == 'export' else
                      self._locked_service(ctx, current.service_id))
            if parent.task_state != expected:
                return None
            return parent, expected
        if operation == 'create_export':
            if env.task_state != states.RECONCILING:
                return None
            return env, states.RECONCILING
        return current, getattr(current, 'task_state', None)

    @staticmethod
    def _locked_service(ctx, service_id):
        return objects.Service.obj_from_db(
            ctx, db_api.services._get(ctx, service_id, lock=True))

    @contextlib.contextmanager
    def _service_write(self, ctx, service):
        # HTTP callers hold only this service; admission takes services in
        # id order before infrastructure. DB-only callers take project first.
        with db_api.writer.using(ctx):
            current = self._locked_service(ctx, service.id)
            if (current.task_state != service.task_state or
                    current.task_owner != service.task_owner or
                    current.task_started_at != service.task_started_at):
                raise exception.Conflict()
            db_api.cas_task_state(ctx, current.repository.model, current.id,
                                  expected=current.task_state,
                                  new=current.task_state)
            yield current

    @staticmethod
    def _hold_stack(kind, lock, env):
        """Take the stack lock when this operation does not already."""
        if (kind in ('stack', 'export') or lock is env or
                kind in SERVICE_SCOPED):
            return False
        env.acquire_task(states.RECONCILING)
        return True

    @staticmethod
    def _release_owned_task(ctx, action, kind, lock, task):
        # Delete releases its own row before the soft-delete hides it.
        if (task and task != states.NONE and not (
                action == 'delete' and kind in
                ('stack', 'server', 'service'))):
            db_api.release_task_state(ctx, lock.repository.model, lock.id,
                                      expected=task)

    def _release_export_stack(self, ctx, operation, export, env):
        if operation != 'delete_export':
            return
        self._release_preheld_export_lock(ctx, export, env)

    def _export_create_settled(self, ctx, export):
        found = objects.EventList.get_all(ctx, filters={
            'resource_id': export.id, 'resource_type': 'export',
            'action': 'create'}, limit=5)
        return any(row.outcome in ('success', 'failure') for row in found)

    def _release_preheld_export_lock(self, ctx, export, env):
        # An in-flight create still holds RECONCILING. A finished export
        # must not release a lock that a later operation now holds.
        if self._export_create_settled(ctx, export):
            return
        fresh = objects.Stack.get_by_id(ctx, env.id)
        if fresh.task_state == states.RECONCILING:
            fresh.release_task(states.RECONCILING)

    @staticmethod
    def _terminal(resource):
        kind = states.kind(resource.repository.resource)
        target = states.FAILED if kind == 'deployment' else states.ERROR
        allowed = states.TRANSITIONS.get(kind, {}).get(resource.status, ())
        if target in allowed:
            return target
        return None

    def _fail_pending_deployment(self, ctx, service, failure_message=FAULT):
        pending = objects.DeploymentList.get_all(
            ctx, filters={'service_id': service.id, 'status': states.PENDING},
            sort_key='number', sort_dir='desc', limit=1)
        if not pending:
            return
        row = pending[0]
        self._save(row, status=states.FAILED,
                   fault=strutils.mask_password(failure_message))
        env = objects.Stack.get_by_id(ctx, service.stack_id)
        self._event(ctx, row, env, 'create', 'failure',
                    failure_message=failure_message)

    def _known(self, driver, env, service):
        try:
            driver.get_app_status(self._endpoint(env), env, service)
        except base.EngineNotFound:
            return False
        return True

    def _variables(self, ctx, service):
        # Values are passed to the driver and must not be logged.
        values = {}
        for row in rows(objects.SecretList, ctx, {'service_id': service.id}):
            values[row.key] = base.EnvVar(
                value=secret_store.call('get', ctx, row.value_ref),
                is_secret=row.is_secret, build_time=row.build_time)
        return values

    def _apply_file_mounts(self, ctx, endpoint, env, driver, service):
        if service.file_mount_secret_refs:
            driver.set_file_mounts(endpoint, env, service, [
                base.FileMount(id=row.id, container_path=row.container_path,
                               content=secret_store.call(
                                   'get', ctx, row.content_ref),
                               resource_name=row.resource_name)
                for row in file_mounts.rows(ctx, service.id)])

    def _log_masks(self, ctx, service):
        masks = self._variables(ctx, service)
        if getattr(service, 'kind', None) == 'database':
            for key in ('password_ref', 'root_password_ref'):
                reference = getattr(service, key)
                if reference:
                    masks[key] = base.EnvVar(
                        value=secret_store.call('get', ctx, reference),
                        is_secret=True)
        for index, ref in enumerate(service.file_mount_secret_refs or []):
            content = secret_store.call('get', ctx, ref)
            for line, value in enumerate([content, *content.splitlines()]):
                masks['file_mount:%s:%s' % (index, line)] = base.EnvVar(
                    value=value, is_secret=True)
        return masks

    def _mounts(self, ctx, service):
        mounts = []
        for row in rows(objects.StorageList, ctx, {'service_id': service.id}):
            if (row.origin != 'api' or row.status != states.IN_USE or
                    not row.container_path):
                continue
            mounts.append(base.Mount(
                name=row.name, container_path=row.container_path,
                type=row.type, host_path=row.host_path,
                read_only=row.read_only, id=row.id))
        return mounts

    def _serving(self, ctx, service):
        """HTTP readiness of the default URL; True when not checkable."""
        code = self._probe(ctx, service)
        return code is None or 200 <= code < 500

    def _worker_address(self, ctx, service):
        if not service.server_id:
            return None
        try:
            server = objects.Server.get_by_id(ctx, service.server_id)
        except exception.NotFound:
            return None
        return server.floating_ip if server is not None else None

    def _computed_host(self, ctx, service):
        if not service.ports and not service.is_engine_service:
            return None
        if naming.optional_attr(service, 'default_dns_zone_id'):
            return naming.optional_attr(service, 'default_domain')
        return naming.default_hostname(
            service.name, service.id, self.conf.domains.default_suffix,
            self._worker_address(ctx, service))

    def _advertised_host(self, ctx, service):
        if naming.optional_attr(service, 'default_domain_enabled') is False:
            return None
        if not service.ports and not service.is_engine_service:
            return None
        stored = naming.optional_attr(service, 'default_domain')
        if stored:
            return stored
        return self._computed_host(ctx, service)

    def _custom_urls(self, ctx, service):
        if not service.ports and not service.is_engine_service:
            return []
        urls = []
        for domain in rows(objects.DomainList, ctx,
                           {'service_id': service.id}):
            if domain.status in PUBLISHED:
                urls.append(naming.domain_url(domain.fqdn, domain.tls))
        return urls

    def _fqdns_for(self, ctx, service, host):
        if not service.ports and not service.is_engine_service:
            return []
        names = []
        if (host and naming.optional_attr(
                service, 'default_domain_enabled') is not False):
            names.append('https://' + host)
        names.extend(self._custom_urls(ctx, service))
        return names

    def _probe(self, ctx, service):
        """HTTP status of the default URL; 0 = no answer, None = unchecked."""
        if service.health_check == 'none':
            return None
        if not self.conf.conductor.http_check or not service.ports and (
                not service.is_engine_service):
            return None
        host = self._advertised_host(ctx, service)
        address = self._worker_address(ctx, service)
        if not host or not address:
            return None
        path = service.healthcheck_path or '/'
        if path != '/':
            return domain_check.http_status(host, address, path=path)
        return domain_check.http_status(host, address)

    def _fqdns(self, ctx, service):
        return self._fqdns_for(
            ctx, service, self._advertised_host(ctx, service))

    def _engine_domains(self, ctx, service, env, driver, old, host):
        endpoint = self._endpoint(env)
        current = driver.list_domains(endpoint, env, service)
        if current is None:
            return endpoint, None, self._fqdns_for(ctx, service, host)
        if not service.ports and not service.is_engine_service:
            return endpoint, current, []
        route_host = (None if naming.optional_attr(
            service, 'default_domain_enabled') is False else host)
        desired = naming.replace_default_domain(
            current, old, route_host, service.name, service.id,
            self.conf.domains.default_suffix)
        present = {naming.hostname_of(item) for item in desired}
        for url in self._custom_urls(ctx, service):
            key = naming.hostname_of(url)
            if key and key not in present:
                desired.append(url)
                present.add(key)
        return endpoint, current, desired

    def _domains_differ(self, endpoint, env, driver, service, current,
                        desired):
        if current is None:
            compared = driver.compare_settings(
                endpoint, env, service, {}, desired)
            return compared.get('domains') is not False
        return naming.domain_set(current) != naming.domain_set(desired)

    def _sync_default_domain(self, ctx, service, env, driver, dry_run=False,
                             fenced=False):
        """Point the default hostname at the current floating IP.

        Custom names and their schemes stay. The call does not deploy.
        """
        old = naming.optional_attr(service, 'default_domain')
        host = self._computed_host(ctx, service)
        endpoint = current = None
        desired = self._fqdns_for(ctx, service, host)
        if service.engine_ref:
            endpoint, current, desired = self._engine_domains(
                ctx, service, env, driver, old, host)
            differ = self._domains_differ(
                endpoint, env, driver, service, current, desired)
        else:
            differ = False
        changed = differ or host != old
        if not dry_run:
            if fenced:
                self._refresh_domain_write(
                    ctx, service, env, driver, endpoint, host, old,
                    desired, differ)
            else:
                managed_dns.ensure_default(self, ctx, service, env, host)
                if differ:
                    driver.set_domains(endpoint, env, service, desired)
                if host != old:
                    self._save(service, default_domain=host)
        return {'service_id': service.id, 'default_domain': host,
                'changed': changed, 'ok': True}

    def _refresh_domain_write(self, ctx, service, env, driver, endpoint,
                              host, old, desired, differ):
        # Repository saves/events lock parents; do these before the HTTP fence.
        with self._domain_write(ctx, service, env):
            managed_dns.ensure_default(self, ctx, service, env, host)
            if host != old:
                self._save(service, default_domain=host)
        if differ:
            with self._domain_write(ctx, service, env, engine=True):
                driver.set_domains(endpoint, env, service, desired)

    def refresh_default_domains(self, ctx, service_id=None, dry_run=False):
        """Update stored and engine default hostnames. Do not redeploy."""
        # Operator-wide maintenance: always read across projects as admin.
        if not getattr(ctx, 'is_admin', False):
            ctx = context_module.RequestContext(is_admin=True)
        if service_id:
            found = [objects.Service.get_by_id(
                ctx, service_id, all_projects=True)]
        else:
            found = list(rows(
                objects.ServiceList, ctx, {}, all_projects=True))
        report = []
        for service in found:
            report.append(self._refresh_one(ctx, service, dry_run))
        return report

    def _refresh_one(self, ctx, service, dry_run):
        try:
            # Later lookups are project scoped; act inside the owner project.
            ctx = context_module.RequestContext(
                project_id=service.project_id, is_admin=True)
            service = objects.Service.get_by_id(ctx, service.id)
            env = objects.Stack.get_by_id(ctx, service.stack_id)
            driver = self._driver(env)
            if dry_run:
                return self._sync_default_domain(
                    ctx, service, env, driver, dry_run=True)
            return self._sync_default_domain(
                ctx, service, env, driver, fenced=True)
        except Exception:
            LOG.exception('Default domain refresh failed for %s', service.id)
            return {'service_id': service.id, 'default_domain': None,
                    'changed': False, 'ok': False}

    def _activate_deployment(self, ctx, deployment, service, env, driver,
                             rollback):
        endpoint = self._endpoint(env)
        self._push_compose(ctx, driver, endpoint, env, service, deployment)
        self._sync_default_domain(ctx, service, env, driver)
        try:
            driver.set_env(endpoint, env, service,
                           self._variables(ctx, service))
        except (db_exception.DBDeadlock, base.EngineRateLimited):
            raise
        except Exception:
            raise deployments.EnvSyncFailed(
                deployments.ENV_SYNC_FAILED) from None
        driver.set_volume_mounts(
            endpoint, env, service, self._mounts(ctx, service))
        self._apply_file_mounts(ctx, endpoint, env, driver, service)
        self._save(deployment, status=states.DEPLOYING,
                   started_at=timeutils.utcnow())
        if rollback:
            target = objects.Deployment.get_by_id(ctx, deployment.rollback_of)
            # Pin the driver input without changing the stored history.
            if target.resolved_ref:
                target.source_ref = target.resolved_ref
            result = driver.rollback(
                endpoint, env, service, target, deployment)
        else:
            result = driver.deploy_app(endpoint, env, service, deployment)
        observed = driver.get_deployment_status(
            endpoint, env, service, deployment)
        if observed.status != states.ACTIVE:
            raise base.DriverError('Deployment did not complete')
        previous_id = service.current_deployment_id
        if previous_id and previous_id != deployment.id:
            previous = objects.Deployment.get_by_id(ctx, previous_id)
            follow = states.ROLLED_BACK if rollback else states.SUPERSEDED
            if follow in states.TRANSITIONS['deployment'].get(
                    previous.status, ()):
                self._save(previous, status=follow)
        self._save(deployment, status=states.ACTIVE,
                   engine_ref=result.engine_ref,
                   resolved_ref=(getattr(observed, 'resolved_ref', None) or
                                 result.extra.get('resolved_ref')),
                   finished_at=timeutils.utcnow())
        self._save(service, status=states.ACTIVE,
                   current_deployment_id=deployment.id)

    def _set_service_parents(self, ctx, service):
        # Q94: the Coolify project is the appgroup, not the stack.
        # The Coolify environment is the environment row, not "production".
        # Not service columns. Git, image, and template create read them.
        if not service.environment_id:
            if service.template_id:
                raise base.DriverError(
                    'Engine service parent reference is missing')
            return
        environment = objects.Environment.get_by_id(
            ctx, service.environment_id)
        appgroup = objects.Appgroup.get_by_id(
            ctx, environment.appgroup_id)
        if not appgroup.engine_ref or not environment.engine_ref:
            raise base.DriverError(
                'Engine service parent reference is missing')
        service.project_engine_ref = appgroup.engine_ref
        service.environment_engine_ref = environment.engine_ref

    def _create_database(self, ctx, service, env, driver, options, undo):
        self._queue(ctx, service, 'create', 'start', 'database')
        self.network_reconciler.apply(ctx, env)
        self._set_service_parents(ctx, service)
        server = objects.Server.get_by_id(ctx, service.server_id)
        service.server_engine_ref = server.engine_ref
        service.server_hostname = server.hostname
        credentials = dict(username=service.db_username,
                           database=service.db_name)
        for key in ('password', 'root_password'):
            reference = getattr(service, key + '_ref')
            if reference:
                credentials[key] = secret_store.call('get', ctx, reference)
        result = driver.create_database(
            self._endpoint(env), env, service, credentials)
        self._save(service, engine_ref=result.engine_ref,
                   status=states.STARTING if env.driver != 'noop'
                   else states.ACTIVE)
        self.network_reconciler.apply(ctx, env)
        self._queue(ctx, service, 'create', 'end', 'database')

    def _create_service(self, ctx, service, env, driver, options, undo):
        if getattr(service, 'kind', None) == 'database':
            return self._create_database(
                ctx, service, env, driver, options, undo)
        self._queue(ctx, service, 'create', 'start')
        if service.template_id:
            # The id is on the service row. The source row has only the type.
            source = {'type': 'template',
                      'template_id': service.template_id}
        else:
            source = objects.Source.get_by_id(ctx, service.source_id)
        if getattr(source, 'type', None) in ('dockerfile', 'compose'):
            field = source.type
            setattr(source, field, secret_store.call(
                'get', ctx, getattr(service, field + '_ref')))
        self._set_service_parents(ctx, service)
        endpoint = self._endpoint(env)
        undo.append(lambda: driver.delete_app(endpoint, env, service))
        if service.server_id:
            # Not a service column. create_service reads this recorded uuid.
            server = objects.Server.get_by_id(ctx, service.server_id)
            service.server_engine_ref = server.engine_ref
            service.server_hostname = server.hostname
        credentials = None
        if not service.template_id and source.credentials_ref:
            credentials = secret_store.call('get', ctx, source.credentials_ref)
        result = driver.create_app(endpoint, env, service, source, credentials)
        self._save(service, engine_ref=result.engine_ref)
        driver.set_env(
            endpoint, env, service, self._variables(ctx, service))
        mounts = self._mounts(ctx, service)
        if mounts or not service.is_engine_service:
            driver.set_volume_mounts(endpoint, env, service, mounts)
        if service.ports or service.is_engine_service:
            managed_dns.ensure_default(
                self, ctx, service, env, self._advertised_host(ctx, service))
            driver.set_domains(
                endpoint, env, service, self._fqdns(ctx, service))
        pending = objects.DeploymentList.get_all(
            ctx, filters={'service_id': service.id, 'status': states.PENDING},
            sort_key='number', sort_dir='desc', limit=1)
        if pending:
            deployment = pending[0]
            if env.driver != 'noop':
                # The engine service now exists: account for it even if its
                # initial deployment fails. Commit the enqueue checkpoint
                # with service creation, then leave this transaction before
                # POST.
                self._save(service, status=states.STOPPED)
                receipt = rpcapi.receipt_id('create_deployment', deployment)
                options['_enqueue'] = deployments.prepare(
                    self, ctx, deployment, env, receipt, show=True)
                self._queue(ctx, service, 'create', 'end')
                return
            self._queue(ctx, deployment, 'create', 'start')
            self._activate_deployment(
                ctx, deployment, service, env, driver, False)
            self._queue(ctx, deployment, 'create', 'end')
            self._event(ctx, deployment, env, 'create', 'success')
        else:
            self._save(service, status=states.STOPPED)
            if env.driver != 'noop':
                cancelled = objects.DeploymentList.get_all(
                    ctx, filters={'service_id': service.id,
                                  'status': states.CANCELLED}, limit=1)
                if cancelled:
                    self._save(cancelled[0], finished_at=timeutils.utcnow())
                    self._event(ctx, cancelled[0], env, 'create', 'failure')
                    self._event(ctx, cancelled[0], env, 'cancel', 'success')
        self._queue(ctx, service, 'create', 'end')

    def _update_service(self, ctx, service, env, driver, options, undo):
        if not service.is_engine_service or not service.engine_ref:
            raise base.DriverError("Template service reference is required")
        driver.set_env(self._endpoint(env), env, service,
                       self._variables(ctx, service))
        self._queue(ctx, service, 'update', 'end')

    def _cancel_engine_deployments(self, ctx, endpoint, env, service,
                                   driver):
        """Best effort: stop engine work for deployments delete cancelled."""
        for deployment in rows(objects.DeploymentList, ctx,
                               {'service_id': service.id,
                                'status': states.CANCELLED}):
            if not deployment.engine_ref or deployment.finished_at:
                continue
            try:
                driver.cancel_deployment(endpoint, env, service, deployment)
            except Exception:
                LOG.warning('Engine cancel failed for deployment %s; the '
                            'app delete continues.', deployment.id)

    def _delete_service(self, ctx, service, env, driver, options, undo):
        if service.kind == 'database':
            self.network_reconciler.apply(ctx, env)
        managed_dns.delete_service(self.conf, ctx, service)
        backups.drop_service(self.conf, ctx, service)
        self._queue(ctx, service, 'delete', 'start')
        source = (objects.Source.get_by_id(ctx, service.source_id)
                  if service.source_id else None)
        if source and source.type == 'git' and source.credentials_ref:
            # Retain retry owners until engine key cleanup succeeds, even
            # when the stack is in ERROR.
            self._delete_service_engine(ctx, service, env)
        else:
            self._delete_engine_cleanup(
                env, lambda: self._delete_service_engine(ctx, service, env))
        for domain in list(rows(objects.DomainList, ctx,
                                {'service_id': service.id})):
            if self._committed(ctx, domain):
                quota.release(ctx, domains=1)
            domain.destroy()
        # Keep every reference discoverable until all deletes succeed.
        for ref in app_secrets.references(ctx, service):
            secret_store.call('delete', ctx, ref)
        # Clear the live deployment FK before retiring deployment history.
        self._save(service, status=states.DELETING,
                   current_deployment_id=None)
        for deployment in list(rows(objects.DeploymentList, ctx,
                                    {'service_id': service.id})):
            deployment.destroy()
        if self._committed(ctx, service):
            quota.release(ctx, apps=1)
        file_mounts.drop(ctx, service, delete_secrets=False)
        storage_rules.drop_service_storages(ctx, service)
        if service.task_state == states.DELETING:
            service.release_task(states.DELETING)
        service.destroy()
        self._queue(ctx, service, 'delete', 'end')

    def _delete_service_engine(self, ctx, service, env):
        driver = self._driver(env)
        if getattr(service, 'kind', None) == 'database':
            return driver.delete_database(self._endpoint(env), env, service)
        endpoint = self._endpoint(env)
        if service.engine_ref and self._known(driver, env, service):
            driver.set_domains(endpoint, env, service, [])
        self._cancel_engine_deployments(ctx, endpoint, env, service, driver)
        # A failed create may have no ref, or an uncertain engine outcome.
        # Idempotent deletion can still find the deterministic engine name.
        driver.delete_app(endpoint, env, service)
        self.collect_registry(
            ctx, env, [registry.repository(env.id, service.id)], strict=True)

    def _set_service_status(self, ctx, service, env, driver, status, method):
        action = ACTION.get()
        self._queue(ctx, service, action, 'start')
        # STARTING is settled by the periodic deployment poll, without the
        # project lock. Never look now: engine status lags a restart.
        configuration_ready = True
        if service.kind == 'database' and status == states.STARTING:
            configuration_ready = driver.update_database(
                self._endpoint(env), env, service) is not None
        getattr(driver, method)(self._endpoint(env), env, service)
        if (service.kind == 'database' and status == states.STARTING and
                configuration_ready):
            self._save(service, settings_changed_at=None)
        if service.kind == 'database' and status == states.STARTING:
            deployments.checkpoint_database_start(ctx, service, action)
        self._save(service, status=status)
        if service.kind == 'database':
            self.network_reconciler.apply(ctx, env)
        self._queue(ctx, service, action, 'end')

    def _start_service(self, ctx, service, env, driver, options, undo):
        # ACTIVE only once the engine reports running (deployments.sweep).
        self._set_service_status(ctx, service, env, driver, states.STARTING,
                                 'start_app')

    def _stop_service(self, ctx, service, env, driver, options, undo):
        self._set_service_status(ctx, service, env, driver, states.STOPPED,
                                 'stop_app')

    def _restart_service(self, ctx, service, env, driver, options, undo):
        self._set_service_status(ctx, service, env, driver, states.STARTING,
                                 'restart_app')

    def _create_deployment(self, ctx, deployment, env, driver, options, undo):
        self._queue(ctx, deployment, 'create', 'start')
        service = self._locked_service(ctx, deployment.service_id)
        self._activate_deployment(ctx, deployment, service, env, driver, False)
        self._queue(ctx, deployment, 'create', 'end')

    def _rollback_deployment(self, ctx, deployment, env, driver, options,
                             undo):
        self._queue(ctx, deployment, 'rollback', 'start')
        service = self._locked_service(ctx, deployment.service_id)
        self._activate_deployment(ctx, deployment, service, env, driver, True)
        self._queue(ctx, deployment, 'rollback', 'end')

    def _cancel_deployment(self, ctx, deployment, env, driver, options, undo):
        self._queue(ctx, deployment, 'cancel', 'start')
        if deployment.engine_ref:
            service = objects.Service.get_by_id(ctx, deployment.service_id)
            driver.cancel_deployment(
                self._endpoint(env), env, service, deployment)
        self._queue(ctx, deployment, 'cancel', 'end')

    def _create_domain(self, ctx, domain, env, driver, options, undo):
        # An unverified name is not published. skip_verification publishes.
        self._queue(ctx, domain, 'create', 'start')
        service = objects.Service.get_by_id(ctx, domain.service_id)
        if not managed_dns.ensure_domain(self, ctx, service, env, domain):
            self._queue(ctx, domain, 'create', 'end')
            return
        publish = (domain.status == states.ACTIVE or
                   self.conf.domains.skip_verification)
        if publish:
            self._publish_domain(ctx, domain, env, driver)
        self._queue(ctx, domain, 'create', 'end')

    def _delete_domain(self, ctx, domain, env, driver, options, undo):
        self._queue(ctx, domain, 'delete', 'start')
        service = objects.Service.get_by_id(ctx, domain.service_id)
        managed_dns.delete_domain(self.conf, service, domain)
        if self._known(driver, env, service):
            names = [name for name in self._fqdns(ctx, service)
                     if naming.hostname_of(name) != domain.fqdn.lower()]
            driver.set_domains(self._endpoint(env), env, service, names)
        if self._committed(ctx, domain):
            quota.release(ctx, domains=1)
        domain.destroy()
        self._queue(ctx, domain, 'delete', 'end')

    def _verify_domain(self, ctx, domain, env, driver, options, undo):
        # The API records the TXT result. Publish a verified name now.
        # drive_domains still polls names that did not match yet.
        self._queue(ctx, domain, 'verify', 'start')
        if domain.status == states.ERROR:
            self._save(domain, status=states.PENDING_VERIFICATION)
        elif domain.status == states.VERIFIED:
            self._publish_domain(ctx, domain, env, driver)
        self._queue(ctx, domain, 'verify', 'end')

    def _update_domain(self, ctx, domain, env, driver, options, undo):
        self._queue(ctx, domain, 'update', 'end')
        service = objects.Service.get_by_id(ctx, domain.service_id)
        managed_dns.ensure_domain(self, ctx, service, env, domain)
        if self._known(driver, env, service):
            driver.set_domains(
                self._endpoint(env), env, service,
                self._fqdns(ctx, service))

    def _rename_service(self, ctx, service, env, driver, options, undo):
        self._queue(ctx, service, 'rename', 'end')
        if getattr(service, 'kind', None) == 'database':
            self.network_reconciler.apply(ctx, env)
            restarting = driver.update_database(
                self._endpoint(env), env, service)
            if restarting:
                deployments.checkpoint_database_start(ctx, service)
            if restarting:
                self._save(service, settings_changed_at=None, **(
                    {'status': states.STARTING} if restarting else {}))
                # ERROR rows contribute no rule until STARTING is saved.
                self.network_reconciler.apply(ctx, env)
        else:
            driver.update_metadata(self._endpoint(env), env, service)

    def _publish_domain(self, ctx, domain, env, driver):
        # Development mode applies the name now. A verified name still
        # waits for routing unless that option is on.
        if (domain.status != states.ACTIVE and
                self.conf.domains.skip_verification):
            self._save(domain, status=states.ACTIVE,
                       verified_at=domain.verified_at or timeutils.utcnow())
        service = objects.Service.get_by_id(ctx, domain.service_id)
        known = self._known(driver, env, service)
        if domain.status == states.VERIFIED:
            if known:
                driver.set_domains(
                    self._endpoint(env), env, service,
                    self._fqdns(ctx, service))
            self._save(domain, status=states.PENDING_DNS)
            return
        if domain.status == states.ACTIVE and known:
            driver.set_domains(
                self._endpoint(env), env, service,
                self._fqdns(ctx, service))

    def _create_export(self, ctx, export, env, driver, options, undo):
        # Noop records completion only. It does not store an archive or URL.
        self._queue(ctx, export, 'create', 'start')
        self._cloud_create('export', export, env, undo)
        for status in (states.SNAPSHOTTING, states.UPLOADING, states.ACTIVE):
            self._save(export, status=status)
        self._queue(ctx, export, 'create', 'end')

    def _delete_export(self, ctx, export, env, driver, options, undo):
        self._queue(ctx, export, 'delete', 'start')
        self.cloud.delete(self.cloud.reference('export', export.id))
        self._release_preheld_export_lock(ctx, export, env)
        export.destroy()
        self._queue(ctx, export, 'delete', 'end')
