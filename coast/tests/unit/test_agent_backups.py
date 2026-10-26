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

"""Database backup streams with fake Docker exec and temporary-link HTTP."""

import base64
import contextlib
import hashlib
from http.client import HTTPMessage
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import mock
import zlib

import requests

from coast.agent import backup
from coast.agent import database
from coast.agent import server
from coast.common import engine_versions
from coast.tests import base


class Docker:
    def __init__(self, data=b'CREATE TABLE example;'):
        self.data = data
        self.loaded = io.BytesIO()
        self.version_value = '16.2'
        self.calls = []

    def resolve(self, service, container):
        self.calls.append((service, container))
        return 'a' * 64

    def version(self, cid, engine):
        return self.version_value

    @contextlib.contextmanager
    def process(self, cid, engine, restore=False):
        self.calls.append(('restore' if restore else 'dump', engine))
        yield self.loaded if restore else io.BytesIO(self.data)


class Response:
    def __init__(self, data=b'', status=200):
        self.data = data
        self.status_code = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        for offset in range(0, len(self.data), 13):
            yield self.data[offset:offset + 13]


class HTTP:
    def __init__(self):
        self.objects = {}
        self.attempts = []
        self.failures = []
        self.get_status = 200

    def put(self, url, data, **kwargs):
        content = data.read()
        self.attempts.append(content)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            if failure:
                return Response(status=failure)
        self.objects[url] = content
        return Response(status=201)

    def get(self, url, **kwargs):
        return Response(self.objects[url], self.get_status)


class BackupTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.docker = Docker()
        self.http = HTTP()
        self.runner = backup.Runner(
            self.docker, self.http, sleep=lambda _: None)
        self.key = b'k' * 32
        self.urls = ['https://objects.example.com/segment-%s' % i
                     for i in range(100)]
        self.body = dict(service_id='service-uuid', container='c-db',
                         engine='postgres', upload_urls=self.urls,
                         segment_bytes=64,
                         key=base64.b64encode(self.key).decode())

    def archive(self, data=None):
        if data is not None:
            self.docker.data = data
        return self.runner.run(self.body)

    def restore(self):
        body = {k: v for k, v in self.body.items()
                if k not in ('upload_urls', 'segment_bytes')}
        body.update(download_urls=list(self.http.objects), target='c-db')
        return self.runner.run(body, restore=True)

    def decode(self, raw):
        return list(backup.decrypt(backup.Input([raw]), self.key))

    def test_round_trip_segments_metadata_and_checksum(self):
        result = self.archive()
        raw = b''.join(self.http.objects.values())
        self.assertEqual(len(raw), result['size_bytes'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['sha256'])
        self.assertEqual('16.2', result['engine_version'])
        self.assertGreater(result['duration_seconds'], 0)
        self.assertGreater(len(result['segments']), 1)
        for data, segment in zip(self.http.objects.values(),
                                 result['segments']):
            self.assertLessEqual(len(data), 64)
            self.assertEqual(len(data), segment['size_bytes'])
            self.assertEqual(hashlib.sha256(
                data).hexdigest(), segment['sha256'])
        self.assertNotIn(self.docker.data, raw)
        self.assertIn('duration_seconds', self.restore())
        self.assertEqual(self.docker.data, self.docker.loaded.getvalue())

    def test_all_engines_and_empty_dump_round_trip(self):
        for engine in database.SCRIPTS:
            with self.subTest(engine=engine):
                self.body['engine'] = engine
                self.http.objects.clear()
                self.docker.loaded = io.BytesIO()
                self.archive(b'')
                self.restore()
                self.assertEqual(b'', self.docker.loaded.getvalue())

    def _run_standalone(self, restore):
        payload = b'customer database contents'
        cid = 'a' * 64
        for engine, info in engine_versions.DATABASES.items():
            if not info['backups']:
                continue
            engine = info.get('backup_engine', engine)
            version = info['default_version'] + '.0'
            body = dict(self.body, resource_type='database', engine=engine,
                        service_id='database-uuid', container='database-uuid')
            self.http.objects.clear()
            if restore:
                body.pop('upload_urls')
                body.pop('segment_bytes')
                self.http.objects[self.urls[0]] = b''.join(backup.encrypt(
                    io.BytesIO(payload), self.key, engine, version))
                body.update(download_urls=self.urls[:1],
                            target=body['container'])
            reader = mock.Mock()
            reader.inventory.return_value = {'database-uuid': cid}
            reader.inspect.return_value = {'State': {'Running': True}}
            docker = database.Docker(reader)
            self.runner.docker = docker
            process = mock.Mock(stdout=io.BytesIO(payload))
            process.wait.return_value = 0
            process.poll.return_value = 0
            with self.subTest(engine=engine), \
                    mock.patch.object(docker, 'resolve',
                                      wraps=docker.resolve) as resolve, \
                    mock.patch.object(docker, 'process',
                                      wraps=docker.process) as dispatch, \
                    mock.patch.object(docker, 'version',
                                      return_value=version), \
                    mock.patch.object(database.subprocess, 'Popen') as popen:
                popen.return_value.__enter__.return_value = process
                result = self.runner.run(body, restore=restore)
                resolve.assert_called_once_with(
                    'database-uuid', 'database-uuid', kind='database')
                reader.inventory.assert_called_once_with(
                    'database', 'database-uuid')
                reader.inspect.assert_called_once_with(
                    cid, 'database', 'database-uuid')
                options = dict(restore=True) if restore else {}
                dispatch.assert_called_once_with(
                    cid, engine, standalone=True, **options)
                self.assertEqual(database.script(
                    engine, restore=restore, standalone=True),
                    popen.call_args.args[0][-1])
                if restore:
                    restored = b''.join(call.args[0] for call in
                                        process.stdin.write.call_args_list)
                    self.assertEqual(payload, restored)
                else:
                    self.assertEqual(version, result['engine_version'])

    def test_standalone_backup_runner_ownership_and_script(self):
        self._run_standalone(restore=False)

    def test_standalone_restore_runner_ownership_and_script(self):
        self._run_standalone(restore=True)

    def test_random_multichunk_round_trip(self):
        self.body['segment_bytes'] = backup.CHUNK
        data = os.urandom(backup.CHUNK * 2 + 100)
        self.archive(data)
        self.restore()
        self.assertEqual(data, self.docker.loaded.getvalue())

    def test_random_nonce_per_backup(self):
        first = b''.join(backup.encrypt(io.BytesIO(b'a'), self.key,
                                        'postgres', '16.2'))
        second = b''.join(backup.encrypt(io.BytesIO(b'a'), self.key,
                                         'postgres', '16.2'))
        self.assertNotEqual(first, second)

    def test_retry_rewinds_segment_on_network_and_server_failure(self):
        self.http.failures = [requests.ConnectionError('secret'), 503]
        self.archive()
        self.assertEqual(self.http.attempts[0], self.http.attempts[1])
        self.assertEqual(self.http.attempts[1], self.http.attempts[2])

    def test_upload_rejection_and_retry_exhaustion(self):
        for failures in ([403], [302], [500, 500, 500]):
            self.http.failures = list(failures)
            self.assertRaises(database.BackupError, self.archive)

    def test_not_enough_upload_urls(self):
        self.body['upload_urls'] = self.urls[:1]
        self.assertRaisesRegex(database.BackupError,
                               'Not enough', self.archive)

    def test_schema_fails_before_docker(self):
        for change in ({'key': 'bad'},
                       {'key': base64.b64encode(b'x').decode()},
                       {'container': '../db'}, {'container': 1},
                       {'engine': 'redis'}, {'engine': []}, {
                           'engine': None}, {'service_id': '../other'},
                       {'segment_bytes': 0}, {'segment_bytes': True},
                       {'segment_bytes': backup.MAX_SEGMENT + 1},
                       {'upload_urls': []}, {'upload_urls': ['http://x']},
                       {'upload_urls': ['https://example.com:invalid/']},
                       {'upload_urls': ['https://example.com/a\nb']},
                       {'upload_urls': ['https://user:password@example.com']},
                       {'upload_urls': ['https://example.com/#fragment']},
                       {'extra': 'no'}):
            with self.subTest(change=change):
                self.assertRaises(ValueError, self.runner.run,
                                  dict(self.body, **change))
        self.assertEqual([], self.docker.calls)

    def test_older_major_refused_before_loader(self):
        self.archive()
        self.docker.version_value = '15.9'
        self.assertRaisesRegex(database.BackupError, 'major version',
                               self.restore)
        self.assertNotIn(('restore', 'postgres'), self.docker.calls)

    def test_newer_major_and_same_major_allowed(self):
        for version in ('16.1', '17.0'):
            self.docker.version_value = '16.2'
            self.archive()
            self.docker.version_value = version
            self.restore()

    def test_wrong_engine_and_key_refused_before_loader(self):
        self.archive()
        self.body['engine'] = 'mysql'
        self.assertRaises(database.BackupError, self.restore)
        self.body['engine'] = 'postgres'
        self.body['key'] = base64.b64encode(b'z' * 32).decode()
        self.assertRaisesRegex(database.BackupError,
                               'authentication', self.restore)
        self.assertNotIn(('restore', 'postgres'), self.docker.calls)

    def test_target_must_match(self):
        body = dict(service_id='service', container='db', engine='postgres',
                    key=self.body['key'], download_urls=self.urls,
                    target='other')
        self.assertRaises(ValueError, self.runner.run, body, restore=True)

    def test_download_rejection_and_safe_network_error(self):
        self.archive()
        self.http.get_status = 302
        self.assertRaisesRegex(database.BackupError, 'download was rejected',
                               self.restore)
        with mock.patch.object(self.http, 'get', side_effect=(
                requests.ConnectionError('https://secret-url'))):
            self.assertRaisesRegex(database.BackupError,
                                   '^Database backup download failed$',
                                   self.restore)

    def test_busy_agent_refuses_second_operation(self):
        with self.runner.lock:
            self.assertRaisesRegex(database.BackupError, 'already running',
                                   self.archive)

    def test_truncation_tampering_and_appended_bytes(self):
        raw = b''.join(backup.encrypt(io.BytesIO(b'dump'), self.key,
                                      'postgres', '16.2'))
        for invalid in (raw[:-1], raw[:-20], raw + b'extra',
                        raw[:30] + bytes([raw[30] ^ 1]) + raw[31:],
                        raw[:-1] + bytes([raw[-1] ^ 1])):
            with self.subTest(size=len(invalid)):
                self.assertRaises((database.BackupError, ValueError),
                                  self.decode, invalid)

    def test_bad_header_and_frame_lengths(self):
        raw = b''.join(backup.encrypt(io.BytesIO(b'dump'), self.key,
                                      'postgres', '16.2'))
        end = 24 + struct.unpack('>I', raw[12:16])[0]
        for invalid in (b'INVALID!' + raw[8:],
                        raw[:8] + struct.pack('>II', 0, 100) + raw[16:],
                        raw[:end] + struct.pack('>I', 2 ** 32 - 1)):
            self.assertRaises(database.BackupError, self.decode, invalid)

    def test_compressed_truncation_detected(self):
        self.archive()
        with mock.patch.object(backup, 'decrypt', return_value=iter([
                ({'engine': 'postgres', 'engine_version': '16.2'},
                 zlib.compress(b'dump'))])):
            self.assertRaises(database.BackupError, self.restore)

    def test_tool_failure_leaves_no_success_result_and_releases_lock(self):
        with mock.patch.object(self.docker, 'version',
                               side_effect=OSError('secret')):
            self.assertRaisesRegex(database.BackupError,
                                   '^Database backup or restore failed$',
                                   self.archive)
        self.archive()

    def test_frame_reordering_is_rejected(self):
        raw = b''.join(backup.encrypt(io.BytesIO(os.urandom(backup.CHUNK * 2)),
                                      self.key, 'postgres', '16.2'))
        start = 24 + struct.unpack('>I', raw[12:16])[0]
        length = 4 + struct.unpack('>I', raw[start:start + 4])[0]
        self.assertRaisesRegex(database.BackupError, 'authentication',
                               self.decode, raw[:start] + raw[start + length:])

    def test_plaintext_compression_bomb_is_written_in_bounded_blocks(self):
        self.archive(b'a' * backup.CHUNK * 4)
        target = mock.Mock()
        with mock.patch.object(self.docker, 'process') as process:
            process.return_value.__enter__.return_value = target
            self.restore()
        blocks = [c.args[0] for c in target.write.call_args_list]
        self.assertEqual(self.docker.data, b''.join(blocks))
        self.assertTrue(all(len(b) <= backup.CHUNK for b in blocks))

    def test_https_adapter_closes_connection_and_preserves_signed_path(self):
        response = mock.Mock(status=200, headers={})
        response.read.side_effect = [b'first', b'second', b'']
        with mock.patch.object(backup.http.client, 'HTTPSConnection') as https:
            connection = https.return_value
            connection.getresponse.return_value = response
            url = 'https://objects.example.com:8443/object?temp_url_sig=sig'
            with backup.Transfer().get(url) as r:
                self.assertEqual([b'first', b'second'],
                                 list(r.iter_content(64)))
            https.assert_called_once_with(
                'objects.example.com', 8443, timeout=10)
            connection.request.assert_called_once_with(
                'GET', '/object?temp_url_sig=sig', body=None, headers={})
            connection.close.assert_called_once()
            connection.connect.assert_called_once()
            connection.sock.settimeout.assert_called_once_with(60)

    def test_https_adapter_closes_on_connection_failure(self):
        with mock.patch.object(backup.http.client, 'HTTPSConnection') as https:
            https.return_value.connect.side_effect = OSError('private')
            with self.assertRaises(OSError):
                url = 'https://objects.example.com/object'
                with backup.Transfer().put(url):
                    self.fail('Connection must fail')
            https.return_value.close.assert_called_once()


class DatabaseTest(base.TestCase):
    def test_server_major_comparison(self):
        self.assertEqual((16, 0), database.major('postgres', '16.2'))
        self.assertEqual((9, 6), database.major('postgres', '9.6.2'))
        self.assertEqual((10, 11), database.major('mariadb', '10.11.1'))
        self.assertRaises(database.BackupError, database.major, 'mysql', 'bad')

    def test_scripts_detect_tools_and_keep_credentials_inside_container(self):
        for engine in database.SCRIPTS:
            for restore in (False, True):
                script = database.script(engine, restore)
                self.assertIn('command -v', script)
                self.assertIn('exit 127', script)
        self.assertIn('--single-transaction', database.script('mysql'))
        self.assertIn('mariadb-dump mysqldump', database.script('mariadb'))
        self.assertIn('--archive', database.script('mongodb'))
        self.assertIn('--format=custom', database.script('postgres'))

    def test_standalone_dump_and_restore_use_official_credentials(self):
        for engine, info in engine_versions.DATABASES.items():
            if not info['backups']:
                continue
            engine = info.get('backup_engine', engine)
            env = {field.upper(): 'pass' if 'password' in key else 'app'
                   for key, field in info['credentials'].items()}
            expected = (['--user=root', 'SQL=pass']
                        if 'root_password' in info['credentials'] else
                        ['--username=app'])
            if info.get('url_query'):
                expected += ['--password=pass',
                             '--authenticationDatabase=admin']
            else:
                expected += ['PG=pass'] if 'backup_engine' in info else []
            for restore, tools in ((False, database.SCRIPTS[engine][1]),
                                   (True, database.SCRIPTS[engine][2])):
                tool = tools[0]
                with self.subTest(engine=engine, restore=restore), \
                        tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / tool
                    path.write_text('#!/bin/sh\n'
                                    'printf "%s\\n" "$@" "SQL=$MYSQL_PWD" '
                                    '"PG=$PGPASSWORD"\n')
                    path.chmod(0o700)
                    result = subprocess.run(
                        ['/bin/sh', '-c', database.script(
                            engine, restore=restore, standalone=True)],
                        env=dict(env, PATH=directory), stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, check=False)
                    self.assertEqual(0, result.returncode, result.stderr)
                    args = result.stdout.decode().splitlines()
                    for value in expected:
                        self.assertIn(value, args)
                    if info.get('url_query'):
                        self.assertIn('--nsInclude=app.*' if restore else
                                      '--db=app', args)

    def test_resolve_checks_ownership_and_running_state(self):
        reader = mock.Mock()
        reader.inventory.return_value = {'db': 'a' * 64}
        reader.inspect.return_value = {'State': {'Running': True}}
        docker = database.Docker(reader)
        self.assertEqual('a' * 64, docker.resolve('service', 'c-db'))
        reader.inspect.assert_called_with('a' * 64, 'service', 'service')
        self.assertRaises(database.BackupError,
                          docker.resolve, 'service', 'other')
        reader.inspect.return_value = {'State': {'Running': False}}
        self.assertRaises(database.BackupError,
                          docker.resolve, 'service', 'db')

    def test_process_exit_failure_and_cleanup(self):
        process = mock.Mock()
        process.wait.return_value = 1
        process.poll.return_value = None
        process.stdout = io.BytesIO(b'dump')
        with mock.patch.object(database.subprocess, 'Popen') as popen:
            popen.return_value.__enter__.return_value = process
            with self.assertRaises(database.BackupError):
                with database.Docker().process('abc', 'postgres') as source:
                    self.assertEqual(b'dump', source.read())
            process.kill.assert_called_once()

    def test_version_query_and_failures(self):
        for engine, output, version in (
                ('postgres', b'16.2\n', '16.2'),
                ('mysql', b'8.4.1', '8.4.1'),
                ('mariadb', b'10.11.8-MariaDB', '10.11.8'),
                ('mongodb', b'db version v7.0.5', '7.0.5')):
            with mock.patch.object(database.subprocess, 'run', return_value=(
                    SimpleNamespace(returncode=0, stdout=output))):
                self.assertEqual(
                    version, database.Docker().version('abc', engine))
        for output in (b'unknown', b'x' * 5000):
            with mock.patch.object(database.subprocess, 'run', return_value=(
                    SimpleNamespace(returncode=0, stdout=output))):
                self.assertRaises(database.BackupError,
                                  database.Docker().version, 'abc', 'postgres')

    def test_official_image_variable_expansion_and_tool_fallback(self):
        cases = [
            ('postgres', 'pg_dump', {}, ['--username=postgres', 'postgres']),
            ('postgres', 'pg_dump', {'POSTGRES_USER': 'alice',
                                     'POSTGRES_DB': 'app',
                                     'POSTGRES_PASSWORD': 'pass'},
             ['--username=alice', 'app', 'PG=pass']),
            ('mysql', 'mysqldump', {'MYSQL_ROOT_PASSWORD': 'rootpass',
                                    'MYSQL_DATABASE': 'app'},
             ['--user=root', 'app', 'SQL=rootpass']),
            ('mysql', 'mysqldump', {'MYSQL_USER': 'alice',
                                    'MYSQL_PASSWORD': 'pass'},
             ['--user=alice', 'mysql', 'SQL=pass']),
            ('mariadb', 'mariadb-dump', {'MARIADB_USER': 'alice',
                                         'MARIADB_PASSWORD': 'pass',
                                         'MARIADB_DATABASE': 'app'},
             ['--user=alice', 'app', 'SQL=pass']),
            ('mariadb', 'mysqldump', {'MYSQL_ROOT_PASSWORD': 'pass'},
             ['--user=root', 'mysql', 'SQL=pass']),
            ('mongodb', 'mongodump', {'MONGO_INITDB_ROOT_USERNAME': 'alice',
                                      'MONGO_INITDB_ROOT_PASSWORD': 'pass'},
             ['--username=alice', '--password=pass',
              '--authenticationDatabase=admin']),
        ]
        for engine, tool, env, expected in cases:
            with self.subTest(engine=engine, tool=tool, env=env):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / tool
                    path.write_text('#!/bin/sh\n'
                                    'printf "%s\\n" "$@" "SQL=$MYSQL_PWD" '
                                    '"PG=$PGPASSWORD"\n')
                    path.chmod(0o700)
                    result = subprocess.run(
                        ['/bin/sh', '-c', database.script(engine)],
                        env=dict(env, PATH=directory), stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, check=False)
                    self.assertEqual(0, result.returncode, result.stderr)
                    args = result.stdout.decode().splitlines()
                    for value in expected:
                        self.assertIn(value, args)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ['/bin/sh', '-c', database.script('postgres')],
                env={'PATH': directory}, check=False)
            self.assertEqual(127, result.returncode)

    def test_restore_process_closes_input_and_checks_exit(self):
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        with mock.patch.object(database.subprocess, 'Popen') as popen:
            popen.return_value.__enter__.return_value = process
            with database.Docker().process(
                    'abc', 'mysql', restore=True) as sink:
                sink.write(b'sql')
            process.stdin.write.assert_called_once_with(b'sql')
            process.stdin.close.assert_called_once()
            self.assertEqual(subprocess.DEVNULL,
                             popen.call_args.kwargs['stderr'])

    def test_version_errors_do_not_expose_process_output(self):
        errors = (OSError('secret'), subprocess.TimeoutExpired('secret', 30))
        for error in errors:
            with mock.patch.object(database.subprocess, 'run',
                                   side_effect=error):
                self.assertRaisesRegex(database.BackupError,
                                       '^Database version query failed$',
                                       database.Docker().version,
                                       'abc', 'mysql')


