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

"""VM add/remove state, transport and cleanup regression tests."""

import datetime
from types import SimpleNamespace
from unittest import mock

import fixtures

from openstack import exceptions as sdk_exceptions
from oslo_utils import timeutils

from coast.api import secret_store
from coast.common import context
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import rpcapi
from coast.conductor import secret_retry
from coast.conductor import server_failover
from coast.db import api
from coast.drivers.coolify import v4
from coast.health_manager import failover
from coast.health_manager import stale
from coast import objects
from coast.tests.unit import test_server_flows


class WorkerFailoverTest(test_server_flows.ServerFlowTest):
    def setUp(self):
        super().setUp()
        self.moves = []
        nova = self.proxies['nova']
        nova.get_server.side_effect = self.get_guest
        nova.lock_server.side_effect = self.lock
        nova.unlock_server.side_effect = self.unlock
        nova.stop_server.side_effect = self.stop
        nova.start_server.side_effect = self.start
        nova.volume_attachments.side_effect = lambda identifier: [
            SimpleNamespace(volume_id=v.id) for v in
            self.cloud_resources['volume'].values()
            if any(a['server_id'] == identifier for a in v.attachments)]
        nova.server_interfaces.side_effect = lambda identifier: [
            SimpleNamespace(port_id=p.id) for p in
            self.cloud_resources['port'].values() if p.device_id == identifier]
        nova.delete_volume_attachment.side_effect = self.detach_volume
        nova.delete_server_interface.side_effect = self.detach_port
        nova.create_volume_attachment.side_effect = self.attach_volume
        nova.create_server_interface.side_effect = self.attach_port
        nova.wait_for_server.side_effect = self.wait_guest
        self.proxies['neutron'].get_ip.side_effect = lambda identifier: (
            SimpleNamespace(port_id=self.env_port))
        self.patch(server_failover.time, 'sleep', return_value=None)

    def secret_outage(self, reference):
        self.provision()
        before = objects.Stack.get_by_id(self.ctx, self.stack.id)
        original = secret_store.call
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            def fetch(operation, ctx, ref, *args):
                if ref == reference:
                    raise exception.SecretStoreUnavailable()
                return original(operation, ctx, ref, *args)
            with mock.patch.object(secret_store, 'call', side_effect=fetch):
                server = self.replace()
                self.assertEqual(states.UNHEALTHY, server.status)
                self.assertEqual(states.NONE, server.task_state)
                self.assertEqual(self.old_id, server.compute_id)
                self.assertEqual([], self.moves)
                stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
                self.assertEqual(before.status, stack.status)
                self.assertEqual(before.fault, stack.fault)
                self.assertEqual(states.NONE, stack.task_state)
                # Retry admission uses the current revision, as the detector
                # does. No receipt or stale claim may prevent recovery.
                self.attempt = server
                self.attempt_revision = rpcapi.revision(server)
                self.assertEqual(states.UNHEALTHY, self.replace().status)
            clock.return_value = 111
            self.assertEqual(states.ACTIVE, self.replace().status)
            self.assertNotEqual(self.old_id, objects.Server.get_by_id(
                self.ctx, self.server.id).compute_id)

    def test_heartbeat_secret_outage_defers_and_recovers(self):
        self.secret_outage('hmac-ref')

    def test_ssh_secret_outage_defers_and_recovers(self):
        self.secret_outage('ssh-ref')

    def test_explicit_unhealthy_outage_preserves_status_and_recovers(self):
        self.provision()
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.UNHEALTHY
        self.server.fault = 'Existing server failure.'
        self.server.save()
        self.admin_claim()
        # RPC datetime fields truncate microseconds; the accepted revision
        # remains exact in the separate RPC argument.
        self.attempt.updated_at = self.attempt.updated_at.replace(
            microsecond=0)
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                result = self.replace()
            self.assertEqual(
                (states.UNHEALTHY, states.NONE, 'Existing server failure.'),
                (result.status, result.task_state, result.fault))
            self.assertEqual([], self.moves)
            clock.return_value = 111
            self.admin_claim()
            result = self.replace()
            self.assertEqual((states.ACTIVE, states.NONE, None),
                             (result.status, result.task_state, result.fault))

    def test_explicit_error_outage_preserves_status_and_recovers(self):
        self.provision()
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.ERROR
        self.server.fault = 'Existing server failure.'
        self.server.save()
        self.admin_claim()
        # RPC datetime fields truncate microseconds; the accepted revision
        # remains exact in the separate RPC argument.
        self.attempt.updated_at = self.attempt.updated_at.replace(
            microsecond=0)
        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                result = self.replace()
            self.assertEqual(
                (states.ERROR, states.NONE, 'Existing server failure.'),
                (result.status, result.task_state, result.fault))
            self.assertEqual([], self.moves)
            clock.return_value = 111
            self.admin_claim()
            result = self.replace()
            self.assertEqual((states.ACTIVE, states.NONE, None),
                             (result.status, result.task_state, result.fault))

    def test_engine_token_outage_does_not_claim_or_fence(self):
        self.provision()
        with mock.patch.object(self.manager.__class__, '_endpoint',
                               side_effect=exception.SecretStoreUnavailable()):
            self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assertEqual([], self.moves)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def create_cloud(self, kind, values):
        result = super().create_cloud(kind, values)
        if kind == 'port':
            result.device_id = ''
        if kind == 'volume':
            result.attachments = []
        if kind == 'server':
            result.status = 'ACTIVE'
            result.is_locked = False
            for mapping in values['block_device_mapping']:
                if mapping['source_type'] == 'volume':
                    self.attach_volume(result.id, mapping['uuid'])
            for nic in values['networks']:
                self.attach_port(result.id, nic['port'])
        return result

    def get_guest(self, identifier):
        self.assert_no_transaction()
        if identifier not in self.cloud_resources['server']:
            raise sdk_exceptions.ResourceNotFound('Guest not found')
        return self.cloud_resources['server'][identifier]

    def lock(self, identifier, locked_reason=None):
        self.get_guest(identifier).is_locked = True

    def unlock(self, identifier):
        self.get_guest(identifier).is_locked = False

    def stop(self, identifier):
        self.get_guest(identifier).status = 'SHUTOFF'
        self.moves.append(('stop', identifier))

    def start(self, identifier):
        self.assertFalse(self.get_guest(identifier).is_locked)
        self.get_guest(identifier).status = 'ACTIVE'
        self.moves.append(('start', identifier))

    def wait_guest(self, guest, status='ACTIVE', **kwargs):
        self.assert_no_transaction()
        self.assertEqual(status, guest.status)

    def detach_volume(self, identifier, volume):
        self.assertEqual('SHUTOFF', self.get_guest(identifier).status)
        self.assertTrue(self.get_guest(identifier).is_locked)
        self.cloud_resources['volume'][volume].attachments = []
        self.moves.append(('detach-volume', identifier))

    def detach_port(self, port, identifier):
        self.assertEqual('SHUTOFF', self.get_guest(identifier).status)
        self.assertTrue(self.get_guest(identifier).is_locked)
        self.cloud_resources['port'][port].device_id = ''
        self.moves.append(('detach-port', identifier))

    def attach_volume(self, identifier, volume):
        self.assert_no_transaction()
        resource = self.cloud_resources['volume'][volume]
        self.assertEqual([], resource.attachments)
        resource.attachments = [dict(server_id=identifier)]
        self.moves.append(('attach-volume', identifier))

    def attach_port(self, identifier, port_id):
        self.assert_no_transaction()
        resource = self.cloud_resources['port'][port_id]
        self.assertEqual('', resource.device_id)
        resource.device_id = identifier
        self.moves.append(('attach-port', identifier))

    def delete_cloud(self, kind, value):
        if kind == 'server':
            identifier = value if isinstance(value, str) else value.id
            for resource in self.cloud_resources['volume'].values():
                resource.attachments = [a for a in resource.attachments
                                        if a['server_id'] != identifier]
            for resource in self.cloud_resources['port'].values():
                if resource.device_id == identifier:
                    resource.device_id = ''
        super().delete_cloud(kind, value)

    def provision(self, role='worker'):
        self.accepted(role=role)
        self.proxies['glance'].images.return_value[0].name = 'worker'
        self.server = self.execute()
        self.assertEqual(states.ACTIVE, self.server.status)
        self.old_id = self.server.compute_id
        self.env_port = [p.id for p in self.cloud_resources['port'].values()
                         if p.name.endswith('-env')][0]
        self.controller = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='controller',
            role='control', kind='vm', status=states.ACTIVE,
            task_state=states.NONE, compute_id='controller-nova',
            mgmt_ip='192.0.2.2')
        self.controller.create()
        self.port_ids = set(self.cloud_resources['port'])
        self.volume_ids = set(self.cloud_resources['volume'])
        self.before_quota = self.counters().in_use_volume_gb
        self.age()
        self.moves = []
        self.driver.set_build_server_mode.reset_mock()
        self.driver.get_server_status.return_value = SimpleNamespace(
            status=states.ACTIVE)

    def resize(self, role='worker', fail=False):
        self.provision(role)
        self.stack.status = states.ACTIVE
        self.stack.save()
        self.stack.acquire_task(states.FAILING_OVER)
        self.stack.save()
        self.server.status = states.ACTIVE
        self.server.save()
        old_flavor = self.server.flavor_id
        old_addresses = (self.server.mgmt_ip, self.server.private_ip,
                         self.server.floating_ip, self.server.floating_ip_id)
        service, _deployment = self.service()
        self.server.acquire_task(states.FAILING_OVER)
        self.server.status = states.FAILOVER
        self.server.save()
        self.driver.get_app_status.return_value = SimpleNamespace(
            status=states.ACTIVE)
        if fail:
            self.agent.wait_health.side_effect = RuntimeError('health failed')
        else:
            def healthy(*args, **kwargs):
                saved = objects.Server.get_by_id(self.ctx, self.server.id)
                self.assertEqual(old_flavor, saved.flavor_id)
            self.agent.wait_health.side_effect = healthy
        self.manager.execute(self.ctx, 'resize_server', self.server,
                             options={'targets': {self.server.id: {
                                 'target_flavor': 'larger-flavor',
                                 'target_plan': 'larger'}}},
                             revision=rpcapi.revision(self.server))
        saved = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(old_flavor if fail else 'larger-flavor',
                         saved.flavor_id)
        self.assertEqual(states.ERROR if fail else states.ACTIVE, saved.status)
        self.assert_retained()
        self.assertEqual(old_addresses, (saved.mgmt_ip, saved.private_ip,
                                         saved.floating_ip,
                                         saved.floating_ip_id))
        self.assertEqual(states.NONE, saved.task_state)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        if fail:
            self.assertEqual(self.old_id, saved.compute_id)
            self.assertEqual('ACTIVE', self.get_guest(self.old_id).status)
            self.assertEqual([self.old_id], list(
                self.cloud_resources['server']))
            for volume in self.cloud_resources['volume'].values():
                self.assertEqual([{'server_id': self.old_id}],
                                 volume.attachments)
            for port in self.cloud_resources['port'].values():
                self.assertEqual(self.old_id, port.device_id)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def test_resize_secret_outage_preserves_server_and_recovers(self):
        self.provision()
        self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.stack.status = states.ACTIVE
        self.stack.save()
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.ACTIVE
        self.server.fault = 'Existing server fault.'
        self.server.save()
        old_id, old_flavor = self.server.compute_id, self.server.flavor_id
        before = list(self.moves)
        options = {'admission_status': states.ACTIVE, 'targets': {
            self.server.id: {'target_flavor': 'larger-flavor',
                             'target_plan': 'larger'}}}

        def execute():
            stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            stack.acquire_task(states.FAILING_OVER)
            server = objects.Server.get_by_id(self.ctx, self.server.id)
            server.acquire_task(states.FAILING_OVER)
            server.status = states.FAILOVER
            server.save()
            self.manager.execute(self.ctx, 'resize_server', server,
                                 options=options,
                                 revision=rpcapi.revision(server))
            return objects.Server.get_by_id(self.ctx, self.server.id)

        with mock.patch.object(secret_retry.time, 'monotonic',
                               return_value=100) as clock:
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                server = execute()
            self.assertEqual((states.ACTIVE, states.NONE, old_id, old_flavor,
                              'Existing server fault.'),
                             (server.status, server.task_state,
                              server.compute_id, server.flavor_id,
                              server.fault))
            self.assertEqual(before, self.moves)
            self.assertEqual(states.NONE, objects.Stack.get_by_id(
                self.ctx, self.stack.id).task_state)
            clock.return_value = 111
            server = execute()
            self.assertEqual((states.ACTIVE, states.NONE, 'larger-flavor'),
                             (server.status, server.task_state,
                              server.flavor_id))
            self.assertNotEqual(old_id, server.compute_id)

    def test_resize_worker_retains_storage_and_ingress(self):
        self.resize()

    def test_resize_build_retains_storage_and_ingress(self):
        self.resize('build')

    def test_resize_health_failure_restores_old_guest(self):
        self.resize(fail=True)

    def test_resize_build_health_failure_restores_old_guest(self):
        self.resize('build', fail=True)

    def rolling_resize(self, fail_health=False, outage=False):
        self.provision()
        self.stack.status = states.ACTIVE
        self.stack.save()
        self.server.status = states.ACTIVE
        self.server.save()
        first = self.server
        second = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='second-worker',
            hostname='second-worker', kind='vm', role='worker',
            status=states.PENDING, task_state=states.NONE)
        with api.writer.using(self.ctx):
            reservation = quota.reserve(self.ctx, servers=1)
            second.create()
            second.acquire_task(states.PROVISIONING)
        self.manager.execute(self.ctx, 'create_server', second,
                             options={'plan': 'worker-plan'},
                             reservation=reservation,
                             revision=rpcapi.revision(second))
        second = objects.Server.get_by_id(self.ctx, second.id)
        self.assertEqual(states.ACTIVE, second.status)
        workers = sorted((first, second), key=lambda server: server.id)
        old_ids = {server.id: server.compute_id for server in workers}
        for server in workers:
            # Floating-IP retention is covered by individual resize tests.
            server.floating_ip_id = None
            if outage:
                server.fault = 'Existing server fault.'
            server.save()
            self.server = server
            service, _deployment = self.service()
            service.name = server.name
            service.save()
        order = []
        create = self.proxies['nova'].create_server.side_effect

        def replacing():
            rows = objects.ServerList.get_all(self.ctx, filters={
                'stack_id': self.stack.id, 'status': states.FAILOVER})
            self.assertEqual(1, len(rows))
            return rows[0]

        def boot(**values):
            server = replacing()
            index = [worker.id for worker in workers].index(server.id)
            # A later Nova boot is forbidden until the preceding worker's
            # real flow has passed both agent and workload health checks.
            if index:
                self.assertIn(('health', workers[index - 1].id), order)
                self.assertIn(('workload', workers[index - 1].id), order)
                self.assertEqual(states.ACTIVE, objects.Server.get_by_id(
                    self.ctx, workers[index - 1].id).status)
            order.append(('boot', server.id))
            return create(**values)

        def health():
            server = replacing()
            self.assertEqual('flavor', server.flavor_id)
            self.assertEqual(('boot', server.id), order[-1])
            if fail_health:
                order.append(('failed-health', server.id))
                raise RuntimeError('replacement health failed')
            order.append(('health', server.id))

        def workload(endpoint, stack, service):
            self.assertEqual(('health', service.server_id), order[-1])
            order.append(('workload', service.server_id))
            return SimpleNamespace(status=states.ACTIVE)

        self.proxies['nova'].create_server.side_effect = boot
        self.agent.wait_health.side_effect = health
        self.driver.get_app_status.side_effect = workload
        self.stack.acquire_task(states.FAILING_OVER)
        targets = {server.id: {'target_flavor': 'larger-flavor',
                               'target_plan': 'larger'} for server in workers}
        if outage:
            before = list(self.moves)
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                self.manager.execute(
                    self.ctx, 'resize_workers_stack', self.stack,
                    options={'targets': targets},
                    revision=rpcapi.revision(self.stack))
            self.assertEqual(before, self.moves)
            self.assertEqual([], order)
            for worker in workers:
                saved = objects.Server.get_by_id(self.ctx, worker.id)
                self.assertEqual((states.ACTIVE, states.NONE,
                                  old_ids[worker.id], 'flavor',
                                  'Existing server fault.'),
                                 (saved.status, saved.task_state,
                                  saved.compute_id, saved.flavor_id,
                                  saved.fault))
            self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
            self.assertEqual(states.NONE, self.stack.task_state)
            self.stack.acquire_task(states.FAILING_OVER)
            for worker in workers:
                secret_retry.clear(worker.id)
        self.manager.execute(self.ctx, 'resize_workers_stack', self.stack,
                             options={'targets': targets},
                             revision=rpcapi.revision(self.stack))
        if fail_health:
            self.assertEqual([('boot', workers[0].id),
                              ('failed-health', workers[0].id)], order)
        else:
            self.assertEqual([(step, server.id) for server in workers
                              for step in ('boot', 'health', 'workload')],
                             order)
        for index, worker in enumerate(workers):
            saved = objects.Server.get_by_id(self.ctx, worker.id)
            self.assertEqual(states.NONE, saved.task_state)
            if fail_health:
                self.assertEqual('flavor', saved.flavor_id)
                self.assertEqual(old_ids[worker.id], saved.compute_id)
                self.assertEqual(states.ERROR if index == 0 else states.ACTIVE,
                                 saved.status)
                self.assertEqual('ACTIVE', self.get_guest(
                    saved.compute_id).status)
            else:
                self.assertEqual('larger-flavor', saved.flavor_id)
                self.assertNotEqual(old_ids[worker.id], saved.compute_id)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)
        for service in objects.ServiceList.get_all(self.ctx, filters={
                'stack_id': self.stack.id}):
            self.assertEqual(states.NONE, service.task_state)

    def test_rolling_resize_secret_outage_defers_and_recovers(self):
        self.rolling_resize(outage=True)

    def test_rolling_resize_waits_for_agent_and_workload_health(self):
        self.rolling_resize()

    def test_rolling_resize_health_failure_prevents_next_replacement(self):
        self.rolling_resize(fail_health=True)

    def age(self):
        now = timeutils.utcnow()
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.server.status = states.UNHEALTHY
        self.server.last_heartbeat_at = now - datetime.timedelta(seconds=300)
        self.server.save()
        with api.writer.using(self.ctx):
            failover.record(self.ctx, self.server, states.UNHEALTHY,
                            now - datetime.timedelta(seconds=121))
        self.stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.stack.status = states.DEGRADED
        self.stack.save()
        self.attempt = self.server.obj_clone()
        self.attempt_revision = rpcapi.revision(self.attempt)

    def replace(self):
        self.manager.execute(self.ctx, 'failover_server', self.attempt,
                             options={'admission_status': getattr(
                                 self, 'admission_status', None)},
                             revision=self.attempt_revision)
        return objects.Server.get_by_id(self.ctx, self.server.id)

    def service(self, status=states.ACTIVE, template=None, source_type='git',
                env=None):
        source_values = (dict(type='git',
                              repo_url='https://example.com/repo.git',
                              branch='main') if source_type == 'git' else
                         dict(type='image', image='example.com/image:stable'))
        source = objects.Source(context=self.ctx, **source_values)
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=self.stack.id, server_id=self.server.id,
            name='service', source_id=source.id, template_id=template,
            ports='8080', replicas=1, auto_deploy=False, status=status,
            task_state=states.NONE, engine_ref='app-ref')
        service.create()
        deployment = objects.Deployment(
            context=self.ctx, service_id=service.id, server_id=self.server.id,
            status=states.ACTIVE,
            source_ref='main' if source_type == 'git' else 'stable',
            resolved_ref='a' * 40 if source_type == 'git' else None,
            trigger='api', spec={'source': source_values, 'env': env or {}})
        deployment.create()
        service.current_deployment_id = deployment.id
        service.save()
        return service, deployment

    def assert_retained(self):
        self.assertEqual(self.port_ids, set(self.cloud_resources['port']))
        self.assertEqual(self.volume_ids, set(self.cloud_resources['volume']))
        self.assertEqual(self.before_quota, self.counters().in_use_volume_gb)
        self.assertEqual('controller-nova', objects.Server.get_by_id(
            self.ctx, self.controller.id).compute_id)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def admin_claim(self):
        self.server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.admission_status = self.server.status
        self.server.acquire_task(states.FAILING_OVER)
        self.server.status = states.FAILOVER
        self.server.save()
        self.attempt = self.server.obj_clone()
        self.attempt_revision = rpcapi.revision(self.attempt)

    def test_admin_claim_does_not_require_unhealthy_grace(self):
        self.provision()
        self.admin_claim()
        result = self.replace()
        self.assertEqual((states.ACTIVE, states.NONE),
                         (result.status, result.task_state))
        self.assertNotEqual(self.old_id, result.compute_id)
        self.assert_retained()
        count = self.proxies['nova'].create_server.call_count
        self.replace()
        self.assertEqual(count, self.proxies['nova'].create_server.call_count)

    def test_admin_refusal_releases_claim_and_emits_error(self):
        self.provision()
        self.admin_claim()
        self.controller.status = states.UNHEALTHY
        self.controller.save()
        result = self.replace()
        self.assertEqual((states.ACTIVE, states.NONE, self.old_id),
                         (result.status, result.task_state, result.compute_id))
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': result.id, 'action': 'failover'})
        self.assertEqual(['failure'], [event.outcome for event in events])
        self.assertIn('coast.server.failover.error',
                      str(self.notifier.mock_calls))

    def refusal_logs(self):
        return self.useFixture(fixtures.FakeLogger(
            format='%(levelname)s %(message)s'))

    def assert_refused(self, logs, reason):
        expected = ('WARNING Failover: stack_id=%s server_id=%s '
                    'event=refused reason=%s' % (
                        self.stack.id, self.server.id, reason))
        self.assertEqual(1, logs.output.count(expected), logs.output)

    def test_log_start_and_end(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.provision()
        old_id = self.server.compute_id
        result = self.replace()
        self.assertEqual(1, logs.output.count('event=start'))
        self.assertEqual(1, logs.output.count('event=end'))
        self.assertIn('outcome=success', logs.output)
        self.assertIn('duration=', logs.output)
        self.assertIn('old_compute_id=%s' % old_id, logs.output)
        self.assertIn('new_compute_id=%s' % result.compute_id, logs.output)

    def test_worker_replacement_preserves_identity_storage_and_ingress(self):
        self.provision()
        service, deployment = self.service()
        self.driver.get_app_status.return_value = SimpleNamespace(
            status=states.ACTIVE)
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertNotEqual(self.old_id, server.compute_id)
        self.assertEqual('worker-engine', server.engine_ref)
        guest = self.cloud_resources['server'][server.compute_id]
        self.assertEqual(self.server.hostname, server.hostname)
        self.assertEqual(server.hostname, guest.name)
        self.assertIn('server_id=' + server.id, guest.tags)
        self.assertTrue(any(tag.startswith('attempt=')
                            and tag != 'attempt=' + server.id
                            for tag in guest.tags))
        self.assertEqual('fip-id', server.floating_ip_id)
        self.assertNotIn(self.old_id, self.cloud_resources['server'])
        self.assertEqual(('stop', self.old_id), self.moves[0])
        self.assert_retained()
        timeline = objects.EventList.get_all(self.ctx, filters={
            'resource_id': server.id, 'action': 'failover'})
        event = next(row for row in timeline if row.outcome == 'success')
        self.assertEqual(server.compute_id, event.detail['new_compute_id'])
        self.assertTrue(event.detail['old_compute_id'])
        self.assertEqual(server.name, event.detail['server_name'])

        self.assertEqual(deployment.id, objects.Service.get_by_id(
            self.ctx, service.id).current_deployment_id)
        self.driver.deploy_app.assert_not_called()
        count = self.proxies['nova'].create_server.call_count
        self.replace()
        self.assertEqual(count, self.proxies['nova'].create_server.call_count)
        self.assertNotIn('sensitive', str(self.notifier.mock_calls))

    def test_replaced_vm_is_deleted_by_saved_id_not_original_name(self):
        self.provision()
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        identifier = server.compute_id
        self.assertIn(identifier, self.cloud_resources['server'])
        self.remove()
        self.assertNotIn(identifier, self.cloud_resources['server'])
        self.assertEqual({}, self.cloud_resources['port'])
        retained = list(self.cloud_resources['volume'].values())
        self.assertEqual(1, len(retained))
        self.assertEqual([], retained[0].attachments)
        self.assertTrue(retained[0].name.endswith('-data'))

    def test_build_vm_preserves_registration_without_build_mode_switch(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.provision(role='build')
        server = self.replace()
        self.assertEqual(states.ACTIVE, server.status)
        self.assertIsNone(server.floating_ip_id)
        self.assert_retained()
        self.driver.set_build_server_mode.assert_not_called()
        self.assertIn('event=end', logs.output)
        self.assertIn('outcome=success', logs.output)

    def test_missing_appgroup_redeploys_pinned_current_deployment(self):
        self.provision()
        service, previous = self.service()
        self.driver.get_app_status.side_effect = [
            SimpleNamespace(status=states.STOPPED),
            SimpleNamespace(status=states.ACTIVE)]
        self.driver.deploy_app.return_value = SimpleNamespace(engine_ref='new')
        self.driver.get_deployment_status.return_value = SimpleNamespace(
            status=states.ACTIVE, resolved_ref='a' * 40)
        self.assertEqual(states.ACTIVE, self.replace().status)
        service = objects.Service.get_by_id(self.ctx, service.id)
        current = objects.Deployment.get_by_id(
            self.ctx, service.current_deployment_id)
        self.assertEqual('a' * 40, current.source_ref)
        self.assertEqual('new', current.engine_ref)
        self.assertEqual('reconcile', current.trigger)
        self.assertEqual(states.ACTIVE, current.status)
        self.assertEqual(states.SUPERSEDED, objects.Deployment.get_by_id(
            self.ctx, previous.id).status)
        self.assertEqual(states.NONE, service.task_state)
        self.assert_retained()

    def test_queued_redeployment_is_polled_to_completion(self):
        self.provision()
        service, _ = self.service()
        self.driver.get_app_status.side_effect = [
            SimpleNamespace(status=states.STOPPED),
            SimpleNamespace(status=states.ACTIVE)]
        self.driver.deploy_app.return_value = SimpleNamespace(engine_ref='new')
        self.driver.get_deployment_status.side_effect = [
            SimpleNamespace(status=status, resolved_ref='a' * 40) for status in
            (states.PENDING, states.BUILDING, states.DEPLOYING, states.ACTIVE)]
        self.assertEqual(states.ACTIVE, self.replace().status)
        current = objects.Service.get_by_id(self.ctx, service.id)
        self.assertNotEqual(service.current_deployment_id,
                            current.current_deployment_id)
        self.assertEqual(states.NONE, current.task_state)
        self.assert_retained()

    def test_controller_cloud_reference_cannot_be_replaced(self):
        self.provision()
        self.controller.compute_id = self.old_id
        self.controller.save()
        self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assertEqual('ACTIVE', self.get_guest(self.old_id).status)
        self.assertEqual([], self.moves)

    def test_concurrent_duplicate_and_other_server_skip_stack_claim(self):
        self.provision()
        peer = objects.Server(
            context=self.ctx, stack_id=self.stack.id, name='peer', kind='vm',
            role='build', status=states.UNHEALTHY, task_state=states.NONE,
            last_heartbeat_at=timeutils.utcnow() -
            datetime.timedelta(seconds=300))
        peer.create()
        with api.writer.using(self.ctx):
            failover.record(self.ctx, peer, states.UNHEALTHY,
                            timeutils.utcnow() -
                            datetime.timedelta(seconds=121))
        original = self.driver.register_server.side_effect

        def concurrent(*args):
            self.manager.execute(self.ctx, 'failover_server', self.attempt,
                                 revision=self.attempt_revision)
            self.manager.execute(self.ctx, 'failover_server', peer,
                                 revision=rpcapi.revision(peer))
            self.assertEqual(states.NONE, objects.Server.get_by_id(
                self.ctx, peer.id).task_state)
            return original(*args)
        self.driver.register_server.side_effect = concurrent
        self.assertEqual(states.ACTIVE, self.replace().status)
        self.assertEqual(2, self.proxies['nova'].create_server.call_count)
        self.assertEqual(states.UNHEALTHY,
                         objects.Server.get_by_id(self.ctx, peer.id).status)

    def test_redeployment_uses_frozen_environment_versions(self):
        self.provision()
        self.payloads['old-env-ref'] = 'original-sensitive'
        self.payloads['new-env-ref'] = 'changed-sensitive'
        service, _ = self.service(env={'TOKEN': {
            'value_ref': 'old-env-ref', 'version': 1, 'secret': True,
            'build_time': False}})
        desired = objects.Secret(
            context=self.ctx, service_id=service.id, key='TOKEN',
            value_ref='new-env-ref', version=2, is_secret=True,
            build_time=False)
        desired.create()
        engine_values = {}
        self.driver.set_env.side_effect = (
            lambda ep, stack, app, values: engine_values.update(
                {key: var.value for key, var in values.items()}))
        self.driver.get_app_status.side_effect = [
            SimpleNamespace(status=states.STOPPED),
            SimpleNamespace(status=states.ACTIVE)]
        self.driver.deploy_app.return_value = SimpleNamespace(engine_ref='new')
        self.driver.get_deployment_status.return_value = SimpleNamespace(
            status=states.ACTIVE, resolved_ref='a' * 40)
        self.assertEqual(states.ACTIVE, self.replace().status)
        self.assertEqual({'TOKEN': 'original-sensitive'}, engine_values)
        current = objects.Service.get_by_id(self.ctx, service.id)
        deployment = objects.Deployment.get_by_id(
            self.ctx, current.current_deployment_id)
        self.assertEqual(1, deployment.spec['env']['TOKEN']['version'])
        retained = objects.Secret.get_by_id(self.ctx, desired.id)
        self.assertEqual(2, retained.version)
        self.assertNotIn('sensitive', str(self.notifier.mock_calls))
        self.assert_retained()

    def test_known_image_digest_never_falls_back_to_mutable_tag(self):
        self.provision()
        service, previous = self.service(source_type='image')
        digest = 'sha256:' + 'a' * 64
        previous.resolved_ref = digest
        previous.save()
        self.driver.get_app_status.return_value = SimpleNamespace(
            status=states.STOPPED)
        self.driver.deploy_app.side_effect = (
            lambda ep, stack, app, deployment: v4.frozen_settings(
                app, deployment))
        self.assertEqual(states.ERROR, self.replace().status)
        failed = objects.DeploymentList.get_all(self.ctx, filters={
            'service_id': service.id, 'status': states.FAILED})
        self.assertEqual(1, len(failed))
        self.assertEqual(digest, failed[0].source_ref)
        self.assertIsNone(failed[0].engine_ref)
        self.assertEqual(previous.id, objects.Service.get_by_id(
            self.ctx, service.id).current_deployment_id)
        self.assert_retained()

    def test_stopped_intent_never_deploys(self):
        self.provision()
        self.service(status=states.STOPPED)
        self.driver.get_app_status.return_value = SimpleNamespace(
            status=states.STOPPED)
        self.assertEqual(states.ACTIVE, self.replace().status)
        self.driver.deploy_app.assert_not_called()

    def test_lost_enqueue_is_failed_once_and_old_guest_restored(self):
        self.provision()
        service, previous = self.service()
        self.driver.get_app_status.return_value = SimpleNamespace(
            status=states.STOPPED)
        self.driver.deploy_app.side_effect = RuntimeError('secret-sensitive')
        self.assertEqual(states.ERROR, self.replace().status)
        self.replace()
        self.assertEqual(1, self.driver.deploy_app.call_count)
        current = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(previous.id, current.current_deployment_id)
        self.assertEqual(states.NONE, current.task_state)
        failed = objects.DeploymentList.get_all(self.ctx, filters={
            'service_id': service.id, 'status': states.FAILED})
        self.assertEqual(1, len(failed))
        self.assertIn('unknown', failed[0].fault)
        self.assertEqual('ACTIVE', self.get_guest(self.old_id).status)
        self.assert_retained()

    def test_enqueue_checkpoint_rollback_releases_claims(self):
        self.provision()
        service, previous = self.service()
        self.driver.get_app_status.return_value = SimpleNamespace(
            status=states.STOPPED)
        with mock.patch.object(server_failover.deployments, 'prepare',
                               side_effect=RuntimeError('secret')):
            self.assertEqual(states.ERROR, self.replace().status)
        self.assertEqual(1, len(objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': service.id})))
        current = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(previous.id, current.current_deployment_id)
        self.assertEqual(states.NONE, current.task_state)
        self.assert_retained()

    def test_completion_rollback_uses_committed_deployment_state(self):
        self.provision()
        service, previous = self.service()
        self.driver.get_app_status.return_value = SimpleNamespace(
            status=states.STOPPED)
        self.driver.deploy_app.return_value = SimpleNamespace(engine_ref='new')
        self.driver.get_deployment_status.return_value = SimpleNamespace(
            status=states.ACTIVE, resolved_ref='a' * 40)
        original = server_failover.FailoverServer.save

        def fail(flow, resource, **values):
            if values.get('status') == states.SUPERSEDED:
                raise RuntimeError('secret')
            return original(flow, resource, **values)
        with mock.patch.object(server_failover.FailoverServer, 'save', fail):
            self.assertEqual(states.ERROR, self.replace().status)
        current = objects.Service.get_by_id(self.ctx, service.id)
        self.assertEqual(previous.id, current.current_deployment_id)
        self.assertEqual(states.NONE, current.task_state)
        failed = objects.DeploymentList.get_all(self.ctx, filters={
            'service_id': service.id, 'status': states.FAILED})
        self.assertEqual(1, len(failed))
        self.assertEqual('new', failed[0].engine_ref)
        self.assertEqual(states.ACTIVE, objects.Deployment.get_by_id(
            self.ctx, previous.id).status)
        self.assert_retained()

    def test_unknown_observation_fails_without_enqueue(self):
        self.provision()
        self.service()
        self.driver.get_app_status.return_value = SimpleNamespace(
            status=states.ERROR)
        self.assertEqual(states.ERROR, self.replace().status)
        self.driver.deploy_app.assert_not_called()
        self.assert_retained()

    def test_failed_health_restores_stopped_old_guest_and_original_cert(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.provision()
        old_cert = self.server.agent_cert_serial
        self.agent.wait_health.side_effect = RuntimeError('secret-sensitive')
        server = self.replace()
        self.assertEqual(states.ERROR, server.status)
        self.assertEqual(self.old_id, server.compute_id)
        self.assertEqual(old_cert, server.agent_cert_serial)
        self.assertEqual('ACTIVE', self.get_guest(self.old_id).status)
        self.assertEqual([self.old_id], list(self.cloud_resources['server']))
        self.assert_retained()
        self.driver.unregister_server.assert_not_called()
        self.assertIn('event=start', logs.output)
        self.assertIn('event=end', logs.output)
        self.assertIn('outcome=failure', logs.output)

    def test_build_failure_logs_start_and_end(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.provision(role='build')
        old_id = self.server.compute_id
        self.agent.wait_health.side_effect = RuntimeError('secret-sensitive')
        result = self.replace()
        lines = [line for line in logs.output.splitlines()
                 if 'Failover:' in line]
        self.assertEqual(2, len(lines))
        self.assertIn('event=start', lines[0])
        self.assertIn('event=end', lines[1])
        self.assertIn('outcome=failure', lines[1])
        self.assertIn('duration=', lines[1])
        self.assertIn('old_compute_id=%s' % old_id, lines[1])
        self.assertIn('new_compute_id=%s' % result.compute_id, lines[1])
        self.assertTrue(all('role=build' in line for line in lines))
        self.assertNotIn('secret-sensitive', logs.output)

    def test_fence_locks_before_moving_and_retirement_unlocks(self):
        self.provision()
        nova = self.proxies['nova']
        nova.reset_mock()
        self.assertEqual(states.ACTIVE, self.replace().status)
        methods = [call[0] for call in nova.mock_calls]
        self.assertLess(methods.index('lock_server'),
                        methods.index('delete_volume_attachment'))
        nova.lock_server.assert_called_once()
        reason = nova.lock_server.call_args.kwargs['locked_reason']
        self.assertIn('Coast failover', reason)
        self.assertIn(rpcapi.receipt_id(
            'failover_server', self.attempt, self.attempt_revision), reason)
        self.assertLess(methods.index('unlock_server'),
                        methods.index('delete_server'))

    def test_old_nova_falls_back_to_plain_lock(self):
        self.provision()
        nova = self.proxies['nova']
        attempts = []

        def lock(identifier, locked_reason=None):
            attempts.append(locked_reason)
            if locked_reason is not None:
                raise sdk_exceptions.HttpException('version', http_status=406)
            self.lock(identifier)

        nova.lock_server.side_effect = lock
        self.assertEqual(states.ACTIVE, self.replace().status)
        self.assertIn('Coast failover', attempts[0])
        self.assertEqual([None], attempts[1:])

    def test_repeated_fence_keeps_shutoff_guest_locked(self):
        self.provision()
        cloud = server_failover.worker.WorkerCloud(self.conf, self.stack)
        cloud.stopped(self.old_id, receipt='repeat')
        self.proxies['nova'].stop_server.reset_mock()
        cloud.stopped(self.old_id, receipt='repeat')
        self.assertTrue(self.get_guest(self.old_id).is_locked)
        self.proxies['nova'].stop_server.assert_not_called()

    def test_lock_failure_never_moves_or_boots(self):
        self.provision()
        nova = self.proxies['nova']
        nova.lock_server.side_effect = RuntimeError('lock')
        self.assertEqual(states.ERROR, self.replace().status)
        nova.lock_server.assert_called_once()
        nova.delete_volume_attachment.assert_not_called()
        nova.delete_server_interface.assert_not_called()
        self.assertFalse(any(step[0].startswith('detach')
                             for step in self.moves))
        self.assertEqual(1, self.proxies['nova'].create_server.call_count)

    def test_revert_unlocks_before_start(self):
        self.provision()
        nova = self.proxies['nova']
        self.agent.wait_health.side_effect = RuntimeError('health')
        nova.reset_mock()
        self.assertEqual(states.ERROR, self.replace().status)
        methods = [call[0] for call in nova.mock_calls]
        unlock = methods.index('unlock_server', methods.index('delete_server'))
        self.assertLess(unlock, methods.index('start_server'))
        self.assertFalse(self.get_guest(self.old_id).is_locked)

    def test_unlock_failure_keeps_original_stopped(self):
        self.provision()
        nova = self.proxies['nova']
        nova.delete_volume_attachment.side_effect = RuntimeError('detach')
        nova.unlock_server.side_effect = RuntimeError('unlock')
        self.assertEqual(states.ERROR, self.replace().status)
        nova.start_server.assert_not_called()
        self.assertEqual('SHUTOFF', self.get_guest(self.old_id).status)
        self.assertTrue(self.get_guest(self.old_id).is_locked)

    def test_fence_failure_never_detaches_or_boots(self):
        self.provision()
        self.proxies['nova'].stop_server.side_effect = RuntimeError('secret')
        server = self.replace()
        self.assertEqual(states.ERROR, server.status)
        self.assertEqual([('start', self.old_id)], self.moves)
        nova = self.proxies['nova']
        nova.unlock_server.assert_called_once_with(self.old_id)
        self.assertLess(nova.mock_calls.index(mock.call.unlock_server(
            self.old_id)), nova.mock_calls.index(mock.call.start_server(
                self.old_id)))
        self.assertFalse(self.get_guest(self.old_id).is_locked)
        self.assertEqual(self.old_id, server.compute_id)
        self.assert_retained()

    def test_failed_replacement_delete_keeps_old_vm_fenced(self):
        self.provision()
        self.agent.wait_health.side_effect = RuntimeError('secret')
        self.proxies['nova'].delete_server.side_effect = RuntimeError('secret')
        self.assertEqual(states.ERROR, self.replace().status)
        self.assertEqual('SHUTOFF', self.get_guest(self.old_id).status)
        self.assertEqual(2, len(self.cloud_resources['server']))
        self.assertTrue(self.get_guest(self.old_id).is_locked)
        self.assertFalse(any(step[0] == 'start' for step in self.moves))
        self.assert_retained()

    def test_lost_boot_reply_is_found_and_deleted_before_restore(self):
        self.provision()
        original = self.proxies['nova'].create_server.side_effect

        def lost(**values):
            original(**values)
            raise RuntimeError('secret')
        self.proxies['nova'].create_server.side_effect = lost
        self.assertEqual(states.ERROR, self.replace().status)
        self.assertEqual([self.old_id], list(self.cloud_resources['server']))
        self.assertEqual('ACTIVE', self.get_guest(self.old_id).status)
        self.assert_retained()

    def test_template_worker_skip_is_rechecked_in_conductor(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.provision()
        # The health manager dispatched before the template was added.
        failover.dispatch(self.ctx, self.server, False, self.conf)
        self.service(template='postgresql')
        self.assertEqual(states.UNHEALTHY, self.replace().status)
        for _ in range(3):
            # Fresh instances model service restarts; warnings live in DB.
            stale.Detector(self.conf).check(self.ctx, self.server.id)
            self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assertEqual(1, logs.output.count('reason=template_worker'))
        self.assertEqual([], self.moves)
        self.assert_retained()

    def test_controller_fresh_heartbeat_busy_stack_and_service_skip(self):
        logs = self.refusal_logs()
        self.provision()
        service, _ = self.service()
        for model, identifier, changes in [
                (api.stacks, self.stack.id, {'status': states.ERROR}),
                (api.servers, self.controller.id,
                 {'status': states.UNHEALTHY}),
                (api.servers, self.server.id,
                 {'last_heartbeat_at': timeutils.utcnow()})]:
            original = model.get(self.ctx, identifier)
            old = {key: getattr(original, key) for key in changes}
            with api.writer.using(self.ctx) as session:
                session.query(model.model).filter_by(id=identifier).update(
                    changes)
            self.attempt = objects.Server.get_by_id(self.ctx, self.server.id)
            self.attempt_revision = rpcapi.revision(self.attempt)
            self.replace()
            self.assertEqual([], self.moves)
            with api.writer.using(self.ctx) as session:
                session.query(model.model).filter_by(id=identifier).update(old)
        for reason in ('stack_unavailable_or_not_due',
                       'controller_unavailable'):
            self.assert_refused(logs, reason)
        service.acquire_task(states.DEPLOYING)
        self.attempt = objects.Server.get_by_id(self.ctx, self.server.id)
        self.attempt_revision = rpcapi.revision(self.attempt)
        self.replace()
        self.assertEqual([], self.moves)
        self.assert_refused(logs, 'stack_busy')

    def test_busy_stack_warns_once_in_conductor(self):
        logs = self.refusal_logs()
        self.provision()
        self.stack.acquire_task(states.RECONCILING)
        self.replace()
        self.replace()
        self.assert_refused(logs, 'stack_busy')
        self.assertEqual([], self.moves)

    def test_claim_conflict_logs_once_after_rollback(self):
        logs = self.refusal_logs()
        self.provision()
        with mock.patch.object(objects.Server, 'acquire_task',
                               side_effect=exception.Conflict()):
            for _ in range(2):
                self.assertRaises(exception.Conflict, self.replace)
        self.assert_refused(logs, 'claim_conflict')
        self.assertEqual([], self.moves)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def test_wrong_project_cannot_claim(self):
        self.provision()
        other = context.RequestContext(project_id='other')
        self.assertRaises(exception.NotFound, self.manager.execute, other,
                          'failover_server', self.attempt)
        self.assertEqual([], self.moves)

    def test_global_cap_counts_other_projects(self):
        logs = self.refusal_logs()
        self.provision()
        self.config.config(failover_max_concurrent=1, group='health_manager')
        other = context.RequestContext(project_id='other')
        stack = objects.Stack(context=other, name='other', plan='small',
                              driver='noop', engine_version='noop',
                              status=states.ACTIVE,
                              task_state=states.NONE)
        stack.create()
        server = objects.Server(context=other, stack_id=stack.id, name='busy',
                                role='build', kind='vm',
                                status=states.FAILOVER,
                                task_state=states.FAILING_OVER)
        server.create()
        self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assertEqual([], self.moves)
        self.assertEqual(states.UNHEALTHY, self.replace().status)
        self.assert_refused(logs, 'global_capacity')
        server.release_task(states.FAILING_OVER)
        self.assertEqual(states.ACTIVE, self.replace().status)
        self.assertIn('reason=global_capacity', logs.output)

    def test_restart_preserves_controller_and_emits_after_commit(self):
        self.provision()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        stack.acquire_task(states.FAILING_OVER)
        stack.conductor_host = self.conf.host
        stack.save()
        self.server.acquire_task(states.FAILING_OVER)
        self.server.status = states.FAILOVER
        self.server.save()
        self.controller.acquire_task(states.FAILING_OVER)
        self.notifier.info.side_effect = self.assert_no_transaction
        self.manager.recover_stacks()
        controller = objects.Server.get_by_id(self.ctx, self.controller.id)
        self.assertEqual(states.ACTIVE, controller.status)
        self.assertEqual(states.FAILING_OVER, controller.task_state)
        self.assertEqual('controller-nova', controller.compute_id)
        self.assertEqual(states.ERROR, objects.Server.get_by_id(
            self.ctx, self.server.id).status)
        self.assertEqual([], self.moves)

    def test_same_host_restart_releases_claims_without_cloud_calls(self):
        self.provision()
        service, _ = self.service()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        stack.acquire_task(states.FAILING_OVER)
        stack.conductor_host = self.conf.host
        stack.save()
        self.server.acquire_task(states.FAILING_OVER)
        self.server.status = states.FAILOVER
        self.server.save()
        service.acquire_task(states.FAILING_OVER)
        self.manager.recover_stacks()
        server = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ERROR, server.status)
        self.assertEqual(states.NONE, server.task_state)
        self.assertEqual(self.old_id, server.compute_id)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.assertEqual([], self.moves)


for _name in dir(test_server_flows.ServerFlowTest):
    if _name.startswith('test_') and _name not in WorkerFailoverTest.__dict__:
        setattr(WorkerFailoverTest, _name, None)
