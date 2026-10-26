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

"""Only the engine/database stop; durable leases share export exclusion."""

import json
import tempfile
from types import SimpleNamespace

from coast.agent import engine_upgrade
from coast.agent.engines import coolify
from coast.agent import export
from coast.tests import base


class QuiesceTest(base.TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.options = SimpleNamespace(agent=SimpleNamespace(
            role='controller', state_path=directory.name,
            export_auto_resume_timeout=60))
        self.now = 100
        self.ids = {'coolify': 'a' * 64, 'coolify-db': 'b' * 64,
                    'registry': 'c' * 64, 'proxy': 'd' * 64}
        self.running = set(self.ids.values())
        self.lease = engine_upgrade.Quiesce(
            self.options, self.docker, lambda: self.now)
        self.body = {'export_id': '11111111-1111-1111-1111-111111111111',
                     'volume_ids': ['22222222-2222-2222-2222-222222222222']}

    def docker(self, *args, **kwargs):
        if args[0] == 'inspect':
            identifier = self.ids[args[-1]]
            return (json.dumps(identifier) + '|' + json.dumps(
                identifier in self.running)).encode()
        if args[0] == 'stop':
            self.running.remove(args[-1])
        if args[0] == 'start':
            self.running.add(args[-1])
        return b''

    def test_only_engine_and_db_stop_and_restart_after_expiry(self):
        self.lease.quiesce(self.body)
        self.assertEqual({self.ids['registry'], self.ids['proxy']},
                         self.running)
        self.now += 61
        restarted = engine_upgrade.Quiesce(
            self.options, self.docker, lambda: self.now)
        restarted.recover()
        self.assertEqual(set(self.ids.values()), self.running)
        self.assertEqual('resumed', restarted.read(
            self.body['export_id'])['state'])

    def test_already_stopped_database_is_not_started(self):
        self.running.remove(self.ids['coolify-db'])
        self.lease.quiesce(self.body)
        self.lease.resume({'export_id': self.body['export_id']})
        self.assertNotIn(self.ids['coolify-db'], self.running)
        self.assertIn(self.ids['coolify'], self.running)

    def test_worker_and_build_cannot_stop_engine_containers(self):
        for role in ('worker', 'build'):
            self.options.agent.role = role
            self.assertRaises(coolify.PreconditionFailed,
                              self.lease.quiesce, self.body)
        self.assertEqual(set(self.ids.values()), self.running)

    def test_export_and_upgrade_share_one_exclusive_lease(self):
        self.lease.quiesce(self.body)
        exports = export.Quiesce(self.options, self.docker, lambda: self.now)
        body = dict(self.body,
                    export_id='33333333-3333-3333-3333-333333333333')
        self.assertRaises(coolify.Conflict, exports.quiesce, body)
        exports.recover(force=True)
        self.assertEqual(set(self.ids.values()), self.running)
