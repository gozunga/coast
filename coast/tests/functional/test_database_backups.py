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

"""API and durable jobs with real agent streams and fake Docker/Swift."""

import gzip
import hashlib
import os
from types import SimpleNamespace
from unittest import mock

from coast.agent import backup as wire
from coast.agent import database
from coast.common import database_backups as common
from coast.common import states
from coast.conductor import backup_storage
from coast.conductor import backups
from coast import objects
from coast.tests import app_api
from coast.tests.unit import test_agent_backups as fakes


class BackupFlowTest(app_api.AppAPITest):
    def test_segmented_backup_download_restore_cleanup(self):
        self.service.engine_ref = 'service-engine-uuid'
        self.service.kind = 'service'
        self.service.template_id = 'postgresql'
        self.service.save()
        self.server.mgmt_ip = '192.0.2.10'
        self.server.save()
        self.config.config(segment_bytes=1048576, max_segments=4,
                           group='backup')
        data = os.urandom(1048600)  # Compression still needs two segments.
        docker = fakes.Docker(data)
        http = fakes.HTTP()
        runner = wire.Runner(docker=docker, session=http)
        proxy = mock.Mock()
        proxy.get_endpoint.return_value = (
            'https://objects.example.com/swift/v1/AUTH_project')
        proxy.get_account_metadata.return_value = SimpleNamespace(
            meta_temp_url_key=None, meta_temp_url_key_2=None)
        storage = backup_storage.SwiftStorage(
            self.conf, self.ctx, self.service.id, proxy=proxy)
        patches = [
            mock.patch.object(backup_storage, 'proxy_for', return_value=proxy),
            mock.patch.object(backup_storage, 'SwiftStorage',
                              return_value=storage),
            mock.patch.object(backups.agent_client, 'AgentClient')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        def metadata(name, container):
            matches = [raw for url, raw in http.objects.items()
                       if '/' + name + '?' in url]
            self.assertTrue(matches)
            return SimpleNamespace(
                content_length=len(matches[0]),
                etag=hashlib.md5(
                    matches[0], usedforsecurity=False).hexdigest())

        proxy.get_object_metadata.side_effect = metadata
        proxy.put.return_value.status_code = 201
        proxy.put.return_value.reason = 'Created'
        commands = []

        def populate_gets(urls):
            for url in urls:
                target = url.split('?')[0]
                http.objects[url] = next(
                    raw for old, raw in http.objects.items()
                    if old.split('?')[0] == target)

        def dispatch(method, path, body):
            commands.append(path)
            if path == '/v1/backup_database':
                return runner.run(body)
            populate_gets(body['download_urls'])
            return runner.run(body, restore=True)

        backups.agent_client.AgentClient.return_value.request.side_effect = (
            dispatch)
        self.request('put', self.path + '/backup_policy', {'backup_policy': {
            'engine': 'postgres', 'container': 'postgresql'}}, version='1.20')
        accepted = self.request('post', self.path + '/backups', {},
                                version='1.20', status=202).json['backup']
        row = objects.Backup.get_by_id(self.ctx, accepted['id'])
        job = next(common.rows(objects.BackupJobList, self.ctx,
                               backup_id=row.id))
        backups.run_job(self.conf, self.ctx, job.id)
        row = objects.Backup.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ACTIVE, row.status)
        self.assertEqual(2, len(row.segments))
        self.assertNotIn(data[:32], b''.join(http.objects.values()))
        path = '/v1/backups/' + row.id
        policy = common.policy_for(self.ctx, self.service.id)
        populate_gets(storage.urls(policy, row, 'GET', len(row.segments)))
        with mock.patch.object(wire.Transfer, 'get', side_effect=http.get):
            response = self.request('get', path + '/download', version='1.20')
        self.assertEqual(data, gzip.decompress(response.body))
        restored = self.request('post', path + '/restore', {
            'mode': 'overwrite', 'confirmation': self.service.name},
            status=202, version='1.20').json['restore']
        backups.run_job(self.conf, self.ctx, restored['id'])
        job = objects.BackupJob.get_by_id(self.ctx, restored['id'])
        self.assertEqual(states.ACTIVE, job.status)
        self.assertEqual(data, docker.loaded.getvalue())
        self.assertEqual(['/v1/backup_database', '/v1/backup_database',
                          '/v1/restore_database'], commands)
        safety = objects.Backup.get_by_id(self.ctx, job.safety_backup_id)
        self.assertEqual(states.ACTIVE, safety.status)
        self.assertEqual('safety', safety.kind)
        docker.version_value = '15.7'
        body = dict(service_id=self.service.engine_ref, container='postgresql',
                    engine='postgres', target='postgresql',
                    key=self.store.get(self.ctx, row.key_ref),
                    download_urls=storage.urls(policy, row, 'GET', 2))
        populate_gets(body['download_urls'])
        previous = docker.loaded.getvalue()
        self.assertRaises(database.BackupError, runner.run, body, restore=True)
        self.assertEqual(previous, docker.loaded.getvalue())
        proxy.objects.return_value = []
        backups.drop_service(self.conf, self.ctx, self.service)
        self.assertEqual([], list(common.rows(objects.BackupList, self.ctx)))
        self.assertEqual([], list(common.rows(
            objects.BackupJobList, self.ctx)))
        signer = next(common.rows(objects.BackupAccountList, self.ctx))
        self.assertEqual({signer.key_ref}, set(self.store.values))
