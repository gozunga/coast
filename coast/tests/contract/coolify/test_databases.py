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

"""Opt-in database contracts; only the deployment lane runs these."""

import re
import secrets
import time
from types import SimpleNamespace
import uuid

from coast.common import engine_versions
from coast.common import states
from coast.drivers import base as engine
from coast.tests.contract.coolify import test_live


class LiveDatabaseTest(test_live.LiveCoolifyBase):
    def test_create_observe_update_delete_every_catalog_engine(self):
        # Engine database responses can contain plaintext credentials.
        self.driver.recorder = None
        application = SimpleNamespace(engine_ref=self.env.engine_ref)
        environment = SimpleNamespace(id=uuid.uuid4().hex, engine_ref=None)
        environment.engine_ref = self.driver.ensure_environment(
            self.ep, self.env, application, environment).engine_ref
        self.addCleanup(self.driver.delete_environment, self.ep, self.env,
                        application, environment)
        for name, info in engine_versions.DATABASES.items():
            database = SimpleNamespace(
                id=uuid.uuid4().hex, engine_ref=None, kind='database',
                engine=name, engine_version=info['default_version'],
                server_id=self.servers[0].id,
                server_engine_ref=self.servers[0].engine_ref,
                project_engine_ref=application.engine_ref,
                environment_engine_ref=environment.engine_ref,
                description='Database contract', cpu_limit=None,
                memory_limit_mb=None)
            self.addCleanup(self.driver.delete_database, self.ep, self.env,
                            database)
            credentials = dict(username='app', database='app',
                               password=secrets.token_urlsafe(32),
                               root_password=secrets.token_urlsafe(32))
            result = self.driver.create_database(
                self.ep, self.env, database, credentials)
            database.engine_ref = result.engine_ref
            self.assertEqual(result, self.driver.create_database(
                self.ep, self.env, database, credentials))
            deadline = time.monotonic() + self.conf.conductor.start_timeout
            while time.monotonic() < deadline:
                observed = self.driver.get_database_status(
                    self.ep, self.env, database)
                if observed.status == states.ACTIVE:
                    break
                time.sleep(2)
            else:
                self.fail('Database did not reach ACTIVE')
            logs = list(self.driver.get_app_logs(
                self.ep, self.env, database, tail=200, masks={
                    key: engine.EnvVar(value=value, is_secret=True)
                    for key, value in credentials.items()}))
            self.assertTrue(logs, 'Database startup logs were empty')
            self.assertTrue(all(isinstance(line, engine.LogLine)
                                for line in logs))
            self.assertTrue(any(re.search(
                r'ready (?:to accept|for) connections|'
                r'waiting for connections|start listening on',
                line.line, re.IGNORECASE) for line in logs),
                'Database logs lacked expected startup text')
            database.cpu_limit = 0.5
            self.driver.update_database(self.ep, self.env, database)
            with self.driver._client(self.ep) as client:
                updated = client.request(
                    'GET', '/databases/' + database.engine_ref)
            self.assertEqual(database.cpu_limit,
                             float(updated['limits_cpus']))
            for action, expected in (('restart', states.ACTIVE),
                                     ('stop', states.STOPPED),
                                     ('start', states.ACTIVE)):
                getattr(self.driver, action + '_app')(
                    self.ep, self.env, database)
                action_deadline = (time.monotonic() +
                                   self.conf.conductor.start_timeout)
                while time.monotonic() < action_deadline:
                    time.sleep(2)
                    if self.driver.get_database_status(
                            self.ep, self.env, database).status == expected:
                        break
                else:
                    self.fail('Database action did not settle: ' + action)
            self.driver.delete_database(self.ep, self.env, database)
            deadline = time.monotonic() + self.conf.conductor.start_timeout
            while time.monotonic() < deadline:
                try:
                    self.driver.get_database_status(
                        self.ep, self.env, database)
                except engine.EngineNotFound:
                    break
                time.sleep(2)
            else:
                self.fail('Database was not deleted')
