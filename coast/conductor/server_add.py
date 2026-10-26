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

"""Ordered VM provisioning with private operation state and explicit revert."""

import uuid

from oslo_log import log as logging
from taskflow import engines
from taskflow.patterns import linear_flow

from coast.api import secret_store
from coast.common import exception
from coast.common import naming
from coast.common import quota
from coast.common import safe_log
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import controller
from coast.conductor import rpcapi
from coast.conductor import stack_create
from coast.conductor import worker
from coast.db import api as db_api
from coast.network import fip
from coast.network import stack as network
from coast import objects

LOG = logging.getLogger(__name__)
FAULT = 'Server operation failed; operator recovery may be required.'


class AddServer(stack_create.CreateStack):
    failure_kind = 'server'
    initial = False
    started = False
    action = 'create'
    task_state = states.PROVISIONING
    initial_status = states.PENDING
    steps = ('network', 'data_volume', 'docker_volume', 'boot', 'active',
             'health', 'floating', 'register', 'build_mode', 'complete')

    def execute(self, ctx, resource, options, reservation, revision):
        self.ctx = ctx
        self.options = options or {}
        self.reservation = reservation or {}
        revision = revision or rpcapi.revision(resource)
        self.receipt = rpcapi.receipt_id(
            self.action + '_server', resource, revision)
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            try:
                self.server = objects.Server.obj_from_db(
                    ctx, db_api.servers._get(ctx, resource.id, lock=True))
            except exception.NotFound:
                if self.action == 'delete':
                    return
                raise
            self.stack = objects.Stack.obj_from_db(
                ctx, db_api.stacks._get(ctx, self.server.stack_id, lock=True))
            receipts = objects.EventList.get_all(
                ctx, filters={'id': self.receipt}, limit=1)
            checks = []
            if receipts:
                checks.append('receipt')
            if self.server.status != self.initial_status:
                checks.append('status')
            if self.server.task_state != self.task_state:
                checks.append('task')
            if rpcapi.revision(self.server) != revision:
                checks.append('revision')
            if checks:
                LOG.info('Server %(operation)s %(server)s skipped: '
                         '%(checks)s',
                         {'operation': self.action,
                          'server': self.server.id,
                          'checks': ','.join(checks)})
                return
            self.stack.acquire_task(states.RECONCILING)
            self.save(self.stack, conductor_host=self.conf.host)
            if self.action == 'create':
                self.save(self.server, status=states.BUILDING)
                quota.commit(ctx, self.reservation)
            self.owner._event(ctx, self.server, self.stack, self.action,
                              'start', self.receipt, detail={
                                  'plan': self.options.get('plan')})
        self.flush()
        try:
            self.prepare()
            flow = linear_flow.Flow('server-' + self.action).add(*[
                stack_create.Step(self, name) for name in self.steps])
            engines.run(flow, engine='serial')
        except Exception as exc:
            safe_log.log_failure(
                LOG,
                'Server %(server)s stack %(stack)s %(action)s failed: '
                '%(cause)s at %(where)s request_id=%(request_id)s',
                exc,
                server=getattr(self.server, 'id', None),
                stack=getattr(self.stack, 'id', None),
                action=self.action,
                request_id=getattr(self.ctx, 'request_id', None))
            self.finish(False)

    def prepare_initial(self):
        with db_api.writer.using(self.ctx):
            self.server.create()
            self.server.acquire_task(self.task_state)
            self.save(self.server, status=states.BUILDING)
            self.owner._event(self.ctx, self.server, self.stack, self.action,
                              'start', self.receipt, detail={
                                  'plan': self.options.get('plan')})
        self.started = True
        self.flush()
        self.prepare()

    def prepare(self):
        options = self.conf.controller_worker
        status = states.BUILDING if self.initial else states.ACTIVE
        if (self.stack.status != status or
                self.server.kind != 'vm' or
                self.server.role not in ('worker', 'build') or
                self.conf.coolify.insecure_direct_url or
                not all((options.mgmt_net_id, options.agent_client_ca,
                         options.health_manager_ips, self.stack.network_id,
                         self.stack.sg_mgmt_id, self.stack.sg_ingress_id,
                         self.stack.ssh_key_ref, self.stack.ssh_public_key,
                         self.stack.heartbeat_key_ref))):
            raise exception.Invalid('Provisioned VM stack is required')
        self.driver = self.owner._driver(self.stack)
        self.endpoint = self.owner._endpoint(self.stack)
        self.cloud = worker.WorkerCloud(self.conf, self.stack)
        image, flavor = self.cloud.image_flavor(
            self.server, self.options.get('plan'), self.options.get('image'))
        self.save(self.server, image_id=image, flavor_id=flavor)
        self.fip = fip.FloatingIP(self.conf)

    def network(self):
        group = self.stack.sg_ingress_id
        if (self.server.role == 'build' or
                self.conf.controller_worker.admin_ssh_key_name):
            groups = network.EnvironmentNetwork(self.conf).create(
                self.stack.project_id, self.stack.id,
                controller.controller_ip(self.conf),
                has_build_servers=(self.server.role == 'build' or
                                   self.other_builds()))
            if groups['network_id'] != self.stack.network_id:
                raise exception.Invalid('Stack network differs')
            if self.server.role == 'build':
                group = groups['security_groups']['build']
        self.ports = [
            self.cloud.port(self.server, 'mgmt',
                            self.conf.controller_worker.mgmt_net_id,
                            self.stack.sg_mgmt_id),
            self.cloud.port(self.server, 'env', self.stack.network_id,
                            group)]
        self.save(self.server,
                  mgmt_ip=self.ports[0].fixed_ips[0]['ip_address'],
                  private_ip=self.ports[1].fixed_ips[0]['ip_address'])
        self.agent = agent_client.AgentClient(self.conf, self.server.mgmt_ip)

    def revert_network(self):
        if self.guest_removed:
            self.cloud.delete_ports(self.server)
            if self.server.role == 'build':
                others = self.other_builds()
                network.EnvironmentNetwork(self.conf).create(
                    self.stack.project_id, self.stack.id,
                    controller.controller_ip(self.conf),
                    has_build_servers=others)

    def other_builds(self):
        from coast.conductor import manager
        return any(server.id != self.server.id and server.compute_id
                   for server in manager.rows(
                       objects.ServerList, self.ctx,
                       {'stack_id': self.stack.id, 'role': 'build'}))

    def create_volume(self, purpose, size, path, volume_type):
        volume = objects.Volume(
            context=self.ctx, id=str(uuid.uuid5(
                uuid.UUID(self.server.id), purpose)),
            stack_id=self.stack.id, server_id=self.server.id,
            name=naming.resource_name(self.server.id, purpose, conf=self.conf),
            size_gb=size, mount_path=path, purpose=purpose,
            volume_type=volume_type, status=states.CREATING)
        with db_api.writer.using(self.ctx):
            delta = quota.reserve(self.ctx, volume_gb=size)
            quota.commit(self.ctx, delta)
            volume.create()
        self.extra['volume_gb'] = self.extra.get('volume_gb', 0) + size
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
        self.create_volume('data',
                           self.conf.controller_worker.data_volume_size_gb,
                           '/data',
                           self.conf.controller_worker.data_volume_type)

    def floating(self):
        if self.server.role == 'worker':
            result = self.fip.ensure(
                self.stack.project_id, self.stack.id, self.server.id,
                self.ports[1].id)
            self.save(self.server, floating_ip_id=result.id,
                      floating_ip=result.floating_ip_address)

    def revert_floating(self):
        if getattr(self, 'registration_uncertain', False):
            return
        self.fip.delete(self.stack.project_id, self.stack.id, self.server.id)
        self.save(self.server, floating_ip_id=None, floating_ip=None)

    def revert_boot(self):
        if not getattr(self, 'registration_uncertain', False):
            super().revert_boot()

    def register(self):
        self.registration_uncertain = True
        result = self.driver.register_server(
            self.endpoint, self.stack, self.server,
            secret_store.call('get', self.ctx, self.stack.ssh_key_ref))
        self.save(self.server, engine_ref=result.engine_ref)

    def build_targets(self):
        from coast.conductor import manager
        workers = [row for row in manager.rows(
            objects.ServerList, self.ctx,
            {'stack_id': self.stack.id, 'role': 'worker'}) if row.engine_ref]
        services = [row for row in manager.rows(
            objects.ServiceList, self.ctx, {'stack_id': self.stack.id})
            if row.engine_ref and not row.is_engine_service and
            objects.Source.get_by_id(self.ctx, row.source_id).type == 'git']
        return workers, services

    def other_usable_builds(self):
        from coast.conductor import manager
        return any(row.id != self.server.id and row.compute_id and
                   row.engine_ref and self.driver.get_server_status(
                       self.endpoint, self.stack, row).status == states.ACTIVE
                   for row in manager.rows(
                       objects.ServerList, self.ctx,
                       {'stack_id': self.stack.id, 'role': 'build'}))

    def build_mode(self):
        if self.server.role != 'build':
            return
        # Even a failed usability read must retain the registered guest.
        self.build_mode_uncertain = True
        if not self.other_usable_builds():
            workers, services = self.build_targets()
            self.driver.set_build_server_mode(
                self.endpoint, self.stack, workers, services, True)
            self.build_mode_changed = True
        self.build_mode_uncertain = False

    def revert_build_mode(self):
        if getattr(self, 'build_mode_changed', False):
            self.build_mode_uncertain = True
            workers, services = self.build_targets()
            self.driver.set_build_server_mode(
                self.endpoint, self.stack, workers, services, False)
            self.build_mode_uncertain = False

    def revert_register(self):
        if getattr(self, 'build_mode_uncertain', False):
            return
        self.driver.unregister_server(self.endpoint, self.stack, self.server)
        self.save(self.server, engine_ref=None)
        self.registration_uncertain = False

    def finish(self, success):
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            self.server = objects.Server.get_by_id(self.ctx, self.server.id)
            self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            if not success and self.action == 'create':
                quota.release(self.ctx, **self.reservation)
                quota.release(self.ctx, **self.extra)
            for item in self.volumes:
                volume = objects.Volume.get_by_id(self.ctx, item.id)
                self.save(volume, status=states.IN_USE if success
                          else states.ERROR)
                self.owner._event(self.ctx, volume, self.stack, 'create',
                                  'success' if success else 'failure')
            self.save(self.server, status=states.ACTIVE if success
                      else states.ERROR, fault=None if success else
                      self.failure_fault or FAULT)
            self.server.release_task(self.task_state)
            if not self.initial:
                self.stack.release_task(states.RECONCILING)
            self.owner._event(self.ctx, self.server, self.stack, self.action,
                              'success' if success else 'failure',
                              self.receipt)
            quota.reconcile(self.ctx)
        if not self.initial:
            self.flush()
