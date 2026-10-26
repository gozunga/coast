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

"""Ordered stack Taskflow with durable refs and explicit revert."""

import json
import uuid

from oslo_log import log as logging
from oslo_utils import timeutils
from taskflow import engines
from taskflow.patterns import linear_flow
from taskflow import task

from coast.api import secret_store
from coast.common import exception
from coast.common import naming
from coast.common import quota
from coast.common import safe_log
from coast.common import secrets
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import controller
from coast.conductor import registry
from coast.conductor import rpcapi
from coast.db import api as db_api
from coast.drivers import loader
from coast.network import stack as network
from coast import objects

LOG = logging.getLogger(__name__)
FAULT = 'Stack provisioning failed; operator recovery may be required.'


class Step(task.Task):
    def __init__(self, flow, name, prefix=''):
        super().__init__(name=prefix + name)
        self.method = name
        self.flow = flow

    def execute(self):
        try:
            getattr(self.flow, self.method)()
        except Exception as exc:
            self._log_failure(exc)
            if isinstance(exc, exception.GuestNetworkError):
                self.flow.failure_fault = type(exc).msg_fmt
                raise type(exc)() from None
            # Taskflow Failure objects must never contain SDK/agent secrets.
            raise exception.CoastException(FAULT) from None

    def _log_failure(self, exc):
        ctx = getattr(self.flow, 'ctx', None)
        stack = getattr(getattr(self.flow, 'stack', None), 'id', None)
        request_id = getattr(ctx, 'request_id', None)
        if getattr(self.flow, 'failure_kind', 'stack') == 'server':
            safe_log.log_failure(
                LOG,
                'Server %(server)s stack %(stack)s step %(step)s failed: '
                '%(cause)s at %(where)s request_id=%(request_id)s',
                exc,
                server=getattr(
                    getattr(self.flow, 'server', None), 'id', None),
                stack=stack, step=self.name, request_id=request_id)
            return
        safe_log.log_failure(
            LOG,
            'Stack %(stack)s step %(step)s failed: %(cause)s at %(where)s '
            'request_id=%(request_id)s',
            exc, stack=stack, step=self.name, request_id=request_id)

    def revert(self, **kwargs):
        method = getattr(self.flow, 'revert_' + self.method, None)
        if method:
            try:
                method()
            except Exception:
                # Keep checkpoints and tagged resources for operator cleanup.
                LOG.warning('Stack step revert failed: %s', self.name)


