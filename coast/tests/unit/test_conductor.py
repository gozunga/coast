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

"""Conductor accounting, replay, revert, and secret-boundary regressions."""

import concurrent.futures
import copy
import datetime
import queue
import threading
import time
import types
from unittest import mock

import fixtures
from oslo_db import exception as db_exception
import oslo_messaging as messaging
from oslo_messaging.notify import _impl_test as test_driver
from oslo_utils import timeutils
from sqlalchemy import orm
from stevedore import extension

from coast.api import errors
from coast.api import microversions
from coast.api import secret_store
from coast.cmd import conductor as conductor_cmd
from coast.common import context
from coast.common import exception
from coast.common import naming
from coast.common import notifications
from coast.common import quota
from coast.common import states
from coast.conductor import dns as managed_dns
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.conductor import service
from coast.db import api as db_api
from coast.drivers import base as driver_base
from coast import objects
from coast.tests import base
from coast.tests import conductor
from coast.tests.functional import test_conductor as functional
from coast.tests.unit.test_adoption import FakeDriver


class ManagerTest(conductor.ConductorTest):
    test_infrastructure_completion = functional.InfrastructureFlowTest.\
        test_stack_server_volume_completion_and_delete
    test_workload_completion = functional.WorkloadFlowTest.\
        test_service_deployment_domain_export_completion

    test_delete_service_soft_deletes_deployment_history = (
        functional.WorkloadFlowTest.
        test_delete_service_soft_deletes_deployment_history)

    def test_concurrent_operations_keep_their_own_notifications(self):
        barrier = threading.Barrier(8)
        first_done = threading.Event()
        resources = [types.SimpleNamespace(
            id=str(i), updated_at=None, created_at=None) for i in range(8)]
        flushed = []
        seen = {}

        def run(ctx, operation, resource, *args):
            # All RPC handlers have initialized their buffers before any
            # queue/commit. The first commit clears only its own buffer.
            barrier.wait(timeout=10)
            self.manager._pending.append(resource.id)
            self.manager._seen.add(resource.id)
            barrier.wait(timeout=10)
            if resource.id != '0':
                self.assertTrue(first_done.wait(timeout=10))
                seen[resource.id] = set(self.manager._seen)

        def execute(resource):
            try:
                self.manager._execute_operation(
                    self.ctx, 'create_service', resource, 'test', 'create',
                    None, None, 'accepted')
            finally:
                if resource.id == '0':
                    first_done.set()

        with mock.patch.object(self.manager, '_run', side_effect=run), \
                mock.patch.object(self.manager, '_flush',
                                  side_effect=lambda rows: flushed.extend(
                                      rows)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(execute, resources))
        self.assertCountEqual([row.id for row in resources], flushed)
        self.assertEqual({str(i): {str(i)} for i in range(1, 8)}, seen)
        self.assertIsNone(self.manager._pending)

    def test_configured_driver_workload_and_cloud_boundary(self):
        path = self.stack()
        env = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        env.driver = 'fake'
        env.engine_version = '4.4.3'
        env.engine_token_ref = 'https://secrets.example.com/opaque'
        env.save()
        self.config.config(enabled_drivers=['noop', 'fake'],
                           group='controller_worker')
        self.config.config(insecure_direct_url=True,
                           adopted_endpoints={
                               env.id: 'https://engine.example.com'},
                           group='coolify')
        plugin = mock.Mock()
        plugin.name = 'fake'
        plugin.load.return_value = FakeDriver
        store = mock.Mock()
        store.get.return_value = 'engine-token'
        before = copy.deepcopy(self.manager.cloud.resources)
        with mock.patch.object(extension.ExtensionManager, 'list_entry_points',
                               return_value=[plugin]), mock.patch.object(
                                   secret_store, 'STORE', store):
            created = self.request('post', '/v1/services', self.service_body(
                self.worker()), status=202, complete=True).json['service']
            service_path = '/v1/services/' + created['id']
            service = objects.Service.get_by_id(self.ctx, created['id'])
            self.assertEqual(states.ACTIVE, service.status)
            second = self.request('post', service_path + '/deployments', {
                'deployment': {'source_ref': 'release'}},
                status=202, complete=True).json['deployment']
            self.assertEqual(states.ACTIVE, objects.Deployment.get_by_id(
                self.ctx, second['id']).status)
            rolled = self.request('post', service_path + '/deployments/' +
                                  created['deployment_id'] + '/action',
                                  {'rollback': {}}, status=202,
                                  complete=True).json['deployment']
            self.assertEqual(states.ACTIVE, objects.Deployment.get_by_id(
                self.ctx, rolled['id']).status)
            self.request('delete', service_path, status=202, complete=True)
            self.request('get', service_path, status=404)
            self.request('post', path + '/volumes', {'volume': {
                'name': 'blocked', 'size_gb': 1}}, status=202, complete=True)
        self.assertEqual(before, self.manager.cloud.resources)
        self.assertNotIn('engine-token', str(self.manager.notifier.events))
        self.assertNotIn(env.engine_token_ref,
                         str(self.manager.notifier.events))

    def worker(self):
        return next(item for item in objects.ServerList.get_all(self.ctx)
                    if item.role == 'worker')

    def service_body(self, worker, **extra):
        body = {**self.service_parents(worker.stack_id),
                'name': 'web', 'server_id': worker.id, 'ports': '8080',
                'source': {'type': 'git',
                           'repo_url': 'https://example.com/web.git'}}
        body.update(extra)
        return {'service': body}

    def capture(self, ctx, operation, resource, options=None):
        self.messages.append(dict(
            operation=operation, resource=resource.obj_clone(),
            options=options or {}, revision=rpcapi.revision(resource),
            reservation=copy.deepcopy(getattr(ctx, 'quota_reservation', {}))))

    def send(self, message):
        self.rpc.client.cast(self.ctx, 'execute', **message)
        self.manager.done.get(timeout=10)

    def counters(self):
        return {k: v for k, v in self.usage().items()
                if k.startswith(('reserved_', 'in_use_'))}

    def test_volume_cast_order_and_duplicate_completion(self):
        path = self.stack()
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            volume = self.request('post', path + '/volumes', {'volume': {
                'name': 'data', 'size_gb': 10}}, status=202).json['volume']
            vp = path + '/volumes/' + volume['id']
            self.request('patch', vp, {'volume': {'size_gb': 20}}, status=202)
            self.request('patch', vp, {'volume': {'size_gb': 30}}, status=202)
        self.assertEqual(30, self.usage()['reserved_volume_gb'])
        self.send(self.messages[2])  # Last extend can arrive before create.
        self.assertEqual(states.AVAILABLE, self.request('get', vp).json[
            'volume']['status'])
        self.assertEqual(40, self.usage()['in_use_volume_gb'])
        self.assertEqual(0, self.usage()['reserved_volume_gb'])
        before = self.counters()
        for message in self.messages:
            self.send(message)
        self.assertEqual(before, self.counters())
        row = objects.Volume.get_by_id(self.ctx, volume['id'])
        self.assertEqual(30, self.manager.cloud.resources[
            row.cinder_volume_id]['size_gb'])
        self.request('patch', vp, {'volume': {'size_gb': 40}},
                     status=202, complete=True)
        self.assertEqual(50, self.usage()['in_use_volume_gb'])
        self.request('post', vp + '/action', {'snapshot': {}},
                     status=202, complete=True)
        self.assertEqual(states.AVAILABLE, objects.Volume.get_by_id(
            self.ctx, volume['id']).status)
        self.request('delete', vp, status=202, complete=True)
        self.assertEqual(states.ERROR, self.request('get', vp).json[
            'volume']['status'])
        self.assertEqual(50, self.usage()['in_use_volume_gb'])
        self.manager.cloud.delete(self.manager.cloud.reference(
            'snapshot', volume['id']))
        self.request('delete', vp, status=202, complete=True)
        self.request('get', vp, status=404)
        self.assertEqual(10, self.usage()['in_use_volume_gb'])

    def test_delete_volume_before_create_cast_is_rejected(self):
        path = self.stack()
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            row = self.request('post', path + '/volumes', {'volume': {
                'name': 'pending', 'size_gb': 15}},
                status=202).json['volume']
            vp = path + '/volumes/' + row['id']
            self.request('delete', vp, status=409)
        self.assertEqual(1, len(self.messages))
        self.send(self.messages[0])
        self.assertEqual(states.AVAILABLE,
                         self.request('get', vp).json['volume']['status'])
        self.request('delete', vp, status=202, complete=True)
        self.request('get', vp, status=404)
        self.assertEqual(0, self.usage()['reserved_volume_gb'])
        self.assertEqual(10, self.usage()['in_use_volume_gb'])

    def test_volume_failure_restores_accounted_size(self):
        path = self.stack()
        volume = self.request('post', path + '/volumes', {'volume': {
            'name': 'data', 'size_gb': 10}}, status=202,
            complete=True).json['volume']
        vp = path + '/volumes/' + volume['id']
        with mock.patch.object(self.manager.cloud, 'extend',
                               side_effect=RuntimeError(
                                   'password=do-not-log')):
            self.request('patch', vp, {'volume': {'size_gb': 20}},
                         status=202, complete=True)
        row = objects.Volume.get_by_id(self.ctx, volume['id'])
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual(10, row.size_gb)
        self.assertEqual(20, self.usage()['in_use_volume_gb'])
        self.assertEqual(0, self.usage()['reserved_volume_gb'])
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'outcome': 'failure'})
        self.assertTrue(events)
        self.assertNotIn('do-not-log', events[0].message)
        self.request('delete', vp, status=202, complete=True)
        self.assertEqual(10, self.usage()['in_use_volume_gb'])

    def test_stack_failure_reverts_and_does_not_reseed_quota(self):
        self.config.config(environments=1, group='quotas')
        driver = self.manager._driver(objects.Stack(
            driver='noop', status=states.PENDING, engine_version='noop-1.0'))
        original = driver.register_server

        def fail_worker(ep, env, server, key):
            if server.role == 'worker':
                raise RuntimeError('token=never-log-this')
            return original(ep, env, server, key)

        with mock.patch.object(driver, 'register_server',
                               side_effect=fail_worker):
            path = self.stack()
        row = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual(states.NONE, row.task_state)
        self.assertNotIn('never-log-this', row.fault)
        self.assertFalse(self.manager.cloud.resources)
        self.assertEqual(0, self.usage()['reserved_environments'])
        self.assertEqual(0, self.usage()['reserved_servers'])
        self.assertEqual(0, self.usage()['reserved_volume_gb'])
        self.assertFalse(objects.ServerList.get_all(self.ctx))
        self.assertFalse(objects.VolumeList.get_all(self.ctx))
        new = self.stack(name='replacement')
        self.assertEqual(states.ACTIVE, self.request('get', new).json[
            'stack']['status'])
        self.assertEqual(1, self.usage()['in_use_environments'])
        self.request('delete', path, status=202, complete=True)
        self.request('get', path, status=404)
        self.assertEqual(states.DELETED, objects.Stack.get_by_id(
            self.ctx, row.id, show_deleted=True).status)
        self.assertEqual(1, self.usage()['in_use_environments'])

    def test_server_completion_and_replay(self):
        path = self.stack()
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            row = self.request('post', path + '/servers', {'server': {
                'name': 'builder', 'kind': 'vm', 'role': 'build',
                'plan': 'small'}}, status=202).json['server']
        self.send(self.messages[0])
        before = self.counters()
        self.send(self.messages[0])
        self.assertEqual(before, self.counters())
        self.send(dict(self.messages[0], operation='failover_server'))
        self.assertEqual(before, self.counters())
        sp = path + '/servers/' + row['id']
        server = objects.Server.get_by_id(self.ctx, row['id'])
        self.assertEqual(states.ACTIVE, server.status)
        self.assertEqual(states.NONE, server.task_state)
        for record in self.manager.cloud.resources.values():
            self.assertIn('project_id=project', record['tags'])
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        self.request('post', sp + '/action', {'failover': {}},
                     headers=admin, status=202, complete=True)
        self.request('post', path + '/action',
                     {'upgrade': {'image_id': 'unsupported'}},
                     headers=admin, status=400)
        self.request('delete', sp, status=202, complete=True)
        self.request('get', sp, status=404)
        listed = self.request('get', path + '/servers').json['servers']
        self.assertNotIn(row['id'], [item['id'] for item in listed])
        kept = objects.Server.get_by_id(
            self.ctx, row['id'], show_deleted=True)
        self.assertIsNotNone(kept.deleted_at)
        self.assertEqual(states.DELETING, kept.status)
        self.assertEqual(states.NONE, kept.task_state)
        self.assertEqual(0, self.usage()['in_use_build_servers'])

    def test_rpc_context_drops_token_and_failure_releases_unclaimed(self):
        self.ctx.auth_token = 'never-forward'
        serializer = rpcapi.Serializer()
        wire = serializer.serialize_context(self.ctx)
        self.assertNotIn('auth_token', wire)
        self.assertEqual('project', serializer.deserialize_context(
            wire).project_id)
        with mock.patch.object(self.rpc, '_client') as client:
            client.prepare.return_value.cast.side_effect = RuntimeError(
                'broker-password')
            self.request('post', '/v1/stacks', {'stack': {
                'name': 'uncertain', 'plan': 'small', 'data_volume_gb': 5}},
                status=500)
        env = objects.StackList.get_all(self.ctx)[0]
        self.assertEqual(states.NONE, env.task_state)
        self.assertEqual(states.ERROR, env.status)
        self.assertEqual(0, self.usage()['reserved_environments'])
        self.assertEqual(0, self.usage()['reserved_volume_gb'])
        with self.assertRaises(exception.Invalid):
            self.manager.execute(self.ctx, 'unsupported_stack', env)
        with self.assertRaises(exception.NotFound):
            other = type(self.ctx).from_dict(self.ctx.to_dict())
            other.project_id = 'other'
            self.manager.execute(other, 'create_stack', env)

    def test_private_git_create_fetches_credentials_only_in_conductor(self):
        self.headers['OpenStack-API-Version'] = (
            'app-hosting ' + microversions.DEPLOY_KEY_VERSION)
        self.stack()
        worker = self.worker()
        driver = self.manager.drivers['noop']
        body = self.service_body(worker, source={
            'type': 'git', 'repo_url': 'git@example.com:org/repo',
            'deploy_key': {'generate': True}})
        with mock.patch.object(driver, 'create_app',
                               wraps=driver.create_app) as create:
            view = self.request('post', '/v1/services', body,
                                status=202, complete=True).json['service']
        saved = objects.Service.get_by_id(self.ctx, view['id'])
        source = objects.Source.get_by_id(self.ctx, saved.source_id)
        self.assertEqual(self.store.get(self.ctx, source.credentials_ref),
                         create.call_args.args[4])
        self.assertNotIn('PRIVATE KEY', str(view))
        self.assertNotIn('PRIVATE KEY', str(self.manager.notifier.events))
        self.request('delete', '/v1/services/' + saved.id,
                     status=202, complete=True)
        self.assertNotIn(source.credentials_ref, self.store.values)

    def test_service_uses_fake_transport(self):
        self.stop_server()
        worker = service.Service(self.conf, endpoint=self.manager)
        worker.start()
        try:
            path = self.stack()
            self.assertEqual(states.ACTIVE, self.request('get', path).json[
                'stack']['status'])
        finally:
            worker.stop()

    def test_real_driver_fails_closed(self):
        self.config.config(driver='coolify_v4', group='controller_worker')
        path = self.stack()
        env = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        self.assertEqual(states.ERROR, env.status)
        self.assertEqual(states.NONE, env.task_state)
        self.assertFalse(self.manager.cloud.resources)
        self.assertEqual(0, self.usage()['reserved_environments'])

    def test_stack_offboarding_with_history_soft_deletes_servers(self):
        path = self.stack(build_server=True)
        env_id = path.rsplit('/', 1)[1]
        before = list(objects.ServerList.get_all(self.ctx))
        worker = next(item for item in before if item.role == 'worker')
        source = objects.Source(context=self.ctx, type='git',
                                repo_url='https://example.com/web.git')
        source.create()
        service = objects.Service(context=self.ctx, stack_id=env_id,
                                  name='web',
                                  source_id=source.id, server_id=worker.id,
                                  ports='3000',
                                  replicas=1, auto_deploy=False,
                                  status=states.ACTIVE,
                                  task_state=states.NONE)
        service.create()
        deploy_ref = self.store.put(
            self.ctx, 'private-deploy-key',
            secret_store.SecretIdentity.for_resource(service, 'deploy-key'))
        source.credentials_ref = deploy_ref
        source.save()
        deployment = objects.Deployment(
            context=self.ctx, service_id=service.id, spec={'ports': '3000'},
            server_id=worker.id, trigger='api', status=states.ACTIVE)
        deployment.create()
        self.request('delete', path, status=202, complete=True)
        self.request('get', path, status=404)
        self.request('get', path + '/servers/' + worker.id, status=404)
        env = objects.Stack.get_by_id(
            self.ctx, env_id, show_deleted=True)
        self.assertEqual(states.DELETED, env.status)
        self.assertIsNotNone(env.deleted_at)
        kept = objects.ServerList.get_all(self.ctx, show_deleted=True)
        self.assertEqual({item.id for item in before},
                         {item.id for item in kept})
        for item in kept:
            self.assertIsNotNone(item.deleted_at)
            self.assertEqual(states.DELETING, item.status)
        self.assertFalse(objects.ServerList.get_all(self.ctx))
        self.assertEqual(worker.id, objects.Service.get_by_id(
            self.ctx, service.id).server_id)
        self.assertEqual(worker.id, objects.Deployment.get_by_id(
            self.ctx, deployment.id).server_id)
        self.assertNotIn(deploy_ref, self.store.values)
        self.assertFalse(self.manager.cloud.resources)
        for name, value in self.counters().items():
            self.assertEqual(0, value, name)

    def test_notifications_use_fake_transport_and_safe_payloads(self):
        received = queue.Queue()

        class Listener:
            def info(self, ctx, publisher_id, event_type, payload, metadata):
                received.put((event_type, payload, publisher_id))
                return messaging.NotificationResult.HANDLED

        listener = messaging.get_notification_listener(
            self.transport, [messaging.Target(topic='notifications')],
            [Listener()], executor='threading', serializer=rpcapi.Serializer())
        listener.start()
        self.config.config(driver=['messagingv2'],
                           group='oslo_messaging_notifications')
        notifications.reset(self.conf)
        self.manager.notifier = None
        try:
            path = self.stack()
            seen = []
            while True:
                event_type, payload, publisher_id = received.get(timeout=10)
                seen.append((publisher_id, event_type, payload))
                if event_type == 'coast.environment.create.end':
                    break
            env_id = path.rsplit('/', 1)[1]
            self.assertTrue(any(
                item[0].startswith('coast-api.') and
                item[1] == 'coast.environment.create.start'
                for item in seen))
            self.assertEqual({'project_id': 'project', 'resource_id': env_id,
                              'user_id': 'user', 'status': states.ACTIVE},
                             payload)
            self.assertTrue(publisher_id.startswith('coast-conductor.'))
            blob = str(seen)
            self.assertNotIn('engine_token', blob)
            self.assertNotIn('auth_token', blob)
        finally:
            listener.stop()
            listener.wait()

    def test_server_and_volume_lifecycle_notifications(self):
        test_driver.reset()
        path = self.stack()
        server = self.request('post', path + '/servers', {'server': {
            'name': 'extra', 'kind': 'vm', 'role': 'worker', 'plan': 'small'}},
            status=202, complete=True).json['server']
        volume = self.request('post', path + '/volumes', {'volume': {
            'name': 'extra-data', 'size_gb': 4}}, status=202,
            complete=True).json['volume']
        self.request('patch', path + '/volumes/' + volume['id'], {'volume': {
            'size_gb': 5}}, status=202, complete=True)
        self.request('delete', path + '/volumes/' + volume['id'], status=202,
                     complete=True)
        self.request('delete', path + '/servers/' + server['id'], status=202,
                     complete=True)
        api_events = [message['event_type'] for unused, message, unused_pri,
                      unused_retry in test_driver.NOTIFICATIONS]
        for event_type in (
                'coast.environment.create.start',
                'coast.server.create.start', 'coast.server.delete.start',
                'coast.volume.create.start', 'coast.volume.update.start',
                'coast.volume.delete.start'):
            self.assertIn(event_type, api_events)
        conductor_events = [event[0] for event in self.manager.notifier.events]
        for event_type in (
                'coast.server.create.start', 'coast.server.create.end',
                'coast.server.delete.start', 'coast.server.delete.end',
                'coast.volume.create.start', 'coast.volume.create.end',
                'coast.volume.update.start', 'coast.volume.update.end',
                'coast.volume.delete.start', 'coast.volume.delete.end'):
            self.assertIn(event_type, conductor_events)
        text = str(test_driver.NOTIFICATIONS) + str(
            self.manager.notifier.events)
        self.assertNotIn('barbican://', text)
        self.assertNotIn('auth_token', text)

    def test_failed_create_emits_error_without_end(self):
        driver = self.manager._driver(objects.Stack(
            driver='noop', status=states.PENDING, engine_version='noop-1.0'))
        with mock.patch.object(driver, 'get_engine_version',
                               return_value='unsupported-engine-version'):
            path = self.stack(name='notify-error')
        env_id = path.rsplit('/', 1)[1]
        phases = [event_type for event_type, payload in
                  self.manager.notifier.events
                  if payload.get('resource_id') == env_id and
                  event_type.startswith('coast.environment.create.')]
        self.assertIn('coast.environment.create.start', phases)
        self.assertIn('coast.environment.create.error', phases)
        self.assertNotIn('coast.environment.create.end', phases)
        error = next(payload for event_type, payload in
                     self.manager.notifier.events
                     if event_type == 'coast.environment.create.error' and
                     payload.get('resource_id') == env_id)
        self.assertEqual(states.ERROR, error['status'])
        self.assertNotIn('unsupported-engine-version', str(error))

    def test_unsupported_pinned_version_releases_volume_reservation(self):
        path = self.stack()
        env = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        env.engine_version = 'unsupported'
        env.save()
        row = self.request('post', path + '/volumes', {'volume': {
            'name': 'rejected', 'size_gb': 5}}, status=202,
            complete=True).json['volume']
        self.assertEqual(states.ERROR, objects.Volume.get_by_id(
            self.ctx, row['id']).status)
        self.assertEqual(0, self.usage()['reserved_volume_gb'])
        self.assertEqual(10, self.usage()['in_use_volume_gb'])

    def test_server_delete_keeps_row_and_history(self):
        path = self.stack()
        worker = next(item for item in objects.ServerList.get_all(self.ctx)
                      if item.role == 'worker')
        source = objects.Source(context=self.ctx, type='git')
        source.create()
        placed = objects.Service(
            context=self.ctx, stack_id=worker.stack_id,
            name='retired', source_id=source.id, server_id=worker.id,
            ports='', auto_deploy=False, status=states.STOPPED,
            task_state=states.NONE)
        placed.create()
        placed.destroy()
        service = objects.Service(context=self.ctx,
                                  stack_id=worker.stack_id,
                                  name='unplaced', source_id=source.id,
                                  server_id=None,
                                  ports='', auto_deploy=False,
                                  status=states.STOPPED,
                                  task_state=states.NONE)
        service.create()
        deployment = objects.Deployment(
            context=self.ctx, service_id=service.id, spec={},
            server_id=worker.id,
            trigger='api', status=states.ACTIVE)
        deployment.create()
        volume = self.request('post', path + '/volumes', {'volume': {
            'name': 'data', 'size_gb': 5, 'server_id': worker.id}},
            status=202, complete=True).json['volume']
        self.request('delete', path + '/servers/' + worker.id,
                     status=202, complete=True)
        self.request('get', path + '/servers/' + worker.id, status=404)
        listed = self.request('get', path + '/servers').json['servers']
        self.assertNotIn(worker.id, [item['id'] for item in listed])
        kept = objects.Server.get_by_id(
            self.ctx, worker.id, show_deleted=True)
        self.assertIsNotNone(kept.deleted_at)
        self.assertEqual(states.DELETING, kept.status)
        self.assertEqual(states.NONE, kept.task_state)
        self.assertEqual(worker.id, objects.Service.get_by_id(
            self.ctx, placed.id, show_deleted=True).server_id)
        self.assertEqual(worker.id, objects.Deployment.get_by_id(
            self.ctx, deployment.id).server_id)
        self.assertRaises(exception.NotFound, objects.Volume.get_by_id,
                          self.ctx, volume['id'])
        cloud = self.manager.cloud
        for kind in ('server', 'mgmt-port', 'env-port'):
            self.assertNotIn(cloud.reference(kind, worker.id),
                             cloud.resources)
        self.assertNotIn(cloud.reference('volume', volume['id']),
                         cloud.resources)
        self.assertEqual(0, self.usage()['in_use_servers'])
        self.assertEqual(10, self.usage()['in_use_volume_gb'])

    def test_revert_and_notification_failures_do_not_expose_secrets(self):
        logs = self.useFixture(fixtures.FakeLogger())
        driver = self.manager._driver(objects.Stack(
            driver='noop', status=states.PENDING, engine_version='noop-1.0'))
        with mock.patch.object(driver, 'get_engine_version',
                               return_value='unsupported-engine-version'):
            path = self.stack()
        self.assertEqual(states.ERROR, self.request('get', path).json[
            'stack']['status'])
        with mock.patch.object(self.manager.cloud, 'delete',
                               side_effect=RuntimeError('unlabeled-secret')), \
                mock.patch.object(driver, 'register_server',
                                  side_effect=RuntimeError(
                                      'unlabeled-secret')):
            path = self.stack(name='revert-failure')
        self.assertEqual(states.ERROR, self.request('get', path).json[
            'stack']['status'])
        self.assertEqual(0, self.usage()['reserved_environments'])
        self.assertTrue(self.manager.cloud.resources)
        with mock.patch.object(self.manager.notifier, 'info',
                               side_effect=RuntimeError('unlabeled-secret')):
            path = self.stack(name='notification-failure')
        self.assertEqual(states.ACTIVE, self.request('get', path).json[
            'stack']['status'])
        self.assertEqual(1, self.usage()['in_use_environments'])

        self.assertIn('builtins.RuntimeError', logs.output)
        self.assertNotIn('unlabeled-secret', logs.output)

    def test_stack_task_host_recorded_and_cleared(self):
        def observe(kind, resource, stack_id):
            if kind == 'network':
                current = objects.Stack.get_by_id(self.ctx, resource.id)
                self.assertEqual(self.conf.host, current.conductor_host)
            return original(kind, resource, stack_id)

        original = self.manager.cloud.create
        with mock.patch.object(self.manager.cloud, 'create',
                               side_effect=observe):
            path = self.stack()
        stack = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        self.assertIsNone(stack.conductor_host)
        self.assertEqual(states.NONE, stack.task_state)
        with mock.patch.object(self.manager.cloud, 'create',
                               side_effect=RuntimeError('failed')):
            path = self.stack(name='failed')
        stack = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertIsNone(stack.conductor_host)

    def test_restart_recovers_only_owned_busy_stacks(self):
        rows = []
        for index, (host, task) in enumerate((
                           (self.conf.host, states.PROVISIONING),
                           ('other-host', states.PROVISIONING),
                           (self.conf.host, states.NONE),
                           (None, states.PROVISIONING))):
            stack = objects.Stack(
                context=self.ctx, name='interrupted-%s' % index, driver='noop',
                engine_version='test', plan='small', status=states.BUILDING,
                task_state=task, conductor_host=host)
            stack.create()
            rows.append(stack)
        worker = service.Service(self.conf, endpoint=self.manager)
        with mock.patch.object(messaging, 'get_rpc_transport',
                               side_effect=RuntimeError('RPC not started')):
            self.assertRaises(exception.CoastException, worker.start)
        recovered = objects.Stack.get_by_id(self.ctx, rows[0].id)
        self.assertEqual(states.ERROR, recovered.status)
        self.assertEqual(states.NONE, recovered.task_state)
        self.assertIsNone(recovered.conductor_host)
        events = objects.EventList.get_all(
            self.ctx, filters={'resource_id': recovered.id})
        self.assertEqual('Interrupted by conductor restart', events[0].message)
        self.assertEqual(1, len([
            event for event in events
            if event.resource_type == 'environment' and
            event.action == 'create' and event.outcome == 'failure']))
        for old in rows[1:]:
            current = objects.Stack.get_by_id(self.ctx, old.id)
            self.assertEqual(old.status, current.status)
            self.assertEqual(old.task_state, current.task_state)
            self.assertEqual(old.conductor_host, current.conductor_host)
        self.manager.recover_stacks()
        self.assertEqual(len(events), len(objects.EventList.get_all(
            self.ctx, filters={'resource_id': recovered.id})))
        self.request('delete', '/v1/stacks/' + recovered.id, status=202)

    def _commit_stack(self, name, **options):
        deltas = quota.deltas('stack', options)
        reservation = quota.reserve(self.ctx, **deltas)
        stack = objects.Stack(
            context=self.ctx, name=name, plan='small', driver='noop',
            engine_version='test', status=states.BUILDING,
            task_state=states.NONE)
        stack.create()
        stack.acquire_task(states.PROVISIONING)
        stack.conductor_host = self.conf.host
        stack.save()
        quota.commit(self.ctx, reservation)
        return stack

    def test_interrupted_create_releases_quota_once(self):
        stack = self._commit_stack(
            'interrupted', workers=[{'kind': 'vm'}], build_server=True,
            data_volume_gb=12)
        objects.Server(
            context=self.ctx, stack_id=stack.id, name='build-1', kind='vm',
            role='build', status=states.BUILDING,
            task_state=states.NONE).create()
        objects.Volume(
            context=self.ctx, stack_id=stack.id, name='data', size_gb=12,
            purpose='data', status=states.CREATING).create()
        self.assertEqual(12, self.usage()['in_use_volume_gb'])
        self.assertEqual(2, self.usage()['in_use_servers'])
        self.assertEqual(1, self.usage()['in_use_build_servers'])
        self.manager.recover_stacks()
        usage = self.usage()
        self.assertEqual(0, usage['in_use_volume_gb'])
        self.assertEqual(0, usage['in_use_servers'])
        self.assertEqual(0, usage['in_use_build_servers'])
        self.assertEqual(0, usage['in_use_environments'])
        before = len(objects.EventList.get_all(
            self.ctx, filters={'resource_id': stack.id}))
        self.manager.recover_stacks()
        self.assertEqual(before, len(objects.EventList.get_all(
            self.ctx, filters={'resource_id': stack.id})))
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        usage = self.usage()
        for name in ('volume_gb', 'servers', 'build_servers', 'environments'):
            self.assertEqual(0, usage['in_use_' + name], name)
            self.assertEqual(0, usage['reserved_' + name], name)
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)
        self.assertEqual(states.DELETED, objects.Stack.get_by_id(
            self.ctx, stack.id, show_deleted=True).status)

    def test_interrupted_create_keeps_a_finished_server(self):
        stack = self._commit_stack(
            'partial', workers=[{'kind': 'vm'}], data_volume_gb=10)
        server = objects.Server(
            context=self.ctx, stack_id=stack.id, name='worker-1', kind='vm',
            role='worker', status=states.ACTIVE, task_state=states.NONE)
        server.create()
        objects.Event(
            context=self.ctx, stack_id=stack.id, resource_type='server',
            resource_id=server.id, action='create',
            outcome='success').create()
        self.manager.recover_stacks()
        usage = self.usage()
        self.assertEqual(1, usage['in_use_servers'])
        self.assertEqual(0, usage['in_use_volume_gb'])
        self.assertEqual(0, usage['in_use_environments'])
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        self.assertEqual(0, self.usage()['in_use_servers'])
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          servers=1)

    def test_failed_create_delete_does_not_release_again(self):
        reservation = quota.reserve(self.ctx, environments=1, volume_gb=10)
        quota.commit(self.ctx, reservation)
        quota.release(self.ctx, **reservation)
        stack = objects.Stack(
            context=self.ctx, name='failed', plan='small', driver='noop',
            engine_version='test', status=states.ERROR,
            task_state=states.NONE)
        stack.create()
        volume = objects.Volume(
            context=self.ctx, stack_id=stack.id, name='data', size_gb=10,
            purpose='data', status=states.ERROR)
        volume.create()
        for resource, kind in ((stack, 'environment'), (volume, 'volume')):
            objects.Event(
                context=self.ctx, stack_id=stack.id, resource_type=kind,
                resource_id=resource.id, action='create',
                outcome='failure').create()
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.request('delete', '/v1/stacks/' + stack.id, status=202,
                     complete=True)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])
        self.assertEqual(0, self.usage()['in_use_environments'])
        self.assertEqual(states.DELETED, objects.Stack.get_by_id(
            self.ctx, stack.id, show_deleted=True).status)
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage()['in_use_volume_gb'])

    def test_restart_skips_reconciling_stack(self):
        stack = objects.Stack(
            context=self.ctx, name='reconciling', driver='noop',
            engine_version='test', plan='small', status=states.ACTIVE,
            task_state=states.RECONCILING, conductor_host=self.conf.host)
        stack.create()
        self.manager.recover_stacks()
        current = objects.Stack.get_by_id(self.ctx, stack.id)
        self.assertEqual(states.ACTIVE, current.status)
        self.assertEqual(states.RECONCILING, current.task_state)
        self.assertEqual(self.conf.host, current.conductor_host)
        self.assertEqual(0, len(objects.EventList.get_all(
            self.ctx, filters={'resource_id': stack.id})))
        current.release_task(states.RECONCILING)
        self.assertIsNone(current.conductor_host)

    def test_service_startup_failure_is_sanitized(self):
        worker = service.Service(self.conf, endpoint=self.manager)
        with mock.patch.object(messaging, 'get_rpc_transport',
                               side_effect=RuntimeError('broker-password')):
            with self.assertRaises(exception.CoastException) as error:
                worker.start()
        self.assertNotIn('broker-password', str(error.exception))
        self.assertIsNone(worker.transport)
        self.assertIsNone(worker.server)

    def test_command_launches_a_working_rpc_service(self):
        self.stop_server()
        worker = service.Service(self.conf, endpoint=self.manager)
        case = self

        class Launcher:
            def wait(self):
                try:
                    case.stack()
                finally:
                    worker.stop()

        def launch(*args, **kwargs):
            worker.start()
            return Launcher()

        with mock.patch.object(conductor_cmd.config, 'parse_args'), \
                mock.patch.object(conductor_cmd.config, 'setup_logging'), \
                mock.patch.object(conductor_cmd.conductor_service, 'Service',
                                  return_value=worker), \
                mock.patch.object(conductor_cmd.service, 'launch',
                                  side_effect=launch):
            self.assertEqual(0, conductor_cmd.main())
        self.assertEqual(states.ACTIVE, objects.StackList.get_all(
            self.ctx)[0].status)
        self.assertEqual(1, self.usage()['in_use_environments'])

    def test_pending_service_and_volume_mount_prevent_destructive_cleanup(
            self):
        path = self.stack()
        worker = next(item for item in objects.ServerList.get_all(self.ctx)
                      if item.role == 'worker')
        volume = self.request('post', path + '/volumes', {'volume': {
            'name': 'data', 'size_gb': 5, 'server_id': worker.id}},
            status=202, complete=True).json['volume']
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            self.request('delete', path + '/servers/' + worker.id, status=202)
        # Drain the service cast before the server delete waits on the same
        # queue.
        service = self.request('post', '/v1/services', {'service': {
            **self.service_parents(worker.stack_id),
            'name': 'pending', 'server_id': worker.id, 'deploy': False,
            'source': {'type': 'git',
                       'repo_url': 'https://example.com/web.git'}}},
            status=202, complete=True).json['service']
        resources = copy.deepcopy(self.manager.cloud.resources)
        self.send(self.messages[0])
        self.assertEqual(states.ERROR, objects.Server.get_by_id(
            self.ctx, worker.id).status)
        self.assertEqual(resources, self.manager.cloud.resources)
        objects.AppVolumeMount(context=self.ctx, service_id=service['id'],
                               volume_id=volume['id'], sub_path='',
                               container_path='/data',
                               read_only=False).create()
        self.request('delete', path + '/volumes/' + volume['id'],
                     status=409)
        self.assertEqual(states.IN_USE, objects.Volume.get_by_id(
            self.ctx, volume['id']).status)
        self.assertEqual(15, self.usage()['in_use_volume_gb'])
        self.assertEqual(resources, self.manager.cloud.resources)

    def test_noop_export_before_delete_fails_before_cleanup(self):
        # The API accepts the flag. The fake-cloud delete still raises
        # and does not remove cloud records or create an export row.
        path = self.stack()
        before = self.counters()
        resources = copy.deepcopy(self.manager.cloud.resources)
        env_id = path.rsplit('/', 1)[1]
        self.request('delete', path, {'export_before_delete': True},
                     status=202, complete=True)
        env = objects.Stack.get_by_id(self.ctx, env_id)
        self.assertEqual(states.ERROR, env.status)
        self.assertEqual(states.NONE, env.task_state)
        self.assertEqual(before, self.counters())
        self.assertEqual(resources, self.manager.cloud.resources)
        self.assertEqual([], list(objects.ExportList.get_all(self.ctx)))

    def test_real_driver_service_create_fails_closed(self):
        path = self.stack()
        env = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        env.driver = 'coolify_v4'
        env.save()
        before = copy.deepcopy(self.manager.cloud.resources)
        created = self.request(
            'post', '/v1/services', self.service_body(self.worker()),
            status=202,
            complete=True).json['service']
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ERROR, service.status)
        self.assertEqual(states.NONE, service.task_state)
        deployment = objects.Deployment.get_by_id(
            self.ctx, created['deployment_id'])
        self.assertEqual(states.FAILED, deployment.status)
        self.assertNotIn('not implemented', deployment.fault or '')
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(0, self.usage()['in_use_apps'])
        self.assertEqual(before, self.manager.cloud.resources)

    def test_building_deployment_logs_are_incomplete(self):
        path = self.stack()
        created = self.request('post', '/v1/services', self.service_body(
            self.worker()), status=202, complete=True).json['service']
        first = objects.Deployment.get_by_id(
            self.ctx, created['deployment_id'])
        deployment = objects.Deployment(
            context=self.ctx, project_id=self.ctx.project_id,
            service_id=created['id'],
            spec=first.spec, trigger='manual', status=states.BUILDING,
            engine_ref='building-deployment')
        deployment.create()
        stack = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        stack.driver = 'coolify_v4'
        stack.save()
        driver = mock.Mock()
        driver.get_deployment_log.return_value = [driver_base.LogLine(
            ts='2026-10-09T14:06:01Z', stream='build', line='Building image')]
        with mock.patch.object(self.manager, '_driver', return_value=driver), \
                mock.patch.object(self.manager, '_endpoint'):
            result = self.manager.get_logs(
                self.ctx, 'deployment', deployment.id)
        self.assertFalse(result['complete'])
        self.assertEqual(deployment.id, result['deployment_id'])
        self.assertEqual('Building image', result['lines'][0]['line'])

    def test_rollback_pins_driver_target_without_changing_history(self):
        self.stack()
        created = self.request('post', '/v1/services', self.service_body(
            self.worker()), status=202, complete=True).json['service']
        path = '/v1/services/' + created['id']
        first = objects.Deployment.get_by_id(
            self.ctx, created['deployment_id'])
        first.resolved_ref = 'a' * 40
        first.save()
        self.request('post', path + '/deployments',
                     {'deployment': {'source_ref': 'release'}}, status=202,
                     complete=True)
        driver = self.manager.drivers['noop']
        original = driver.rollback

        def rollback(ep, env, service, target, new):
            self.assertEqual(first.resolved_ref, target.source_ref)
            self.assertEqual(first.resolved_ref, new.source_ref)
            return original(ep, env, service, target, new)

        with mock.patch.object(driver, 'rollback', rollback):
            accepted = self.request(
                'post', path + '/deployments/' + first.id + '/action',
                {'rollback': {}}, status=202, complete=True).json['deployment']
        self.assertEqual(states.ACTIVE, objects.Deployment.get_by_id(
            self.ctx, accepted['id']).status)
        stored = objects.Deployment.get_by_id(self.ctx, first.id)
        self.assertEqual(first.source_ref, stored.source_ref)
        self.assertEqual(first.resolved_ref, stored.resolved_ref)

    def test_driver_reported_resolved_ref(self):
        self.stack()
        created = self.request('post', '/v1/services', self.service_body(
            self.worker()), status=202, complete=True).json['service']
        driver = self.manager.drivers['noop']
        original = driver.deploy_app
        cases = (({}, None, None),
                 ({'resolved_ref': 'a' * 40}, None, 'a' * 40),
                 ({}, 'sha256:' + 'b' * 64, 'sha256:' + 'b' * 64),
                 ({'resolved_ref': 'a' * 40}, 'c' * 40, 'c' * 40))
        for extra, observed_ref, expected in cases:
            with self.subTest(expected=expected):
                def deploy(*args):
                    result = original(*args)
                    return driver_base.DriverResult(
                        engine_ref=result.engine_ref, extra=extra)

                observed = driver_base.ObservedStatus(
                    status=states.ACTIVE, resolved_ref=observed_ref)
                with mock.patch.object(driver, 'deploy_app', deploy), \
                        mock.patch.object(driver, 'get_deployment_status',
                                          return_value=observed):
                    accepted = self.request(
                        'post',
                        '/v1/services/' + created['id'] + '/deployments',
                        {'deployment': {'source_ref': 'release'}},
                        status=202, complete=True).json['deployment']
                row = objects.Deployment.get_by_id(self.ctx, accepted['id'])
                self.assertEqual(states.ACTIVE, row.status)
                self.assertEqual(expected, row.resolved_ref)

    def test_deployment_failure_masks_secrets(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.stack()
        created = self.request('post', '/v1/services', self.service_body(
            self.worker(), env={'PASSWORD': {
                'value': 'secret-value', 'secret': True}}), status=202,
            complete=True).json['service']
        current = objects.Service.get_by_id(self.ctx, created['id'])
        driver = self.manager.drivers['noop']
        with mock.patch.object(driver, 'deploy_app', side_effect=RuntimeError(
                'password=do-not-log')):
            failed = self.request(
                'post', '/v1/services/' + created['id'] + '/deployments',
                {'deployment': {'source_ref': 'release'}}, status=202,
                complete=True).json['deployment']
        row = objects.Deployment.get_by_id(self.ctx, failed['id'])
        self.assertEqual(states.FAILED, row.status)
        self.assertNotIn('do-not-log', row.fault or '')
        self.assertNotIn('secret-value', row.fault or '')
        fresh = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(current.current_deployment_id,
                         fresh.current_deployment_id)
        self.assertEqual(states.NONE, fresh.task_state)
        blob = logs.output + str(self.manager.notifier.events)
        self.assertNotIn('do-not-log', blob)
        self.assertNotIn('secret-value', blob)

    def test_service_create_replay_does_not_double_commit(self):
        self.stack()
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            created = self.request(
                'post', '/v1/services', self.service_body(self.worker()),
                status=202).json['service']
        self.send(self.messages[0])
        before = self.counters()
        self.send(self.messages[0])
        self.assertEqual(before, self.counters())
        self.assertEqual(1, self.usage()['in_use_apps'])
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, created['id']).status)

    def test_cancel_before_create_releases_reservation(self):
        self.stack()
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            created = self.request(
                'post', '/v1/services', self.service_body(self.worker()),
                status=202).json['service']
            self.request(
                'post', '/v1/services/' + created['id'] + '/deployments/' +
                created['deployment_id'] + '/action', {'cancel': {}},
                status=202)
        self.send(self.messages[1])
        self.send(self.messages[0])
        self.send(self.messages[0])
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.PENDING, service.status)
        self.assertEqual(states.NONE, service.task_state)
        self.assertEqual(states.CANCELLED, objects.Deployment.get_by_id(
            self.ctx, created['deployment_id']).status)
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(0, self.usage()['in_use_apps'])
        self.assertEqual(1, len(objects.EventList.get_all(self.ctx, filters={
            'resource_id': created['deployment_id'], 'action': 'cancel',
            'outcome': 'success'})))
        kinds = [event[0] for event in self.manager.notifier.events]
        self.assertIn('coast.deployment.update.end', kinds)
        self.assertNotIn('coast.deployment.cancel.end', kinds)
        self.assertNotIn('coast.app.create.end', kinds)
        self.request('delete', '/v1/services/' + created['id'], status=202,
                     complete=True)
        self.assertEqual(0, self.usage()['in_use_apps'])
        self.request('get', '/v1/services/' + created['id'], status=404)

    def test_no_port_service_create_does_not_publish_route(self):
        self.stack()
        driver = self.manager.drivers['noop']
        with mock.patch.object(driver, 'set_domains',
                               wraps=driver.set_domains) as route:
            created = self.request(
                'post', '/v1/services',
                self.service_body(self.worker(), ports=None),
                status=202, complete=True).json['service']
            route.assert_not_called()
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ACTIVE, service.status)
        self.assertEqual([], self.manager._fqdns(self.ctx, service))
        shown = self.request('get', '/v1/services/' + service.id).json[
            'service']
        self.assertIsNone(shown['default_url'])
        self.assertEqual([], shown['dns_records'])

    def test_domain_publish_waits_for_verify(self):
        self.stack()
        created = self.request(
            'post', '/v1/services', self.service_body(self.worker()),
            status=202,
            complete=True).json['service']
        driver = self.manager.drivers['noop']
        calls = []
        original = driver.set_domains

        def spy(endpoint, env, service, names):
            calls.append(list(names))
            return original(endpoint, env, service, names)

        route = {}

        def routes(fqdn, host, fip, resolver):
            route['fqdn'] = fqdn
            route['host'] = host
            route['fip'] = fip
            return True

        with mock.patch.object(driver, 'set_domains', side_effect=spy), \
                mock.patch('coast.conductor.manager.domain_check.txt_matches',
                           return_value=True), \
                mock.patch('coast.conductor.manager.domain_check.routes_to',
                           side_effect=routes), \
                mock.patch('coast.conductor.manager.domain_check.tls_ready',
                           return_value=True):
            domain = self.request(
                'post', '/v1/services/' + created['id'] + '/domains',
                {'domain': {'fqdn': 'web.example.com'}}, status=201,
                complete=True).json['domain']
            self.assertEqual([], calls)
            self.request(
                'post', '/v1/services/' + created['id'] + '/domains/' +
                domain['id'] + '/action', {'verify': {}}, status=202,
                complete=True)
            # "Verify now" re-checks DNS immediately and publishes on success.
            self.manager.drive_domains()
        self.assertIn('https://web.example.com', calls[-1])
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.ACTIVE, stored.status)
        self.assertIsNotNone(stored.verified_at)
        self.assertEqual('web.example.com', route['fqdn'])
        self.assertEqual(naming.default_hostname(
            'web', created['id'], self.conf.domains.default_suffix,
            self.worker().floating_ip), route['host'])
        self.assertEqual(self.worker().floating_ip, route['fip'])
        with mock.patch.object(driver, 'set_domains', side_effect=spy):
            self.request(
                'delete', '/v1/services/' + created['id'] + '/domains/' +
                domain['id'], status=202, complete=True)
        self.assertNotIn('https://web.example.com', calls[-1])

    def test_deploy_replaces_only_the_default_domain(self):
        self.stack()
        created = self.request(
            'post', '/v1/services', self.service_body(self.worker()),
            status=202, complete=True).json['service']
        service = objects.Service.get_by_id(self.ctx, created['id'])
        objects.Domain(
            context=self.ctx, service_id=service.id,
            fqdn='custom.example.com', auto_dns=False, dns_mode='manual',
            tls='none', verification_token='token',
            status=states.ACTIVE).create()
        old = service.default_domain
        worker = self.worker()
        worker.floating_ip = '198.51.100.20'
        worker.save()
        new = naming.default_hostname(
            'web', service.id, self.conf.domains.default_suffix,
            worker.floating_ip)
        driver = self.manager.drivers['noop']
        order = []
        original_domains = driver.set_domains
        original_deploy = driver.deploy_app

        def set_domains(endpoint, env, row, names):
            order.append(('domains', list(names)))
            return original_domains(endpoint, env, row, names)

        def deploy_app(endpoint, env, row, deployment):
            order.append(('deploy', None))
            return original_deploy(endpoint, env, row, deployment)

        with mock.patch.object(driver, 'set_domains',
                               side_effect=set_domains), mock.patch.object(
                driver, 'deploy_app', side_effect=deploy_app):
            self.request('post', '/v1/services/' + created['id'] +
                         '/deployments',
                         {'deployment': {'source_ref': 'release'}},
                         status=202, complete=True)
        self.assertEqual(['domains', 'deploy'], [item[0] for item in order])
        sent = order[0][1]
        self.assertIn('https://' + new, sent)
        self.assertIn('http://custom.example.com', sent)
        self.assertNotIn(old, [naming.hostname_of(item) for item in sent])
        stored = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(new, stored.default_domain)

    def test_refresh_default_domains_is_idempotent(self):
        self.stack()
        worker = self.worker()
        created = self.request(
            'post', '/v1/services', self.service_body(worker),
            status=202, complete=True).json['service']
        second = self.request(
            'post', '/v1/services', self.service_body(worker, name='shop'),
            status=202, complete=True).json['service']
        report = self.manager.refresh_default_domains(
            self.ctx, service_id=created['id'], dry_run=True)
        self.assertEqual(
            [created['id']], [item['service_id'] for item in report])
        self.assertTrue(report[0]['ok'], report)
        self.assertFalse(report[0]['changed'])
        before = objects.Service.get_by_id(
            self.ctx, second['id']).default_domain
        original = objects.Service.get_by_id(
            self.ctx, created['id']).default_domain
        worker.floating_ip = '198.51.100.21'
        worker.save()
        driver = self.manager.drivers['noop']
        with mock.patch.object(driver, 'set_domains') as published:
            preview = self.manager.refresh_default_domains(
                self.ctx, service_id=created['id'], dry_run=True)
        published.assert_not_called()
        self.assertTrue(preview[0]['changed'])
        stored = objects.Service.get_by_id(
            self.ctx, created['id']).default_domain
        self.assertEqual(original, stored)
        applied = self.manager.refresh_default_domains(
            self.ctx, service_id=created['id'])
        self.assertTrue(applied[0]['ok'])
        self.assertTrue(applied[0]['changed'])
        self.assertEqual(before, objects.Service.get_by_id(
            self.ctx, second['id']).default_domain)
        again = self.manager.refresh_default_domains(
            self.ctx, service_id=created['id'])
        self.assertFalse(again[0]['changed'])

    def test_skip_verification_publishes_the_custom_domain(self):
        self.config.config(skip_verification=True, group='domains')
        self.stack()
        created = self.request(
            'post', '/v1/services', self.service_body(self.worker()),
            status=202, complete=True).json['service']
        driver = self.manager.drivers['noop']
        calls = []
        original = driver.set_domains

        def spy(endpoint, env, service, names):
            calls.append(list(names))
            return original(endpoint, env, service, names)

        with mock.patch.object(driver, 'set_domains', side_effect=spy):
            domain = self.request(
                'post', '/v1/services/' + created['id'] + '/domains',
                {'domain': {'fqdn': 'custom.example.com'}}, status=201,
                complete=True).json['domain']
        self.assertEqual(states.ACTIVE, domain['status'])
        self.assertIn('https://custom.example.com', calls[-1])

    def test_skip_verification_verify_now_applies_a_pending_domain(self):
        self.stack()
        created = self.request(
            'post', '/v1/services', self.service_body(self.worker()),
            status=202, complete=True).json['service']
        domain = objects.Domain(
            context=self.ctx, service_id=created['id'],
            fqdn='old.example.com', auto_dns=False, dns_mode='manual',
            tls='none', verification_token='token',
            status=states.PENDING_VERIFICATION).create()
        self.config.config(skip_verification=True, group='domains')
        driver = self.manager.drivers['noop']
        with mock.patch.object(driver, 'set_domains') as published, \
                mock.patch('coast.conductor.manager.domain_check.txt_matches',
                           return_value=False):
            self.request(
                'post', '/v1/services/' + created['id'] + '/domains/' +
                domain.id + '/action', {'verify': {}}, status=202,
                complete=True)
        self.assertEqual(states.ACTIVE, objects.Domain.get_by_id(
            self.ctx, domain.id).status)
        self.assertIn('http://old.example.com',
                      published.call_args.args[-1])

    def test_tls_none_is_sent_as_http(self):
        self.stack()
        created = self.request(
            'post', '/v1/services', self.service_body(self.worker()),
            status=202, complete=True).json['service']
        domain = objects.Domain(
            context=self.ctx, service_id=created['id'],
            fqdn='plain.example.com', auto_dns=False, dns_mode='manual',
            tls='letsencrypt', verification_token='token',
            status=states.ACTIVE).create()
        driver = self.manager.drivers['noop']
        headers = dict(self.headers)
        headers['OpenStack-API-Version'] = 'app-hosting 1.2'
        with mock.patch.object(driver, 'set_domains') as published:
            self.request(
                'patch', '/v1/services/' + created['id'] + '/domains/' +
                domain.id, {'domain': {'tls': 'none'}}, status=202,
                headers=headers, complete=True)
        self.assertIn('http://plain.example.com',
                      published.call_args.args[-1])

    def test_verify_from_error_is_one_step(self):
        self.stack()
        created = self.request(
            'post', '/v1/services', self.service_body(
                self.worker(), deploy=False),
            status=202, complete=True).json['service']
        domain = self.request(
            'post', '/v1/services/' + created['id'] + '/domains',
            {'domain': {'fqdn': 'web.example.com'}}, status=201,
            complete=True).json['domain']
        row = objects.Domain.get_by_id(self.ctx, domain['id'])
        row.status = states.ERROR
        row.save()
        driver = self.manager.drivers['noop']
        with mock.patch.object(driver, 'set_domains') as published:
            self.request(
                'post', '/v1/services/' + created['id'] + '/domains/' +
                domain['id'] + '/action', {'verify': {}}, status=202,
                complete=True)
        published.assert_not_called()
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.PENDING_VERIFICATION, stored.status)
        self.assertIsNone(stored.verified_at)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, created['id']).task_state)

    def test_verify_before_domain_create_commits_quota(self):
        self.stack()
        created = self.request(
            'post', '/v1/services', self.service_body(self.worker()),
            status=202,
            complete=True).json['service']
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            domain = self.request(
                'post', '/v1/services/' + created['id'] + '/domains',
                {'domain': {'fqdn': 'early.example.com'}}, status=201).json[
                    'domain']
            self.request(
                'post', '/v1/services/' + created['id'] + '/domains/' +
                domain['id'] + '/action', {'verify': {}}, status=202)
        self.send(self.messages[1])
        self.send(self.messages[0])
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.PENDING_VERIFICATION, stored.status)
        self.assertIsNone(stored.verified_at)
        self.assertEqual(0, self.usage()['reserved_domains'])
        self.assertEqual(1, self.usage()['in_use_domains'])
        self.request('delete', '/v1/services/' + created['id'] + '/domains/' +
                     domain['id'], status=202, complete=True)
        self.assertEqual(0, self.usage()['in_use_domains'])

    def _open_domain(self, fqdn):
        self.stack()
        created = self.request(
            'post', '/v1/services', self.service_body(self.worker()),
            status=202, complete=True).json['service']
        path = '/v1/services/' + created['id'] + '/domains'
        domain = self.request(
            'post', path, {'domain': {'fqdn': fqdn}}, status=201,
            complete=True).json['domain']
        return created, path, domain

    def test_auto_dns_save_deadlock_retries_whole_operation(self):
        created, path, domain = self._open_domain('retry.example.com')
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            added = self.request(
                'post', path, {'domain': {'fqdn': 'auto.example.com'}},
                status=201).json['domain']
        current = objects.Domain.get_by_id(self.ctx, added['id'])
        current.auto_dns = True
        current.zone_id = 'zone-id'
        current.save()
        original = self.manager._save
        saves = []

        def save(resource, **values):
            if isinstance(resource, objects.Domain):
                saves.append(values)
                if len(saves) == 1:
                    raise db_exception.DBDeadlock()
            return original(resource, **values)

        with mock.patch.object(managed_dns, 'client') as dns_client, \
                mock.patch.object(self.manager, '_worker_address',
                                  return_value='192.0.2.1'), \
                mock.patch.object(self.manager, '_save', side_effect=save), \
                mock.patch.object(self.manager, '_stack',
                                  wraps=self.manager._stack) as stack:
            dns_client.return_value.ensure.return_value = []
            self.manager.execute(self.ctx, **self.messages[0])
        self.assertEqual(2, stack.call_count)
        self.assertEqual(2, len(saves))
        self.assertEqual(2, dns_client.return_value.ensure.call_count)
        self.assertTrue(all('managed_records' in values for values in saves))
        stored = objects.Domain.get_by_id(self.ctx, added['id'])
        self.assertIsNone(stored.dns_previous)
        self.assertIsNone(stored.status_reason)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, created['id']).task_state)
        with mock.patch.object(managed_dns, 'client'):
            self.request('delete', path + '/' + added['id'],
                         status=202, complete=True)

    def test_domain_delete_retries_deadlock_at_stack(self):
        self._domain_delete_retries_deadlock('_stack')

    def test_domain_delete_retries_deadlock_at_operation_lock(self):
        self._domain_delete_retries_deadlock('_operation_lock')

    def test_domain_delete_retries_deadlock_during_mutation(self):
        self._domain_delete_retries_deadlock('_delete_domain')

    def _domain_delete_retries_deadlock(self, method):
        created, path, domain = self._open_domain('retry.example.com')
        original = getattr(self.manager, method)
        attempts = []

        def contend(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                if method == '_delete_domain':
                    original(*args, **kwargs)
                raise db_exception.DBDeadlock()
            return original(*args, **kwargs)

        with mock.patch.object(self.manager, method, side_effect=contend):
            self.request('delete', path + '/' + domain['id'],
                         status=202, complete=True)
        self.assertEqual(2, len(attempts))
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, created['id']).task_state)
        self.assertRaises(exception.NotFound, objects.Domain.get_by_id,
                          self.ctx, domain['id'])

    def test_domain_delete_retries_db_error_caused_by_deadlock(self):
        error = db_exception.DBError(Exception('cleanup failed'))
        error.__cause__ = db_exception.DBDeadlock()
        self._domain_delete_retries_masked_deadlock(error)

    def test_domain_delete_retries_db_error_context_deadlock(self):
        error = db_exception.DBError(Exception('cleanup failed'))
        error.__context__ = db_exception.DBDeadlock()
        self._domain_delete_retries_masked_deadlock(error)

    def test_domain_delete_retries_missing_savepoint(self):
        error = db_exception.DBError(Exception(
            1305, 'SAVEPOINT sa_savepoint_1 does not exist'))
        self._domain_delete_retries_masked_deadlock(error)

    def _domain_delete_retries_masked_deadlock(self, error):
        created, path, domain = self._open_domain('retry.example.com')
        original = self.manager._delete_domain
        attempts = []

        def contend(*args, **kwargs):
            attempts.append(True)
            original(*args, **kwargs)
            if len(attempts) == 1:
                raise error

        with mock.patch.object(self.manager, '_delete_domain',
                               side_effect=contend):
            self.request('delete', path + '/' + domain['id'],
                         status=202, complete=True)
        self.assertEqual(2, len(attempts))
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.NONE, service.task_state)
        self.assertNotEqual(states.ERROR, service.status)
        self.assertRaises(exception.NotFound, objects.Domain.get_by_id,
                          self.ctx, domain['id'])

    def test_domain_delete_nested_task_write_retries_operation(self):
        created, path, domain = self._open_domain('retry.example.com')
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            self.request('delete', path + '/' + domain['id'], status=202)
        original = orm.Query.update
        deadlocks = []

        def update(query, values, **kwargs):
            if 'task_state' in values and not deadlocks:
                deadlocks.append(True)
                raise db_exception.DBDeadlock()
            return original(query, values, **kwargs)

        with mock.patch.object(orm.Query, 'update', new=update):
            self.manager.execute(self.ctx, **self.messages[0])
        self.assertEqual([True], deadlocks)
        self.assertRaises(exception.NotFound, objects.Domain.get_by_id,
                          self.ctx, domain['id'])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, created['id']).task_state)

    def test_domain_delete_exhausted_deadlocks_release_claim(self):
        self._domain_delete_exhausted_deadlocks('_stack')

    def test_domain_delete_exhausted_mutation_deadlocks_release_claim(self):
        self._domain_delete_exhausted_deadlocks('_delete_domain')

    def test_domain_delete_exhausted_lock_deadlocks_release_claim(self):
        self._domain_delete_exhausted_deadlocks('_operation_lock')

    def _domain_delete_exhausted_deadlocks(self, method):
        created, path, domain = self._open_domain('retry.example.com')
        with mock.patch.object(self.manager, method,
                               side_effect=db_exception.DBDeadlock()) as get:
            self.request('delete', path + '/' + domain['id'],
                         status=202, complete=True)
        self.assertEqual(4, get.call_count)
        parent = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.NONE, parent.task_state)
        self.assertIsNone(parent.task_owner)
        self.assertIsNone(parent.task_started_at)
        self.request('delete', path + '/' + domain['id'],
                     status=202, complete=True)

    def test_domain_delete_after_two_failed_claims_completes(self):
        created, path, domain = self._open_domain('retry.example.com')
        for attempt in range(2):
            with mock.patch.object(
                    self.manager, '_stack',
                    side_effect=db_exception.DBDeadlock()) as get:
                self.request('delete', path + '/' + domain['id'],
                             status=202, complete=True)
            self.assertEqual(4, get.call_count)
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, created['id']).task_state)
        self.request('delete', path + '/' + domain['id'],
                     status=202, complete=True)
        self.assertRaises(exception.NotFound, objects.Domain.get_by_id,
                          self.ctx, domain['id'])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, created['id']).task_state)

    def test_domain_delete_setup_error_releases_claim(self):
        created, path, domain = self._open_domain('retry.example.com')
        with mock.patch.object(self.manager, '_operation_lock',
                               side_effect=RuntimeError('failure')):
            self.request('delete', path + '/' + domain['id'],
                         status=202, complete=True)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, created['id']).task_state)
        self.request('delete', path + '/' + domain['id'],
                     status=202, complete=True)

    def test_failed_domain_cleanup_preserves_newer_claim(self):
        created, path, domain = self._open_domain('retry.example.com')
        parent = objects.Service.get_by_id(self.ctx, created['id'])
        parent.acquire_task(states.RECONCILING, owner='domain_change')
        started = parent.task_started_at
        claim = dict(parent._context._task_claims[('service', parent.id)])
        parent.release_task(states.RECONCILING)
        with mock.patch.object(timeutils, 'utcnow', return_value=(
                started + datetime.timedelta(seconds=1))):
            parent.acquire_task(states.RECONCILING, owner='domain_change')
        resource = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.manager._failed_operation(
            self.ctx, 'delete_domain', resource, 'domain', 'delete', {},
            rpcapi.receipt_id('delete_domain', resource), claim)
        current = objects.Service.get_by_id(self.ctx, parent.id)
        self.assertEqual(parent.task_started_at, current.task_started_at)
        self.assertEqual(states.RECONCILING, current.task_state)
        self.request('delete', path + '/' + domain['id'], status=409)

    def test_recovered_domain_cast_cannot_take_new_claim(self):
        created, path, domain = self._open_domain('retry.example.com')
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            self.request('delete', path + '/' + domain['id'], status=202)
        old = self.messages.pop()
        parent = objects.Service.get_by_id(self.ctx, created['id'])
        with mock.patch.object(timeutils, 'utcnow', return_value=(
                parent.task_started_at + datetime.timedelta(seconds=301))):
            self.manager.settle_deployments()
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, parent.id).task_state)
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            self.request('delete', path + '/' + domain['id'], status=202)
        new = self.messages.pop()
        self.assertNotEqual(old['options']['_task_claim'],
                            new['options']['_task_claim'])
        with mock.patch.object(self.manager, '_delete_domain') as delete:
            self.manager.execute(self.ctx, **old)
        delete.assert_not_called()
        current = objects.Service.get_by_id(self.ctx, parent.id)
        self.assertEqual(states.RECONCILING, current.task_state)
        self.assertEqual(new['options']['_task_claim']['started_at'],
                         timeutils.normalize_time(
                             current.task_started_at).isoformat())
        self.manager.execute(self.ctx, **new)
        self.assertRaises(exception.NotFound, objects.Domain.get_by_id,
                          self.ctx, domain['id'])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, parent.id).task_state)

    def test_delete_response_survives_immediate_cleanup(self):
        created, path, domain = self._open_domain('delete.example.com')
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertIsNotNone(service.current_deployment_id)

        def dispatch(ctx, operation, resource, options=None):
            self.manager.execute(ctx, operation, resource, options=options,
                                 revision=rpcapi.revision(resource))

        with mock.patch.object(self.rpc, 'cast', side_effect=dispatch):
            response = self.request('delete', '/v1/services/' + service.id,
                                    status=202)
        self.assertEqual(states.DELETING, response.json['service']['status'])
        self.assertRaises(exception.NotFound, objects.Service.get_by_id,
                          self.ctx, service.id)

    def test_service_stack_setup_failure_releases_original_claim(self):
        self._service_setup_failure_releases_original_claim('_stack')

    def test_service_lock_setup_failure_releases_original_claim(self):
        self._service_setup_failure_releases_original_claim('_operation_lock')

    def test_service_snapshot_setup_failure_releases_original_claim(self):
        self._service_setup_failure_releases_original_claim(
            '_stack', snapshot=True)

    def _service_setup_failure_releases_original_claim(
            self, method, snapshot=False):
        created, path, domain = self._open_domain('retry.example.com')
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            self.request('post', '/v1/services/' + created['id'] + '/action',
                         {'restart': {}}, status=202)
        claim = self.messages[0]['options']['_task_claim']
        if snapshot:
            self.messages[0]['options'].pop('_task_claim')
        with mock.patch.object(self.manager, method,
                               side_effect=RuntimeError('setup')) as setup:
            self.assertRaises(RuntimeError, self.manager.execute,
                              self.ctx, **self.messages[0])
        self.assertEqual(1, setup.call_count)
        current = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.NONE, current.task_state)
        self.assertIsNone(current.task_owner)
        self.assertIsNone(current.task_started_at)
        receipt = rpcapi.receipt_id('restart_service',
                                    self.messages[0]['resource'],
                                    self.messages[0]['revision'],
                                    task_claim=claim)
        event = objects.Event.get_by_id(self.ctx, receipt)
        self.assertEqual('failure', event.outcome)
        self.request('delete', '/v1/services/' + created['id'],
                     status=202, complete=True)

    def test_stale_service_claim_recovers_and_fences_delayed_cast(self):
        created, path, domain = self._open_domain('retry.example.com')
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            self.request('post', '/v1/services/' + created['id'] + '/action',
                         {'restart': {}}, status=202)
        old = self.messages.pop()
        parent = objects.Service.get_by_id(self.ctx, created['id'])
        with mock.patch.object(timeutils, 'utcnow', return_value=(
                parent.task_started_at + datetime.timedelta(seconds=301))):
            self.manager.settle_deployments()
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, parent.id).task_state)
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            self.request('delete', path + '/' + domain['id'], status=202)
        with mock.patch.object(self.manager, '_restart_service') as restart:
            self.manager.execute(self.ctx, **old)
        restart.assert_not_called()
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, parent.id).task_state)
        self.manager.execute(self.ctx, **self.messages.pop())
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, parent.id).task_state)

    def test_stale_pending_create_recovers_and_fences_cast(self):
        self.stack()
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            created = self.request(
                'post', '/v1/services', self.service_body(self.worker()),
                status=202).json['service']
        self._assert_pending_recovery(created['id'], self.messages.pop())

    def test_stale_pending_deployment_recovers_and_fences_cast(self):
        created, path, domain = self._open_domain('pending.example.com')
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            self.request('post', '/v1/services/' + created['id'] +
                         '/deployments',
                         {'deployment': {'source_ref': 'next'}},
                         status=202)
        self._assert_pending_recovery(created['id'], self.messages.pop())

    def _assert_pending_recovery(self, service_id, old):
        self.assertIn('_task_claim', old['options'])
        parent = objects.Service.get_by_id(self.ctx, service_id)
        pending = objects.DeploymentList.get_all(self.ctx, filters={
            'service_id': service_id, 'status': states.PENDING})
        self.assertEqual(1, len(pending))
        # A recent pending record still protects the original claim.
        self.manager.settle_deployments()
        self.assertEqual(parent.task_state, objects.Service.get_by_id(
            self.ctx, service_id).task_state)
        with mock.patch.object(timeutils, 'utcnow', return_value=(
                parent.task_started_at + datetime.timedelta(seconds=301))):
            self.manager.settle_deployments()
        current = objects.Service.get_by_id(self.ctx, service_id)
        self.assertEqual(states.NONE, current.task_state)
        self.assertIsNone(current.task_owner)
        self.assertEqual(states.FAILED, objects.Deployment.get_by_id(
            self.ctx, pending[0].id).status)
        current.acquire_task(states.RECONCILING, owner='domain_change')
        claim_time = current.task_started_at
        for driver_name in ('noop', 'fake'):
            stack = objects.Stack.get_by_id(self.ctx, current.stack_id)
            stack.driver = driver_name
            stack.save()
            with mock.patch.object(self.manager, '_create_service') as create:
                self.manager.execute(self.ctx, **old)
            create.assert_not_called()
            fresh = objects.Service.get_by_id(self.ctx, service_id)
            self.assertEqual(states.RECONCILING, fresh.task_state)
            self.assertEqual(claim_time, fresh.task_started_at)

    def test_stale_claim_preserves_durable_work(self):
        created, path, domain = self._open_domain('retry.example.com')
        parent = objects.Service.get_by_id(self.ctx, created['id'])
        parent.acquire_task(states.RECONCILING, owner='settings_sync')
        for pending in (objects.DeploymentList, objects.BackupJobList):
            with self.subTest(pending=pending.__name__), \
                    mock.patch.object(timeutils, 'utcnow', return_value=(
                        parent.task_started_at +
                        datetime.timedelta(seconds=301))), \
                    mock.patch.object(pending, 'get_all',
                                      return_value=[mock.sentinel.job]):
                self.assertIsNone(self.manager._recover_domain_task(
                    self.ctx, parent.id))
            self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
                self.ctx, parent.id).task_state)

    def test_stale_claim_recovers_domain_and_periodic_owners(self):
        created, path, domain = self._open_domain('retry.example.com')
        parent = objects.Service.get_by_id(self.ctx, created['id'])
        parent.acquire_task(states.RECONCILING, owner='domain_change')
        self.manager.settle_deployments()
        self.request('delete', path + '/' + domain['id'], status=409)
        parent = objects.Service.get_by_id(self.ctx, parent.id)
        self.assertEqual(states.RECONCILING, parent.task_state)
        with db_api.writer.using(self.ctx):
            row = db_api.services._get(self.ctx, parent.id, lock=True)
            row.task_started_at = timeutils.utcnow() - datetime.timedelta(
                seconds=301)
        self.manager.settle_deployments()
        parent = objects.Service.get_by_id(self.ctx, parent.id)
        self.assertEqual(states.NONE, parent.task_state)
        parent.acquire_task(states.RECONCILING, owner='domain_check')
        with db_api.writer.using(self.ctx):
            row = db_api.services._get(self.ctx, parent.id, lock=True)
            row.task_started_at = timeutils.utcnow() - datetime.timedelta(
                seconds=301)
        self.manager.settle_deployments()
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, parent.id).task_state)
        self.request('delete', path + '/' + domain['id'],
                     status=202, complete=True)

    def _verify_domain(self, path, domain_id):
        self.request('post', path + '/' + domain_id + '/action',
                     {'verify': {}}, status=202, complete=True)

    def test_domain_network_checks_leave_service_available(self):
        self._domain_network_checks_leave_service_available(False)

    def test_skip_verification_network_checks_leave_service_available(self):
        self._domain_network_checks_leave_service_available(True)

    def _domain_network_checks_leave_service_available(self, skip):
        self.config.config(skip_verification=skip, group='domains')
        created, path, domain = self._open_domain('web.example.com')
        if not skip:
            self._verify_domain(path, domain['id'])

        def check(*args, **kwargs):
            time.sleep(0.01)
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, created['id']).task_state)
            return True

        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        side_effect=check), \
                mock.patch('coast.conductor.manager.domain_check.routes_to',
                           side_effect=check), \
                mock.patch('coast.conductor.manager.domain_check.tls_ready',
                           side_effect=check):
            self.manager._drive_domain(objects.Domain.get_by_id(
                self.ctx, domain['id']))
        self.request('delete', path + '/' + domain['id'], status=202,
                     complete=True)

    def test_stale_domain_check_cannot_overwrite_edit_or_busy_task(self):
        self._stale_domain_check_during_edit(complete=False)

    def test_stale_domain_check_cannot_overwrite_completed_edit(self):
        self._stale_domain_check_during_edit(complete=True)

    def _stale_domain_check_during_edit(self, complete):
        created, path, domain = self._open_domain('web.example.com')
        self._verify_domain(path, domain['id'])
        self.messages = []

        def edit(*args):
            # Keep RPC delivery under test control: the fake conductor can
            # otherwise finish the edit before the busy-state assertion.
            with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
                self.request('patch', path + '/' + domain['id'],
                             {'domain': {'tls': 'none'}}, status=202,
                             headers=dict(self.headers, **{
                                 'OpenStack-API-Version': 'app-hosting 1.2'}))
            if complete:
                self.send(self.messages[0])
            return True

        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        side_effect=edit):
            self.manager._drive_domain(objects.Domain.get_by_id(
                self.ctx, domain['id']))
        self.assertEqual(1, len(self.messages))
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.PENDING_VERIFICATION, stored.status)
        self.assertEqual('none', stored.tls)
        self.assertEqual(states.NONE if complete else states.RECONCILING,
                         objects.Service.get_by_id(
                             self.ctx, created['id']).task_state)
        if not complete:
            self.send(self.messages[0])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, created['id']).task_state)

    def test_dns_compensation_preserves_link_after_unrelated_task(self):
        created, _path, domain = self._open_domain('web.example.com')
        service = objects.Service.get_by_id(self.ctx, created['id'])
        row = objects.Domain.get_by_id(self.ctx, domain['id'])
        with mock.patch.object(managed_dns, 'delete_link') as cleanup:
            with managed_dns.domain_records(
                    self.manager, self.ctx, service, row, True):
                current = objects.Service.get_by_id(self.ctx, service.id)
                current.acquire_task(states.RECONCILING, owner='restart')
                current.release_task(states.RECONCILING)
                fresh = objects.Domain.get_by_id(self.ctx, row.id)
                fresh.status_reason = 'Updated observation'
                fresh.save()
            cleanup.assert_not_called()

    def test_dns_compensation_preserves_link_on_busy_save(self):
        created, _path, domain = self._open_domain('web.example.com')
        service = objects.Service.get_by_id(self.ctx, created['id'])
        row = objects.Domain.get_by_id(self.ctx, domain['id'])
        with mock.patch.object(managed_dns, 'delete_link') as cleanup:
            with self.assertRaises(exception.Conflict):
                with managed_dns.domain_records(
                        self.manager, self.ctx, service, row, True):
                    current = objects.Service.get_by_id(self.ctx, service.id)
                    current.acquire_task(states.RECONCILING, owner='restart')
                    raise exception.Conflict()
            cleanup.assert_not_called()
        current.release_task(states.RECONCILING)

    def test_dns_compensation_removes_superseded_link(self):
        created, _path, domain = self._open_domain('web.example.com')
        service = objects.Service.get_by_id(self.ctx, created['id'])
        row = objects.Domain.get_by_id(self.ctx, domain['id'])
        with mock.patch.object(managed_dns, 'delete_link') as cleanup:
            with self.assertRaises(exception.Conflict):
                with managed_dns.domain_records(
                        self.manager, self.ctx, service, row, True):
                    fresh = objects.Domain.get_by_id(self.ctx, row.id)
                    fresh.fqdn = 'new.example.com'
                    fresh.save()
            self.assertEqual('web.example.com',
                             cleanup.call_args.args[-1]['fqdn'])

    def test_dns_compensation_removes_link_after_finished_delete(self):
        created, path, domain = self._open_domain('web.example.com')
        service = objects.Service.get_by_id(self.ctx, created['id'])
        row = objects.Domain.get_by_id(self.ctx, domain['id'])
        with mock.patch.object(managed_dns, 'delete_link') as cleanup:
            with self.assertRaises(exception.Conflict):
                with managed_dns.domain_records(
                        self.manager, self.ctx, service, row, True):
                    self.request('delete', path + '/' + row.id, status=202,
                                 complete=True)
                    cleanup.reset_mock()
            cleanup.assert_called_once()

    def test_delete_is_accepted_during_slow_domain_check(self):
        _created, path, domain = self._open_domain('web.example.com')
        self._verify_domain(path, domain['id'])

        def delete(*args):
            time.sleep(0.01)
            self.request('delete', path + '/' + domain['id'], status=202,
                         complete=True)
            return True

        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        side_effect=delete):
            self.manager._drive_domain(objects.Domain.get_by_id(
                self.ctx, domain['id']))
        self.assertRaises(exception.NotFound, objects.Domain.get_by_id,
                          self.ctx, domain['id'])

    def test_domain_driver_write_is_locked_and_failure_releases_task(self):
        created, path, domain = self._open_domain('web.example.com')
        self._verify_domain(path, domain['id'])
        service = objects.Service.get_by_id(self.ctx, created['id'])
        stack = objects.Stack.get_by_id(self.ctx, service.stack_id)
        driver = self.manager._driver(stack)

        def fail(*args):
            locked = objects.Service.get_by_id(self.ctx, created['id'])
            self.assertEqual(states.RECONCILING, locked.task_state)
            self.assertEqual('domain_check', locked.task_owner)
            raise RuntimeError('write failed')

        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        return_value=True), \
                mock.patch.object(driver, 'set_domains', side_effect=fail):
            self.assertRaises(RuntimeError, self.manager._drive_domain,
                              objects.Domain.get_by_id(self.ctx, domain['id']))
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.NONE, service.task_state)
        self.assertIsNone(service.task_owner)
        self.assertIsNone(service.task_started_at)

    def test_domain_timeout_does_not_leave_service_busy(self):
        created, path, domain = self._open_domain('web.example.com')
        self._verify_domain(path, domain['id'])

        def timeout(*args):
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, created['id']).task_state)
            raise TimeoutError()

        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        side_effect=timeout):
            self.manager.drive_domains()
        self.request('delete', path + '/' + domain['id'], status=202,
                     complete=True)

    def test_drive_skips_domain_without_verify_request(self):
        _created, _path, domain = self._open_domain('web.example.com')
        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        return_value=True) as checked:
            self.manager.drive_domains()
        checked.assert_not_called()
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.PENDING_VERIFICATION, stored.status)

    def test_verification_timeout_sets_error(self):
        _created, path, domain = self._open_domain('web.example.com')
        self._verify_domain(path, domain['id'])
        later = timeutils.utcnow() + datetime.timedelta(days=2)
        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        return_value=True) as checked, \
                mock.patch('coast.conductor.manager.timeutils.utcnow',
                           return_value=later):
            self.manager.drive_domains()
        checked.assert_not_called()
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.ERROR, stored.status)
        messages = [item.message for item in objects.EventList.get_all(
            self.ctx, filters={'resource_id': domain['id'],
                               'outcome': 'failure'})]
        self.assertEqual(['Domain verification timed out.'], messages)

    def test_activation_timeout_sets_error(self):
        _created, path, domain = self._open_domain('web.example.com')
        self._verify_domain(path, domain['id'])
        row = objects.Domain.get_by_id(self.ctx, domain['id'])
        row.status = states.VERIFIED
        row.verified_at = timeutils.utcnow() - datetime.timedelta(hours=2)
        row.save()
        with mock.patch('coast.conductor.manager.domain_check.routes_to',
                        return_value=True) as checked:
            self.manager.drive_domains()
        checked.assert_not_called()
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.ERROR, stored.status)
        messages = [item.message for item in objects.EventList.get_all(
            self.ctx, filters={'resource_id': domain['id'],
                               'outcome': 'failure'})]
        self.assertEqual(['Domain activation timed out.'], messages)

    def test_drive_logs_one_domain_error_and_continues(self):
        _created, path, bad = self._open_domain('bad.example.com')
        good = self.request(
            'post', path, {'domain': {'fqdn': 'good.example.com'}},
            status=201, complete=True).json['domain']
        self._verify_domain(path, bad['id'])
        self._verify_domain(path, good['id'])
        token = bad['dns_records'][0]['value']

        def txt(fqdn, value, resolver):
            if fqdn == 'bad.example.com':
                raise RuntimeError('leak-' + value)
            return True

        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        side_effect=txt), \
                mock.patch('coast.conductor.manager.domain_check.routes_to',
                           return_value=True), \
                mock.patch('coast.conductor.manager.domain_check.tls_ready',
                           return_value=True), \
                mock.patch('coast.conductor.manager.LOG.warning') as warned:
            self.manager.drive_domains()
        logged = str(warned.call_args_list)
        self.assertIn(bad['id'], logged)
        self.assertIn('RuntimeError', logged)
        self.assertNotIn(token, logged)
        self.assertNotIn('leak-', logged)
        self.assertEqual(states.PENDING_VERIFICATION, objects.Domain.get_by_id(
            self.ctx, bad['id']).status)
        self.assertEqual(states.ACTIVE, objects.Domain.get_by_id(
            self.ctx, good['id']).status)

    def test_export_delete_before_create_releases_stack(self):
        path = self.stack()
        env_id = path.rsplit('/', 1)[1]
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            created = self.request('post', path + '/exports', {'export': {}},
                                   status=202).json['export']
            self.request('delete', path + '/exports/' + created['id'],
                         status=202)
        self.send(self.messages[1])
        self.send(self.messages[0])
        self.request('get', path + '/exports/' + created['id'], status=404)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, env_id).task_state)
        self.assertNotIn(self.manager.cloud.reference('export', created['id']),
                         self.manager.cloud.resources)

    def test_finished_export_delete_keeps_other_lock(self):
        path = self.stack()
        env_id = path.rsplit('/', 1)[1]
        first = self.request('post', path + '/exports', {'export': {}},
                             status=202, complete=True).json['export']
        self.messages = []
        with mock.patch.object(self.rpc, 'cast', side_effect=self.capture):
            second = self.request('post', path + '/exports', {'export': {}},
                                  status=202).json['export']
            self.request('delete', path + '/exports/' + first['id'],
                         status=202)
        self.send(self.messages[1])
        self.assertEqual(states.RECONCILING, objects.Stack.get_by_id(
            self.ctx, env_id).task_state)
        self.send(self.messages[0])
        self.assertEqual(states.ACTIVE, objects.Export.get_by_id(
            self.ctx, second['id']).status)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, env_id).task_state)

    def test_volume_observations_cross_rpc_without_changing_accounts(self):
        path = self.stack()
        created = self.request('post', '/v1/services', self.service_body(
            self.worker(), deploy=False), status=202, complete=True)
        service_id = created.json['service']['id']
        stack = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        driver = self.manager._driver(stack)
        mounts = [{'name': 'compose-data', 'container_path': '/data'}]
        with mock.patch.object(driver, 'list_volume_mounts',
                               return_value=mounts):
            self.assertEqual(mounts,
                             self.rpc.list_volume_mounts(self.ctx, service_id))
            for version in ('1.16', '1.24'):
                response = self.request(
                    'get', path + '/storages?service_id=' + service_id,
                    headers={**self.headers, 'OpenStack-API-Version':
                             'app-hosting ' + version})
                self.assertEqual(['/data'], [
                    row['container_path'] for row in
                    response.json['storages']])
        self.assertEqual([], list(objects.StorageList.get_all(self.ctx)))
        other = context.RequestContext(project_id='other')
        self.assertRaises(
            errors.StorageUnavailable, self.rpc.list_volume_mounts,
            other, service_id)

    def test_set_env_passes_secret_without_logging(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.stack()
        driver = self.manager._driver(objects.Stack(
            driver='noop', status=states.ACTIVE, engine_version='noop-1.0'))
        seen = []
        original = driver.set_env

        def spy(endpoint, env, service, variables):
            seen.append(variables)
            return original(endpoint, env, service, variables)

        with mock.patch.object(driver, 'set_env', side_effect=spy):
            self.request('post', '/v1/services', self.service_body(
                self.worker(), deploy=False, env={'PASSWORD': {
                    'value': 'secret-value', 'secret': True}}), status=202,
                complete=True)
        self.assertEqual('secret-value', seen[0]['PASSWORD'].value)
        self.assertTrue(seen[0]['PASSWORD'].is_secret)
        self.assertNotIn('secret-value', logs.output)
        self.assertNotIn('secret-value', str(self.manager.notifier.events))

    def test_template_create_and_env_updates_skip_empty_mounts(self):
        logs = self.useFixture(fixtures.FakeLogger())
        path = self.stack()
        stack_id = path.rsplit('/', 1)[1]
        worker = self.worker()
        appgroup = objects.Appgroup(
            context=self.ctx, stack_id=stack_id, name='site',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='appgroup-project')
        appgroup.create()
        environment = objects.Environment(
            context=self.ctx, appgroup_id=appgroup.id, name='stage',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='stage-uuid')
        environment.create()
        source = objects.Source(context=self.ctx, type='template')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=stack_id,
            environment_id=environment.id, name='blog',
            source_id=source.id, template_id='wordpress-with-mariadb',
            server_id=worker.id, ports='', replicas=1, auto_deploy=False,
            status=states.PENDING, task_state=states.PROVISIONING)
        service.create()
        driver = self.manager._driver(objects.Stack(
            driver='noop', status=states.ACTIVE, engine_version='noop-1.0'))
        with mock.patch.object(driver, 'set_env') as set_env, \
                mock.patch.object(driver, 'set_volume_mounts') as mounts, \
                mock.patch.object(driver, 'set_domains') as domains:
            self.manager.execute(self.ctx, 'create_service', service)
        set_env.assert_called_once()
        self.assertEqual({}, set_env.call_args.args[-1])
        mounts.assert_not_called()
        domains.assert_called_once()
        saved = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual('noop-app-' + service.id, saved.engine_ref)
        self.assertEqual(states.STOPPED, saved.status)
        self.assertEqual(states.NONE, saved.task_state)
        self.manager.done.get(timeout=10)  # Direct create completion.
        env_path = '/v1/services/' + service.id + '/env'
        seen = []
        with mock.patch.object(driver, 'set_env', side_effect=lambda *args:
                               seen.append(args[-1])):
            self.request('patch', env_path, {'env': {'PASSWORD': {
                'value': 'template-secret', 'secret': True}}},
                         status=200, complete=True)
            self.request('put', env_path, {'env': {}},
                         status=200, complete=True)
        self.assertEqual('template-secret', seen[0]['PASSWORD'].value)
        self.assertEqual({}, seen[1])
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.assertNotIn('template-secret', logs.output)
        self.assertNotIn('template-secret', str(self.manager.notifier.events))
        domain_path = '/v1/services/' + service.id + '/domains'
        with mock.patch.object(driver, 'set_domains') as domains, \
                mock.patch('coast.conductor.manager.domain_check.txt_matches',
                           return_value=True), \
                mock.patch('coast.conductor.manager.domain_check.routes_to',
                           return_value=True), \
                mock.patch('coast.conductor.manager.domain_check.tls_ready',
                           return_value=True):
            domain = self.request('post', domain_path, {'domain': {
                'fqdn': 'template.example.com'}}, status=201,
                complete=True).json['domain']
            domains.assert_not_called()
            self.request('post', domain_path + '/' + domain['id'] + '/action',
                         {'verify': {}}, status=202, complete=True)
            domains.assert_called()
            self.manager.drive_domains()
            self.assertIn('https://template.example.com',
                          domains.call_args.args[-1])
            self.request('delete', domain_path + '/' + domain['id'],
                         status=202, complete=True)
            self.assertNotIn('https://template.example.com',
                             domains.call_args.args[-1])
        self.assertNotIn('SERVICE_', logs.output)
        self.assertNotIn('wordpress-with-mariadb', logs.output)

    def test_git_create_passes_appgroup_and_environment_refs(self):
        path = self.stack()
        stack_id = path.rsplit('/', 1)[1]
        worker = self.worker()
        appgroup = objects.Appgroup(
            context=self.ctx, stack_id=stack_id, name='site',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='appgroup-project')
        appgroup.create()
        environment = objects.Environment(
            context=self.ctx, appgroup_id=appgroup.id, name='stage',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='stage-uuid')
        environment.create()
        source = objects.Source(
            context=self.ctx, type='git',
            repo_url='https://example.com/web.git')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=stack_id,
            environment_id=environment.id, name='web',
            source_id=source.id, server_id=worker.id, ports='8080',
            replicas=1, auto_deploy=False, status=states.PENDING,
            task_state=states.PROVISIONING)
        service.create()
        driver = self.manager._driver(objects.Stack(
            driver='noop', status=states.ACTIVE, engine_version='noop-1.0'))
        seen = {}
        original = driver.create_app

        def spy(endpoint, env, row, source_row, credentials):
            seen['project'] = row.project_engine_ref
            seen['environment'] = row.environment_engine_ref
            return original(endpoint, env, row, source_row, credentials)

        with mock.patch.object(driver, 'create_app', side_effect=spy):
            self.manager.execute(self.ctx, 'create_service', service)
        self.assertEqual('appgroup-project', seen['project'])
        self.assertEqual('stage-uuid', seen['environment'])
        self.assertNotEqual(
            objects.Stack.get_by_id(self.ctx, stack_id).engine_ref,
            seen['project'])

    def test_git_create_missing_parent_ref_fails(self):
        path = self.stack()
        stack_id = path.rsplit('/', 1)[1]
        worker = self.worker()
        appgroup = objects.Appgroup(
            context=self.ctx, stack_id=stack_id, name='site',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='appgroup-project')
        appgroup.create()
        environment = objects.Environment(
            context=self.ctx, appgroup_id=appgroup.id, name='stage',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref=None)
        environment.create()
        source = objects.Source(
            context=self.ctx, type='git',
            repo_url='https://example.com/web.git')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=stack_id,
            environment_id=environment.id, name='web',
            source_id=source.id, server_id=worker.id, ports='8080',
            replicas=1, auto_deploy=False, status=states.PENDING,
            task_state=states.PROVISIONING)
        service.create()
        self.manager.execute(self.ctx, 'create_service', service)
        saved = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(states.ERROR, saved.status)
        self.assertEqual(states.NONE, saved.task_state)


class RealFlowTest(base.TestCase):
    def test_service_actions_allowed_for_real_drivers(self):
        template = types.SimpleNamespace(template_id='uptime-kuma')
        app = types.SimpleNamespace(template_id=None)
        for operation in ('start_service', 'stop_service',
                          'restart_service', 'create_service',
                          'delete_service', 'create_deployment',
                          'update_service'):
            self.assertTrue(manager.real_flow(operation, template))
            self.assertTrue(manager.real_flow(operation, app))
        self.assertFalse(manager.real_flow('create_export', template))
        self.assertFalse(manager.real_flow('unsupported_stack', app))
