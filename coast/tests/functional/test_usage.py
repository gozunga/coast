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

"""Current-resource snapshots through the periodic task and fake notifier."""

import datetime
from unittest import mock

from coast.common import context
from coast.common import notifications
from coast.conductor import service
from coast.conductor import usage
from coast import objects
from coast.tests import conductor


class UsageTest(conductor.ConductorTest):
    def test_current_snapshot_and_period_suppression(self):
        path = self.stack()
        worker = next(row for row in objects.ServerList.get_all(self.ctx)
                      if row.role == 'worker')
        self.request('post', path + '/volumes', {'volume': {
            'name': 'extra', 'size_gb': 5, 'server_id': worker.id}},
            status=202, complete=True)
        service = self.request('post', '/v1/services', {'service': {
            **self.service_parents(worker.stack_id),
            'name': 'web', 'server_id': worker.id,
            'source': {'type': 'git',
                       'repo_url': 'https://example.com/web.git'},
            'cpu_limit': 0.5, 'memory_limit_mb': 128}},
            status=202, complete=True).json['service']
        self.config.config(service_id='service-id', group='notifications')
        notifier = self.manager.notifier
        notifier.events.clear()
        tasks = usage.UsageTasks(self.conf, notifier)
        now = datetime.datetime(2026, 10, 10, 12, 30)
        with mock.patch.object(usage.timeutils, 'utcnow', return_value=now), \
                mock.patch.object(usage, 'BATCH_SIZE', 1):
            tasks.run_periodic_tasks(None)
            first = list(notifier.events)
            tasks.audit(None)
        self.assertEqual(first, notifier.events)
        counts = {name: sum(event == 'coast.%s.exists' % name
                            for event, body in first)
                  for name in usage.RESOURCES}
        self.assertEqual(
            {'environment': 1, 'server': 2, 'volume': 2, 'app': 1}, counts)
        for event, body in first:
            self.assertEqual('project', body['project_id'])
            self.assertEqual('2026-10-10T11:00:00Z',
                             body['audit_period_beginning'])
            self.assertEqual('2026-10-10T12:00:00Z',
                             body['audit_period_ending'])
            self.assertEqual('service-id', body['service_id'])
            self.assertTrue(body['created_at'].endswith('Z'))
            self.assertNotIn('user_id', body)
            self.assertNotIn('name', body)
            self.assertLessEqual(set(body), set(notifications.USAGE_ALLOWED))
        service_payload = next(body for event, body in first
                               if event == 'coast.app.exists')
        self.assertEqual(service['id'], service_payload['resource_id'])
        self.assertEqual(0.5, service_payload['cpu_limit'])
        self.assertEqual(128, service_payload['memory_limit_mb'])
        plans = [body['plan'] for event, body in first
                 if event == 'coast.environment.exists']
        sizes = [body['size_gb'] for event, body in first
                 if event == 'coast.volume.exists']
        self.assertEqual(['small'], plans)
        self.assertEqual([5, 10], sorted(sizes))
        self.request('delete', '/v1/services/' + service['id'], status=202,
                     complete=True)
        self.request('delete', '/v1/environments/' + service['environment_id'],
                     status=202, complete=True)
        self.request('delete', '/v1/appgroups/' + service['appgroup_id'],
                     status=202, complete=True)
        self.request('delete', path, status=202, complete=True)
        notifier.events.clear()
        later = now + datetime.timedelta(hours=1)
        with mock.patch.object(usage.timeutils, 'utcnow', return_value=later):
            tasks.audit(None)
        self.assertEqual([], notifier.events)

    def test_projects_restart_and_service_lifecycle(self):
        path = self.stack()
        env = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        env.engine_token_ref = 'secret-engine-ref'
        env.ssh_key_ref = 'secret-ssh-ref'
        env.save()
        other = context.RequestContext(project_id='other')
        foreign = objects.Stack(
            context=other, name='foreign', driver='noop',
            engine_version='noop', plan='large', status='ACTIVE')
        foreign.create()
        self.stop_server()
        self.manager.notifier.events.clear()
        self.config.config(exists_notification_interval=123,
                           group='notifications')
        worker = service.Service(self.conf, endpoint=self.manager)
        worker.start()
        try:
            events = list(self.manager.notifier.events)
            stacks = [body for event, body in events
                      if event == 'coast.environment.exists']
            self.assertEqual({'project', 'other'},
                             {body['project_id'] for body in stacks})
            self.assertEqual({env.id, foreign.id},
                             {body['resource_id'] for body in stacks})
            self.assertNotIn('secret-', str(events))
            # A fresh task intentionally repeats the latest period; no
            # durable checkpoint or replica election is introduced.
            usage.UsageTasks(self.conf, self.manager.notifier).audit(None)
            self.assertEqual(2 * len(events),
                             len(self.manager.notifier.events))
        finally:
            worker.stop()
        # Closing the service also stops the usage timer.
        self.assertFalse(worker.tg.timers)
