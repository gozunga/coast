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

from unittest import mock

from coast.common import app_secrets
from coast.common import exception
from coast.common import states
from coast.conductor import manager
from coast.conductor import stack_teardown
from coast.db import api as db_api
from coast import objects
from coast.tests import app_api


class AppSecretCleanupTest(app_api.AppAPITest):
    def env(self, value):
        self.request('put', self.path + '/env',
                     {'env': {'TOKEN': {'value': value, 'secret': True}}})
        return list(app_secrets.rows(
            objects.SecretList, self.ctx, self.service.id))[0].value_ref

    def deployment_with(self, **spec):
        row = objects.Deployment(
            context=self.ctx, service_id=self.service.id,
            server_id=self.server.id, spec=spec, trigger='api',
            status=states.SUPERSEDED)
        row.create()
        return row

    def inventory(self):
        old_env = self.env('old')
        old_compose = self.store.put(self.ctx, 'old compose')
        old_file = self.store.put(self.ctx, 'old mount')
        self.deployment_with(
            env={'TOKEN': {'value_ref': old_env}}, compose_ref=old_compose,
            file_mounts=[{'content_ref': old_file}])
        self.env('new')
        self.service.compose_ref = self.store.put(self.ctx, 'new compose')
        current_file = self.store.put(self.ctx, 'new mount')
        objects.FileMount(
            context=self.ctx, service_id=self.service.id,
            container_path='/etc/app.conf', content_ref=current_file).create()
        self.service.file_mount_secret_refs = [old_file, current_file]
        self.service.save()
        return set(self.store.values)

    def test_stack_delete_includes_snapshot_only_versions_and_compose(self):
        expected = self.inventory()
        self.stack.ssh_key_ref = self.store.put(self.ctx, 'stack key')
        self.stack.save()
        expected.add(self.stack.ssh_key_ref)
        self.assertEqual(expected, set(stack_teardown._secret_refs(
            self.ctx, self.stack)))
        clients = mock.Mock(secrets=self.store)
        stack_teardown._secrets(self.ctx, self.stack, clients)
        self.assertEqual({}, self.store.values)

    def delete_app(self):
        owner = mock.Mock()
        owner._committed.return_value = False
        owner._save.side_effect = lambda row, **values: self.save(row, values)
        with mock.patch.object(manager.managed_dns, 'delete_service'), \
                mock.patch.object(manager.backups, 'drop_service'), \
                mock.patch.object(manager.storage_rules,
                                  'drop_service_storages'):
            manager.Manager._delete_service(
                owner, self.ctx, self.service, self.stack, mock.Mock(), {},
                None)

    def save(self, row, values):
        for key, value in values.items():
            setattr(row, key, value)
        row.save()

    def test_app_delete_all_versions_and_partial_failure_retry(self):
        expected = self.inventory()
        original = self.store.delete
        deleted = set()

        def fail_partial(ctx, ref):
            if deleted:
                raise RuntimeError('unavailable')
            original(ctx, ref)
            deleted.add(ref)

        with mock.patch.object(self.store, 'delete', side_effect=fail_partial):
            self.assertRaises(exception.CoastException, self.delete_app)
        self.assertEqual(expected, set(app_secrets.references(
            self.ctx, self.service)))
        # Barbican delete(ignore_missing=True) allows the already deleted ref.
        with mock.patch.object(
                self.store, 'delete', side_effect=lambda ctx, ref:
                self.store.values.pop(ref, None)):
            self.delete_app()
        self.assertEqual({}, self.store.values)

    def test_env_prunes_unreferenced_but_keeps_rollback_target(self):
        first = self.env('first')
        retained = self.deployment_with(env={'TOKEN': {'value_ref': first}})
        second = self.env('second')
        third = self.env('third')
        self.assertEqual({first, third}, set(self.store.values))
        self.assertNotIn(second, self.store.values)
        retained.destroy()
        self.assertEqual({third}, set(self.store.values))

    def test_compose_and_mount_prune_after_last_deployment_is_retired(self):
        first = self.store.put(self.ctx, 'compose')
        mount = self.store.put(self.ctx, 'file')
        self.service.compose_ref = first
        self.service.file_mount_secret_refs = [mount]
        retained = self.deployment_with(
            compose_ref=first, file_mounts=[{'content_ref': mount}])
        self.service.save()
        next_ref = self.store.put(self.ctx, 'next')
        self.service.compose_ref = next_ref
        self.service.save()
        self.assertEqual({first, mount, next_ref}, set(self.store.values))
        retained.destroy()
        self.assertEqual({next_ref}, set(self.store.values))
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual([], service.file_mount_secret_refs)

    def test_rollback_and_outer_transaction_do_not_delete_early(self):
        first = self.env('first')
        row = list(app_secrets.rows(
            objects.SecretList, self.ctx, self.service.id))[0]
        second = self.store.put(self.ctx, 'second')
        with self.assertRaises(RuntimeError):
            with db_api.writer.using(self.ctx):
                row.value_ref = second
                row.save()
                self.assertIn(first, self.store.values)
                raise RuntimeError('rollback')
        self.assertIn(first, self.store.values)
        row = objects.Secret.get_by_id(self.ctx, row.id)
        self.assertEqual(first, row.value_ref)
        with db_api.writer.using(self.ctx):
            row.value_ref = second
            row.save()
            self.assertIn(first, self.store.values)
        self.assertNotIn(first, self.store.values)

    def test_prune_failure_leaves_orphan_and_committed_new_reference(self):
        first = self.env('first')
        with mock.patch.object(self.store, 'delete', side_effect=RuntimeError):
            second = self.env('second')
        self.assertEqual({first, second}, set(self.store.values))

    def test_stack_secrets_retry_after_partial_failure(self):
        expected = self.inventory()
        deleted = set()

        def partial(ctx, ref):
            if deleted:
                raise RuntimeError('unavailable')
            self.store.values.pop(ref, None)
            deleted.add(ref)

        clients = mock.Mock()
        clients.secrets.call.side_effect = (
            lambda op, ctx, ref: partial(ctx, ref))
        self.assertRaises(RuntimeError, stack_teardown._secrets,
                          self.ctx, self.stack, clients)
        self.assertEqual(expected, set(stack_teardown._secret_refs(
            self.ctx, self.stack)))
        clients.secrets.call.side_effect = lambda op, ctx, ref: (
            self.store.values.pop(ref, None))
        stack_teardown._secrets(self.ctx, self.stack, clients)
        self.assertEqual({}, self.store.values)

    def test_savepoint_commit_and_rollback_wait_for_outer_commit(self):
        first = self.env('first')
        row = list(app_secrets.rows(
            objects.SecretList, self.ctx, self.service.id))[0]
        second = self.store.put(self.ctx, 'second')
        with self.assertRaises(RuntimeError):
            with db_api.writer.using(self.ctx) as session:
                with session.begin_nested():
                    row.value_ref = second
                    row.save()
                self.assertIn(first, self.store.values)
                raise RuntimeError('outer rollback')
        self.assertIn(first, self.store.values)
        with db_api.writer.using(self.ctx) as session:
            with self.assertRaises(RuntimeError):
                with session.begin_nested():
                    row = objects.Secret.get_by_id(self.ctx, row.id)
                    row.value_ref = second
                    row.save()
                    raise RuntimeError('savepoint rollback')
        self.assertIn(first, self.store.values)
        self.assertEqual(first, objects.Secret.get_by_id(
            self.ctx, row.id).value_ref)

    def test_env_removal_prunes_only_after_last_rollback_reference(self):
        first = self.env('first')
        retained = self.deployment_with(env={'TOKEN': {'value_ref': first}})
        self.request('put', self.path + '/env', {'env': {}})
        self.assertIn(first, self.store.values)
        retained.destroy()
        self.assertNotIn(first, self.store.values)

    def test_compose_replacement_prunes_unsnapshotted_content(self):
        first = self.store.put(self.ctx, 'first')
        self.service.compose_ref = first
        self.service.save()
        second = self.store.put(self.ctx, 'second')
        self.service.compose_ref = second
        self.service.save()
        self.assertEqual({second}, set(self.store.values))

    def test_pruning_protects_source_and_rotation_checkpoint_refs(self):
        refs = [self.store.put(self.ctx, value)
                for value in ('live', 'replacement', 'previous')]
        self.source.credentials_ref = refs[0]
        self.source.deploy_key_rotation = {
            'reference': refs[1], 'previous': refs[2], 'phase': 'rollback'}
        self.source.save()
        for ref in refs:
            retained = self.deployment_with(compose_ref=ref)
            retained.destroy()
            self.assertIn(ref, self.store.values)
