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

from coast.common import context
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast.health_manager import keys
from coast.health_manager import service
from coast.health_manager import stale
from coast.tests import base


class StaleTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        base_models.Base.metadata.create_all(self.db.engine)
        self.owner = context.RequestContext(project_id='tenant')
        self.stack = api.stacks.create(self.owner, dict(
            name='stack', plan='test', driver='noop', engine_version='test',
            status='ACTIVE', task_state='NONE'))
        self.now = datetime.datetime(2026, 10, 13)
        self.elapsed = 0
        self.detector = stale.Detector(
            self.conf, clock=lambda: self.now, monotonic=lambda: self.elapsed)
        self.elapsed = 61
        self.events = []
        self.useFixture(fixtures.MonkeyPatch(
            'coast.common.notifications.emit',
            lambda ctx, service, resource, action, phase, payload, **kw:
            self.events.append((ctx.project_id, resource, phase, payload))))

    def test_secret_outage_without_key_defers_stale_and_recovers(self):
        server = self.server()
        lookup = mock.Mock(side_effect=keys.KeyUnavailable())
        self.detector.key_lookup = lookup
        self.detector.run()
        self.assertEqual('ACTIVE', api.servers.get(
            self.owner, server.id).status)
        self.assertEqual([], self.events)
        lookup.side_effect = None
        self.detector.run()
        self.assertEqual('UNHEALTHY', api.servers.get(
            self.owner, server.id).status)

    def server(self, role='worker', **changes):
        values = dict(stack_id=self.stack.id, name=role, kind='vm', role=role,
                      status='ACTIVE', task_state='NONE',
                      last_heartbeat_at=self.now - datetime.timedelta(
                          seconds=61), created_at=self.now)
        values.update(changes)
        timestamps = {key: values.pop(key) for key in
                      ('created_at', 'deleted_at') if key in values}
        server = api.servers.create(self.owner, values)
        self.set_row(models.Server, server.id, timestamps)
        return server

    def set_row(self, model, row_id, values):
        with api.writer.using(self.owner) as session:
            session.query(model).filter_by(
                id=row_id, project_id=self.owner.project_id).update(values)

    def status(self, server):
        return api.servers.get(self.owner, server.id).status

    def test_all_roles_stale_recover_and_notifications_are_idempotent(self):
        for role in ('control', 'worker', 'build'):
            server = self.server(role)
            self.detector.run()
            self.assertEqual('UNHEALTHY', self.status(server))
            self.assertEqual('DEGRADED', api.stacks.get(
                self.owner, self.stack.id).status)
            count = len(self.events)
            self.detector.run()
            self.assertEqual(count, len(self.events))
            api.servers.update(self.owner, server.id,
                               {'last_heartbeat_at': self.now})
            self.detector.run()
            self.assertEqual('ACTIVE', self.status(server))
            self.assertEqual('ACTIVE', api.stacks.get(
                self.owner, self.stack.id).status)
        self.assertEqual(24, len(self.events))
        self.assertTrue(all(e[0] == 'tenant' for e in self.events))
        self.assertEqual({'server', 'environment'},
                         {e[1] for e in self.events})

    def test_strict_timeout_and_missing_heartbeat_use_creation_time(self):
        boundary = self.server(last_heartbeat_at=self.now -
                               datetime.timedelta(seconds=60))
        new = self.server(last_heartbeat_at=None)
        old = self.server(last_heartbeat_at=None, created_at=self.now -
                          datetime.timedelta(seconds=61))
        self.detector.run()
        self.assertEqual('ACTIVE', self.status(boundary))
        self.assertEqual('ACTIVE', self.status(new))
        self.assertEqual('UNHEALTHY', self.status(old))
        self.now += datetime.timedelta(seconds=1)
        self.detector.run()
        self.assertEqual('UNHEALTHY', self.status(boundary))

    def test_startup_grace(self):
        server = self.server()
        self.elapsed = 60
        self.detector.run()
        self.assertEqual('ACTIVE', self.status(server))
        self.elapsed = 61
        self.detector.run()
        self.assertEqual('UNHEALTHY', self.status(server))

    def test_skip_transitional_stacks_and_busy_servers(self):
        server = self.server()
        for status, task in [('BUILDING', 'NONE'), ('UPGRADING', 'NONE'),
                             ('ERROR', 'NONE'), ('DELETING', 'NONE'),
                             ('ACTIVE', 'RECONCILING'),
                             ('DEGRADED', 'DELETING')]:
            self.set_row(models.Stack, self.stack.id,
                         {'status': status, 'task_state': task})
            self.detector.run()
            # Recheck protects against a task acquired after the scan.
            self.detector.check(self.owner, server.id)
            self.assertEqual('ACTIVE', self.status(server))
        self.set_row(models.Stack, self.stack.id,
                     {'status': 'ACTIVE', 'task_state': 'NONE'})
        self.set_row(models.Server, server.id,
                     {'task_state': 'PROVISIONING'})
        self.detector.run()
        self.assertEqual('ACTIVE', self.status(server))
        self.assertEqual([], self.events)

    def test_deleted_resources_are_excluded(self):
        server = self.server(deleted_at=self.now)
        self.detector.run()
        self.assertEqual('ACTIVE', api.servers.get(
            self.owner, server.id, show_deleted=True).status)
        live = self.server()
        self.set_row(models.Stack, self.stack.id,
                     {'deleted_at': self.now})
        self.detector.run()
        self.assertEqual('ACTIVE', self.status(live))
        self.assertEqual([], self.events)

    def test_recovery_waits_for_all_peers_and_preserves_unrelated_fault(self):
        one, two = self.server(), self.server('control')
        self.detector.run()
        api.servers.update(self.owner, one.id, {'last_heartbeat_at': self.now})
        self.detector.run()
        self.assertEqual('DEGRADED', api.stacks.get(
            self.owner, self.stack.id).status)
        api.stacks.update(self.owner, self.stack.id,
                          {'fault': 'Other failure'})
        api.servers.update(self.owner, two.id, {'last_heartbeat_at': self.now})
        self.detector.run()
        stack = api.stacks.get(self.owner, self.stack.id)
        self.assertEqual('DEGRADED', stack.status)
        self.assertEqual('Other failure', stack.fault)

    def test_bounded_cursor_revisits_without_starvation(self):
        servers = [self.server() for _ in range(3)]
        with mock.patch.object(stale, 'BATCH_SIZE', 2):
            self.detector.run()
            self.assertEqual(2, sum(self.status(s) == 'UNHEALTHY'
                                    for s in servers))
            self.detector.run()
            self.assertTrue(all(self.status(s) == 'UNHEALTHY'
                                for s in servers))
            for server in servers:
                api.servers.update(self.owner, server.id,
                                   {'last_heartbeat_at': self.now})
            self.detector.run()
            self.detector.run()
            self.assertTrue(all(self.status(s) == 'ACTIVE' for s in servers))

    def test_failed_item_does_not_block_later_rows_or_leak_errors(self):
        self.server()
        self.server()
        logs = self.useFixture(fixtures.FakeLogger())
        with mock.patch.object(self.detector, 'check',
                               side_effect=[RuntimeError('secret'), None]):
            self.detector.run()
        self.assertNotIn('secret', logs.output)
        self.assertIn('will retry', logs.output)

    def test_cross_project_scan_uses_recorded_owner(self):
        other = context.RequestContext(project_id='other')
        stack = api.stacks.create(other, dict(
            name='other', plan='test', driver='noop', engine_version='test',
            status='ACTIVE', task_state='NONE'))
        server = api.servers.create(other, dict(
            stack_id=stack.id, name='worker', kind='vm', role='worker',
            status='ACTIVE', task_state='NONE', last_heartbeat_at=self.now -
            datetime.timedelta(seconds=61)))
        self.detector.run()
        self.assertEqual('UNHEALTHY', api.servers.get(other, server.id).status)
        self.assertTrue(all(e[0] == 'other' for e in self.events))

    def test_service_periodic_path_changes_status(self):
        server = self.server()
        instance = service.Service(self.conf)
        instance.detector = self.detector
        instance._check_health()
        self.assertEqual('UNHEALTHY', self.status(server))
        self.assertEqual('DEGRADED', api.stacks.get(
            self.owner, self.stack.id).status)

    def test_non_health_server_statuses_are_preserved(self):
        for status in ('PENDING', 'BUILDING', 'FAILOVER', 'ERROR', 'DELETING'):
            server = self.server(status=status)
            self.detector.run()
            self.detector.check(self.owner, server.id)
            self.assertEqual(status, self.status(server))
        self.assertEqual([], self.events)

    def test_consecutive_managers_do_not_repeat_status_notifications(self):
        server = self.server()
        other = stale.Detector(self.conf, clock=lambda: self.now)
        self.detector.check(self.owner, server.id)
        other.check(self.owner, server.id)
        self.assertEqual('UNHEALTHY', self.status(server))
        self.assertEqual(4, len(self.events))