class CommandTest(base.TestCase):
    def handler(self, role='worker', method='POST',
                path='/v1/backup_database'):
        handler = object.__new__(server.Handler)
        self.config.config(role=role, group='agent')
        handler.server = SimpleNamespace(conf=self.conf, databases=mock.Mock())
        handler.server.databases.run.return_value = {'size_bytes': 42}
        handler.path = path
        handler.command = method
        handler.deadline = mock.Mock()
        handler.connection = mock.Mock()
        handler.reply = mock.Mock()
        handler.send_error = mock.Mock()
        return handler

    def test_worker_post_commands_and_safe_failure(self):
        for path, restore in (('/v1/backup_database', False),
                              ('/v1/restore_database', True)):
            handler = self.handler(path=path)
            with mock.patch.object(server.threading, 'Timer'):
                self.assertTrue(handler._database(self.conf, b'{}'))
            handler.server.databases.run.assert_called_once_with(
                {}, restore=restore)
            self.assertEqual(200, handler.reply.call_args.args[0])
            handler.server.databases.run.side_effect = database.BackupError(
                'safe')
            with mock.patch.object(server.threading, 'Timer'):
                handler._database(self.conf, b'{}')
            self.assertEqual(412, handler.reply.call_args.args[0])
            self.assertEqual({'error': 'safe'}, json.loads(
                handler.reply.call_args.args[2]))

    def test_wrong_roles_and_methods(self):
        for role, method, code in (('controller', 'POST', 404),
                                   ('build', 'POST', 404),
                                   ('worker', 'GET', 405)):
            handler = self.handler(role, method)
            self.assertTrue(handler._database(self.conf, b'{}'))
            handler.send_error.assert_called_once_with(code)
            handler.server.databases.run.assert_not_called()

    def test_dispatch_registers_command(self):
        handler = self.handler()
        handler.headers = HTTPMessage()
        handler.headers['Content-Length'] = '2'
        handler.rfile = io.BytesIO(b'{}')
        with mock.patch.object(server.threading, 'Timer'):
            handler.dispatch()
        self.assertEqual(200, handler.reply.call_args.args[0])
