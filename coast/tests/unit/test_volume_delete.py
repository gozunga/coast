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

"""Retained storage safety, receipt lifetime, and accounting regressions."""

import json
from types import SimpleNamespace
from unittest import mock

import fixtures

from openstack import exceptions as sdk_exceptions

from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.conductor import volume_delete
from coast.db import api
from coast.db import models
from coast import objects
from coast.tests.unit import test_controllers


class VolumeDeleteTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.owner = manager.Manager(self.conf, notifier=mock.Mock())
        self.addCleanup(self.owner.close)
        self.env.driver = 'coolify_v4'
        self.env.save()
        with api.writer.using(self.ctx):
            # Seed usage from this retained live row, without double counting.
            quota.reserve(self.ctx, volume_gb=1)
            quota.rollback(self.ctx, {'volume_gb': 1})
        self.block = mock.Mock()
        self.observed = SimpleNamespace(
            id='cinder-1', metadata={'coast': 'true', 'project_id': 'project',
                                     'stack_id': self.env.id},
            attachments=[], status='available')
        self.gone = False
        self.block.get_volume.side_effect = self.get_volume
        self.block.snapshots.side_effect = self.snapshots
        self.block.volumes.side_effect = self.volumes
        self.block.delete_volume.side_effect = self.delete_volume
        self.block.wait_for_delete.side_effect = self.wait_for_delete
        self.cloud = volume_delete.controller.ControllerCloud(
            self.conf, self.env)
        self.cloud.proxy = mock.Mock(return_value=self.block)
        self.useFixture(test_controllers.fixtures.MonkeyPatch(
            'coast.conductor.volume_delete.controller.ControllerCloud',
            mock.Mock(return_value=self.cloud)))

    def outside_transaction(self):
        with self.db.engine.connect() as connection:
            self.assertFalse(connection.in_transaction())
        try:
            self.ctx.session
        except AttributeError:
            return
        self.fail('Cloud I/O held a DB transaction')

    def get_volume(self, identifier):
        self.outside_transaction()
        self.assertEqual('cinder-1', identifier)
        if self.gone:
            raise sdk_exceptions.ResourceNotFound()
        return self.observed

    def snapshots(self, **kwargs):
        self.outside_transaction()
        return []

    def volumes(self, **kwargs):
        self.outside_transaction()
        return []

    def delete_volume(self, identifier, **kwargs):
        self.outside_transaction()
        self.assertFalse(kwargs['force'])
        self.assertFalse(kwargs['cascade'])
        self.assertFalse(kwargs['ignore_missing'])
        self.gone = True

    def wait_for_delete(self, observed, **kwargs):
        self.outside_transaction()
        self.assertEqual(self.conf.controller_worker.volume_delete_timeout,
                         kwargs['wait'])

    def accept(self):
        volume_delete.accept(self.ctx, self.volume)
        self.revision = rpcapi.revision(self.volume)
        self.identifier = rpcapi.receipt_id('delete_volume', self.volume)

    def execute(self, replay=False):
        volume_delete.DeleteVolume(self.owner).execute(
            self.ctx, self.volume, self.revision, replay=replay)

    def receipt(self):
        return objects.Event.get_by_id(self.ctx, self.identifier)

    def usage(self):
        with self.db.engine.connect() as connection:
            return connection.execute(models.QuotaUsage.__table__.select(
            )).mappings().one()['in_use_volume_gb']

    def test_receipt_survives_row_deletion_and_duplicate_completion(self):
        self.accept()
        self.execute()
        self.assertRaises(exception.NotFound, objects.Volume.get_by_id,
                          self.ctx, self.volume.id)
        self.assertEqual('success', self.receipt().outcome)
        self.assertEqual(0, self.usage())
        self.execute()
        self.assertEqual(0, self.usage())
        self.assertEqual(1, self.block.delete_volume.call_count)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_attachment_and_wrong_metadata_fail_closed(self):
        for changed in ({'attachments': [{'server_id': 'late'}]},
                        {'metadata': {'coast': 'true'}}):
            with self.subTest(changed=changed):
                self.volume = objects.Volume.get_by_id(
                    self.ctx, self.volume.id)
                self.accept()
                for key, value in changed.items():
                    setattr(self.observed, key, value)
                self.execute()
                self.assertEqual(states.ERROR, objects.Volume.get_by_id(
                    self.ctx, self.volume.id).status)
                self.assertEqual(10, self.usage())
                self.assertFalse(self.block.delete_volume.called)
                self.observed.attachments = []

    def test_snapshot_on_later_page_and_clone_dependency(self):
        self.accept()
        self.block.snapshots.side_effect = lambda **kwargs: iter([
            SimpleNamespace(volume_id='unrelated'),
            SimpleNamespace(volume_id='cinder-1')])
        self.execute()
        self.assertFalse(self.block.delete_volume.called)
        self.assertEqual(10, self.usage())

    def test_clone_dependency_even_after_export_is_active(self):
        self.accept()
        self.block.volumes.side_effect = lambda **kwargs: iter([
            SimpleNamespace(source_volume_id=None, metadata={}),
            SimpleNamespace(source_volume_id='cinder-1', metadata={})])
        self.execute()
        self.assertFalse(self.block.delete_volume.called)
        self.assertEqual(10, self.usage())

    def test_delete_failure_logs_class_without_secret(self):
        self.accept()
        logs = self.useFixture(fixtures.FakeLogger())
        secret = 'password=secret'
        with mock.patch.object(volume_delete.engines, 'run',
                               side_effect=Exception(secret)):
            self.execute()
        self.assertEqual(states.ERROR, objects.Volume.get_by_id(
            self.ctx, self.volume.id).status)
        self.assertNotIn(secret, logs.output)
        self.assertIn('builtins.Exception', logs.output)
        self.assertIn(self.volume.id, logs.output)
        self.assertIn(self.env.id, logs.output)

    def test_timeout_keeps_quota_then_new_delete_confirms_absence(self):
        self.accept()
        self.block.wait_for_delete.side_effect = sdk_exceptions.ResourceTimeout
        self.execute()
        self.assertEqual(10, self.usage())
        self.volume = objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.assertEqual(states.ERROR, self.volume.status)
        self.accept()
        self.execute()
        self.assertEqual(0, self.usage())
        self.assertEqual(1, self.block.delete_volume.call_count)

    def test_unverified_404_and_forbidden_never_release_quota(self):
        self.accept()
        self.gone = True
        self.execute()
        self.assertEqual(10, self.usage())
        self.assertEqual('failure', self.receipt().outcome)
        self.volume = objects.Volume.get_by_id(self.ctx, self.volume.id)
        self.accept()
        self.block.get_volume.side_effect = sdk_exceptions.ForbiddenException
        self.execute()
        self.assertEqual(10, self.usage())

    def test_crash_checkpoint_requires_explicit_replay(self):
        self.accept()
        event = self.receipt()
        data = json.loads(event.message)
        data.update(running=True, steps=['owned', 'detached', 'intent'])
        event.message = json.dumps(data)
        event.save()
        self.gone = True
        self.execute()
        self.assertEqual(10, self.usage())
        self.assertFalse(self.block.get_volume.called)
        self.execute(replay=True)
        self.assertEqual(0, self.usage())
        self.assertFalse(self.block.delete_volume.called)

    def test_notification_retry_after_row_deletion_does_no_cloud_io(self):
        self.accept()
        self.owner.notifier.info.side_effect = RuntimeError('broker secret')
        self.execute()
        self.assertEqual('success', self.receipt().outcome)
        self.assertEqual(['start', 'end'],
                         json.loads(self.receipt().message)['pending'])
        self.block.reset_mock()
        self.owner.notifier.info.side_effect = None
        self.execute()
        self.assertFalse(self.block.get_volume.called)
        self.assertEqual([], json.loads(self.receipt().message)['pending'])
        self.assertEqual(0, self.usage())

    def test_missing_receipt_and_wrong_project_do_no_cloud_io(self):
        self.owner.execute(self.ctx, 'delete_volume', self.volume)
        self.assertFalse(self.block.get_volume.called)
        other = test_controllers.context.RequestContext(project_id='other')
        self.assertRaises(exception.NotFound, self.owner.execute,
                          other, 'delete_volume', self.volume)

    def test_api_gate_and_stack_task_prevent_competing_acceptance(self):
        self.volume.server_id = self.server.id
        self.volume.save()
        self.request('delete', self.volume_path, status=409)
        self.volume.server_id = None
        self.volume.save()
        self.env.acquire_task(states.RECONCILING)
        self.request('delete', self.volume_path, status=409)
        self.env.release_task(states.RECONCILING)
        with mock.patch('coast.api.handlers.Handler.delete_volume'):
            self.request('delete', self.volume_path, status=202)
        for method, path, body in (
                ('delete', self.path, None),
                ('delete', self.server_path, None),
                ('patch', self.volume_path, {'volume': {'size_gb': 12}}),
                ('post', self.volume_path + '/action', {'snapshot': {}})):
            self.request(method, path, body, status=409)

    def test_receipt_message_is_not_exposed_to_tenants(self):
        self.accept()
        response = self.request('get', '/v1/events?id=' + self.identifier)
        self.assertNotIn('cinder-1', response.text)

    def test_failed_export_dependency_is_blocked(self):
        objects.Export(context=self.ctx, stack_id=self.env.id,
                       status=states.ERROR, volume_ids=[self.volume.id],
                       backend='swift').create()
        self.request('delete', self.volume_path, status=409)
