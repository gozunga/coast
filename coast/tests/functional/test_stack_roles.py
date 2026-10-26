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

"""Stack API, serialized fake RPC, real Taskflow and role cloud selection."""

import datetime
import json
from types import SimpleNamespace
from unittest import mock

import fixtures
from openstack import exceptions as sdk_exceptions
from openstack.image.v2 import image as sdk_image

from coast.common import clients
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import controller
from coast.conductor import manager
from coast.conductor import server_add
from coast.conductor import stack_create
from coast.conductor import worker
from coast.db import api as db_api
from coast.drivers import base
from coast.network import fip
from coast.network import stack as network
from coast import objects
from coast.tests import conductor


class StackRoleFlowTest(conductor.ConductorTest):
    def setUp(self):
        super().setUp()
        # These retained fixtures exercise the older supported release.
        self.config.config(pinned_version='4.4.3', group='coolify')
        self.config.config(driver='coolify_v4',
                           enabled_drivers=['coolify_v4'], mgmt_net_id='mgmt',
                           env_cidr_pool='192.0.2.0/24', env_subnet_prefix=24,
                           agent_client_ca='ca.pem',
                           agent_client_cert='client.pem',
                           agent_client_key='client.key',
                           health_manager_ips=['192.0.2.5'],
                           image_owner_id='owner',
                           image_tag_controller='golden',
                           image_tag_worker='golden',
                           controller_image_id='control-image-id',
                           worker_image_id='worker-image-id',
                           data_volume_size_gb=10,
                           docker_volume_size_gb=20, group='controller_worker')
        self.config.config(ca_certificate='ca.pem', group='certificates')
        self.config.config(definitions=json.dumps([
            dict(name=role, kind='vm', roles=[role], flavor=role + '-flavor')
            for role in ('control', 'worker', 'build')]), group='plans')
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.1'
        self.proxies = {service: mock.Mock() for service in (
            'glance', 'nova', 'neutron', 'cinder')}
        self.images = [
            sdk_image.Image(
                id=role + '-image-id', name=role + '-image', owner='owner',
                status='active', tags=['golden'],
                properties={'coast_role': 'controller' if role == 'control'
                            else 'worker'})
            for role in ('control', 'worker', 'build')]
        self.proxies['glance'].images.side_effect = lambda **kw: self.images
        self.proxies['nova'].find_flavor.side_effect = lambda name, **kw: (
            SimpleNamespace(id=name + '-id'))
        self.proxies['nova'].create_server.side_effect = lambda **kw: (
            SimpleNamespace(id=kw['name']))
        self.patch_cloud(clients, 'get_connection', return_value=(
            SimpleNamespace(network=self.proxies['neutron'])))
        self.patch_cloud(
            controller.ControllerCloud, 'proxy',
            side_effect=lambda service: self.proxies[service])
        self.patch_cloud(
            network.EnvironmentNetwork, 'create', return_value=dict(
                network_id='net', subnet_id='subnet', router_id='router',
                security_groups={'worker': 'worker-sg', 'ctrl': 'ctrl-sg',
                                 'agent-mgmt': 'agent-sg',
                                 'build': 'build-sg'}))
        self.patch_cloud(controller.ControllerCloud, 'check_router')
        self.patch_cloud(controller.ControllerCloud, 'port', side_effect=(
            lambda server, role, *args: SimpleNamespace(
                id=server.id + '-' + role,
                fixed_ips=[dict(ip_address='192.0.2.10')])))
        self.patch_cloud(controller.ControllerCloud, 'volume', side_effect=(
            lambda volume: SimpleNamespace(id=volume.id)))
        self.patch_cloud(controller.ControllerCloud, 'wait_volume')
        self.patch_cloud(controller.ControllerCloud, 'wait_server')
        drive = ('data', 'serial', datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(days=30))
        for cls in (controller.ControllerCloud, worker.WorkerCloud):
            self.patch_cloud(cls, 'config_drive', return_value=drive)
        agent = mock.Mock()
        agent.bootstrap.return_value = dict(
            token='test-token')
        self.agent = agent
        self.patch_cloud(agent_client, 'AgentClient', return_value=agent)
        self.real_endpoint = manager.Manager._endpoint
        self.patch_cloud(manager.Manager, '_endpoint', return_value=(
            base.EngineEndpoint('https://example.com/engine', '', '', '',
                                'test-token')))
        driver = mock.Mock(SUPPORTED_ENGINE_VERSIONS=['4.4.3'])
        driver.get_engine_version.return_value = '4.4.3'
        driver.bootstrap_environment.return_value = base.DriverResult('engine')
        driver.register_server.return_value = base.DriverResult('registered')
        self.patch_cloud(stack_create.loader, 'load_driver',
                         return_value=driver)
        self.manager.drivers['coolify_v4'] = driver
        self.patch_cloud(server_add.fip.FloatingIP, 'ensure', return_value=(
            SimpleNamespace(id='fip', floating_ip_address='198.51.100.10')))
        self.failures = []
        # safe_log is shared by every module, so record and still log;
        # inherited API tests in this class assert on the real log line.
        real_log_failure = stack_create.safe_log.log_failure

        def record_failure(logger, message, error, **kw):
            self.failures.append((type(error).__name__, kw))
            return real_log_failure(logger, message, error, **kw)

        self.patch_cloud(stack_create.safe_log, 'log_failure',
                         side_effect=record_failure)
        self.body = dict(name='selected', plan='control',
                         image='control-image',
                         workers=[dict(kind='vm', plan='worker',
                                       image='worker-image')],
                         data_volume_gb=10)

    def patch_cloud(self, target, name, **kwargs):
        return self.useFixture(fixtures.MockPatchObject(
            target, name, **kwargs)).mock

    def check_create(self, build):
        if build:
            self.body.update(build_server=True, build_server_plan='build',
                             build_server_image='build-image')
        created = self.request('post', '/v1/stacks', {'stack': self.body},
                               status=202, complete=True).json['stack']
        stack = objects.Stack.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ACTIVE, stack.status, self.failures)
        servers = objects.ServerList.get_all(
            self.ctx, filters={'stack_id': stack.id})
        self.assertEqual(3 if build else 2, len(servers))
        for server in servers:
            self.assertEqual(states.ACTIVE, server.status)
            role = server.role
            self.assertEqual(role + '-image-id', server.image_id)
            self.assertEqual(role + '-flavor-id', server.flavor_id)
            if role == 'worker':
                self.assertEqual('198.51.100.10', server.floating_ip)
            else:
                self.assertIsNone(server.floating_ip)
        boots = self.proxies['nova'].create_server.call_args_list
        self.assertEqual({server.image_id for server in servers},
                         {call.kwargs['image_id'] for call in boots})
        usage = self.usage()
        self.assertEqual(2 if build else 1, usage['in_use_servers'])
        self.assertEqual(1 if build else 0, usage['in_use_build_servers'])
        self.assertEqual(110 if build else 80, usage['in_use_volume_gb'])
        self.assertEqual(0, usage['reserved_servers'])
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        self.request('get', '/v1/stacks/' + stack.id, headers=foreign,
                     status=404)

    def test_controller_worker_selections_over_rpc(self):
        self.check_create(False)

    def test_controller_worker_build_selections_over_rpc(self):
        self.check_create(True)

    def test_build_quota_rejection_creates_no_rows(self):
        self.config.config(build_servers=0, group='quotas')
        self.body.update(build_server=True, build_server_plan='build',
                         build_server_image='build-image')
        self.request('post', '/v1/stacks', {'stack': self.body}, status=403)
        self.assertFalse(objects.StackList.get_all(self.ctx))
        self.assertFalse(objects.ServerList.get_all(self.ctx))

    def test_omitted_images_keep_configured_defaults(self):
        self.body.pop('image')
        self.body['workers'][0].pop('image')
        self.body.update(build_server=True)
        created = self.request('post', '/v1/stacks', {'stack': self.body},
                               status=202, complete=True).json['stack']
        self.assertEqual(states.ACTIVE, objects.Stack.get_by_id(
            self.ctx, created['id']).status, self.failures)
        servers = objects.ServerList.get_all(
            self.ctx, filters={'stack_id': created['id']})
        self.assertEqual({'control': 'control-image-id',
                          'worker': 'worker-image-id',
                          'build': 'worker-image-id'},
                         {server.role: server.image_id for server in servers})

    def test_catalog_change_after_acceptance_fails_closed(self):
        self.proxies['glance'].images.side_effect = [
            self.images, self.images, []]
        created = self.request('post', '/v1/stacks', {'stack': self.body},
                               status=202, complete=True).json['stack']
        stack = objects.Stack.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ERROR, stack.status)
        self.assertFalse(objects.ServerList.get_all(self.ctx))
        usage = self.usage()
        self.assertEqual(0, usage['in_use_servers'])
        self.assertEqual(0, usage['reserved_servers'])
        self.assertEqual(0, usage['in_use_volume_gb'])

    def _allow_cloud_delete(self, stack):
        gone = set()

        def get_volume(ident):
            if ident in gone:
                raise sdk_exceptions.ResourceNotFound('missing')
            return SimpleNamespace(
                id=ident, status='available', attachments=[],
                metadata={'coast': 'true', 'project_id': stack.project_id,
                          'stack_id': stack.id})

        def delete_volume(ident, **kwargs):
            gone.add(ident if isinstance(ident, str) else ident.id)

        cinder = self.proxies['cinder']
        cinder.get_volume.side_effect = get_volume
        cinder.delete_volume.side_effect = delete_volume
        cinder.wait_for_delete.side_effect = lambda *args, **kwargs: None
        cinder.snapshots.side_effect = lambda **kwargs: []
        cinder.volumes.side_effect = lambda **kwargs: []
        self.proxies['nova'].servers.side_effect = lambda **kwargs: []
        self.proxies['nova'].get_server.side_effect = (
            sdk_exceptions.ResourceNotFound('missing'))
        self.proxies['neutron'].ports.side_effect = lambda **kwargs: []
        self.patch_cloud(network.EnvironmentNetwork, 'delete')
        self.patch_cloud(fip.FloatingIP, 'delete')

    def test_delete_releases_volume_quota(self):
        self.check_create(True)
        stack = objects.StackList.get_all(self.ctx)[0]
        self.assertEqual(110, self.usage()['in_use_volume_gb'])
        self._allow_cloud_delete(stack)
        deleted = self.request(
            'delete', '/v1/stacks/' + stack.id, status=202,
            complete=True).json['stack']
        current = objects.Stack.get_by_id(
            self.ctx, deleted['id'], show_deleted=True)
        self.assertEqual(states.DELETED, current.status, current.fault)
        usage = self.usage()
        self.assertEqual(0, usage['in_use_volume_gb'])
        self.assertEqual(0, usage['in_use_servers'])
        self.assertEqual(0, usage['in_use_build_servers'])
        self.assertEqual(0, usage['in_use_environments'])
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])

    def test_later_volume_overquota_then_delete_releases_once(self):
        self.config.config(volume_gb=50, group='quotas')
        self.body.update(build_server=True, build_server_plan='build',
                         build_server_image='build-image')
        created = self.request(
            'post', '/v1/stacks', {'stack': self.body}, status=202,
            complete=True).json['stack']
        stack = objects.Stack.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ERROR, stack.status, self.failures)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertTrue(any(
            name == 'OverQuota' and str(kw.get('step', '')).endswith(
                '-data_volume') for name, kw in self.failures),
            self.failures)
        usage = self.usage()
        for key in ('volume_gb', 'servers', 'build_servers', 'environments'):
            self.assertEqual(0, usage['in_use_' + key], key)
            self.assertEqual(0, usage['reserved_' + key], key)
        self.assertEqual(50, sum(
            item.size_gb for item in objects.VolumeList.get_all(self.ctx)))
        self._allow_cloud_delete(stack)
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        current = objects.Stack.get_by_id(
            self.ctx, stack.id, show_deleted=True)
        self.assertEqual(states.DELETED, current.status, current.fault)
        usage = self.usage()
        for key in ('volume_gb', 'servers', 'build_servers', 'environments'):
            self.assertEqual(0, usage['in_use_' + key], key)
            self.assertEqual(0, usage['reserved_' + key], key)
        self.assertFalse(objects.VolumeList.get_all(self.ctx))
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.request('delete', '/v1/stacks/' + stack.id, status=404)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])

    def test_health_failure_then_delete_releases_volume_quota_once(self):
        guests = {}

        def create_server(**kwargs):
            guest = SimpleNamespace(
                id=kwargs['name'], name=kwargs['name'], status='ACTIVE',
                tags=list(kwargs.get('tags') or []))
            guests[guest.id] = guest
            return guest

        def list_servers(**kwargs):
            name = kwargs.get('name')
            return [item for item in guests.values()
                    if name is None or item.name == name]

        def get_server(ident):
            if ident not in guests:
                raise sdk_exceptions.ResourceNotFound('missing')
            return guests[ident]

        def delete_server(item, **kwargs):
            guests.pop(getattr(item, 'id', item), None)

        self.proxies['nova'].create_server.side_effect = create_server
        self.proxies['nova'].servers.side_effect = list_servers
        self.proxies['nova'].get_server.side_effect = get_server
        self.proxies['nova'].delete_server.side_effect = delete_server
        self.proxies['nova'].wait_for_delete.side_effect = (
            lambda *args, **kwargs: None)
        self.agent.wait_health.side_effect = base.EngineUnreachable(
            'timeout')
        created = self.request(
            'post', '/v1/stacks', {'stack': self.body}, status=202,
            complete=True).json['stack']
        stack = objects.Stack.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ERROR, stack.status, self.failures)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertFalse(guests)
        usage = self.usage()
        for key in ('volume_gb', 'servers', 'environments'):
            self.assertEqual(0, usage['in_use_' + key], key)
            self.assertEqual(0, usage['reserved_' + key], key)
        self._allow_cloud_delete(stack)
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        current = objects.Stack.get_by_id(
            self.ctx, stack.id, show_deleted=True)
        self.assertEqual(states.DELETED, current.status, current.fault)
        usage = self.usage()
        for key in ('volume_gb', 'servers', 'environments'):
            self.assertEqual(0, usage['in_use_' + key], key)
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.request('delete', '/v1/stacks/' + stack.id, status=404)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])

    def _delete_with_engine_error(self, error):
        self.check_create(True)
        stack = objects.StackList.get_all(self.ctx)[0]
        self._allow_cloud_delete(stack)
        # Keep the controller visible so teardown must attempt engine calls.
        servers = objects.ServerList.get_all(self.ctx, filters={
            'stack_id': stack.id})
        server_ids = {server.compute_id: server.id for server in servers}
        self.proxies['nova'].get_server.side_effect = lambda ident: (
            SimpleNamespace(id=ident, status='ACTIVE', tags=[
                'coast', 'project_id=' + stack.project_id,
                'stack_id=' + stack.id, 'server_id=' + server_ids[ident]]))
        driver = self.manager.drivers['coolify_v4']
        driver.unregister_server.side_effect = error
        return stack

    def _delete_children_with_engine_failure(self, status, error,
                                             refuses=False,
                                             missing_controller=False):
        stack = self._delete_with_engine_error(
            base.EngineUnreachable('private transport detail'))
        parents = self.service_parents(stack.id)
        source = objects.Source(context=self.ctx, type='image', image='web:1')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=stack.id, **parents, name='web',
            source_id=source.id, engine_ref='app-ref', ports='80',
            replicas=1, auto_deploy=False, status=states.ERROR,
            task_state=states.NONE)
        service.create()
        stack.status = status
        stack.save()
        if missing_controller:
            controller_ids = set()
            for server in objects.ServerList.get_all(
                    self.ctx, filters={
                        'stack_id': stack.id, 'role': 'control'}):
                controller_ids.add(server.compute_id)
                server.mgmt_ip = None
                server.save()
            get_server = self.proxies['nova'].get_server.side_effect

            def observed_server(identifier):
                if identifier in controller_ids:
                    raise sdk_exceptions.ResourceNotFound('controller removed')
                return get_server(identifier)

            self.proxies['nova'].get_server.side_effect = observed_server
            self.patch_cloud(manager.Manager, '_endpoint',
                             new=self.real_endpoint)
            self.assertRaisesRegex(
                base.DriverError, 'Engine endpoint is not configured',
                self.manager._endpoint, stack)
        driver = self.manager.drivers['coolify_v4']
        for method in ('get_app', 'set_domains', 'delete_app',
                       'delete_environment', 'delete_project'):
            getattr(driver, method).side_effect = error
        for path, cls, identifier in (
                ('services', objects.Service, service.id),
                ('environments', objects.Environment,
                 parents['environment_id']),
                ('appgroups', objects.Appgroup, parents['appgroup_id'])):
            self.request('delete', '/v1/%s/%s' % (path, identifier),
                         status=202, complete=True)
            if refuses:
                saved = cls.get_by_id(self.ctx, identifier)
                self.assertEqual(states.ERROR, saved.status)
                self.assertIsNone(saved.deleted_at)
                # Remove the failed record to exercise the next child type.
                saved.destroy()
            else:
                self.assertRaises(exception.NotFound, cls.get_by_id,
                                  self.ctx, identifier)
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        saved = objects.Stack.get_by_id(self.ctx, stack.id, show_deleted=True)
        self.assertEqual(states.DELETED, saved.status, saved.fault)
        self.assertFalse(objects.ServerList.get_all(self.ctx))
        self.assertFalse(objects.VolumeList.get_all(self.ctx))
        self.proxies['nova'].delete_server.assert_called()
        self.proxies['cinder'].delete_volume.assert_called()
        network.EnvironmentNetwork.delete.assert_called_once()
        fip.FloatingIP.delete.assert_called()
        self.assertEqual(0, self.usage()['in_use_environments'])

    def test_active_reachable_engine_errors_preserve_children_over_rpc(self):
        self._delete_children_with_engine_failure(
            states.ACTIVE, base.EngineAuthError('private engine detail'),
            refuses=True)

    def test_error_stack_without_controller_deletes_children_over_rpc(self):
        self._delete_children_with_engine_failure(
            states.ERROR, base.EngineUnreachable('private transport detail'),
            missing_controller=True)

    def test_error_stack_children_can_be_deleted_over_rpc(self):
        self._delete_children_with_engine_failure(
            states.ERROR, base.EngineAuthError('private engine detail'))

    def test_unreachable_stack_children_can_be_deleted_over_rpc(self):
        self._delete_children_with_engine_failure(
            states.ACTIVE, base.EngineUnreachable('private transport detail'))

    def test_delete_unreachable_engine_over_rpc(self):
        stack = self._delete_with_engine_error(
            base.EngineUnreachable('sensitive transport detail'))
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        saved = objects.Stack.get_by_id(self.ctx, stack.id, show_deleted=True)
        self.assertEqual(states.DELETED, saved.status, saved.fault)
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': stack.id, 'action': 'delete.engine.skip'})
        self.assertEqual(1, len(events))
        self.assertEqual('failure', events[0].outcome)
        self.assertEqual('Engine cleanup skipped: engine unreachable.',
                         events[0].message)
        for key in ('volume_gb', 'servers', 'build_servers', 'environments'):
            self.assertEqual(0, self.usage()['in_use_' + key])
        self.assertFalse(objects.VolumeList.get_all(self.ctx))
        end = [payload for name, payload in self.manager.notifier.events
               if name == 'coast.environment.delete.end'
               and payload['resource_id'] == stack.id]
        self.assertEqual(1, len(end))
        self.assertEqual(states.DELETED, end[0]['status'])

    def test_unreachable_engine_then_cloud_failure_has_no_delete_end(self):
        stack = self._delete_with_engine_error(
            base.EngineUnreachable('offline'))
        self.patch_cloud(network.EnvironmentNetwork, 'delete',
                         side_effect=RuntimeError('network failure'))
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        saved = objects.Stack.get_by_id(self.ctx, stack.id)
        self.assertEqual(states.ERROR, saved.status)
        self.assertEqual('network failure', saved.fault)
        self.assertEqual(1, self.usage()['in_use_environments'])
        self.assertTrue(saved.engine_token_ref)
        self.assertTrue(objects.EventList.get_all(self.ctx, filters={
            'resource_id': stack.id, 'action': 'delete.engine.skip'}))
        self.assertFalse([payload for name, payload
                          in self.manager.notifier.events
                          if name == 'coast.environment.delete.end'])

    def test_delete_auth_error_over_rpc_still_blocks(self):
        stack = self._delete_with_engine_error(
            base.EngineAuthError('Coolify API returned HTTP 401'))
        before = self.usage()['in_use_volume_gb']
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        saved = objects.Stack.get_by_id(self.ctx, stack.id)
        self.assertEqual(states.ERROR, saved.status)
        # The serialized conductor failure retains operator diagnostics,
        # while tenant show and list responses use the public wording.
        self.assertEqual('Coolify API returned HTTP 401', saved.fault)
        path = '/v1/stacks/' + stack.id
        shown = self.request('get', path).json['stack']
        listed = self.request('get', '/v1/stacks').json['stacks']
        listed = next(row for row in listed if row['id'] == stack.id)
        for row in (shown, listed):
            self.assertEqual('application engine API returned HTTP 401',
                             row['fault'])
        self.assertEqual(before, self.usage()['in_use_volume_gb'])
        self.assertTrue(objects.VolumeList.get_all(self.ctx))
        self.assertFalse(objects.EventList.get_all(self.ctx, filters={
            'resource_id': stack.id, 'action': 'delete.engine.skip'}))
        self.assertFalse([payload for name, payload
                          in self.manager.notifier.events
                          if name == 'coast.environment.delete.end'])

    def test_interrupted_create_then_delete_releases_quota(self):
        deltas = quota.deltas('stack', dict(
            workers=[dict(kind='vm')], build_server=True, data_volume_gb=12))
        with db_api.writer.using(self.ctx):
            reservation = quota.reserve(self.ctx, **deltas)
            stack = objects.Stack(
                context=self.ctx, name='interrupted', plan='control',
                driver='coolify_v4', engine_version='4.4.3',
                status=states.BUILDING, task_state=states.NONE)
            stack.create()
            stack.acquire_task(states.PROVISIONING)
            stack.conductor_host = self.conf.host
            stack.save()
            quota.commit(self.ctx, reservation)
        self._allow_cloud_delete(stack)
        self.manager.recover_stacks()
        self.assertFalse(objects.VolumeList.get_all(
            self.ctx, filters={'stack_id': stack.id}))
        usage = self.usage()
        self.assertEqual(0, usage['in_use_volume_gb'])
        self.assertEqual(0, usage['in_use_servers'])
        self.assertEqual(0, usage['in_use_build_servers'])
        self.assertEqual(0, usage['in_use_environments'])
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        current = objects.Stack.get_by_id(
            self.ctx, stack.id, show_deleted=True)
        self.assertEqual(states.DELETED, current.status, current.fault)
        self.proxies['cinder'].snapshots.assert_called_once_with(details=True)
        self.proxies['cinder'].volumes.assert_called_once_with()
        usage = self.usage()
        self.assertEqual(0, usage['in_use_volume_gb'])
        self.assertEqual(0, usage['in_use_servers'])
        self.assertEqual(0, usage['in_use_build_servers'])
        self.assertEqual(0, usage['in_use_environments'])
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
