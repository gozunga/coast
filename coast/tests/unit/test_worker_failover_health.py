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

import datetime
from unittest import mock

import fixtures

from oslo_db import exception as db_exception

from coast.common import states
from coast.db import api
from coast.health_manager import failover
from coast.health_manager import stale
from coast import objects
from coast.tests.unit import test_stale_detection


class FailoverHealthTest(test_stale_detection.StaleTest):
    def setUp(self):
        super().setUp()
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        api.stacks.update(self.owner, self.stack.id, {'driver': 'coolify_v4'})
        self.rpc = mock.Mock()
        patcher = mock.patch.object(
            failover.rpcapi, 'ConductorAPI', return_value=self.rpc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_log_unhealthy_and_dispatch_fields(self):
        logs = self.useFixture(fixtures.FakeLogger())
        server = self.server(compute_id='old-vm')
        self.detector.run()
        self.detector.run()
        self.assertEqual(1, logs.output.count('event=unhealthy'))
        self.pass_grace()
        self.assertIn(
            'Failover: stack_id=%s server_id=%s event=dispatch '
            'reason=grace_expired' % (self.stack.id, server.id), logs.output)
        for field in ('stack_id', 'server_id', 'server_name', 'role',
                      'compute_id', 'last_heartbeat_at', 'reason'):
            self.assertIn(field + '=', logs.output)
        self.assertIn('reason=heartbeat_timeout', logs.output)
        self.assertIn('reason=grace_expired', logs.output)
        self.assertIn(server.id, logs.output)

    def test_names_are_masked_and_kept_on_one_line(self):
        logs = self.useFixture(fixtures.FakeLogger())
        server = self.server(name='password=private\nforged')
        self.detector.run()
        self.pass_grace()
        self.assertNotIn('private', logs.output)
        lines = [line for line in logs.output.splitlines()
                 if 'Failover:' in line]
        self.assertEqual(2, len(lines))
        self.assertTrue(all('server_name=' in line for line in lines))
        self.assertIn(server.id, logs.output)

    def test_template_warning_once_per_episode(self):
        logs = self.useFixture(fixtures.FakeLogger())
        server = self.server()
        self.service(server, 'database')
        self.detector.run()
        self.pass_grace()
        self.pass_grace()
        self.assertEqual(1, logs.output.count('reason=template_worker'))
        api.servers.update(self.owner, server.id,
                           {'last_heartbeat_at': self.now})
        self.detector.run()
        self.now += datetime.timedelta(seconds=61)
        self.detector.run()
        self.pass_grace()
        self.assertEqual(2, logs.output.count('reason=template_worker'))

    def test_warning_helper_deduplicates_reasons_across_restarts(self):
        logs = self.useFixture(fixtures.FakeLogger())
        server = self.server()
        self.detector.run()
        self.pass_grace()
        reasons = (
            'stale_request', 'not_due', 'image_unavailable',
            'global_capacity', 'stack_busy', 'server_unavailable',
            'services_busy', 'stack_unavailable_or_not_due',
            'template_worker', 'controller_unavailable', 'service_limit',
            'bare_metal', 'claim_conflict')
        # Test helper persistence; conductor tests drive admission. A fresh
        # context/logger has no process-local memory after conductor restart.
        for reason in reasons:
            failover.warn_once(self.owner, server, failover.LOG, reason)
        self.service(server, 'database')
        for _ in range(3):
            self.pass_grace()
        api.servers.update(self.owner, server.id, {'kind': 'baremetal'})
        self.pass_grace()
        self.set_row(stale.models.Stack, self.stack.id,
                     {'task_state': states.RECONCILING})
        self.pass_grace()
        owner = stale.context.RequestContext(project_id=self.owner.project_id)
        restarted = objects.Server.get_by_id(owner, server.id)
        for reason in reasons:
            failover.warn_once(owner, restarted, stale.LOG, reason)
            self.assertEqual(1, logs.output.count('reason=' + reason))
        # Recovery clears every reason for the next unhealthy episode.
        self.set_row(stale.models.Stack, self.stack.id,
                     {'task_state': states.NONE})
        api.servers.update(self.owner, server.id,
                           {'kind': 'vm', 'last_heartbeat_at': self.now})
        self.detector.run()
        self.now += datetime.timedelta(seconds=61)
        self.detector.run()
        for reason in reasons:
            failover.warn_once(owner, restarted, stale.LOG, reason)
            self.assertEqual(2, logs.output.count('reason=' + reason))

    def test_busy_stack_warning_survives_restart(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.server()
        self.detector.run()
        self.set_row(stale.models.Stack, self.stack.id,
                     {'task_state': states.RECONCILING})
        self.pass_grace()
        self.detector = stale.Detector(
            self.conf, clock=lambda: self.now, monotonic=lambda: self.elapsed)
        self.elapsed += 61
        self.pass_grace()
        self.assertEqual(1, logs.output.count('reason=stack_busy'))
        self.rpc.cast.assert_not_called()

    def test_upgrading_stack_logs_refusal_without_health_changes(self):
        logs = self.useFixture(fixtures.FakeLogger())
        server = self.server()
        self.detector.run()
        self.set_row(stale.models.Stack, self.stack.id,
                     {'status': states.UPGRADING,
                      'task_state': states.UPGRADING})
        self.pass_grace()
        self.pass_grace()
        self.assertEqual(1, logs.output.count('reason=stack_busy'))
        self.assertIn('server_id=%s event=refused' % server.id, logs.output)
        self.assertEqual(states.UNHEALTHY, self.status(server))
        self.assertEqual(states.UPGRADING,
                         api.stacks.get(self.owner, self.stack.id).status)
        self.rpc.cast.assert_not_called()

    def test_baremetal_warning_once(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.server('control', kind='baremetal')
        self.detector.run()
        self.pass_grace()
        self.pass_grace()
        self.assertEqual(1, logs.output.count('reason=bare_metal'))

    def test_unconfigured_database_logs_refusal_and_propagates_failure(self):
        server = self.server()
        logs = self.useFixture(fixtures.FakeLogger())
        with mock.patch.object(api.writer, 'using', side_effect=(
                db_exception.CantStartEngineError())):
            self.assertRaises(db_exception.CantStartEngineError,
                              failover.dispatch, self.owner, server, True,
                              self.conf)
        self.assertIn('event=refused reason=template_worker', logs.output)
        self.assertIn('deduplication=unavailable', logs.output)

    def test_dispatch_logs_at_info_and_refusal_at_warning(self):
        server = self.server()
        with mock.patch.object(failover.LOG, 'info') as info:
            failover.dispatch(self.owner, server, False, self.conf)
        self.assertEqual(1, info.call_count)
        self.assertIn('event=dispatch reason=grace_expired',
                      info.call_args.args[1])
        with mock.patch.object(failover.LOG, 'warning') as warning:
            failover.dispatch(self.owner, server, True, self.conf)
        self.assertEqual(1, warning.call_count)
        self.assertIn(
            'Failover: stack_id=%s server_id=%s event=refused '
            'reason=template_worker' % (self.stack.id, server.id),
            warning.call_args.args[0] % warning.call_args.args[1:])

    def test_pasted_compose_keeps_template_worker_failover_guard(self):
        server = self.server()
        service = self.service(server)
        with api.reader.using(self.owner):
            self.assertIsNone(failover.templates(self.owner, server))
        source = objects.Source.get_by_id(self.owner, service.source_id)
        source.type = 'compose'
        source.save()
        with api.reader.using(self.owner):
            self.assertIsNotNone(failover.templates(self.owner, server))

    def pass_grace(self):
        self.now += datetime.timedelta(seconds=121)
        self.detector.run()

    def service(self, server, template=None):
        source = objects.Source(context=self.owner, type='image')
        source.create()
        service = objects.Service(
            context=self.owner, stack_id=self.stack.id, server_id=server.id,
            source_id=source.id, name='service', template_id=template,
            ports='8080', replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE)
        service.create()
        return service

    def test_grace_starts_at_unhealthy_and_cast_is_after_commit(self):
        server = self.server()
        self.detector.run()
        self.rpc.cast.assert_not_called()
        self.now += datetime.timedelta(seconds=120)
        self.detector.run()
        self.rpc.cast.assert_not_called()

        def cast(ctx, operation, resource):
            self.assertRaises(AttributeError, lambda: ctx.session)
            self.assertEqual('tenant', ctx.project_id)
            self.assertEqual('failover_server', operation)
            self.assertEqual(server.id, resource.id)
            self.assertEqual(states.UNHEALTHY, resource.status)
            self.assertEqual(states.NONE, resource.task_state)
        self.rpc.cast.side_effect = cast
        self.now += datetime.timedelta(seconds=1)
        self.detector.run()
        self.assertEqual(1, self.rpc.cast.call_count)

    def test_controller_vm_is_cast_after_grace(self):
        server = self.server('control')
        self.detector.run()
        self.pass_grace()
        cast_server = self.rpc.cast.call_args.args[2]
        self.assertEqual(server.id, cast_server.id)
        self.assertEqual(states.UNHEALTHY, cast_server.status)

    def test_baremetal_worker_is_not_cast(self):
        server = self.server(kind='baremetal')
        self.detector.run()
        self.pass_grace()
        self.rpc.cast.assert_not_called()
        self.assertEqual(states.UNHEALTHY, self.status(server))

    def test_baremetal_controller_becomes_error_once(self):
        server = self.server('control', kind='baremetal')
        self.detector.run()
        self.pass_grace()
        self.rpc.cast.assert_not_called()
        self.assertEqual(states.ERROR, self.status(server))
        self.assertEqual(failover.BAREMETAL_FAULT, api.servers.get(
            self.owner, server.id).fault)
        events = objects.EventList.get_all(self.owner, filters={
            'resource_id': server.id, 'action': 'failover'})
        self.assertEqual(1, len(events))
        self.assertEqual(failover.BAREMETAL_FAULT, events[0].message)
        self.detector.check(self.owner, server.id)
        self.assertEqual(1, len(objects.EventList.get_all(self.owner, filters={
            'resource_id': server.id, 'action': 'failover'})))
        self.rpc.cast.assert_not_called()

    def test_disabled_failover_does_not_replace_a_controller(self):
        self.config.config(enable_failover=False, group='health_manager')
        server = self.server('control')
        self.detector.run()
        self.pass_grace()
        self.rpc.cast.assert_not_called()
        self.assertEqual(states.UNHEALTHY, self.status(server))
        # A stack has one control server. Check bare metal on that row.
        api.servers.update(self.owner, server.id, {'kind': 'baremetal'})
        self.pass_grace()
        self.rpc.cast.assert_not_called()
        self.assertEqual(states.UNHEALTHY, self.status(server))

    def test_noop_controller_is_not_rebuilt_or_failed(self):
        api.stacks.update(self.owner, self.stack.id, {'driver': 'noop'})
        server = self.server('control')
        self.detector.run()
        self.pass_grace()
        self.rpc.cast.assert_not_called()
        self.assertEqual(states.UNHEALTHY, self.status(server))
        api.servers.update(self.owner, server.id, {'kind': 'baremetal'})
        self.pass_grace()
        self.rpc.cast.assert_not_called()
        self.assertEqual(states.UNHEALTHY, self.status(server))

    def test_build_vm_is_cast(self):
        server = self.server('build')
        self.detector.run()
        self.pass_grace()
        self.assertEqual(server.id, self.rpc.cast.call_args.args[2].id)

    def test_disabled_failover_still_detects_and_recovers(self):
        self.config.config(enable_failover=False, group='health_manager')
        server = self.server()
        self.detector.run()
        self.pass_grace()
        self.rpc.cast.assert_not_called()
        self.assertEqual(states.UNHEALTHY, self.status(server))
        api.servers.update(self.owner, server.id,
                           {'last_heartbeat_at': self.now})
        self.detector.run()
        self.assertEqual(states.ACTIVE, self.status(server))

    def test_fresh_heartbeat_and_new_episode_get_full_grace(self):
        server = self.server()
        self.detector.run()
        self.now += datetime.timedelta(seconds=121)
        api.servers.update(self.owner, server.id,
                           {'last_heartbeat_at': self.now})
        self.detector.run()
        self.rpc.cast.assert_not_called()
        self.now += datetime.timedelta(seconds=61)
        self.detector.run()
        self.rpc.cast.assert_not_called()
        self.pass_grace()
        self.assertEqual(1, self.rpc.cast.call_count)

    def test_active_completion_restarts_grace_without_fresh_heartbeat(self):
        server = self.server()
        self.detector.run()
        self.pass_grace()
        self.assertEqual(1, self.rpc.cast.call_count)
        # A conductor can return ACTIVE after authenticated guest health,
        # before a new UDP heartbeat arrives. A later stale episode must
        # never reuse the previous grace period.
        api.servers.update(self.owner, server.id, {'status': states.ACTIVE})
        self.now += datetime.timedelta(seconds=1)
        self.detector.run()
        self.assertEqual(states.UNHEALTHY, self.status(server))
        self.assertEqual(1, self.rpc.cast.call_count)
        self.pass_grace()
        self.assertEqual(2, self.rpc.cast.call_count)

    def test_template_event_once_across_sweeps_and_restart(self):
        server = self.server()
        self.service(server, 'postgresql')
        self.detector.run()
        self.pass_grace()
        self.detector = stale.Detector(
            self.conf, clock=lambda: self.now, monotonic=lambda: self.elapsed)
        self.elapsed += 61
        count = len(self.events)
        for _ in range(3):
            self.pass_grace()
        self.assertEqual(count, len(self.events))
        self.assertEqual(states.UNHEALTHY, self.status(server))
        events = objects.EventList.get_all(self.owner, filters={
            'resource_id': server.id, 'action': 'failover'})
        self.assertEqual(1, len(events))
        self.assertEqual(failover.TEMPLATE_FAULT, events[0].message)
        self.rpc.cast.assert_not_called()
        api.servers.update(self.owner, server.id,
                           {'last_heartbeat_at': self.now})
        self.detector.run()
        self.assertEqual(states.ACTIVE, self.status(server))

    def test_appgroup_services_do_not_block_cast(self):
        server = self.server()
        self.service(server)
        self.detector.run()
        self.pass_grace()
        self.assertEqual(1, self.rpc.cast.call_count)

    def test_failed_cast_does_not_claim_capacity_and_can_retry(self):
        server = self.server()
        self.detector.run()
        self.rpc.cast.side_effect = RuntimeError('secret')
        self.pass_grace()
        self.assertEqual(states.NONE,
                         api.servers.get(self.owner, server.id).task_state)
        self.rpc.cast.side_effect = None
        self.detector.run()
        self.assertEqual(2, self.rpc.cast.call_count)


for _name in dir(test_stale_detection.StaleTest):
    if _name.startswith('test_') and _name not in FailoverHealthTest.__dict__:
        setattr(FailoverHealthTest, _name, None)