class CreateStack:
    def __init__(self, owner):
        # Notification buffers are private to this RPC operation.
        from coast.conductor import manager
        self.owner = manager.Manager(owner.conf, notifier=owner.notifier)
        self.owner.drivers = owner.drivers
        self.owner._pending = []
        self.conf = owner.conf
        self.volumes = []
        self.server = None
        self.guest_removed = True
        self.extra = {}
        self.bootstrap_result = None
        self.initial_servers = []
        self.failure_fault = None

    def flush(self):
        pending = self.owner._pending
        self.owner._pending = []
        self.owner._seen = set()
        self.owner._flush(pending)

    def save(self, resource, **values):
        # Object/repository writes each have their own short transaction.
        self.owner._save(resource, **values)

    def execute(self, ctx, resource, options, reservation, revision):
        self.ctx = ctx
        self.options = options or {}
        self.reservation = reservation or {}
        self.receipt = rpcapi.receipt_id(
            'create_stack', resource, revision)
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            self.stack = objects.Stack.obj_from_db(
                ctx, db_api.stacks._get(ctx, resource.id, lock=True))
            if (self.stack.status != states.PENDING or
                    self.stack.task_state != states.PROVISIONING):
                return  # Duplicates cannot run a second cloud/bootstrap flow.
            self.save(self.stack, status=states.BUILDING,
                      conductor_host=self.conf.host)
            quota.commit(ctx, self.reservation)
            self.owner._event(ctx, self.stack, self.stack, 'create', 'start',
                              self.receipt, detail={
                                  'plan': self.stack.plan,
                                  'worker_count': len(self.options.get(
                                      'workers', [])),
                                  'build_server': bool(self.options.get(
                                      'build_server', False)),
                                  'controller_image_id': self.options.get(
                                      'image')})
        self.flush()
        try:
            self.prepare()
            flow = linear_flow.Flow('stack-create').add(*[
                Step(self, name) for name in (
                    'network', 'credentials', 'data_volume', 'docker_volume',
                    'registry_volume', 'boot', 'active', 'health', 'bootstrap',
                    'token', 'engine', 'controller_ready')])
            for child in self.initial_servers:
                flow.add(*[Step(child, name, child.server.id + '-')
                           for name in ('prepare_initial',) +
                           child.steps[:-1]])
            flow.add(Step(self, 'complete'))
            # No sensitive return values or persistent Taskflow payloads.
            engines.run(flow, engine='serial')
        except Exception as exc:
            if isinstance(exc, exception.GuestNetworkError):
                self.failure_fault = type(exc).msg_fmt
            safe_log.log_failure(
                LOG,
                'Stack %(stack)s create failed: %(cause)s at %(where)s '
                'request_id=%(request_id)s',
                exc, stack=getattr(self.stack, 'id', None),
                request_id=getattr(self.ctx, 'request_id', None))
            # Discard notifications queued by a rolled-back final transaction.
            for operation in [self] + self.initial_servers:
                operation.owner._pending = []
                operation.owner._seen = set()
            self.finish(False)
        finally:
            self.bootstrap_result = None

    def prepare(self):
        if (self.conf.coolify.insecure_direct_url or
                self.stack.driver not in
                self.conf.controller_worker.enabled_drivers):
            raise exception.Invalid('Production provisioning is required')
        self.prepare_requests()
        options = self.conf.controller_worker
        if not all((options.mgmt_net_id, options.agent_client_ca,
                    options.health_manager_ips)):
            raise exception.Invalid('Controller management config is required')
        self.cloud = controller.ControllerCloud(self.conf, self.stack)
        image_id, flavor_id = self.cloud.image_flavor(
            self.options.get('image'))
        self.driver = loader.load_driver(self.stack.driver, self.conf)
        if (self.stack.engine_version not in
                self.driver.SUPPORTED_ENGINE_VERSIONS):
            raise exception.Invalid('Unsupported controller engine version')
        identifier = str(uuid.uuid5(uuid.UUID(self.stack.id), '0'))
        server = objects.Server(
            context=self.ctx, id=identifier, stack_id=self.stack.id,
            name='controller', role='control', kind='vm',
            status=states.PENDING,
            task_state=states.NONE, image_id=image_id, flavor_id=flavor_id)
        naming.assign_server_hostname(
            self.ctx, self.stack, server,
            [child.server.hostname for child in self.initial_servers],
            conf=self.conf)
        server.create()
        self.server = server
        self.save(self.server, status=states.BUILDING)
        self.owner._queue(self.ctx, self.server, 'create', 'start')
        self.flush()

    def prepare_requests(self):
        from coast.conductor import server_add
        requests = []
        if self.options.get('build_server'):
            plans = [plan['name'] for plan in
                     json.loads(self.conf.plans.definitions)
                     if plan['kind'] == 'vm' and 'build' in plan['roles'] and
                     (not self.options.get('build_server_plan') or
                      plan['name'] == self.options['build_server_plan'])]
            if len(plans) != 1:
                raise exception.Invalid(
                    'Exactly one build VM plan is required')
            request = dict(kind='vm', role='build', plan=plans[0])
            if 'build_server_image' in self.options:
                request['image'] = self.options['build_server_image']
            requests.append(request)
        requests.extend(dict(request, role='worker')
                        for request in self.options.get('workers', []))
        counts = {}
        for index, request in enumerate(requests, 1):
            # IDs keep the stack-wide index; names count per role (Q152).
            counts[request['role']] = counts.get(request['role'], 0) + 1
            child = server_add.AddServer(self.owner)
            child.initial = True
            child.ctx = self.ctx
            child.stack = self.stack
            child.options = request
            child.reservation = {}
            child.server = objects.Server(
                context=self.ctx,
                id=str(uuid.uuid5(uuid.UUID(self.stack.id), str(index))),
                stack_id=self.stack.id,
                name='%s-%s' % (request['role'], counts[request['role']]),
                role=request['role'], kind=request['kind'],
                status=states.PENDING, task_state=states.NONE)
            naming.assign_server_hostname(
                self.ctx, self.stack, child.server,
                [item.server.hostname for item in self.initial_servers],
                conf=self.conf)
            child.receipt = rpcapi.receipt_id(
                'create_server', child.server, self.receipt)
            self.initial_servers.append(child)

    def controller_ready(self):
        self.save(self.server, status=states.ACTIVE)
        self.owner._queue(self.ctx, self.server, 'create', 'start')
        self.flush()

    def workers_removed(self):
        return all(child.guest_removed for child in self.initial_servers)

    def network(self):
        self.network_step = network.EnvironmentNetwork(self.conf)
        result = self.network_step.create(
            self.stack.project_id, self.stack.id,
            controller.controller_ip(self.conf))
        self.groups = result['security_groups']
        self.save(self.stack, network_id=result['network_id'],
                  subnet_id=result['subnet_id'], router_id=result['router_id'],
                  sg_ingress_id=self.groups['worker'],
                  sg_mgmt_id=self.groups['agent-mgmt'])
        self.ports = [self.cloud.port(
            self.server, 'mgmt', self.conf.controller_worker.mgmt_net_id,
            self.groups['agent-mgmt']), self.cloud.port(
                self.server, 'env', self.stack.network_id, self.groups['ctrl'],
                self.stack.subnet_id)]
        address = self.ports[0].fixed_ips[0]['ip_address']
        self.save(self.server, mgmt_ip=address,
                  private_ip=controller.controller_ip(self.conf))
        self.agent = agent_client.AgentClient(self.conf, address)

    def revert_network(self):
        if not self.guest_removed or not self.workers_removed():
            return
        self.cloud.delete_ports(self.server)
        self.network_step.delete(self.stack.project_id, self.stack.id)
        self.save(self.stack, network_id=None, subnet_id=None, router_id=None,
                  sg_ingress_id=None, sg_mgmt_id=None)

    def checkpoint_secret(self, field, reference, **values):
        try:
            self.save(self.stack, **{field: reference}, **values)
        except Exception:
            secret_store.call('delete', self.ctx, reference)
            raise

    def credentials(self):
        if not self.stack.ssh_key_ref:
            key = secrets.create_ssh_keypair(
                self.ctx, secret_store.SecretIdentity.for_resource(
                    self.stack, 'ssh-key'))
            self.checkpoint_secret('ssh_key_ref', key['ssh_key_ref'],
                                   ssh_public_key=key['ssh_public_key'])
        if not self.stack.heartbeat_key_ref:
            self.checkpoint_secret(
                'heartbeat_key_ref', secrets.create_hmac_key(
                    self.ctx, secret_store.SecretIdentity.for_resource(
                        self.stack, 'heartbeat-key')))
        if not self.stack.registry_secret_ref:
            self.checkpoint_secret(
                'registry_secret_ref', registry.create_secret(
                    self.ctx, secret_store.SecretIdentity.for_resource(
                        self.stack, 'registry')))

    def revert_credentials(self):
        if not self.guest_removed or not self.workers_removed():
            return
        for field in ('engine_token_ref', 'engine_token_previous_ref',
                      'heartbeat_key_ref', 'ssh_key_ref',
                      'registry_secret_ref'):
            reference = getattr(self.stack, field)
            if reference:
                secret_store.call('delete', self.ctx, reference)
                self.save(self.stack, **{field: None})
        self.save(self.stack, ssh_public_key=None)

    def create_volume(self, purpose, size, path, volume_type):
        volume = objects.Volume(
            context=self.ctx, id=str(uuid.uuid5(uuid.UUID(self.server.id),
                                                purpose)),
            stack_id=self.stack.id, server_id=self.server.id,
            name=naming.resource_name(self.server.id, purpose, conf=self.conf),
            size_gb=size,
            mount_path=path, purpose=purpose, volume_type=volume_type,
            status=states.CREATING)
        accounted = purpose in ('docker', 'registry')
        with db_api.writer.using(self.ctx):
            if accounted:
                extra = quota.reserve(self.ctx, volume_gb=size)
                quota.commit(self.ctx, extra)
            volume.create()
        if accounted:
            self.extra['volume_gb'] = (
                self.extra.get('volume_gb', 0) + size)
        self.volumes.append(volume)
        self.owner._queue(self.ctx, volume, 'create', 'start')
        self.flush()
        result = self.cloud.volume(volume)
        self.save(volume, cinder_volume_id=result.id)
        self.cloud.wait_volume(result.id)
        self.save(volume, status=states.AVAILABLE)
        self.owner._queue(self.ctx, volume, 'create', 'start')
        self.flush()

    def data_volume(self):
        self.create_volume('data', self.reservation['volume_gb'], '/data',
                           self.conf.controller_worker.data_volume_type)

    def docker_volume(self):
        self.create_volume('docker',
                           self.conf.controller_worker.docker_volume_size_gb,
                           '/var/lib/docker',
                           self.conf.controller_worker.docker_volume_type)

    def revert_volume(self, purpose):
        if not self.guest_removed or not self.workers_removed():
            return
        for volume in self.volumes:
            if volume.purpose == purpose:
                # Adopt by owned deterministic name if create lost its reply.
                identifier = volume.cinder_volume_id
                if not identifier:
                    found = self.cloud.find_volume(volume)
                    identifier = found.id if found else None
                if identifier:
                    self.cloud.delete_volume(identifier)
                self.save(volume, cinder_volume_id=None)

    def revert_data_volume(self):
        self.revert_volume('data')

    def revert_docker_volume(self):
        self.revert_volume('docker')

    def registry_volume(self):
        self.create_volume(
            'registry', self.conf.controller_worker.registry_volume_size_gb,
            '/var/lib/registry',
            self.conf.controller_worker.registry_volume_type)

    def revert_registry_volume(self):
        self.revert_volume('registry')

    def boot(self):
        if self.server.role == 'control':
            self.cloud.check_router()
        key = secret_store.call('get', self.ctx, self.stack.heartbeat_key_ref)
        data, serial, expires = self.cloud.config_drive(
            self.server, self.volumes, key)
        self.save(self.server, agent_cert_serial=serial,
                  agent_cert_expires_at=expires)
        self.guest_removed = False
        result = self.cloud.boot(self.server, self.ports, self.volumes, data)
        self.save(self.server, compute_id=result.id)

    def revert_boot(self):
        if not self.workers_removed():
            return
        self.cloud.delete_server(self.server)
        self.guest_removed = True
        self.save(self.server, compute_id=None)

    def active(self):
        self.cloud.wait_server(self.server.compute_id)
        for volume in self.volumes:
            self.save(volume, status=states.IN_USE)
            self.owner._queue(self.ctx, volume, 'create', 'start')
        self.flush()

    def health(self):
        self.agent.wait_health()

    def bootstrap(self):
        # Persist a receipt BEFORE this one-shot POST; never resubmit it.
        event = objects.Event(
            context=self.ctx, stack_id=self.stack.id,
            resource_type='environment', resource_id=self.stack.id,
            action='bootstrap', outcome='start',
            request_id=self.ctx.request_id)
        event.id = str(uuid.uuid5(uuid.UUID(self.stack.id), 'bootstrap'))
        event.create()
        self.bootstrap_result = self.agent.bootstrap(self.stack.engine_version)
        self.save(event, outcome='success')

    def token(self):
        if not self.stack.engine_token_ref:
            reference = secrets.store_engine_token(
                self.ctx, self.bootstrap_result['token'],
                secret_store.SecretIdentity.for_resource(
                    self.stack, 'engine-token'))
            self.checkpoint_secret(
                'engine_token_ref', reference,
                engine_token_created_at=timeutils.utcnow(),
                engine_token_name='coast-agent-bootstrap-v1')
        self.bootstrap_result = None

    def engine(self):
        endpoint = agent_client.endpoint(
            self.conf, self.server.mgmt_ip,
            secret_store.call('get', self.ctx, self.stack.engine_token_ref))
        result = self.driver.bootstrap_environment(endpoint, self.stack)
        self.save(self.stack, engine_ref=result.engine_ref)
        if (self.driver.get_engine_version(endpoint) !=
                self.stack.engine_version):
            raise exception.Invalid('Engine image version does not match')

    def complete(self):
        self.finish(True)

    def finish(self, success):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            for child in self.initial_servers:
                if child.started:
                    child.finish(success)
            self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            self.volumes = [objects.Volume.get_by_id(self.ctx, item.id)
                            for item in self.volumes]
            if self.server:
                self.server = objects.Server.get_by_id(
                    self.ctx, self.server.id)
            if not success:
                quota.release(self.ctx, **self.reservation)
                if self.extra:
                    quota.release(self.ctx, **self.extra)
            resources = self.volumes + ([self.server] if self.server else [])
            for resource in resources:
                self.save(resource, status=(states.ACTIVE if
                          resource.repository.resource == 'server' else
                          states.IN_USE) if success else states.ERROR)
                self.owner._event(self.ctx, resource, self.stack, 'create',
                                  'success' if success else 'failure')
            self.save(self.stack,
                      status=states.ACTIVE if success else states.ERROR,
                      fault=None if success else self.failure_fault or FAULT)
            self.stack.release_task(states.PROVISIONING)
            self.owner._event(self.ctx, self.stack, self.stack, 'create',
                              'success' if success else 'failure',
                              self.receipt, detail={
                                  'controller_image_id': (self.server.image_id
                                                          if self.server
                                                          else None)})
            # The task is no longer PROVISIONING, so this create is not
            # still in flight. Recount settles an earlier skipped leak.
            quota.reconcile(self.ctx)
            # A held volume reservation makes that recount skip volume_gb.
            # Release it once when the counter is exactly these volumes.
            if not success:
                amount = sum(item.size_gb for item in self.volumes)
                for child in self.initial_servers:
                    if not child.started:
                        continue
                    for item in child.volumes:
                        fresh = objects.Volume.get_by_id(
                            self.ctx, item.id)
                        amount += fresh.size_gb
                quota.release_exact_failed_volume_gb(self.ctx, amount)
        for child in self.initial_servers:
            child.flush()
        self.flush()
