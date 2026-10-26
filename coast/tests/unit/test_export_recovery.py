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

"""Interrupted export cleanup on the shared recovery timer."""

from collections import deque
from types import SimpleNamespace
from unittest import mock

from coast.api import secret_store
from coast.common import states
from coast.conductor import exports
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.db import api as db_api
from coast import objects
from coast.tests.unit import test_controllers
from coast.tests.unit.test_export_wiring import configure_s3
from coast.tests.unit.test_export_wiring import s3_client_factory
from coast.tests.unit.test_export_wiring import S3_PAYLOAD


class ExportRecoveryTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.env.driver = 'coolify'
        self.env.conductor_host = self.conf.host
        self.env.save()
        self.env.acquire_task(states.RECONCILING)
        self.server.compute_id = 'nova-owner'
        self.server.save()
        self.export = objects.Export(
            context=self.ctx, stack_id=self.env.id, backend='swift',
            volume_ids=[self.volume.id], status=states.PENDING)
        self.export.create()
        self.manager = manager.Manager(self.conf, notifier=mock.Mock())
        self.block = mock.Mock()
        self.compute = mock.Mock()
        self.agent = mock.Mock()
        self.agent.post.return_value = {'status': 'resumed'}
        metadata = {'coast': 'true', 'project_id': self.ctx.project_id,
                    'stack_id': self.env.id, 'export_id': self.export.id,
                    'volume_id': self.volume.id}
        self.clone = SimpleNamespace(
            id='clone', metadata=metadata, attachments=[{
                'server_id': self.server.compute_id}])
        self.snapshot = SimpleNamespace(id='snapshot', metadata=metadata)
        self.block.volumes.return_value = [self.clone]
        self.block.snapshots.return_value = [self.snapshot]
        for target, name, value in (
                (exports.clients, 'get_connection', SimpleNamespace(
                    block_storage=self.block)),
                (exports.clients, 'get_sensitive_proxy', self.compute),
                (exports, 'AgentClient', self.agent)):
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(exports, 'SwiftStorage')
        self.storage = patch.start().return_value
        self.addCleanup(patch.stop)

    def prepare(self, status):
        if status == states.UPLOADING:
            self.export.status = states.SNAPSHOTTING
            self.export.save()
        self.export.status = status
        self.export.save()
        if status != states.PENDING:
            self.manager._event(self.ctx, self.export, self.env, 'create',
                                'start', rpcapi.receipt_id(
                                    'create_export', self.export))
        exports.prepare_recovery(self.manager)
        self.manager._export_recovery_after = 0

    def assert_recovered(self, status):
        self.prepare(status)
        exports.recover(self.manager)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(exports.RECOVERY_FAULT, current.fault)
        self.assertEqual([], current.artifacts)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        self.assertEqual(('nova-owner', 'clone'),
                         self.compute.delete_volume_attachment.call_args.args)
        self.assertEqual(
            self.clone, self.block.delete_volume.call_args.args[0])
        self.assertEqual(self.snapshot,
                         self.block.delete_snapshot.call_args.args[0])
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.export.id})
        self.assertTrue(events)
        self.assertTrue(all(event.outcome == 'failure' for event in events))
        calls = self.storage.delete.call_count
        exports.recover(self.manager)
        self.assertEqual(calls, self.storage.delete.call_count)

    def test_interrupted_pending(self):
        self.assert_recovered(states.PENDING)

    def test_interrupted_snapshotting(self):
        self.assert_recovered(states.SNAPSHOTTING)

    def test_interrupted_uploading(self):
        self.assert_recovered(states.UPLOADING)

    def test_interrupted_deleting(self):
        self.assert_recovered(states.DELETING)

    def test_resume_precedes_cloud_cleanup_and_waits_for_old_upload(self):
        self.prepare(states.UPLOADING)
        self.manager._export_recovery_after = float('inf')
        exports.recover(self.manager)
        self.assertTrue(self.agent.post.called)
        self.assertFalse(self.block.volumes.called)
        self.assertFalse(self.storage.delete.called)
        self.assertEqual(1, len(self.manager._export_recovery))
        self.manager._export_recovery_after = 0
        order = mock.Mock()
        order.attach_mock(self.agent.post, 'resume')
        order.attach_mock(self.block.volumes, 'cloud')
        exports.recover(self.manager)
        self.assertEqual('resume', order.mock_calls[0][0])

    def test_failed_resume_does_not_skip_other_owners_or_cleanup(self):
        second = objects.Server(
            context=self.ctx, stack_id=self.env.id, name='build', role='build',
            kind='vm', status=states.ACTIVE, task_state=states.NONE)
        second.create()
        self.prepare(states.SNAPSHOTTING)
        self.agent.post.side_effect = [RuntimeError('secret'),
                                       {'status': 'resumed'}]
        exports.recover(self.manager)
        self.assertEqual(2, self.agent.post.call_count)
        self.assertTrue(self.block.delete_volume.called)
        self.assertEqual(states.SNAPSHOTTING, objects.Export.get_by_id(
            self.ctx, self.export.id).status)
        self.agent.post.side_effect = None
        exports.recover(self.manager)
        self.assertEqual(states.ERROR, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_failed_cloud_cleanup_retries_after_another_restart(self):
        self.prepare(states.UPLOADING)
        self.block.delete_volume.side_effect = RuntimeError('secret')
        exports.recover(self.manager)
        self.assertTrue(self.block.delete_snapshot.called)
        self.assertTrue(self.storage.delete.called)
        self.assertEqual(states.UPLOADING, objects.Export.get_by_id(
            self.ctx, self.export.id).status)
        self.assertEqual(states.RECONCILING, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        self.block.delete_volume.side_effect = None
        exports.prepare_recovery(self.manager)
        self.manager._export_recovery_after = 0
        exports.recover(self.manager)
        self.assertEqual(states.ERROR, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_failed_object_delete_retries_and_clears_partial_receipts(self):
        self.prepare(states.UPLOADING)
        self.storage.delete.side_effect = RuntimeError('secret')
        exports.recover(self.manager)
        self.assertEqual(1, len(self.manager._export_recovery))
        self.storage.delete.side_effect = None
        exports.recover(self.manager)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertNotIn('secret', current.fault)

    def test_foreign_resources_and_original_volume_are_not_deleted(self):
        foreign = SimpleNamespace(
            id='foreign', metadata=dict(self.clone.metadata,
                                        project_id='other'), attachments=[])
        original = SimpleNamespace(id='original', metadata={'coast': 'true'},
                                   attachments=[])
        self.block.volumes.return_value = [foreign, original, self.clone]
        self.block.snapshots.return_value = [foreign, self.snapshot]
        self.prepare(states.SNAPSHOTTING)
        exports.recover(self.manager)
        self.assertEqual([mock.call(self.clone, ignore_missing=True)],
                         self.block.delete_volume.call_args_list)
        self.assertEqual([mock.call(self.snapshot, ignore_missing=True)],
                         self.block.delete_snapshot.call_args_list)

    def test_unknown_attachment_is_not_detached_or_deleted(self):
        self.clone.attachments = [{'server_id': 'foreign-server'}]
        self.prepare(states.UPLOADING)
        exports.recover(self.manager)
        self.assertFalse(self.compute.delete_volume_attachment.called)
        self.assertFalse(self.block.delete_volume.called)
        self.assertEqual(1, len(self.manager._export_recovery))

    def test_terminal_states_and_other_hosts_are_not_captured(self):
        for status in (states.ACTIVE, states.EXPIRED, states.ERROR):
            self.export.status = status
            # Test scan eligibility, not transitions.
            with db_api.writer.using(self.ctx):
                row = db_api.exports._get(self.ctx, self.export.id)
                row.status = status
            exports.prepare_recovery(self.manager)
            self.assertEqual(0, len(self.manager._export_recovery))
        self.export = objects.Export.get_by_id(self.ctx, self.export.id)
        self.export.status = states.DELETING
        self.export.save()
        self.env.conductor_host = 'another-host'
        self.env.save()
        exports.prepare_recovery(self.manager)
        self.assertEqual(0, len(self.manager._export_recovery))
        self.assertFalse(self.agent.post.called)

    def test_new_or_changed_attempt_is_not_recovered(self):
        # MySQL/MariaDB can persist timestamps at whole-second precision.
        with mock.patch.object(rpcapi, 'revision', return_value='same-second'):
            self.prepare(states.PENDING)
            self.export.status = states.SNAPSHOTTING
            self.export.save()
            exports.recover(self.manager)
        self.assertFalse(self.agent.post.called)
        self.assertEqual(states.SNAPSHOTTING, objects.Export.get_by_id(
            self.ctx, self.export.id).status)
        exports.prepare_recovery(self.manager)
        new = objects.Export(
            context=self.ctx, stack_id=self.env.id, backend='swift',
            volume_ids=[self.volume.id], status=states.PENDING)
        new.create()
        self.assertNotIn(new.id, [entry[0] for entry in
                                  self.manager._export_recovery])

    def test_bounded_fair_retry_queue(self):
        self.manager._export_recovery = deque(
            (str(index), 'project', 'revision') for index in range(101))
        patch = mock.patch.object(exports, 'recover_one', return_value=False)
        with patch as r:
            exports.recover(self.manager)
            self.assertEqual(100, r.call_count)
            self.assertEqual('100', self.manager._export_recovery[0][0])
            exports.recover(self.manager)
            self.assertEqual('100', r.call_args_list[100].args[2])

    def test_stack_delete_lock_survives_until_export_cleanup(self):
        self.env.release_task(states.RECONCILING)
        self.env.status = states.DELETING
        self.env.save()
        self.env.acquire_task(states.DELETING)
        self.env.conductor_host = self.conf.host
        self.env.save()
        self.export.status = states.SNAPSHOTTING
        self.export.save()
        self.export.status = states.UPLOADING
        self.export.save()
        self.manager.recover_stacks()
        self.assertEqual(states.DELETING, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        # A second restart must still see the original host ownership.
        self.manager.recover_stacks()
        self.manager._export_recovery_after = 0
        self.manager.reconcile_deployments()
        current = objects.Stack.get_by_id(self.ctx, self.env.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertEqual(states.ERROR, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_pending_claim_blocks_queued_create_during_cleanup(self):
        self.prepare(states.PENDING)

        def resume(server, path, body):
            with mock.patch.object(exports.ExportFlow, 'create') as create:
                self.manager.execute(self.ctx, 'create_export', self.export)
                self.assertFalse(create.called)
            return {'status': 'resumed'}

        self.agent.post.side_effect = resume
        exports.recover(self.manager)
        self.assertEqual(states.ERROR, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_unclaimed_deleting_export_is_captured(self):
        self.env.release_task(states.RECONCILING)
        self.env = objects.Stack.get_by_id(self.ctx, self.env.id)
        self.assertIsNone(self.env.conductor_host)
        self.prepare(states.DELETING)
        exports.recover(self.manager)
        self.assertEqual(states.ERROR, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_missing_guest_keeps_cleanup_pending(self):
        self.server.destroy()
        self.prepare(states.SNAPSHOTTING)
        exports.recover(self.manager)
        self.assertEqual(1, len(self.manager._export_recovery))
        self.assertEqual(states.SNAPSHOTTING, objects.Export.get_by_id(
            self.ctx, self.export.id).status)

    def test_noop_and_s3_are_not_recovered(self):
        self.env.driver = 'noop'
        self.env.save()
        exports.prepare_recovery(self.manager)
        self.assertEqual(0, len(self.manager._export_recovery))
        self.env.driver = 'coolify'
        self.env.save()
        self.export.backend = 's3'
        self.export.save()
        exports.prepare_recovery(self.manager)
        self.assertEqual(0, len(self.manager._export_recovery))

    def test_configured_s3_recovers_s3_and_skips_swift(self):
        configure_s3(self)
        exports.prepare_recovery(self.manager)
        self.assertEqual(0, len(self.manager._export_recovery))
        self.export.backend = 's3'
        self.export.save()
        deleted = []
        missing = {'value': False}
        with mock.patch.object(
                secret_store, 'call', return_value=S3_PAYLOAD), \
                mock.patch.object(
                    exports, '_s3_client',
                    side_effect=s3_client_factory(deleted, missing)):
            self.prepare(states.UPLOADING)
            exports.recover(self.manager)
        current = objects.Export.get_by_id(self.ctx, self.export.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(exports.RECOVERY_FAULT, current.fault)
        self.assertEqual(1, len(deleted))
        self.assertIn(self.volume.id + '.tar.gz', deleted[0])
        self.storage.delete.assert_not_called()
