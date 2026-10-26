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

"""Real Taskflow ordering and cleanup with mocked SDK and agent transport."""

import base64
from contextlib import nullcontext
import copy
import errno
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock
import uuid

import bcrypt
import fixtures
from openstack import exceptions as sdk_exceptions
import requests
import requests_mock

from coast.api import secret_store
from coast.common import clients
from coast.common import context
from coast.common import engine_versions
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import controller as controller_mod
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.conductor import stack_create
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast.drivers import base as driver_base
from coast import objects
from coast.tests import base
from coast.tests.unit import test_tls


class StackCreateTest(base.TestCase):
    def test_timeline_create_detail_contains_only_selected_values(self):
        self.accepted(dict(password='secret-password', token='secret-token',
                           cert='secret-cert', env={'KEY': 'secret-env'}))
        self.execute()
        rows = objects.EventList.get_all(self.ctx, filters={
            'stack_id': self.stack.id, 'resource_type': 'stack',
            'action': 'create'})
        self.assertEqual(1, len(rows))
        detail = rows[0].detail
        self.assertEqual(self.stack.plan, detail['plan'])
        self.assertEqual(len(self.options.get('workers', [])),
                         detail['worker_count'])
        self.assertEqual(self.options.get('build_server', False),
                         detail['build_server'])
        self.assertEqual(objects.ServerList.get_all(self.ctx, filters={
            'stack_id': self.stack.id, 'role': 'control'})[0].image_id,
            detail['controller_image_id'])
        for value in ('secret-password', 'secret-token', 'secret-cert',
                      'secret-env'):
            self.assertNotIn(value, str(detail))

    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        self.useFixture(fixtures.MonkeyPatch(
            'oslo_config.cfg.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(
            project_id='project', request_id='req')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        test_tls.TLSTest._write_ca(self)
        self.config.config(
            enabled_drivers=['coolify_v4'], mgmt_net_id='mgmt-net',
            mgmt_sg_id='operator-sg', env_cidr_pool='192.0.2.0/24',
            env_subnet_prefix=24, image_tag_controller='controller-pin',
            image_owner_id='image-owner',
            agent_client_ca=str(Path(self.directory.name) / 'ca.pem'),
            agent_client_cert='client.pem', agent_client_key='client.key',
            health_manager_ips=['198.51.100.2'], docker_volume_size_gb=20,
            build_poll_interval=1, group='controller_worker')
        self.config.config(definitions=json.dumps([dict(
            name='control', kind='vm', roles=['control'], flavor='flavor')]),
            group='plans')
        self.notifier = mock.Mock()
        self.manager = manager.Manager(self.conf, notifier=self.notifier)
        self.driver = mock.Mock(SUPPORTED_ENGINE_VERSIONS=['4.4.3'])
        self.driver.bootstrap_environment.return_value = (
            driver_base.DriverResult(engine_ref='engine-project'))
        self.driver.get_engine_version.return_value = '4.4.3'
        self.patch(stack_create.loader, 'load_driver',
                   return_value=self.driver)
        self.payloads = {}
        self.store = mock.Mock()
        self.store.put.side_effect = self.put_secret
        self.store.get.side_effect = lambda ctx, ref: self.payloads[ref]
        self.store.delete.side_effect = lambda ctx, ref: self.payloads.pop(ref)
        self.patch(secret_store, 'STORE', new=self.store)
        self.agent = mock.Mock()
        self.agent.bootstrap.return_value = dict(
            token='bootstrap-sensitive', engine_version='4.4.3')
        self.patch(agent_client, 'AgentClient', return_value=self.agent)
        self.cloud_resources = {
            kind: {} for kind in ('port', 'volume', 'server')}
        self.networks = set()
        self.network = mock.Mock()
        self.network.create.side_effect = self.create_network
        self.network.delete.side_effect = self.delete_network
        network_factory = self.patch(
            stack_create.network, 'EnvironmentNetwork',
            return_value=self.network)
        network_factory._tags.side_effect = lambda project, stack_id: [
            'coast', 'project_id=' + project, 'stack_id=' + stack_id]
        self.proxies = {name: mock.Mock() for name in
                        ('nova', 'neutron', 'cinder', 'glance')}
        self.patch(clients, 'get_connection', side_effect=self.connection)
        image = SimpleNamespace(id='golden-image', status='active',
                                tags=['controller-pin'], owner='image-owner',
                                properties={'coast_role': 'controller'})
        self.proxies['neutron'].get_router.return_value = SimpleNamespace(
            external_gateway_info={'network_id': 'external'})
        self.proxies['neutron'].get_subnet.return_value = SimpleNamespace(
            gateway_ip='192.0.2.1')
        self.proxies['glance'].images.return_value = [image]
        self.proxies['nova'].get_keypair.return_value = SimpleNamespace(
            public_key='ssh-ed25519 admin-public')
        self.proxies['nova'].find_flavor.return_value = (
            SimpleNamespace(id='flavor'))
        for service, kind, plural in (
                ('nova', 'server', 'servers'), ('neutron', 'port', 'ports'),
                ('cinder', 'volume', 'volumes')):
            proxy = self.proxies[service]
            getattr(proxy, 'create_' + kind).side_effect = (
                lambda _kind=kind, **values: self.create_cloud(_kind, values))
            getattr(proxy, 'delete_' + kind).side_effect = (
                lambda value, _kind=kind, **kwargs: self.delete_cloud(
                    _kind, value))
            getattr(proxy, plural).side_effect = (
                lambda _kind=kind, **query: [
                    value for value in self.cloud_resources[_kind].values()
                    if ('name' not in query or value.name == query['name'])
                    and set(query.get('tags', '').split(',')) - {''}
                    <= set(getattr(value, 'tags', []))])
            getattr(proxy, 'get_' + kind).side_effect = (
                lambda id, _kind=kind: self.cloud_resources[_kind][id])
            proxy.wait_for_delete.side_effect = self.assert_no_transaction
        self.proxies['nova'].wait_for_server.side_effect = (
            self.assert_no_transaction)
        self.proxies['cinder'].snapshots.return_value = []
        self.proxies['cinder'].wait_for_status.side_effect = (
            self.assert_no_transaction)

        def apply_tags(resource, tag_values):
            resource.tags = list(tag_values)
            return resource

        self.proxies['neutron'].set_tags.side_effect = apply_tags

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def assert_no_transaction(self, *args, **kwargs):
        try:
            session = self.ctx.session
        except AttributeError:
            session = None
        self.assertIsNone(session, 'Cloud request inside a DB transaction')

    def connection(self, service, conf):
        self.assert_no_transaction()
        attribute = dict(nova='compute', neutron='network',
                         cinder='block_storage', glance='image')[service]
        return SimpleNamespace(**{attribute: self.proxies[service]})

    def put_secret(self, ctx, value, identity):
        self.assertIsInstance(identity, secret_store.SecretIdentity)
        self.assert_no_transaction()
        ref = 'https://secrets.example.com/v1/secrets/' + str(uuid.uuid4())
        self.payloads[ref] = value
        return ref

    def create_network(self, project, stack_id, address):
        self.assert_no_transaction()
        self.networks.add(stack_id)
        return dict(network_id='net', subnet_id='subnet', router_id='router',
                    security_groups=dict(worker='worker-sg', ctrl='ctrl-sg',
                                         **{'agent-mgmt': 'agent-sg'}))

    def delete_network(self, project, stack_id):
        self.assert_no_transaction()
        self.assertFalse(self.cloud_resources['port'])
        self.networks.discard(stack_id)

    def create_cloud(self, kind, values):
        self.assert_no_transaction()
        values = copy.deepcopy(values)
        if kind == 'port' and 'tags' in values:
            raise RuntimeError("Attribute 'tags' not allowed in POST")
        if kind == 'port':
            values.setdefault('fixed_ips', [dict(ip_address='198.51.100.10',
                                                 subnet_id='mgmt-subnet')])
        if kind == 'volume':
            values['status'] = 'available'
        result = SimpleNamespace(id=str(uuid.uuid4()), **values)
        self.cloud_resources[kind][result.id] = result
        return result

    def delete_cloud(self, kind, value):
        self.assert_no_transaction()
        if kind == 'volume':
            self.assertFalse(self.cloud_resources['server'])
        identifier = value if isinstance(value, str) else value.id
        self.cloud_resources[kind].pop(identifier, None)

    def accepted(self, options=None):
        self.options = options or {}
        self.reservation = quota.deltas('stack', dict(
            self.options, data_volume_gb=10))
        with api.writer.using(self.ctx):
            quota.reserve(self.ctx, **self.reservation)
            self.stack = objects.Stack(
                context=self.ctx, name='env', plan='control',
                driver='coolify_v4',
                engine_version='4.4.3', status=states.PENDING,
                task_state=states.NONE)
            self.stack.create()
            self.stack.acquire_task(states.PROVISIONING)
        self.revision = rpcapi.revision(self.stack)
        return self.stack

    def execute(self):
        self.manager.execute(self.ctx, 'create_stack', self.stack,
                             options=self.options,
                             reservation=self.reservation,
                             revision=self.revision)
        return objects.Stack.get_by_id(self.ctx, self.stack.id)

    def counters(self):
        with api.reader.using(self.ctx):
            return self.ctx.session.query(models.QuotaUsage).filter_by(
                project_id=self.ctx.project_id).one()

    def test_happy_path_config_drive_and_duplicate(self):
        self.accepted()
        stack = self.execute()
        self.assertEqual(states.ACTIVE, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual('engine-project', stack.engine_ref)
        self.assertEqual(64, len(self.payloads[stack.heartbeat_key_ref]))
        self.assertEqual('bootstrap-sensitive',
                         self.payloads[stack.engine_token_ref])
        self.assertNotIn('APP_KEY=backup-sensitive', self.payloads.values())
        servers = objects.ServerList.get_all(self.ctx)
        self.assertEqual(1, len(servers))
        server = servers[0]
        self.assertEqual('control', server.role)
        self.assertEqual(states.ACTIVE, server.status)
        self.assertIsNone(server.floating_ip)
        volumes = list(objects.VolumeList.get_all(self.ctx))
        self.assertEqual({'/data', '/var/lib/docker', '/var/lib/registry'},
                         {volume.mount_path for volume in volumes})
        self.assertEqual({states.IN_USE},
                         {volume.status for volume in volumes})
        nova = self.cloud_resources['server'][server.compute_id]
        self.assertNotIn('key_name', vars(nova))
        self.assertTrue(nova.config_drive)
        self.assertEqual(server.hostname, nova.name)
        self.assertTrue(server.hostname.endswith('-controller'))
        self.assertIn('server_id=' + server.id, nova.tags)
        self.assertEqual(2, len(nova.networks))
        self.assertEqual(4, len(nova.block_device_mapping))
        self.assertEqual(dict(
            uuid=nova.image_id, source_type='image',
            destination_type='local', boot_index=0,
            delete_on_termination=True), nova.block_device_mapping[0])
        self.assertFalse(any(item['delete_on_termination'] for item in
                             nova.block_device_mapping[1:]))
        neutron = self.proxies['neutron']
        self.assertGreaterEqual(neutron.create_port.call_count, 1)
        self.assertEqual(neutron.create_port.call_count,
                         neutron.set_tags.call_count)
        for call in neutron.create_port.call_args_list:
            self.assertNotIn('tags', call.kwargs)
        for resource in self.cloud_resources['port'].values():
            self.assertIn('stack_id=' + self.stack.id, resource.tags)
        data = json.loads(
            base64.b64decode(nova.user_data).decode().split('\n', 1)[1])
        self.assertNotIn('users', data)
        self.assertEqual(server.hostname, data['hostname'])
        self.assertEqual(server.hostname, data['fqdn'])
        self.assertFalse(data['preserve_hostname'])
        files = {file['path']: file for file in data['write_files']}
        agent_conf = files['/etc/coast/coast.conf']['content']
        self.assertIn('role=controller', agent_conf)
        self.assertIn('project_id=project\n', agent_conf)
        self.assertIn('server_ca=/etc/coast/server-ca.crt', agent_conf)
        self.assertEqual(
            Path(self.conf.certificates.ca_certificate).read_text(),
            files['/etc/coast/server-ca.crt']['content'])
        self.assertIn(self.payloads[stack.heartbeat_key_ref], agent_conf)
        self.assertNotIn('bootstrap-sensitive', nova.user_data)
        self.assertEqual({'/data', '/var/lib/docker', '/var/lib/registry'},
                         {mount[1] for mount in data['mounts']})
        aliases = {controller_mod.disk_alias(volume.cinder_volume_id)
                   for volume in volumes}
        devices = {item['device'] for item in data['fs_setup']}
        self.assertEqual(aliases, devices)
        # 'none' formats the raw disk on every new instance (failover).
        self.assertEqual({('auto', False)}, {
            (item['partition'], item['overwrite'])
            for item in data['fs_setup']})
        self.assertEqual(
            aliases, {mount[0] for mount in data['mounts']})
        script = data['bootcmd'][0]
        for volume in volumes:
            for path in controller_mod.disk_candidates(
                    volume.cinder_volume_id):
                self.assertIn(path, script)
        self.assertNotIn('/dev/disk/by-id/virtio-', ''.join(
            item['device'] for item in data['fs_setup']))
        self.assertEqual({'0600'},
                         {file['permissions'] for file in files.values()})
        secret = json.loads(self.payloads[stack.registry_secret_ref])
        decoded = base64.b64decode(nova.user_data).decode()
        self.assertNotIn(secret['password'], decoded)
        self.assertIn(
            '[registry]\n', files['/etc/coast/coast.conf']['content'])
        self.assertIn('/etc/coast/registry/htpasswd', files)
        self.assertIn('/etc/coast/registry/tls.key', files)
        line = files['/etc/coast/registry/htpasswd']['content'].strip()
        user, hashed = line.split(':', 1)
        self.assertEqual('coast', user)
        self.assertTrue(bcrypt.checkpw(secret['password'].encode(),
                                       hashed.encode()))
        self.assertEqual(50, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().reserved_volume_gb)
        self.assertEqual(1, self.counters().in_use_environments)
        self.assertEqual(0, self.counters().in_use_servers)
        before = copy.deepcopy(self.cloud_resources)
        self.execute()
        self.assertEqual(before, self.cloud_resources)
        self.assertEqual(1, self.agent.bootstrap.call_count)
        endpoint = self.manager._endpoint(stack)
        self.assertIn(server.mgmt_ip, endpoint.base_url)
        self.assertEqual('bootstrap-sensitive', endpoint.token)
        self.assertNotIn('bootstrap-sensitive', str(self.notifier.mock_calls))
        self.assertNotIn('backup-sensitive', str(self.notifier.mock_calls))
        self.assertNotIn(
            stack.heartbeat_key_ref, str(self.notifier.mock_calls))

    def fail_step(self, name):
        self.accepted()
        original = getattr(stack_create.CreateStack, name)

        def fail(flow):
            original(flow)
            raise RuntimeError('bootstrap-sensitive backup-sensitive')

        with mock.patch.object(
                stack_create.CreateStack, name, fail):
            stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertNotIn('sensitive', stack.fault)
        self.assertEqual({}, self.payloads)
        self.assertFalse(self.networks)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertEqual(0, self.counters().in_use_environments)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().reserved_volume_gb)
        for volume in objects.VolumeList.get_all(self.ctx):
            self.assertEqual(states.ERROR, volume.status)
            self.assertIsNone(volume.cinder_volume_id)
        before = self.agent.bootstrap.call_count
        self.execute()
        self.assertEqual(before, self.agent.bootstrap.call_count)

    def test_credentials_reuse_saved_references(self):
        self.accepted()
        flow = stack_create.CreateStack(self.manager)
        flow.ctx = self.ctx
        flow.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        flow.credentials()
        before = dict(self.payloads)
        refs = (flow.stack.ssh_key_ref, flow.stack.heartbeat_key_ref,
                flow.stack.registry_secret_ref)
        flow.credentials()
        self.assertEqual(before, self.payloads)
        self.assertEqual(refs, (flow.stack.ssh_key_ref,
                                flow.stack.heartbeat_key_ref,
                                flow.stack.registry_secret_ref))

    def test_secret_checkpoint_failure_deletes_uncheckpointed_ref(self):
        self.accepted()
        original = stack_create.CreateStack.save

        def fail(flow, resource, **values):
            if 'heartbeat_key_ref' in values and values['heartbeat_key_ref']:
                raise RuntimeError('sensitive')
            return original(flow, resource, **values)

        with mock.patch.object(stack_create.CreateStack,
                               'save', fail):
            self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(self.payloads)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_docker_quota_failure_reverts_preceding_resources(self):
        self.accepted()
        quota.update(context.RequestContext(project_id='project',
                                            is_admin=True),
                     'project', {'volume_gb': 10})
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.payloads)
        self.assertEqual(0, self.counters().in_use_volume_gb)

    def test_failed_final_db_commit_reverts_cloud_resources(self):
        self.accepted()
        original = stack_create.CreateStack.save

        def fail(flow, resource, **values):
            if (resource is flow.stack and
                    values.get('status') == states.ACTIVE):
                raise RuntimeError('sensitive')
            return original(flow, resource, **values)

        with mock.patch.object(stack_create.CreateStack,
                               'save', fail):
            stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.payloads)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual({states.ERROR},
                         {server.status for server in
                          objects.ServerList.get_all(self.ctx)})

    def test_building_duplicate_cannot_take_over(self):
        self.accepted()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        stack.status = states.BUILDING
        stack.save()
        self.execute()
        self.assertFalse(self.networks)
        self.assertFalse(self.payloads)
        self.assertEqual(10, self.counters().reserved_volume_gb)

    def test_missing_router_or_gateway_fails_before_nova(self):
        for router in (None, SimpleNamespace(external_gateway_info=None),
                       SimpleNamespace(external_gateway_info={})):
            self.proxies['neutron'].get_router.return_value = router
            self.accepted()
            stack = self.execute()
            self.assertEqual(states.ERROR, stack.status)
            self.assertEqual(exception.RouterGatewayError.msg_fmt, stack.fault)
            self.assertFalse(any(self.cloud_resources.values()))
            self.assertFalse(self.networks)
            self.assertFalse(self.payloads)
            self.assertEqual(0, self.counters().in_use_volume_gb)
            self.proxies['nova'].create_server.assert_not_called()
            # Failed stack names remain reserved; use a new name next time.
            stack.name = stack.id
            stack.save()

    def test_missing_subnet_gateway_fails_before_nova(self):
        for subnet in (None, SimpleNamespace(gateway_ip=None)):
            self.proxies['neutron'].get_subnet.return_value = subnet
            self.accepted()
            stack = self.execute()
            self.assertEqual(states.ERROR, stack.status)
            self.assertEqual(exception.SubnetGatewayError.msg_fmt, stack.fault)
            self.assertFalse(any(self.cloud_resources.values()))
            self.assertFalse(self.networks)
            self.assertFalse(self.payloads)
            self.proxies['nova'].create_server.assert_not_called()
            # Failed stack names remain reserved; use a new name next time.
            stack.name = stack.id
            stack.save()

    def test_neutron_router_not_found_has_safe_preboot_fault(self):
        self.proxies['neutron'].get_router.side_effect = (
            sdk_exceptions.ResourceNotFound('secret'))
        self.accepted()
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(exception.RouterGatewayError.msg_fmt, stack.fault)
        self.assertFalse(any(self.cloud_resources.values()))
        self.proxies['nova'].create_server.assert_not_called()

    def test_neutron_subnet_not_found_has_safe_preboot_fault(self):
        self.proxies['neutron'].get_subnet.side_effect = (
            sdk_exceptions.ResourceNotFound('secret'))
        self.accepted()
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(exception.SubnetGatewayError.msg_fmt, stack.fault)
        self.assertFalse(any(self.cloud_resources.values()))
        self.proxies['nova'].create_server.assert_not_called()

    def test_egress_fault_survives_revert_without_exception_secrets(self):
        self.accepted()
        self.agent.wait_health.side_effect = (
            exception.GuestEgressError('secret'))
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual('no internet egress via stack router', stack.fault)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.networks)
        self.assertFalse(self.payloads)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.agent.bootstrap.assert_not_called()

    def test_health_unreachable_then_delete_releases_volume_quota_once(self):
        self.accepted()
        self.agent.wait_health.side_effect = driver_base.EngineUnreachable(
            'timeout')
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual(stack_create.FAULT, stack.fault)
        self.assertFalse(self.cloud_resources['server'])
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(0, self.counters().in_use_environments)
        self.assertEqual(0, self.counters().reserved_volume_gb)
        self.proxies['neutron'].ips.return_value = []
        self.manager.execute(self.ctx, 'delete_stack', stack)
        deleted = objects.Stack.get_by_id(
            self.ctx, stack.id, show_deleted=True)
        self.assertEqual(states.DELETED, deleted.status, deleted.fault)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().reserved_volume_gb)
        self.assertEqual(0, self.counters().in_use_servers)
        self.assertEqual(0, self.counters().in_use_environments)
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        self.manager.execute(self.ctx, 'delete_stack', stack)
        self.assertEqual(0, self.counters().in_use_volume_gb)
        self.assertEqual(0, self.counters().reserved_volume_gb)

    def test_probe_url_is_rendered_in_controller_config_drive(self):
        self.config.config(egress_check_url='https://registry.example.com/v2/',
                           group='controller_worker')
        self.accepted()
        stack = self.execute()
        self.assertEqual(states.ACTIVE, stack.status)
        server = objects.ServerList.get_all(self.ctx)[0]
        data = self.cloud_resources['server'][server.compute_id].user_data
        payload = json.loads(base64.b64decode(data).decode().split('\n', 1)[1])
        configuration = payload['write_files'][0]['content']
        self.assertIn('egress_check_url=https://registry.example.com/v2/\n',
                      configuration)

    def test_create_failure_logs_safe_cause(self):
        self.accepted()
        logs = self.useFixture(fixtures.FakeLogger())
        secret = 'password=secret'
        with mock.patch.object(stack_create.CreateStack, 'prepare',
                               side_effect=Exception(secret)):
            stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(stack_create.FAULT, stack.fault)
        self.assertNotIn(secret, logs.output)
        self.assertIn('builtins.Exception', logs.output)
        self.assertIn(self.stack.id, logs.output)
        self.assertIn('request_id=req', logs.output)

    def test_direct_mode_cannot_provision_a_guest(self):
        self.accepted()
        self.config.config(insecure_direct_url=True, group='coolify')
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_wrong_project_never_reaches_cloud(self):
        self.accepted()
        other = context.RequestContext(project_id='other')
        with self.assertRaises(exception.NotFound):
            self.manager.execute(other, 'create_stack', self.stack)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertEqual(states.PENDING, objects.Stack.get_by_id(
            self.ctx, self.stack.id).status)

    def test_invalid_worker_plan_reverts_and_returns_accepted_quota(self):
        self.accepted({'workers': [{'kind': 'vm', 'plan': 'worker'}]})
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(0, self.counters().in_use_environments)
        self.assertFalse(self.networks)

    def test_failed_guest_revert_preserves_dependency_refs(self):
        self.accepted()
        self.agent.wait_health.side_effect = RuntimeError('sensitive')
        self.proxies['nova'].delete_server.side_effect = (
            RuntimeError('sensitive'))
        stack = self.execute()
        self.assertEqual(states.ERROR, stack.status)
        self.assertTrue(stack.network_id)
        self.assertIn(stack.ssh_key_ref, self.payloads)
        self.assertIn(stack.heartbeat_key_ref, self.payloads)
        self.assertEqual(3, len(self.cloud_resources['volume']))
        self.assertEqual(1, len(self.cloud_resources['server']))
        for volume in objects.VolumeList.get_all(self.ctx):
            self.assertTrue(volume.cinder_volume_id)

    def test_partial_network_failure_reverts_ports_and_network(self):
        self.accepted()
        original = self.proxies['neutron'].create_port.side_effect

        def fail(**values):
            if values['name'].endswith('-env'):
                raise RuntimeError('sensitive')
            return original(**values)

        self.proxies['neutron'].create_port.side_effect = fail
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(self.networks)
        self.assertFalse(self.cloud_resources['port'])

    def test_lost_cinder_create_reply_is_found_and_removed(self):
        self.accepted()
        original = self.proxies['cinder'].create_volume.side_effect

        def lost(**values):
            original(**values)
            raise RuntimeError('sensitive')

        self.proxies['cinder'].create_volume.side_effect = lost
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(self.cloud_resources['volume'])

    def test_volume_failure_does_not_create_during_revert(self):
        self.accepted()
        self.proxies['cinder'].create_volume.side_effect = (
            RuntimeError('sensitive'))
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertEqual(1, self.proxies['cinder'].create_volume.call_count)
        self.assertFalse(self.cloud_resources['volume'])

    def test_lost_nova_create_reply_is_found_before_volume_cleanup(self):
        self.accepted()
        original = self.proxies['nova'].create_server.side_effect

        def lost(**values):
            original(**values)
            raise RuntimeError('sensitive')

        self.proxies['nova'].create_server.side_effect = lost
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_bootstrap_reply_loss_never_retries_post(self):
        self.accepted()
        self.agent.bootstrap.side_effect = RuntimeError('sensitive')
        self.assertEqual(states.ERROR, self.execute().status)
        self.execute()
        self.assertEqual(1, self.agent.bootstrap.call_count)
        event = objects.EventList.get_all(
            self.ctx, filters={'action': 'bootstrap'})
        self.assertEqual('start', event[0].outcome)
        self.assertFalse(any(self.cloud_resources.values()))

    def test_wrong_image_owner_fails_before_resources(self):
        self.accepted()
        self.proxies['glance'].images.return_value[0].owner = 'foreign'
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(self.networks)

    def test_version_mismatch_reverts_bootstrap_secrets_and_guest(self):
        self.accepted()
        self.driver.get_engine_version.return_value = 'foreign-version'
        self.assertEqual(states.ERROR, self.execute().status)
        self.assertFalse(any(self.cloud_resources.values()))
        self.assertFalse(self.payloads)


