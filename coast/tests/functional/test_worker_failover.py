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

"""API to serialized fake RPC to VM server flow integration."""

import datetime
from types import SimpleNamespace
from unittest import mock

from oslo_utils import timeutils

from coast.api import secret_store
from coast.api.v1 import resize as resize_api
from coast.common import exception
from coast.common import states
from coast.conductor import manager
from coast.conductor import secret_retry
from coast.conductor import server_add
from coast.conductor import worker
from coast.db import models
from coast.drivers import base
from coast.health_manager import failover
from coast.health_manager import stale
from coast import objects
from coast.tests import conductor


class WorkerFailoverRPCTest(conductor.ConductorTest):
    def usage(self):
        with self.engine.connect() as connection:
            return dict(connection.execute(
                models.QuotaUsage.__table__.select().where(
                    models.QuotaUsage.project_id == self.ctx.project_id)
            ).mappings().one())

    def run_failover(self, role='worker', template=False, redeploy=False,
                     admin=False, resize=False, rollback=False,
                     outage=False, rolling=False):
        restore = worker.WorkerCloud.restore
        path = self.stack(workers=[])
        stack_id = path.rsplit('/', 1)[-1]
        stack = objects.Stack.get_by_id(self.ctx, stack_id)
        stack.driver = 'coolify_v4'
        stack.engine_version = '4.4.3'
        stack.network_id = 'stack-net'
        stack.sg_mgmt_id = 'agent-sg'
        stack.sg_ingress_id = 'worker-sg'
        stack.ssh_key_ref = self.store.put(self.ctx, 'private-sensitive')
        stack.heartbeat_key_ref = self.store.put(self.ctx, 'a' * 64)
        stack.ssh_public_key = 'ssh-ed25519 public'
        stack.save()
        self.config.config(enabled_drivers=['noop', 'coolify_v4'],
                           mgmt_net_id='mgmt', agent_client_ca='ca.pem',
                           health_manager_ips=['192.0.2.5'],
                           data_volume_size_gb=10, build_poll_interval=1,
                           env_cidr_pool='198.51.100.0/24',
                           group='controller_worker')
        driver = mock.Mock(SUPPORTED_ENGINE_VERSIONS=['4.4.3'])
        driver.register_server.return_value = base.DriverResult('engine')
        driver.get_server_status.return_value = base.ObservedStatus(
            states.ACTIVE)
        driver.get_app_status.return_value = base.ObservedStatus(states.ACTIVE)
        self.manager.drivers['coolify_v4'] = driver
        cloud = mock.Mock()
        cloud.image_flavor.return_value = ('image', 'flavor')
        ports = [SimpleNamespace(id='mgmt-port', fixed_ips=[
            {'ip_address': '192.0.2.10'}]),
            SimpleNamespace(id='stack-port', fixed_ips=[
                {'ip_address': '198.51.100.10'}])]
        cloud.port.side_effect = ports
        cloud.retained_ports.return_value = ports
        cloud.volume.side_effect = lambda volume: SimpleNamespace(id=volume.id)
        cloud.find_volume.side_effect = lambda volume: SimpleNamespace(
            id=volume.cinder_volume_id, attachments=[{'server_id': 'old-vm'}])
        cloud.config_drive.return_value = (
            'config-drive', 'serial', datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(days=30))
        cloud.boot.side_effect = [SimpleNamespace(id='old-vm'),
                                  SimpleNamespace(id='new-vm')]
        cloud.proxy.return_value.get_ip.return_value = SimpleNamespace(
            port_id='stack-port')
        network = mock.Mock()
        network.create.return_value = dict(
            network_id='stack-net', security_groups={'build': 'build-sg'})
        floating = mock.Mock()
        floating.ensure.return_value = SimpleNamespace(
            id='floating-id', floating_ip_address='203.0.113.10')
        endpoint = base.EngineEndpoint(
            'https://192.0.2.2:9443/api/v1', '', '', '', 'token-sensitive')
        with (
            mock.patch.object(server_add.network, 'EnvironmentNetwork',
                              return_value=network),
            mock.patch.object(server_add.worker, 'WorkerCloud',
                              return_value=cloud),
            mock.patch.object(server_add.fip, 'FloatingIP',
                              return_value=floating),
            mock.patch.object(server_add.agent_client, 'AgentClient'),
            mock.patch.object(manager.Manager, '_endpoint',
                              return_value=endpoint),
        ):
            row = self.request('post', path + '/servers', {'server': {
                'name': role, 'kind': 'vm', 'role': role, 'plan': 'small'}},
                status=202, complete=True).json['server']
            server = objects.Server.get_by_id(self.ctx, row['id'])
            self.assertEqual(states.ACTIVE, server.status)
            parents = self.service_parents(stack_id)
            source = objects.Source(context=self.ctx, type='image',
                                    image='example.com/image:stable')
            source.create()
            service = objects.Service(
                context=self.ctx, stack_id=stack_id, server_id=server.id,
                environment_id=parents['environment_id'], source_id=source.id,
                name='service', template_id='postgresql' if template else None,
                ports='8080', replicas=1, auto_deploy=False,
                status=states.ACTIVE, task_state=states.NONE,
                engine_ref='service-ref')
            service.create()
            if redeploy:
                target = objects.Deployment(
                    context=self.ctx, service_id=service.id,
                    server_id=server.id, status=states.ACTIVE, trigger='api',
                    source_ref='stable', spec={'source': {
                        'type': 'image', 'image': 'example.com/image:stable'}})
                target.create()
                service.current_deployment_id = target.id
                service.save()
                driver.get_app_status.side_effect = [
                    base.ObservedStatus(states.STOPPED),
                    base.ObservedStatus(states.ACTIVE)]
                driver.deploy_app.return_value = base.DriverResult('deploy')
                driver.get_deployment_status.side_effect = [
                    base.ObservedStatus(states.PENDING),
                    base.ObservedStatus(states.ACTIVE)]
            volumes = objects.VolumeList.get_all(self.ctx, filters={
                'server_id': server.id})
            volume_ids = [volume.cinder_volume_id for volume in volumes]
            quota_before = self.usage()
            now = timeutils.utcnow()
            # Keep the existing controller healthy; only the node is stale.
            for controller in objects.ServerList.get_all(self.ctx, filters={
                    'stack_id': stack_id, 'role': 'control'}):
                controller.last_heartbeat_at = now
                controller.save()
            detector = stale.Detector(self.conf, clock=lambda: now)
            detector.started -= 61
            addresses = (server.mgmt_ip, server.private_ip,
                         server.floating_ip_id, server.floating_ip)
            if rollback:
                cloud.replacement.return_value = SimpleNamespace(id='new-vm')
                cloud.proxy.return_value.volume_attachments.return_value = []
                cloud.proxy.return_value.server_interfaces.return_value = []
                cloud.restore.side_effect = lambda *args: restore(
                    cloud, *args)
                cloud.wait_server.side_effect = [RuntimeError('failed'), None]
            if resize:
                action = (path if rolling else
                          path + '/servers/' + server.id) + '/action'
                body = {'resize_workers' if rolling else 'resize': {
                    'flavor': 'large'}}
                headers = dict(self.headers, **{
                    'X-Roles': 'member',
                    'OpenStack-API-Version': 'app-hosting 1.26'})
                with mock.patch.object(resize_api, 'selection', return_value={
                        'target_flavor': 'large', 'target_plan': 'large'}):
                    if outage:
                        self.resize_outage(action, body, headers,
                                           server, cloud)
                    else:
                        self.request('post', action, body, status=202,
                                     headers=headers)
            elif admin:
                action = path + '/servers/' + server.id + '/action'
                self.request('post', action, {'failover': {}}, status=403)
                response = self.request(
                    'post', action, {'failover': {}}, status=202,
                    headers=dict(self.headers, **{'X-Roles': 'admin'}))
                self.assertEqual(states.FAILING_OVER,
                                 response.json['server']['task_state'])
            else:
                server.last_heartbeat_at = now - datetime.timedelta(
                    seconds=500)
                server.save()
                with mock.patch.object(failover.rpcapi, 'ConductorAPI',
                                       return_value=self.rpc):
                    now -= datetime.timedelta(seconds=121)
                    detector.run()
                    self.assertEqual(states.UNHEALTHY, self.request(
                        'get', path + '/servers/' + server.id).json['server'][
                            'status'])
                    now += datetime.timedelta(seconds=121)
                    detector.run()
            if template:
                detector.run()
                self.assertEqual(states.UNHEALTHY,
                                 objects.Server.get_by_id(
                                     self.ctx, server.id).status)
                errors = objects.EventList.get_all(self.ctx, filters={
                    'resource_id': server.id, 'action': 'failover'})
                self.assertEqual(1, len(errors))
                self.assertEqual(failover.TEMPLATE_FAULT,
                                 errors[0].message)
                self.assertEqual(1, cloud.boot.call_count)
            elif rollback:
                self.manager.done.get(timeout=30)
                saved = objects.Server.get_by_id(self.ctx, server.id)
                self.assertEqual('old-vm', saved.compute_id)
                self.assertEqual('flavor', saved.flavor_id)
                self.assertEqual(states.ERROR, saved.status)
                self.assertTrue(saved.fault)
                self.assertEqual(states.NONE, saved.task_state)
                cloud.delete_guest.assert_called_once_with('new-vm')
                cloud.proxy.return_value.delete_port.assert_not_called()
                cloud.proxy.return_value.delete_volume.assert_not_called()
                cloud.restore.assert_called_once()
                cloud.proxy.return_value.start_server.assert_called_once_with(
                    'old-vm')
                args = cloud.restore.call_args.args
                self.assertEqual('old-vm', args[0])
                self.assertEqual(ports, args[1])
                self.assertEqual(volume_ids,
                                 [v.cinder_volume_id for v in args[2]])
                current_stack = objects.Stack.get_by_id(self.ctx, stack_id)
                self.assertEqual(states.ACTIVE, current_stack.status)
                self.assertEqual(states.NONE, current_stack.task_state)
                self.assertEqual(quota_before, self.usage())
                self.assertEqual(2, cloud.boot.call_count)
                self.assertEqual(2, cloud.port.call_count)
                self.assertEqual(len(volume_ids), cloud.volume.call_count)
                retained = objects.VolumeList.get_all(
                    self.ctx, filters={'server_id': server.id})
                self.assertEqual(volume_ids,
                                 [v.cinder_volume_id for v in retained])
                self.assertEqual([mock.call('old-vm', identifier) for
                                  identifier in volume_ids],
                                 cloud.proxy.return_value.
                                 create_volume_attachment.call_args_list)
                self.assertEqual([mock.call('old-vm', port_id=p.id) for
                                  p in ports], cloud.proxy.return_value.
                                 create_server_interface.call_args_list)
                self.assertEqual(addresses, (saved.mgmt_ip, saved.private_ip,
                                             saved.floating_ip_id,
                                             saved.floating_ip))
                types = {name for name, _ in self.manager.notifier.events}
                self.assertIn('coast.server.resize.error', types)
            else:
                self.manager.done.get(timeout=10)
                saved = objects.Server.get_by_id(self.ctx, server.id)
                self.assertEqual(states.ACTIVE, saved.status)
                self.assertEqual('new-vm', saved.compute_id)
                self.assertEqual(addresses, (saved.mgmt_ip, saved.private_ip,
                                             saved.floating_ip_id,
                                             saved.floating_ip))
                self.assertEqual(ports, cloud.boot.call_args.args[1])
                self.assertEqual(volume_ids, [v.cinder_volume_id for v in
                                 cloud.boot.call_args.args[2]])
                self.assertEqual('engine', saved.engine_ref)
                self.assertEqual(states.NONE, saved.task_state)
                self.assertEqual('image', saved.image_id)
                self.assertEqual('large' if resize else 'flavor',
                                 saved.flavor_id)
                retained = objects.VolumeList.get_all(
                    self.ctx, filters={'server_id': server.id})
                self.assertEqual(volume_ids,
                                 [v.cinder_volume_id for v in retained])
                self.assertTrue(all(v.status == states.IN_USE
                                    for v in retained))
                self.assertEqual(quota_before, self.usage())
                # A heartbeat completes the existing health rollup.
                saved.last_heartbeat_at = now
                saved.save()
                detector.run()
                self.assertEqual(states.ACTIVE, self.request(
                    'get', path).json['stack']['status'])
                errors = objects.EventList.get_all(self.ctx, filters={
                    'resource_id': server.id, 'action': 'failover'})
                self.assertEqual(1, len(errors))
                self.assertEqual('success', errors[0].outcome)
                types = {name for name, payload in
                         self.manager.notifier.events}
                self.assertIn('coast.server.failover.start', types)
                self.assertIn('coast.server.failover.end', types)
                public_events = self.request(
                    'get', '/v1/events?resource_id=' + server.id).json[
                        'events']
                receipts = [event for event in public_events if
                            event['resource_id'] == server.id and
                            event['action'] == 'delete.cleanup']
                self.assertEqual(1, len(receipts))
                self.assertIsNone(receipts[0]['message'])
                self.assertNotIn('old-vm', str(public_events))
                foreign = dict(self.headers, **{'X-Project-Id': 'other'})
                self.request('get', path + '/servers/' + server.id,
                             headers=foreign, status=404)
            self.assertNotIn('sensitive', str(self.manager.notifier.events))
            if redeploy:
                current = objects.Service.get_by_id(self.ctx, service.id)
                deployment = objects.Deployment.get_by_id(
                    self.ctx, current.current_deployment_id)
                self.assertEqual(states.ACTIVE, deployment.status)
                self.assertEqual('deploy', deployment.engine_ref)
                self.assertEqual('stable', deployment.source_ref)
                self.assertEqual('reconcile', deployment.trigger)
                self.assertNotEqual(target.id, deployment.id)
                previous = objects.Deployment.get_by_id(self.ctx, target.id)
                self.assertEqual(states.SUPERSEDED, previous.status)
            else:
                driver.deploy_app.assert_not_called()
            floating.delete.assert_not_called()

    def resize_outage(self, action, body, headers, server, cloud):
        server.fault = 'Existing server fault.'
        server.save()
        old_id, old_flavor = server.compute_id, server.flavor_id
        boots, stops = cloud.boot.call_count, cloud.stopped.call_count
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                self.request('post', action, body, status=202,
                             headers=headers, complete=True)
            saved = objects.Server.get_by_id(self.ctx, server.id)
            self.assertEqual((states.ACTIVE, states.NONE, old_id, old_flavor,
                              'Existing server fault.'),
                             (saved.status, saved.task_state,
                              saved.compute_id, saved.flavor_id, saved.fault))
            self.assertEqual(boots, cloud.boot.call_count)
            self.assertEqual(stops, cloud.stopped.call_count)
            self.assertEqual(states.NONE, objects.Stack.get_by_id(
                self.ctx, server.stack_id).task_state)
            clock.return_value = 111
            self.request('post', action, body, status=202, headers=headers)

    def test_resize_worker_secret_outage_defers_and_recovers(self):
        self.run_failover(resize=True, outage=True)

    def test_rolling_resize_secret_outage_defers_and_recovers(self):
        self.run_failover(resize=True, rolling=True, outage=True)

    def test_resize_worker_rollback(self):
        self.run_failover(resize=True, rollback=True)

    def test_resize_worker_api_to_rpc_replacement(self):
        self.run_failover(resize=True)

    def test_resize_build_api_to_rpc_replacement(self):
        self.run_failover(role='build', resize=True)

    def test_admin_worker_api_to_rpc_replacement(self):
        self.run_failover(admin=True)

    def test_admin_build_api_to_rpc_replacement(self):
        self.run_failover(role='build', admin=True)

    def test_worker_health_to_rpc_replacement(self):
        self.run_failover()

    def test_missing_appgroup_redeploys_over_rpc(self):
        self.run_failover(redeploy=True)

    def test_build_health_to_rpc_replacement(self):
        self.run_failover(role='build')

    def test_template_worker_emits_one_event_without_rpc_replacement(self):
        self.run_failover(template=True)


for _name in dir(conductor.ConductorTest):
    if (_name.startswith('test_') and
            _name not in WorkerFailoverRPCTest.__dict__):
        setattr(WorkerFailoverRPCTest, _name, None)
