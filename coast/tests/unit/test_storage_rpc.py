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

import os
from pathlib import Path
from unittest import mock

import fixtures
from oslo_concurrency import lockutils
from oslo_messaging._drivers import amqpdriver

from coast.api import errors
from coast.api import microversions
from coast.common import states
from coast.conductor import rpcapi
from coast.drivers import base as driver_base
from coast import objects
from coast.tests import conductor


class StorageRPCTest(conductor.ConductorTest):
    def prepare_stack(self):
        self.path = self.stack()
        worker = next(row for row in objects.ServerList.get_all(self.ctx)
                      if row.role == 'worker')
        self.services = []
        for index in range(2):
            source = objects.Source(context=self.ctx, type='image',
                                    image='example.com/web:latest')
            source.create()
            service = objects.Service(
                context=self.ctx, stack_id=worker.stack_id,
                name='web%s' % index, kind='app', source_id=source.id,
                auto_deploy=False,
                server_id=worker.id, engine_ref='engine%s' % index,
                status=states.ACTIVE, task_state=states.NONE)
            service.create()
            self.services.append(service)
        stack = objects.Stack.get_by_id(self.ctx, worker.stack_id)
        self.driver = self.manager._driver(stack)
        # Exercise the Rabbit reply-name allocation that fake:// omits,
        # then deliver through the real client/serializer/dispatcher.
        directory = Path(self.useFixture(fixtures.TempDir()).path)
        # Detached test runners can outlive their process-group leader,
        # leaving /proc/<pgrp>/stat absent. Keep QManager's proc lookup on
        # this live process without depending on the launcher lifecycle.
        with mock.patch.object(amqpdriver.os, 'getpgrp',
                               return_value=os.getpid()):
            self.queue_manager = amqpdriver.QManager('test', 'coast-api')
        self.queue_manager.file_name = str(directory / 'queue-counter')
        self.useFixture(fixtures.MonkeyPatch(
            'oslo_concurrency.lockutils.CONF', self.conf))
        lockutils.get_lock_path(self.conf)
        self.config.config(lock_path=None, group='oslo_concurrency')
        self.lock_directory = directory / 'locks'
        self.lock_directory.mkdir(mode=0o700)
        send = self.transport._driver.send

        def allocate_reply(*args, **kwargs):
            if kwargs.get('wait_for_reply'):
                self.queue_manager.get()
            return send(*args, **kwargs)

        # Share this transport, while retaining the real lazy client setup.
        self.useFixture(fixtures.MonkeyPatch(
            'coast.conductor.rpcapi.messaging.get_rpc_transport',
            lambda conf: self.transport))
        self.patch_send = mock.patch.object(
            self.transport._driver, 'send', side_effect=allocate_reply)
        self.patch_send.start()
        self.addCleanup(self.patch_send.stop)

    def configure_locks(self):
        template = Path('tools/kolla/ansible/roles/coast/templates/'
                        'coast.conf.j2').read_text()
        section = '[oslo_concurrency]\n' + template.split(
            '[oslo_concurrency]\n')[1].split('\n[')[0]
        self.assertIn('lock_path = /var/lib/coast/locks', section)
        filename = self.lock_directory.parent / 'coast-test.conf'
        filename.write_text(section.replace('/var/lib/coast/locks',
                                            str(self.lock_directory)))
        self.conf(['--config-file', str(filename)])
        self.conf.clear_override('lock_path', group='oslo_concurrency')

    def read(self, version, status=200, service_id=None, headers=None):
        path = self.path + '/storages'
        if service_id:
            path += '?service_id=' + service_id
        return self.request('get', path, status=status, headers={
            **(headers or self.headers),
            microversions.HEADER: 'app-hosting ' + version})

    def test_queue_manager_requires_configured_lock_path(self):
        self.prepare_stack()
        with mock.patch.object(self.driver, 'list_volume_mounts') as mounts:
            with mock.patch.object(rpcapi.LOG, 'error') as log:
                response = self.read('1.24', status=503)
            mounts.assert_not_called()
        self.assertEqual(errors.STORAGE_UNAVAILABLE,
                         response.json['faultstring'])
        log.assert_called_once()
        values = log.call_args.args[1]
        self.assertEqual('oslo_config.cfg.RequiredOptError', values['cause'])
        self.assertEqual(self.path.rsplit('/', 1)[1], values['stack_id'])
        self.assertEqual('list_volume_mounts', values['operation'])
        self.assertEqual(response.headers['X-OpenStack-Request-Id'],
                         values['request_id'])

    def test_apps_mounts_cross_rpc_with_kolla_lock_config(self):
        self.prepare_stack()
        self.configure_locks()
        mounts = [dict(type='volume', name='data', source='data',
                       container_path='/data', read_only=False)]
        with mock.patch.object(self.driver, 'list_volume_mounts',
                               return_value=mounts) as observed:
            for version in ('1.16', '1.24', microversions.MAX_VERSION):
                response = self.read(version)
                self.assertEqual({row.id for row in self.services}, {
                    row['service_id'] for row in response.json['storages']})
                for service in self.services:
                    filtered = self.read(version, service_id=service.id)
                    self.assertEqual([service.id], [
                        row['service_id'] for row in
                        filtered.json['storages']])
            self.assertEqual(12, observed.call_count)
            observed.reset_mock()
            self.assertEqual([], self.read('1.15').json['storages'])
            self.read('1.24', status=404, headers={
                **self.headers, 'X-Project-Id': 'other'})
            observed.assert_not_called()
        self.assertEqual([], list(objects.StorageList.get_all(self.ctx)))
        self.assertTrue(list(self.lock_directory.iterdir()))

    def test_engine_failure_is_logged_once_and_returns_safe_message(self):
        self.prepare_stack()
        self.configure_locks()
        for error in (driver_base.EngineUnreachable('password=secret'),
                      driver_base.EngineAuthError('token=secret'),
                      RuntimeError('password=secret')):
            with mock.patch.object(self.driver, 'list_volume_mounts',
                                   side_effect=error):
                with mock.patch.object(rpcapi.LOG, 'error') as log:
                    response = self.read('1.24', status=503)
            log.assert_called_once()
            values = log.call_args.args[1]
            self.assertIn(type(error).__name__,
                          values['cause'] + values['remote_class'])
            self.assertEqual(errors.STORAGE_UNAVAILABLE,
                             response.json['faultstring'])
            self.assertNotIn('secret', str(log.call_args))
            self.assertNotIn('secret', response.text)
