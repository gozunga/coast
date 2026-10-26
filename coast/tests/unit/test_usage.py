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

"""Usage audit bounds, payload boundaries, and periodic service wiring."""

import datetime
from unittest import mock

import fixtures
from oslo_messaging.notify import _impl_test as test_driver

from coast.common import context
from coast.common import notifications
from coast.conductor import usage
from coast.db import base_models
from coast.db import models
from coast.tests import base
from coast.tests import conductor


class UsageTest(base.TestCase):
    def test_calendar_bounds_and_timezone(self):
        now = datetime.datetime(2024, 3, 1, 12, 34, 56)
        cases = (
            ('hour', datetime.datetime(2024, 3, 1, 11),
             datetime.datetime(2024, 3, 1, 12)),
            ('day', datetime.datetime(2024, 2, 29),
             datetime.datetime(2024, 3, 1)),
            ('month', datetime.datetime(2024, 2, 1),
             datetime.datetime(2024, 3, 1)),
        )
        for period, begin, end in cases:
            self.assertEqual((begin, end), usage.audit_bounds(now, period))
        january = datetime.datetime(2025, 1, 15)
        self.assertEqual((datetime.datetime(2024, 12, 1),
                          datetime.datetime(2025, 1, 1)),
                         usage.audit_bounds(january, 'month'))
        aware = now.replace(tzinfo=datetime.timezone(
            datetime.timedelta(hours=2)))
        self.assertEqual((datetime.datetime(2024, 3, 1, 9),
                          datetime.datetime(2024, 3, 1, 10)),
                         usage.audit_bounds(aware, 'hour'))

    def test_exact_boundaries_match_nova(self):
        for period, now, begin, end in (
                ('hour', datetime.datetime(2024, 3, 1, 12),
                 datetime.datetime(2024, 3, 1, 10),
                 datetime.datetime(2024, 3, 1, 11)),
                ('day', datetime.datetime(2024, 3, 1),
                 datetime.datetime(2024, 2, 28),
                 datetime.datetime(2024, 2, 29)),
                ('month', datetime.datetime(2025, 1, 1),
                 datetime.datetime(2024, 11, 1),
                 datetime.datetime(2024, 12, 1))):
            self.assertEqual((begin, end), usage.audit_bounds(now, period))

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):
            usage.audit_bounds(datetime.datetime(2024, 1, 1), 'week')
        with self.assertRaises(ValueError):
            self.conf.set_override('usage_audit_period', 'week',
                                   group='notifications')
        with self.assertRaises(ValueError):
            self.conf.set_override('exists_notification_interval', 0,
                                   group='notifications')

    def test_usage_allow_list_does_not_expand_lifecycle(self):
        test_driver.reset()
        self.addCleanup(test_driver.reset)
        payload = dict(resource_id='id', project_id='project', plan='small',
                       flavor_id='flavor', size_gb=0, cpu_limit=0.5,
                       memory_limit_mb=128, replicas=1, user_id='user',
                       name='secret-name', engine_token_ref='secret-ref',
                       auth_token='secret-token', spec={'password': 'secret'},
                       service_id=None, audit_period_beginning='begin',
                       audit_period_ending='end')
        notifications.exists(None, 'volume', payload, self.conf)
        message = test_driver.NOTIFICATIONS[-1][1]
        self.assertEqual('coast.volume.exists', message['event_type'])
        clean = message['payload']
        self.assertEqual(0, clean['size_gb'])
        self.assertEqual(0.5, clean['cpu_limit'])
        self.assertNotIn('user_id', clean)
        self.assertNotIn('service_id', clean)
        self.assertNotIn('secret', str(message))
        self.assertEqual({'resource_id': 'id', 'project_id': 'project',
                          'user_id': 'user'}, notifications.sanitize(payload))
        self.assertEqual({}, notifications.sanitize(
            {'plan': {'password': 'secret'}}, notifications.USAGE_ALLOWED))

    def test_delivery_failure_is_sanitized(self):
        logs = self.useFixture(fixtures.FakeLogger())
        notifier = mock.Mock()
        notifier.info.side_effect = RuntimeError('secret-password')
        notifications.exists(None, 'server', {'resource_id': 'id'},
                             self.conf, notifier)
        self.assertIn('coast.server.exists', logs.output)
        self.assertNotIn('secret-password', logs.output)

    def test_noop_disables_usage_delivery(self):
        test_driver.reset()
        self.addCleanup(test_driver.reset)
        self.config.config(driver=['noop'],
                           group='oslo_messaging_notifications')
        notifications.exists(None, 'server', {'resource_id': 'id'}, self.conf)
        self.assertEqual([], test_driver.NOTIFICATIONS)

    def test_sweep_failure_is_sanitized_and_remains_retryable(self):
        logs = self.useFixture(fixtures.FakeLogger())
        tasks = usage.UsageTasks(self.conf)
        with mock.patch.object(usage.db_api.reader, 'using',
                               side_effect=RuntimeError('db-password')):
            tasks.audit(None)
        self.assertIsNone(tasks.last_period)
        self.assertNotIn('db-password', logs.output)
        self.assertIn('Usage audit sweep failed', logs.output)

    def test_snapshot_query_pages_projects_and_excludes_deleted_rows(self):
        self.useFixture(fixtures.MonkeyPatch(
            'coast.db.api.reader', self.db.facade.reader))
        base_models.Base.metadata.create_all(self.db.engine)
        ctx = context.RequestContext(is_admin=True)
        now = datetime.datetime(2026, 10, 10, 12, 30)
        with self.db.facade.writer.using(ctx):
            for id, project, deleted in (('a', 'one', None),
                                         ('b', 'two', None),
                                         ('c', 'one', now)):
                ctx.session.add(models.Stack(
                    id=id, project_id=project, name='private-name',
                    driver='noop', engine_version='noop', plan='small',
                    status='ACTIVE', created_at=now, deleted_at=deleted,
                    engine_token_ref='secret-ref'))
        notifier = conductor.RecordingNotifier()
        tasks = usage.UsageTasks(self.conf, notifier)
        with mock.patch.object(usage.timeutils, 'utcnow', return_value=now), \
                mock.patch.object(usage, 'BATCH_SIZE', 1):
            tasks.audit(None)
            tasks.audit(None)
        self.assertEqual(2, len(notifier.events))
        projects = {body['project_id'] for event, body in notifier.events}
        resources = {body['resource_id'] for event, body in notifier.events}
        self.assertEqual({'one', 'two'}, projects)
        self.assertEqual({'a', 'b'}, resources)
        for event, body in notifier.events:
            self.assertEqual('coast.environment.exists', event)
            self.assertNotIn('service_id', body)
        self.assertNotIn('secret-ref', str(notifier.events))
        self.assertNotIn('private-name', str(notifier.events))