# A failure after each step proves the failed step and completed predecessors
# are reverted; read-only wait steps must still trigger guest teardown.
def failure_test(name):
    def test(self):
        self.fail_step(name)
    return test


for _step in ('network', 'credentials', 'data_volume', 'docker_volume',
              'registry_volume', 'boot', 'active', 'health', 'bootstrap',
              'token', 'engine'):
    setattr(StackCreateTest, 'test_revert_after_' + _step,
            failure_test(_step))


class AgentClientTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(agent_client_cert='client.pem',
                           agent_client_key='client.key', build_timeout=1,
                           group='controller_worker')
        self.config.config(ca_certificate='server-ca.pem',
                           group='certificates')
        self.client = agent_client.AgentClient(self.conf, '192.0.2.10')
        patcher = mock.patch(
            'coast.agent.namespace.management_namespace',
            side_effect=lambda name: nullcontext())
        self.scope = patcher.start()
        self.addCleanup(patcher.stop)

    def test_health_and_bootstrap_mtls_transport(self):
        url = 'https://192.0.2.10:9443'
        with requests_mock.Mocker() as http:
            http.get(url + '/health', json={
                'status': 'ok', 'egress': {'status': 'ok'}})
            http.post(url + '/v1/bootstrap', json={
                'token': 'sensitive', 'engine_version': '4.4.3'})
            self.client.wait_health()
            self.assertEqual('sensitive',
                             self.client.bootstrap('4.4.3')['token'])
            history = http.request_history
            self.assertEqual('server-ca.pem', history[-1].verify)
            self.assertEqual(('client.pem', 'client.key'), history[-1].cert)
            self.assertEqual({'engine_version': '4.4.3'}, history[-1].json())
            self.assertEqual(['coast-mgmt'] * 2,
                             [item.args[0] for item in
                              self.scope.call_args_list])

    def test_token_rotation_phases_and_response_validation(self):
        version = engine_versions.DEFAULT
        identifier = '11111111-1111-1111-1111-111111111111'
        previous = 'coast-agent-bootstrap-v1'
        url = 'https://192.0.2.10:9443/v1/bootstrap/rotate-token'
        with requests_mock.Mocker() as http:
            http.post(url, json={'token': 'replacement',
                                 'engine_version': version})
            self.assertEqual('replacement', self.client.rotate_token(
                version, identifier, previous))
            self.assertEqual(dict(engine_version=version,
                                  rotation_id=identifier,
                                  previous_name=previous, phase='create'),
                             http.last_request.json())
            http.post(url, json={'status': 'ok'})
            self.client.revoke_token(version, identifier, previous)
            self.assertEqual('revoke', http.last_request.json()['phase'])
            http.post(url, json={'token': 'private'})
            self.assertRaises(driver_base.DriverError,
                              self.client.rotate_token,
                              version, identifier, previous)
            self.assertRaises(driver_base.DriverError,
                              self.client.revoke_token,
                              version, identifier, previous)

    def test_endpoint_and_non_engine_routes(self):
        url = 'https://192.0.2.10:9443'
        self.assertEqual(url + '/api/v1', self.client.ep.base_url)
        with requests_mock.Mocker() as http:
            for path in ('/health', '/v1/health', '/v1/bootstrap',
                         '/v1/export'):
                with self.subTest(path=path):
                    http.post(url + path, json={'status': 'ok'})
                    self.assertEqual({'status': 'ok'},
                                     self.client.request('POST', path))
                    self.assertEqual(url + path, http.last_request.url)

    def test_post_error_is_sanitized_and_not_retried(self):
        with requests_mock.Mocker() as http:
            http.post('https://192.0.2.10:9443/v1/bootstrap', status_code=409,
                      text='APP_KEY=sensitive')
            with self.assertRaises(driver_base.DriverError) as error:
                self.client.bootstrap('4.4.3')
            self.assertNotIn('sensitive', str(error.exception))
            self.assertEqual(1, len(http.request_history))

    def test_bootstrap_schema_and_pin_fail_closed(self):
        for body in ({'token': 'sensitive'},
                     {'token': 'sensitive', 'env_file_backup': 'sensitive',
                      'engine_version': 'wrong'}):
            with requests_mock.Mocker() as http:
                http.post('https://192.0.2.10:9443/v1/bootstrap', json=body)
                with self.assertRaises(driver_base.DriverError):
                    self.client.bootstrap('4.4.3')

    def test_redirect_and_response_limit_fail_closed(self):
        with requests_mock.Mocker() as http:
            http.post('https://192.0.2.10:9443/v1/bootstrap', status_code=302,
                      headers={'Location': 'https://foreign.example.com'})
            with self.assertRaises(driver_base.DriverError):
                self.client.bootstrap('4.4.3')
            self.assertEqual(1, len(http.request_history))
        self.config.config(max_body_size=4, group='agent')
        with requests_mock.Mocker() as http:
            http.get('https://192.0.2.10:9443/health', text='sensitive')
            with self.assertRaises(driver_base.DriverError):
                self.client.request('GET', '/health')

    def test_egress_error_is_immediate_and_never_contains_guest_reason(self):
        body = {'status': 'ok',
                'egress': {'status': 'error', 'reason': 'secret'}}
        with mock.patch.object(self.client, 'request', return_value=body), \
                mock.patch.object(agent_client.time, 'sleep',
                                  side_effect=AssertionError('No wait')):
            error = self.assertRaises(exception.GuestEgressError,
                                      self.client.wait_health)
        self.assertEqual('no internet egress via stack router', str(error))

    def test_pending_egress_waits_then_completes(self):
        with mock.patch.object(self.client, 'request', side_effect=[
                {'status': 'ok', 'egress': {'status': 'pending'}},
                {'status': 'ok', 'egress': {'status': 'ok'}}]), \
                mock.patch.object(agent_client.time, 'sleep'), \
                mock.patch.object(agent_client.time, 'monotonic',
                                  side_effect=[0, 0]):
            self.client.wait_health()

    def test_old_health_without_egress_cannot_pass(self):
        with mock.patch.object(self.client, 'request',
                               return_value={'status': 'ok'}), \
                mock.patch.object(agent_client.time, 'monotonic',
                                  side_effect=[0, 2]):
            error = self.assertRaises(driver_base.EngineUnreachable,
                                      self.client.wait_health)
        self.assertEqual('Agent health deadline exceeded: non-ok body',
                         str(error))

    def test_health_deadline(self):
        with mock.patch.object(self.client, 'request',
                               side_effect=driver_base.EngineUnreachable()), \
                mock.patch.object(agent_client.time, 'monotonic',
                                  side_effect=[0, 2]):
            with self.assertRaises(driver_base.EngineUnreachable):
                self.client.wait_health()

    def test_request_failure_reasons(self):
        wrapped = OSError('sensitive')
        wrapped.__cause__ = OSError(errno.ECONNREFUSED, 'sensitive')
        retry = requests.exceptions.ConnectionError('sensitive')
        retry.reason = wrapped
        failures = [
            (requests.exceptions.ConnectionError(retry), 'connect refused'),
            (requests.exceptions.ConnectionError(
                OSError(errno.ECONNREFUSED, 'sensitive')), 'connect refused'),
            (requests.exceptions.ConnectionError('sensitive'),
             'connection error'),
            (requests.exceptions.ConnectTimeout('sensitive'), 'timeout'),
            (requests.exceptions.ReadTimeout('sensitive'), 'timeout'),
            (requests.exceptions.SSLError('sensitive'), 'TLS'),
            (requests.exceptions.RequestException('sensitive'),
             'request error'),
        ]
        for failure, reason in failures:
            with requests_mock.Mocker() as http:
                http.get('https://192.0.2.10:9443/health', exc=failure)
                with self.assertRaises(driver_base.EngineUnreachable) as error:
                    self.client.request('GET', '/health')
                self.assertEqual('Agent request failed', str(error.exception))
                self.assertEqual(reason, error.exception.reason)

    def test_health_failure_reasons_are_safe(self):
        cases = [({'status': 'sensitive'}, 200, 'non-ok body'),
                 ('sensitive', 200, 'non-ok body'),
                 ('sensitive', 503, 'HTTP status 503'),
                 ('sensitive', 302, 'HTTP status 302')]
        for body, status, reason in cases:
            with requests_mock.Mocker() as http, \
                    mock.patch.object(agent_client.time, 'monotonic',
                                      side_effect=[0, 2]), \
                    mock.patch.object(agent_client.LOG, 'info') as log:
                http.get('https://192.0.2.10:9443/health', json=body,
                         status_code=status)
                with self.assertRaises(driver_base.EngineUnreachable) as error:
                    self.client.wait_health()
                self.assertEqual('Agent health deadline exceeded: ' + reason,
                                 str(error.exception))
                self.assertEqual(reason, log.call_args.args[1]['reason'])
                self.assertNotIn('sensitive', str(log.call_args))

    def test_health_logs_once_per_minute_and_keeps_last_failure(self):
        self.config.config(build_timeout=130, group='controller_worker')
        failure = driver_base.EngineUnreachable('Agent request failed')
        failure.reason = 'TLS'
        with mock.patch.object(self.client, 'request', side_effect=[
                failure, {}, {}, failure, {}, failure]), \
                mock.patch.object(agent_client.time, 'monotonic',
                                  side_effect=[0, 0, 30, 59, 60, 119, 130]), \
                mock.patch.object(agent_client.time, 'sleep') as sleep, \
                mock.patch.object(agent_client.LOG, 'info') as log:
            with self.assertRaises(driver_base.EngineUnreachable) as error:
                self.client.wait_health()
            self.assertEqual('Agent health deadline exceeded: TLS',
                             str(error.exception))
            self.assertEqual([0, 60, 130],
                             [call.args[1]['elapsed']
                              for call in log.call_args_list])
            self.assertEqual(['192.0.2.10'] * 3,
                             [call.args[1]['address']
                              for call in log.call_args_list])
            self.assertEqual(5, sleep.call_count)

    def test_health_success_after_failure_does_not_log_success(self):
        with mock.patch.object(self.client, 'request', side_effect=[
                {}, {'status': 'ok', 'egress': {'status': 'ok'}}]), \
                mock.patch.object(agent_client.time, 'monotonic',
                                  side_effect=[0, 0]), \
                mock.patch.object(agent_client.time, 'sleep'), \
                mock.patch.object(agent_client.LOG, 'info') as log:
            self.client.wait_health()
            self.assertEqual(1, log.call_count)
