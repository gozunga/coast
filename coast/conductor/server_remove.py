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

"""Empty-server removal; irreversible steps keep checkpoints on failure."""

from openstack import exceptions as sdk_exceptions

from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import controller
from coast.conductor import server_add
from coast.conductor import worker
from coast.db import api as db_api
from coast.health_manager import stale
from coast.network import fip
from coast.network import stack as network
from coast import objects


def _if_present(fn):
    """Return None when Nova or Neutron already removed the resource.

    get_server raises ResourceNotFound for a missing guest. Treat that
    result the same way as a None lookup and continue cleanup.
    """
    try:
        return fn()
    except Exception as exc:
        if isinstance(exc, (sdk_exceptions.ResourceNotFound,
                            exception.NotFound)):
            return None
        for name in ('status_code', 'http_status', 'code'):
            if getattr(exc, name, None) == 404:
                return None
        raise


class RemoveServer(server_add.AddServer):
    action = 'delete'
    task_state = states.DELETING
    initial_status = states.DELETING
    steps = ('drain', 'build_mode', 'deregister', 'guest', 'floating', 'ports',
             'storage', 'network_rules', 'complete')

    def prepare(self):
        if (self.stack.status not in (states.ACTIVE, states.DEGRADED) or
                self.server.role not in ('worker', 'build') or
                self.server.kind != 'vm' or
                self.conf.coolify.insecure_direct_url):
            raise exception.Invalid('Provisioned VM stack is required')
        self.driver = self.owner._driver(self.stack)
        self.endpoint = self.owner._endpoint(self.stack)
        self.cloud = worker.WorkerCloud(self.conf, self.stack)
        self.fip = fip.FloatingIP(self.conf)

    def drain(self):
        # No drain/move method exists in the approved driver contract.
        if objects.ServiceList.get_all(
                self.ctx, filters={'server_id': self.server.id}, limit=1):
            raise exception.Conflict()
        if objects.StorageList.get_all(
                self.ctx, filters={'server_id': self.server.id}, limit=1):
            raise exception.Conflict()
        for volume in self.volume_rows():
            if objects.AppVolumeMountList.get_all(
                    self.ctx, filters={'volume_id': volume.id}, limit=1):
                raise exception.Conflict()

    def volume_rows(self):
        from coast.conductor import manager
        return list(manager.rows(objects.VolumeList, self.ctx,
                                 {'server_id': self.server.id}))

    def build_mode(self):
        if self.server.role == 'build' and not self.other_builds():
            workers, services = self.build_targets()
            self.driver.set_build_server_mode(
                self.endpoint, self.stack, workers, services, False)
            self.build_mode_changed = True

    def revert_build_mode(self):
        # Restore only while the engine registration checkpoint remains.
        # After deregistration, local builds must stay enabled, even if Nova
        # deletion or later cleanup fails. A retry must not need a builder.
        if (getattr(self, 'build_mode_changed', False) and
                self.server.engine_ref):
            workers, services = self.build_targets()
            self.driver.set_build_server_mode(
                self.endpoint, self.stack, workers, services, True)

    def deregister(self):
        self.driver.unregister_server(self.endpoint, self.stack, self.server)
        self.save(self.server, engine_ref=None)

    def guest(self):
        # Confirm Nova deletion before changing volumes or network ports.
        # A missing guest is already gone. Keep the name lookup when the
        # stored compute id is empty so a lost create reply can still match.
        if self.server.compute_id:
            observed = _if_present(lambda: self.cloud.proxy('nova').get_server(
                self.server.compute_id))
            if observed is not None:
                _if_present(lambda: self.cloud.delete_server(self.server))
        else:
            _if_present(lambda: self.cloud.delete_server(self.server))
        self.save(self.server, compute_id=None)

    def floating(self):
        self.fip.delete(self.stack.project_id, self.stack.id, self.server.id)
        self.save(self.server, floating_ip_id=None, floating_ip=None)

    def revert_floating(self):
        # Never recreate ingress after an irreversible guest deletion.
        pass

    def ports(self):
        _if_present(lambda: self.cloud.delete_ports(self.server))

    def storage(self):
        for volume in self.volume_rows():
            if volume.purpose == 'docker':
                self.owner._queue(self.ctx, volume, 'delete', 'start')
                self.flush()
                identifier = volume.cinder_volume_id
                if not identifier:
                    found = self.cloud.find_volume(volume)
                    identifier = found.id if found else None
                if identifier:
                    self.cloud.delete_volume(identifier)
                with db_api.writer.using(self.ctx):
                    if self.owner._accounted(self.ctx, volume):
                        quota.release(self.ctx, volume_gb=volume.size_gb)
                    volume.destroy()
                    self.owner._event(self.ctx, volume, self.stack,
                                      'delete', 'success')
                self.flush()
            else:
                if volume.cinder_volume_id:
                    self.cloud.wait_volume(volume.cinder_volume_id)
                status = (states.AVAILABLE if volume.status == states.IN_USE
                          else volume.status)
                self.save(volume, server_id=None, status=status)
                self.owner._queue(self.ctx, volume, 'update', 'end')
                self.flush()

    def network_rules(self):
        if self.server.role == 'build':
            network.EnvironmentNetwork(self.conf).create(
                self.stack.project_id, self.stack.id,
                controller.controller_ip(self.conf),
                has_build_servers=self.other_builds())

    def finish(self, success):
        if not success:
            super().finish(False)
            return
        with db_api.writer.using(self.ctx) as session:
            db_api._lock_project(session, self.ctx.project_id)
            self.server = objects.Server.get_by_id(self.ctx, self.server.id)
            self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            if self.owner._accounted(self.ctx, self.server):
                quota.release(self.ctx, **quota.deltas('server', self.server))
            self.server.release_task(states.DELETING)
            self.server.destroy()
            target = stale.apply_health_rollup(
                session, self.ctx.project_id, self.stack)
            if target is not None:
                self.save(self.stack, status=target, fault=self.stack.fault)
            self.stack.release_task(states.RECONCILING)
            self.owner._event(self.ctx, self.server, self.stack, 'delete',
                              'success', self.receipt)
        self.flush()
