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

"""Archive content, retry, size and credential-disclosure regressions."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock

from coast.agent.engines import coolify
from coast.agent import export_archive
from coast.common import exception
from coast.conductor import exports
from coast.tests import base


EXPORT = '11111111-1111-1111-1111-111111111111'
VOLUME = '22222222-2222-2222-2222-222222222222'
URL = 'https://storage.example.com/v1/account/exports/object?sig=SECRET'


class ArchiveTest(base.TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / 'source'
        self.data.mkdir()
        (self.data / 'database').write_bytes(b'persistent data')
        self.config.config(role='worker', state_path=str(self.root),
                           group='agent')
        self.uploads = []
        self.fail_put = False

        @contextlib.contextmanager
        def mount(*args):
            yield self.data

        self.mount = mount

        def put(url, stream, size, deadline):
            stream.seek(0)
            self.uploads.append(stream.read())
            if self.fail_put:
                raise RuntimeError(URL)

        self.put = put
        self.agent = export_archive.Archive(self.conf, mount, put)
        self.body = {'export_id': EXPORT, 'volume_id': VOLUME,
                     'device': '/dev/vdb', 'filesystem': 'ext4',
                     'purpose': 'data', 'put_url': URL,
                     'manifest': {'services': [], 'consistency': 'stopped'}}

    def contents(self):
        return tarfile.open(fileobj=io.BytesIO(self.uploads[-1]), mode='r:gz')

    def test_contents_checksums_and_private_receipt(self):
        result = self.agent.archive(self.body)
        with self.contents() as archive:
            self.assertEqual(['data/database', 'manifest.json'],
                             archive.getnames())
            manifest = json.load(archive.extractfile('manifest.json'))
            self.assertEqual(VOLUME, manifest['volume_id'])
            self.assertEqual(hashlib.sha256(b'persistent data').hexdigest(),
                             manifest['checksums']['data/database'])
            self.assertEqual(b'persistent data',
                             archive.extractfile('data/database').read())
        self.assertEqual(len(self.uploads[0]), result['size'])
        self.assertEqual(hashlib.sha256(self.uploads[0]).hexdigest(),
                         result['sha256'])
        receipt = self.agent.root / (EXPORT + '-' + VOLUME)
        self.assertEqual(0o600, receipt.stat().st_mode & 0o777)
        self.assertNotIn('SECRET', receipt.read_text())
        self.assertNotIn('storage.example.com', receipt.read_text())

    def test_restart_returns_receipt_and_accepts_renewed_signature(self):
        result = self.agent.archive(self.body)
        restarted = export_archive.Archive(self.conf, self.mount, self.put)
        self.body['put_url'] = URL.replace('SECRET', 'RENEWED')
        self.assertEqual(result, restarted.archive(self.body))
        self.assertEqual(1, len(self.uploads))

    def test_concurrent_duplicate_requests_share_one_receipt(self):
        results = []
        failures = []
        barrier = threading.Barrier(2)

        def run():
            try:
                agent = export_archive.Archive(self.conf, self.mount, self.put)
                barrier.wait(timeout=5)
                results.append(agent.archive(self.body))
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1])
        self.assertEqual(1, len(self.uploads))

    def test_uncertain_put_retries_identical_bytes_and_masks_error(self):
        self.fail_put = True
        error = self.assertRaises(coolify.PreconditionFailed,
                                  self.agent.archive, self.body)
        self.assertNotIn('SECRET', str(error))
        self.fail_put = False
        restarted = export_archive.Archive(self.conf, self.mount, self.put)
        result = restarted.archive(self.body)
        self.assertEqual(self.uploads[0], self.uploads[1])
        self.assertEqual('uploaded', result['status'])

    def test_changed_object_or_manifest_conflicts(self):
        self.agent.archive(self.body)
        changed = dict(self.body, put_url=URL.replace('/object?', '/other?'))
        self.assertRaises(coolify.Conflict, self.agent.archive, changed)
        changed = dict(self.body, manifest={'services': ['changed']})
        self.assertRaises(coolify.Conflict, self.agent.archive, changed)
        self.assertEqual(1, len(self.uploads))

    def test_docker_selection_preserves_empty_directory(self):
        (self.data / 'volumes').mkdir()
        (self.data / 'volumes' / 'empty').mkdir()
        (self.data / 'volumes' / 'named').write_bytes(b'volume')
        (self.data / 'overlay2').mkdir()
        (self.data / 'overlay2' / 'layer').write_bytes(b'cache')
        self.body['purpose'] = 'docker'
        self.agent.archive(self.body)
        with self.contents() as archive:
            self.assertEqual(['data/volumes/empty', 'data/volumes/named',
                              'manifest.json'], archive.getnames())

    def test_symlink_and_special_file_are_rejected_without_put(self):
        for kind in ('symlink', 'fifo'):
            path = self.data / 'unsafe'
            if kind == 'symlink':
                path.symlink_to('/etc/passwd')
            else:
                os.mkfifo(path)
            self.assertRaises(coolify.PreconditionFailed,
                              self.agent.archive, self.body)
            path.unlink()
        self.assertEqual([], self.uploads)

    def test_hardlinks_and_unreadable_subtrees_fail(self):
        os.link(self.data / 'database', self.data / 'hardlink')
        self.assertRaises(coolify.PreconditionFailed,
                          self.agent.archive, self.body)
        (self.data / 'hardlink').unlink()

        def denied_walk(*args, **kwargs):
            kwargs['onerror'](PermissionError('sensitive path'))

        with mock.patch.object(export_archive.os, 'walk',
                               side_effect=denied_walk):
            self.assertRaises(coolify.PreconditionFailed,
                              self.agent.archive, self.body)
        self.assertEqual([], self.uploads)

    def test_source_object_metadata_and_deadline_limits(self):
        for raw, compressed, deadline, metadata in (
                (1, 4096, time.monotonic() + 10, 4096),
                (4096, 1, time.monotonic() + 10, 4096),
                (4096, 4096, time.monotonic() - 1, 4096),
                (4096, 4096, time.monotonic() + 10, 1)):
            self.assertRaises(
                coolify.PreconditionFailed, export_archive.build_archive,
                self.data, 'data', {}, io.BytesIO(), raw, compressed,
                deadline, metadata)

    def test_invalid_destination_schema_role_and_uuid(self):
        for changed in (
                {'put_url': 'http://storage.example.com/object?sig=SECRET'},
                {'put_url': 'https://user:SECRET@example.com/object?sig=x'},
                {'put_url': URL + '#fragment'},
                {'put_url': 'https://example.com:SECRET/object?sig=x'},
                {'filesystem': 'btrfs'},
                {'purpose': 'scratch'}, {'device': '/etc/passwd'},
                {'export_id': '../escape'}, {'extra': True}):
            self.assertRaises(ValueError, self.agent.archive,
                              dict(self.body, **changed))
        with mock.patch.object(
                self.agent, 'conf',
                SimpleNamespace(agent=SimpleNamespace(role='invalid'))):
            self.assertRaises(ValueError, self.agent.archive, self.body)
        self.assertEqual([], self.uploads)

    def test_mount_rejects_non_device(self):
        with self.assertRaises(coolify.PreconditionFailed):
            with export_archive.mounted(str(self.data / 'database'), 'ext4',
                                        self.root, time.monotonic() + 10):
                self.fail('Mounted a regular file')

    def test_mount_options_cleanup_and_mounted_device_rejection(self):
        info = SimpleNamespace(st_mode=stat.S_IFBLK,
                               st_rdev=os.makedev(253, 1))
        for filesystem, options in (('ext4', 'ro,noload'),
                                    ('xfs', 'ro,nouuid,norecovery')):
            directory = self.root / filesystem
            directory.mkdir()
            with mock.patch.object(Path, 'resolve',
                                   return_value=Path('/dev/vdb')), \
                    mock.patch.object(Path, 'stat', return_value=info), \
                    mock.patch('builtins.open', mock.mock_open()), \
                    mock.patch.object(export_archive, 'command') as run:
                with export_archive.mounted(
                        '/dev/vdb', filesystem, directory,
                        time.monotonic() + 10) as target:
                    self.assertEqual(directory / 'mount', target)
                self.assertEqual(
                    ['mount', '-t', filesystem, '-o', options,
                     '/dev/vdb', str(target)], run.call_args_list[0].args[0])
                self.assertEqual(['umount', str(target)],
                                 run.call_args_list[1].args[0])
        with mock.patch.object(Path, 'resolve',
                               return_value=Path('/dev/vdb')), \
                mock.patch.object(Path, 'stat', return_value=info), \
                mock.patch('builtins.open', mock.mock_open(
                    read_data='100 99 253:1 / /data ro - ext4 /dev/vdb ro')):
            with self.assertRaises(coolify.PreconditionFailed):
                with export_archive.mounted(
                        '/dev/vdb', 'ext4', self.root,
                        time.monotonic() + 10):
                    self.fail('Mounted a live device')

    def test_cleanup_failure_does_not_record_success(self):
        @contextlib.contextmanager
        def failed_cleanup(*args):
            yield self.data
            raise RuntimeError('sensitive cleanup error')

        agent = export_archive.Archive(self.conf, failed_cleanup, self.put)
        self.assertRaises(coolify.PreconditionFailed, agent.archive, self.body)
        receipt = agent.read(EXPORT + '-' + VOLUME)
        self.assertNotIn('result', receipt)
        result = self.agent.archive(self.body)
        self.assertEqual('uploaded', result['status'])
        self.assertEqual(self.uploads[0], self.uploads[1])

    def test_upload_sends_content_length_and_never_follows_redirect(self):
        connection = mock.Mock()
        connection.getresponse.return_value.status = 307
        with mock.patch.object(export_archive.http.client, 'HTTPSConnection',
                               return_value=connection):
            self.assertRaises(coolify.PreconditionFailed,
                              export_archive.upload, URL, io.BytesIO(b'data'),
                              4, time.monotonic() + 10)
        self.assertEqual(('PUT', '/v1/account/exports/object?sig=SECRET'),
                         connection.putrequest.call_args.args)
        self.assertIn(mock.call('Content-Length', '4'),
                      connection.putheader.call_args_list)
        self.assertEqual(b'data', connection.send.call_args.args[0])


class ConductorArchiveTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.server = SimpleNamespace(id='server', role='worker',
                                      project_id='project', stack_id='stack')
        self.volume = SimpleNamespace(id=VOLUME, server_id='server',
                                      project_id='project', stack_id='stack',
                                      purpose='data', cinder_volume_id='clone')
        self.export = SimpleNamespace(id=EXPORT, volume_ids=[VOLUME, 'other'],
                                      project_id='project', stack_id='stack')
        self.agent = mock.Mock()
        self.agent.post.return_value = {'status': 'uploaded', 'size': 100,
                                        'sha256': 'a' * 64}

    def execute(self):
        return exports.archive_volume(self.conf, self.server, self.export,
                                      self.volume, '/dev/vdb', 'ext4', URL,
                                      {}, agent=self.agent)

    def test_valid_receipt(self):
        self.assertEqual({'size': 100, 'sha256': 'a' * 64}, self.execute())

    def test_foreign_project_stack_owner_and_unselected_volume(self):
        for resource, field in ((self.server, 'project_id'),
                                (self.server, 'stack_id'),
                                (self.volume, 'project_id'),
                                (self.volume, 'stack_id'),
                                (self.volume, 'server_id'),
                                (self.volume, 'id')):
            previous = getattr(resource, field)
            setattr(resource, field, 'foreign')
            self.assertRaises(exception.CoastException, self.execute)
            setattr(resource, field, previous)
        self.agent.post.assert_not_called()

    def test_malformed_receipts_and_failure_do_not_complete(self):
        for changed in ({'status': 'pending'}, {'size': True}, {'size': -1},
                        {'size': 6 * 1024 ** 3}, {'sha256': 'SECRET'}):
            self.agent.post.return_value = {
                'status': 'uploaded', 'size': 100, 'sha256': 'a' * 64}
            self.agent.post.return_value.update(changed)
            error = self.assertRaises(exception.CoastException, self.execute)
            self.assertNotIn('SECRET', str(error))
        self.agent.post.side_effect = RuntimeError(URL)
        error = self.assertRaises(exception.CoastException, self.execute)
        self.assertNotIn('SECRET', str(error))
