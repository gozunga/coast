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

"""Database public HTTP contract tests (offline)."""

import json
from types import SimpleNamespace

import requests_mock

from coast.agent import proxy
from coast.common import engine_versions
from coast.common import states
from coast.conductor import runtime_logs
from coast.drivers import base as engine
from coast.drivers.coolify import v4
from coast.tests import base


class DatabaseDriverTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(insecure_direct_url=True, group='coolify')
        self.driver = v4.CoolifyV4Driver(self.conf)
        self.ep = engine.EngineEndpoint('https://engine.example.com',
                                        '', '', '', 'test-token')
        self.url = 'https://engine.example.com/api/v1'
        self.http = requests_mock.Mocker()
        self.http.start()
        self.addCleanup(self.http.stop)
        self.database = SimpleNamespace(
            id='coast-database', engine_ref=None, description='Database',
            server_id='worker', server_engine_ref='worker-engine',
            project_engine_ref='project-engine',
            environment_engine_ref='environment-engine',
            cpu_limit=0.5, memory_limit_mb=256, kind='database')
        self.http.get(self.url + '/databases', json=[])
        self.http.get(self.url + '/servers/worker-engine',
                      json={'uuid': 'worker-engine'})

    def test_create_each_engine_and_retry(self):
        for name, info in engine_versions.DATABASES.items():
            database = self.database
            database.engine = name
            database.engine_version = info['default_version']
            credentials = dict(username='app', database='app',
                               password='generated-secret',
                               root_password='generated-root-secret')
            path = '/databases/' + name
            self.assertTrue(proxy.allowed('POST', '/api/v1' + path))
            self.http.post(self.url + path, json={'uuid': 'database-uuid'})
            result = self.driver.create_database(
                self.ep, None, database, credentials)
            payload = self.http.last_request.json()
            self.assertEqual(info['versions'][info['default_version']],
                             payload['image'])
            self.assertEqual('worker-engine', payload['server_uuid'])
            self.assertEqual('project-engine', payload['project_uuid'])
            self.assertEqual('environment-engine', payload['environment_uuid'])
            self.assertTrue(payload['instant_deploy'])
            self.assertFalse(payload['is_public'])
            self.assertEqual('256M', payload['limits_memory'])
            for key, field in info['credentials'].items():
                self.assertEqual(credentials[key], payload[field])
            self.assertEqual('database-uuid', result.engine_ref)
            self.http.get(self.url + '/databases', json=[{
                'name': v4.name(database), 'uuid': result.engine_ref}])
            self.assertEqual(result, self.driver.create_database(
                self.ep, None, database, credentials))
            self.assertEqual('GET', self.http.last_request.method)
            self.http.get(self.url + '/databases', json=[])

    def test_public_settings_restart_and_create(self):
        database = self.database
        database.engine_ref = 'database-uuid'
        path = self.url + '/databases/database-uuid'
        stored = dict(self.driver._database_settings(database),
                      status='running:healthy')
        runtime = dict(stored)
        self.http.get(path, json=lambda request, context: dict(stored))

        def patch(request, context):
            stored.update(request.json())
            return {}

        def restart(request, context):
            runtime.update(stored)
            return {}

        self.http.patch(path, json=patch)
        restart_route = self.http.post(path + '/restart', json=restart)
        for count, (enabled, port) in enumerate(
                ((True, 16000), (True, 17000), (False, 17000)), start=1):
            database.public_enabled = enabled
            database.public_port = port
            self.assertTrue(self.driver.update_database(
                self.ep, None, database))
            self.assertEqual(enabled, stored['is_public'])
            self.assertEqual(port, stored['public_port'])
            self.assertEqual(count, restart_route.call_count)
            self.assertEqual('POST', self.http.last_request.method)
            self.assertEqual(path + '/restart', self.http.last_request.url)
            self.assertEqual(enabled, runtime['is_public'])
            self.assertEqual(port, runtime['public_port'])
            self.assertFalse(self.driver.update_database(
                self.ep, None, database))
            self.assertEqual(count, restart_route.call_count)
        database.public_enabled = True
        database.engine_ref = None
        database.engine, info = next(iter(engine_versions.DATABASES.items()))
        database.engine_version = info['default_version']
        self.http.post(self.url + '/databases/' + database.engine,
                       json={'uuid': 'database-uuid'})
        self.driver.create_database(
            self.ep, None, database,
            dict.fromkeys(info['credentials'], 'value'))
        self.assertTrue(self.http.last_request.json()['is_public'])
        self.assertEqual(17000, self.http.last_request.json()['public_port'])

    def test_public_restart_failure_preserves_retry(self):
        database = self.database
        database.engine_ref = 'database-uuid'
        stored = dict(self.driver._database_settings(database),
                      status='running:healthy')
        path = self.url + '/databases/database-uuid'
        self.http.get(path, json=lambda request, context: dict(stored))

        def patch(request, context):
            stored.update(request.json())
            return {}

        self.http.patch(path, json=patch)
        restart = self.http.post(path + '/restart', [
            {'status_code': 400, 'json': {}}, {'json': {}}])
        database.public_enabled = True
        database.public_port = 16000
        # A simultaneous limit update is applied by the public restart too.
        database.cpu_limit = 1
        self.assertRaises(engine.DriverError, self.driver.update_database,
                          self.ep, None, database)
        self.assertFalse(stored['is_public'])
        self.assertIsNone(stored['public_port'])
        self.assertTrue(self.driver.update_database(self.ep, None, database))
        self.assertEqual(2, restart.call_count)
        self.assertTrue(stored['is_public'])
        self.assertEqual('1', stored['limits_cpus'])

    def test_status_update_delete_with_volumes(self):
        database = self.database
        database.engine_ref = 'database-uuid'
        path = '/databases/database-uuid'
        for status, expected in (('running:healthy', states.ACTIVE),
                                 ('healthy', states.ACTIVE),
                                 ('exited', states.STOPPED),
                                 ('running:unhealthy', states.ERROR),
                                 ('starting', states.DEPLOYING)):
            self.http.get(self.url + path, json={'status': status})
            self.assertEqual(expected, self.driver.get_database_status(
                self.ep, None, database).status)
            self.assertEqual(expected, self.driver.get_app_status(
                self.ep, None, database).status)
        for action in ('start', 'stop', 'restart'):
            self.http.post(self.url + path + '/' + action, json={})
            getattr(self.driver, action + '_app')(self.ep, None, database)
            self.assertTrue(proxy.allowed(
                'POST', '/api/v1' + path + '/' + action))
        self.http.get(self.url + path, json={'status': 'exited'})
        self.http.patch(self.url + path, json={})
        self.driver.update_database(self.ep, None, database)
        data = self.http.last_request.json()
        self.assertNotIn('password', json.dumps(data))
        self.http.delete(self.url + path, json={})
        self.driver.delete_database(self.ep, None, database)
        self.assertEqual({key: ['true'] for key in (
            'delete_configurations', 'delete_volumes', 'docker_cleanup',
            'delete_connected_networks')}, self.http.last_request.qs)
        self.http.delete(self.url + path, status_code=404)
        self.driver.delete_database(self.ep, None, database)
        for method in ('GET', 'PATCH', 'DELETE'):
            self.assertTrue(proxy.allowed(method, '/api/v1' + path))
        self.assertFalse(proxy.allowed('POST', '/databases/unknown-engine'))

    def test_changed_limits_recreate_runtime_once_and_preserve_stopped(self):
        self.database.engine_ref = 'database-uuid'
        path = self.url + '/databases/database-uuid'
        stored = dict(self.driver._database_settings(self.database),
                      status='running:healthy')
        runtime = dict(stored)

        def read(request, context):
            return dict(stored)

        def patch(request, context):
            stored.update(request.json())
            return {}

        def restart(request, context):
            runtime.update(stored)
            return {}

        self.http.get(path, json=read)
        self.http.patch(path, json=patch)
        self.http.post(path + '/restart', json=restart)
        self.database.cpu_limit = 1.5
        self.database.memory_limit_mb = 512
        self.assertFalse(self.driver.update_database(
            self.ep, None, self.database))
        self.assertEqual('1.5', stored['limits_cpus'])
        self.assertEqual('512M', stored['limits_memory'])
        self.driver.restart_app(self.ep, None, self.database)
        self.assertEqual('512M', runtime['limits_memory'])
        self.assertFalse(self.driver.update_database(
            self.ep, None, self.database))
        self.database.description = 'Renamed metadata'
        self.assertFalse(self.driver.update_database(
            self.ep, None, self.database))
        posts = [request for request in self.http.request_history
                 if request.method == 'POST']
        self.assertEqual(1, len(posts))
        stored['status'] = 'exited'
        self.database.cpu_limit = None
        self.database.memory_limit_mb = None
        self.assertFalse(self.driver.update_database(
            self.ep, None, self.database))
        self.assertEqual('0', stored['limits_cpus'])
        self.assertEqual('0', stored['limits_memory'])
        posts = [request for request in self.http.request_history
                 if request.method == 'POST']
        self.assertEqual(1, len(posts))

    def test_saved_limits_are_ready_for_restart(self):
        self.database.engine_ref = 'database-uuid'
        self.database.settings_changed_at = 'pending'
        path = self.url + '/databases/database-uuid'
        self.http.get(path, json=dict(
            self.driver._database_settings(self.database), status='running'))
        self.assertIs(self.driver.update_database(
            self.ep, None, self.database), False)
        self.assertEqual(['GET'], [request.method for request in
                                   self.http.request_history])

    def test_pending_limits_defer_during_startup(self):
        self.database.engine_ref = 'database-uuid'
        self.database.settings_changed_at = 'pending'
        path = self.url + '/databases/database-uuid'
        self.http.get(path, json=dict(
            self.driver._database_settings(self.database), status='starting'))
        self.assertIsNone(self.driver.update_database(
            self.ep, None, self.database))
        self.assertEqual(['GET'], [request.method for request in
                                   self.http.request_history])

    def test_database_runtime_logs(self):
        self.database.engine_ref = 'database-uuid'
        path = self.url + '/databases/database-uuid/logs'
        self.http.get(path, json={
            'logs': '2026-01-01T00:00:00Z database ready'})
        lines = list(self.driver.get_app_logs(
            self.ep, None, self.database, tail=42))
        self.assertEqual('database ready', lines[0].line)
        self.assertEqual(['42'], self.http.last_request.qs['lines'])
        self.assertTrue(proxy.allowed(
            'GET', '/api/v1/databases/database-uuid/logs'))

    def test_crash_loop_capture_masks_database_credentials(self):
        self.database.engine_ref = 'database-uuid'
        path = self.url + '/databases/database-uuid/logs'
        self.http.get(path, [
            {'json': {'logs': ''}},
            {'json': {'logs': '2026-01-01T00:00:00Z failure private-value'}}])
        lines = runtime_logs.capture(
            self.driver, self.ep, None, self.database, attempts=2,
            sleep=lambda delay: None, masks={
                'password_ref': engine.EnvVar(
                    value='private-value', is_secret=True)})
        self.addCleanup(runtime_logs.LAST.pop, self.database.id, None)
        self.assertEqual(1, len(lines))
        self.assertNotIn('private-value', lines[0].line)
        self.assertEqual(lines, runtime_logs.last(self.database.id)[1])
