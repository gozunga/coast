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

from coast.common import states
from coast.conductor import controller
from coast.conductor import manager
from coast.conductor import server_add
from coast.drivers import base
from coast import objects
from coast.tests import conductor


class VMServerFlowTest(conductor.ConductorTest):
    def test_worker_add_with_admin_key_over_rpc(self):
        self.config.config(group='controller_worker',
                           admin_ssh_key_name='operator-key')
        self.test_worker_add_and_empty_remove_over_rpc()

    def test_worker_add_and_empty_remove_over_rpc(self):
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
                           env_cidr_pool='198.51.100.0/24',
                           health_manager_ips=['192.0.2.5'],
                           data_volume_size_gb=10,
                           group='controller_worker')
        driver = mock.Mock(SUPPORTED_ENGINE_VERSIONS=['4.4.3'])
        driver.register_server.return_value = base.DriverResult('engine')
        self.manager.drivers['coolify_v4'] = driver
        cloud = mock.Mock()
        cloud.image_flavor.return_value = ('image', 'flavor')
        cloud.port.side_effect = [
            SimpleNamespace(id='mgmt-port', fixed_ips=[
                {'ip_address': '192.0.2.10'}]),
            SimpleNamespace(id='stack-port', fixed_ips=[
                {'ip_address': '198.51.100.10'}])]
        cloud.volume.side_effect = lambda volume: SimpleNamespace(
            id=volume.id)
        cloud.config_drive.return_value = (
            'config-drive', 'serial', datetime.datetime.now(
                datetime.timezone.utc) + datetime.timedelta(days=30))
        nova = mock.Mock()
        nova.create_server.return_value = SimpleNamespace(id='nova-server')
        cloud.proxy.return_value = nova
        cloud.conf = self.conf
        cloud.tags = ['coast', 'project_id=' + self.ctx.project_id,
                      'stack_id=' + stack_id]
        cloud.boot.side_effect = lambda *args: controller.ControllerCloud.boot(
            cloud, *args)
        network = mock.Mock()
        network.create.return_value = dict(network_id='stack-net',
                                           security_groups={})
        floating = mock.Mock()
        floating.ensure.return_value = SimpleNamespace(
            id='floating-id', floating_ip_address='203.0.113.10')
        endpoint = base.EngineEndpoint(
            'https://192.0.2.2:9443/engine', '', '', '', 'token-sensitive')
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
                'name': 'worker', 'kind': 'vm', 'role': 'worker',
                'plan': 'small'}}, status=202, complete=True).json['server']
            server_path = path + '/servers/' + row['id']
            server = self.request('get', server_path).json['server']
            self.assertEqual(states.ACTIVE, server['status'])
            saved = objects.Server.get_by_id(self.ctx, row['id'])
            self.assertEqual('nova-server', saved.compute_id)
            key_name = self.conf.controller_worker.admin_ssh_key_name
            if key_name:
                self.assertEqual(
                    key_name, nova.create_server.call_args.kwargs['key_name'])
            else:
                self.assertNotIn('key_name',
                                 nova.create_server.call_args.kwargs)
            self.assertEqual('203.0.113.10', saved.floating_ip)
            self.assertEqual('small', cloud.image_flavor.call_args.args[1])
            self.assertEqual(40, self.usage()['in_use_volume_gb'])
            foreign = dict(self.headers, **{'X-Project-Id': 'other'})
            self.request('get', server_path, headers=foreign, status=404)
            reader = dict(self.headers, **{'X-Roles': 'reader'})
            self.request('delete', server_path, headers=reader, status=403)
            self.request('delete', server_path, status=202, complete=True)
            self.request('get', server_path, status=404)
        data = [volume for volume in objects.VolumeList.get_all(
            self.ctx) if volume.name == 'coast-' + row['id'] + '-data']
        self.assertEqual(1, len(data))
        self.assertIsNone(data[0].server_id)
        self.assertEqual(states.AVAILABLE, data[0].status)
        self.assertEqual(20, self.usage()['in_use_volume_gb'])
        self.assertEqual(0, self.usage()['in_use_servers'])
        self.assertNotIn('sensitive', str(self.manager.notifier.events))
