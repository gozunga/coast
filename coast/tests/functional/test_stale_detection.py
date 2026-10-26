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

"""API-created stack health through signed heartbeat ingestion and recovery."""

import datetime
from unittest import mock

import fixtures

from coast.agent import heartbeat
from coast.common import notifications
from coast.db import api
from coast.health_manager import receiver
from coast.health_manager import service
from coast.tests import conductor


class StaleDetectionTest(conductor.ConductorTest):
    def test_api_stack_stale_then_signed_heartbeat_recovers(self):
        path = self.stack()
        stack_id = path.rsplit('/', 1)[-1]
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        epoch = now.replace(tzinfo=datetime.timezone.utc).timestamp()
        servers = api.servers.list(self.ctx, filters={'stack_id': stack_id})
        key = b'k' * 32
        directory = self.useFixture(fixtures.TempDir()).path
        self.config.config(state_path=directory, group='health_manager')
        listener = receiver.Receiver(lambda stack: key, self.conf,
                                     clock=lambda: epoch)
        self.addCleanup(listener.close)
        instance = service.Service(self.conf)
        detector = instance.detector
        detector.clock = lambda: now
        detector.started -= 61
        events = conductor.RecordingNotifier()
        with mock.patch.object(notifications, 'notifier_for',
                               return_value=events):
            for index, server in enumerate(servers):
                api.servers.update(self.ctx, server.id, dict(
                    last_heartbeat_at=now - datetime.timedelta(seconds=61),
                    mgmt_ip='192.0.2.%d' % (index + 10)))
            instance._check_health()
            self.assertEqual('DEGRADED', self.request('get', path).json[
                'stack']['status'])
            for server in servers:
                self.assertEqual('UNHEALTHY', self.request(
                    'get', path + '/servers/' + server.id).json[
                        'server']['status'])
            count = len(events.events)
            instance._check_health()
            self.assertEqual(count, len(events.events))
            for index, server in enumerate(servers):
                packet = heartbeat.encode(dict(
                    server_id=server.id, env_id=stack_id, seq=1, ts=epoch,
                    docker_ok=True, engine_ok=True, disk_pct=10), key)
                self.assertTrue(listener.accept(
                    packet, '192.0.2.%d' % (index + 10)))
            listener.flush()
            instance._check_health()
            self.assertEqual('ACTIVE', self.request('get', path).json[
                'stack']['status'])
            for server in servers:
                self.assertEqual('ACTIVE', self.request(
                    'get', path + '/servers/' + server.id).json[
                        'server']['status'])
            types = {name for name, payload in events.events}
            self.assertEqual({
                'coast.server.update.start', 'coast.server.update.end',
                'coast.environment.update.start',
                'coast.environment.update.end'}, types)
            foreign = dict(self.headers, **{'X-Project-Id': 'other'})
            self.request('get', path, headers=foreign, status=404)
