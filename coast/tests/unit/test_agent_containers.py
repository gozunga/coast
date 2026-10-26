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

"""Agent read-only ownership, projection and bounded log tests."""

from http.client import HTTPMessage
import io
import json
from types import SimpleNamespace
from unittest import mock

from coast.agent import containers
from coast.agent import database
from coast.agent import docker_exec
from coast.agent import server
from coast.tests import base
from coast.tests import container_views as data


class AgentContainersTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(project_id='project', role='worker', group='agent')
        self.body = dict(project_id='project', resource_type='service',
                         resource_uuid='app-uuid')
        self.docker = data.Docker()

    def read(self, **options):
        return containers.read(self.conf, dict(self.body, **options),
                               self.docker)

    def test_list_detail_and_missing_container(self):
        result = self.read()['containers']
        self.assertEqual(['db', 'web'], [r['name'] for r in result])
        self.assertEqual('exited', result[0]['state'])
        self.assertEqual(3, result[0]['restart_count'])
        detail = self.read(container='c-web')['container']
        self.assertEqual({'cpu': .5, 'memory_bytes': 134217728},
                         detail['limits'])
        self.assertEqual('plain', detail['environment']['PASSWORD'])
        for forbidden in ('Labels', 'Mounts', 'HostConfig', '/host',
                          'engine-internal', 'COOLIFY_URL'):
            self.assertNotIn(forbidden, str(detail))
        self.assertEqual({'not_found': True}, self.read(container='missing'))
        self.docker.rows = []
        self.assertEqual({'containers': []}, self.read())

    def test_cpu_quota_and_unlimited_missing_health(self):
        self.docker.detail['HostConfig'] = {'CpuQuota': 25000,
                                            'CpuPeriod': 100000}
        self.docker.detail['State'] = {}
        self.docker.detail.pop('RestartCount')
        row = self.read(container='c-web')['container']
        self.assertEqual(.25, row['limits']['cpu'])
        self.assertIsNone(row['limits']['memory_bytes'])
        self.assertIsNone(row['health'])
        self.assertIsNone(row['restart_count'])
        self.docker.detail['HostConfig'] = {}
        self.assertIsNone(self.read(container='c-web')['container'][
            'limits']['cpu'])

    def test_project_schema_and_host_target_rejected_without_docker(self):
        for change in ({'project_id': 'other'}, {'resource_uuid': '../host'},
                       {'resource_type': 'host'}, {'container': '/host'},
                       {'docker_id': 'abc123'}, {'tail': -1}, {'tail': 10001},
                       {'tail': True}, {'since': 'invalid'}, {'since': 1},
                       {'logs': True}, {'logs': 'true'}):
            with self.subTest(change=change):
                self.assertRaises(ValueError, containers.read, self.conf,
                                  dict(self.body, **change), self.docker)
        self.assertEqual([], self.docker.calls)
        self.config.config(project_id='', group='agent')
        self.assertRaises(ValueError, containers.read, self.conf,
                          self.body, self.docker)

    def test_workload_labels_rechecked_and_ambiguous_names_rejected(self):
        self.docker.rows.append({'Id': 'ffff', 'Labels': data.labels(
            'foreign', resource='other')})
        self.docker.rows.append({'Id': 'eeee', 'Labels': {}})
        self.assertEqual(2, len(self.read()['containers']))
        self.docker.detail['Config']['Labels'] = data.labels(resource='other')
        self.assertRaises(OSError, self.read, container='c-web')
        self.docker.rows.append(self.docker.rows[0])
        self.assertRaises(OSError, self.read)

    def test_database_backup_labels_and_ownership_recheck(self):
        labels = {'coolify.managed': 'true', 'coolify.type': 'database',
                  'coolify.databaseUuid': 'db-uuid',
                  'com.docker.compose.service': 'db-uuid'}
        self.docker.rows = [{'Id': 'abc123', 'Labels': labels}]
        self.docker.detail['Config']['Labels'] = dict(labels)
        self.docker.detail['State']['Running'] = True
        docker = database.Docker(self.docker)
        self.assertEqual('abc123', docker.resolve(
            'db-uuid', 'db-uuid', kind='database'))
        self.assertIn('coolify.databaseUuid', self.docker.calls[0][1])
        self.assertRaises(database.BackupError, docker.resolve,
                          'other-uuid', 'db-uuid', kind='database')
        labels = self.docker.detail['Config']['Labels']
        labels['coolify.databaseUuid'] = 'other'
        self.assertRaises(OSError, docker.resolve,
                          'db-uuid', 'db-uuid', kind='database')

    def test_git_compose_application_labels(self):
        self.body['resource_type'] = 'app'
        self.docker.rows = [{'Id': 'abc123', 'Labels': data.labels(
            kind='application')}]
        self.docker.detail['Config']['Labels'] = data.labels(
            kind='application')
        self.assertEqual('web', self.read()['containers'][0]['name'])
        self.assertIn('coolify.applicationUuid', self.docker.calls[0][1])
        self.assertIn('all=true', self.docker.calls[0][1])

    def test_log_tail_since_streams_tty_and_partial_frames(self):
        row = self.read(container='c-web', logs=True, tail=1,
                        since='2026-10-10T01:00:00')['lines']
        self.assertEqual('stderr', row[0]['stream'])
        self.assertIn('hidden-value', row[0]['line'])
        before = len(self.docker.calls)
        empty = self.read(container='c-web', logs=True, tail=0)
        self.assertEqual([], empty['lines'])
        self.assertEqual(before + 2, len(self.docker.calls))
        self.docker.raw = data.frame('2026-10-10T01:00:00Z par') + data.frame(
            'tial\n')
        self.assertEqual('partial', self.read(container='c-web', logs=True)[
            'lines'][0]['line'])
        self.docker.detail['Config']['Tty'] = True
        self.docker.raw = b'2026-10-10T01:00:00Z tty\n'
        self.assertEqual('tty', self.read(container='c-web', logs=True)[
            'lines'][0]['line'])
        self.docker.detail['Config']['Tty'] = False
        for raw in (b'broken', b'\x01\x00\x00\x00\x00\x00\x00\xffshort'):
            self.docker.raw = raw
            self.assertRaises(OSError, self.read, container='c-web', logs=True)

    def test_docker_body_limit_rejects_truncation(self):
        connection = mock.MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'x' * 1048577
        with mock.patch.object(docker_exec, 'UnixConnection',
                               return_value=connection):
            self.assertRaises(OSError, containers.Reader()._http,
                              'GET', '/v1.44/containers/json', None)
        self.assertTrue(connection.close.called)

    def test_worker_route_get_only(self):
        for role, method, code in (('worker', 'GET', 200),
                                   ('worker', 'POST', 404),
                                   ('controller', 'GET', 404)):
            self.config.config(role=role, group='agent')
            payload = json.dumps(self.body).encode()
            handler = object.__new__(server.Handler)
            handler.server = SimpleNamespace(conf=self.conf)
            handler.path = '/v1/containers'
            handler.command = method
            handler.headers = HTTPMessage()
            handler.headers['Content-Length'] = str(len(payload))
            handler.rfile = io.BytesIO(payload)
            handler.reply = mock.Mock()
            handler.send_error = mock.Mock()
            with mock.patch.object(containers, 'Reader',
                                   return_value=self.docker):
                handler.dispatch()
            if code == 200:
                self.assertEqual(200, handler.reply.call_args.args[0])
            else:
                handler.send_error.assert_called_once_with(code)

    def test_inventory_limit_invalid_timestamp_and_long_compose_key(self):
        name = 'x' * 128
        self.docker.rows = [{'Id': 'abc123', 'Labels': data.labels(name)}]
        self.docker.detail['Config']['Labels'] = data.labels(name)
        result = self.read(container='c-' + name)['container']
        self.assertEqual('c-' + name, result['id'])
        self.docker.raw = data.frame('invalid timestamp\n')
        self.assertRaises(ValueError, self.read, container='c-' + name,
                          logs=True)
        self.docker.rows = [{}] * 101
        self.assertRaises(OSError, self.read)
