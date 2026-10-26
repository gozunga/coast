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

"""Quota accounting, project locks and public API regression tests."""

import concurrent.futures
import tempfile
import threading
from unittest import mock

import fixtures
from oslo_db import exception as db_exception
from oslo_db.sqlalchemy import enginefacade
from oslo_policy import policy as policy_module

from coast.api import handlers
from coast.common import context
from coast.common import exception
from coast.common import policy
from coast.common import quota
from coast.common import states
from coast.db import api as db_api
from coast.db import base_models
from coast.db import models
from coast import objects
from coast.tests import app_api
from coast.tests import base


class QuotaEngineTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.useFixture(fixtures.MonkeyPatch(
            'oslo_config.cfg.CONF', self.conf))
        self.useFixture(fixtures.MonkeyPatch(
            'coast.db.api.CONF', self.conf))
        self.use_facade(self.db.facade)
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(project_id='project')
        self.other = context.RequestContext(project_id='other')
        self.admin = context.RequestContext(
            project_id='project', is_admin=True)

    def use_facade(self, facade):
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(facade, name)))

    def usage(self, ctx=None):
        ctx = ctx or self.ctx
        with db_api.reader.using(ctx):
            return ctx.session.query(models.QuotaUsage).filter_by(
                project_id=ctx.project_id).one()

    def test_reserve_commit_rollback_and_release_all_resources(self):
        quota.update(self.admin, 'project', dict.fromkeys(quota.RESOURCES, 5))
        delta = dict.fromkeys(quota.RESOURCES, 2)
        first = quota.reserve(self.ctx, **delta)
        second = quota.reserve(self.ctx, **delta)
        for name in quota.RESOURCES:
            self.assertEqual(4, getattr(self.usage(), 'reserved_' + name))
            self.assertEqual(0, getattr(self.usage(), 'in_use_' + name))
        quota.commit(self.ctx, first)
        quota.rollback(self.ctx, second)
        for name in quota.RESOURCES:
            self.assertEqual(0, getattr(self.usage(), 'reserved_' + name))
            self.assertEqual(2, getattr(self.usage(), 'in_use_' + name))
        quota.release(self.ctx, **first)
        for name in quota.RESOURCES:
            self.assertEqual(0, getattr(self.usage(), 'in_use_' + name))

    def test_nested_reservation_defers_deadlock_to_owner(self):
        outer_attempts = []
        inner_attempts = []
        original = db_api._lock_project

        def contend(*args, **kwargs):
            inner_attempts.append(len(outer_attempts))
            if len(inner_attempts) == 1:
                raise db_exception.DBDeadlock()
            return original(*args, **kwargs)

        @db_api.retry_transaction
        def operation(ctx):
            outer_attempts.append(True)
            with db_api.writer.using(ctx):
                quota.reserve(ctx, apps=1)

        with mock.patch.object(db_api, '_lock_project', side_effect=contend):
            operation(self.ctx)
        self.assertEqual([1, 2], inner_attempts)
        self.assertEqual(1, self.usage().reserved_apps)

    def test_top_level_reservation_retries_deadlock(self):
        original = db_api._lock_project
        attempts = []

        def contend(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise db_exception.DBDeadlock()
            return original(*args, **kwargs)

        with mock.patch.object(db_api, '_lock_project', side_effect=contend):
            quota.reserve(self.ctx, apps=1)
        self.assertEqual(2, len(attempts))
        self.assertEqual(1, self.usage().reserved_apps)

    def test_nested_quota_writes_defer_deadlock_to_owner(self):
        for method in ('update', 'reset', 'commit', 'rollback', 'release',
                       'reconcile', 'release_exact_failed_volume_gb',
                       'lower_volume_gb_to_live'):
            with self.subTest(method=method):
                self._check_quota_write_retry(method, nested=True)

    def test_top_level_quota_writes_retry_deadlock(self):
        for method in ('update', 'reset', 'commit', 'rollback', 'release',
                       'reconcile', 'release_exact_failed_volume_gb',
                       'lower_volume_gb_to_live'):
            with self.subTest(method=method):
                self._check_quota_write_retry(method, nested=False)

    def _check_quota_write_retry(self, method, nested):
        ctx = context.RequestContext(project_id=method, is_admin=True)
        with db_api.writer.using(ctx) as session:
            session.add(models.Quota(project_id=method, apps=5))
            session.add(models.QuotaUsage(
                project_id=method, in_use_apps=2,
                reserved_apps=0 if method == 'reconcile' else 2,
                in_use_volume_gb=2, reserved_volume_gb=1))
        outer_attempts = []
        inner_attempts = []
        original = db_api._lock_project

        def contend(*args, **kwargs):
            inner_attempts.append(len(outer_attempts))
            if len(inner_attempts) == 1:
                raise db_exception.DBDeadlock()
            return original(*args, **kwargs)

        def write(ctx):
            if method == 'update':
                return quota.update(ctx, method, {'apps': 7})
            if method == 'reset':
                return quota.reset(ctx, method)
            if method in ('commit', 'rollback', 'release'):
                if method == 'release':
                    return quota.release(ctx, apps=1)
                return getattr(quota, method)(ctx, {'apps': 1})
            if method == 'release_exact_failed_volume_gb':
                return quota.release_exact_failed_volume_gb(ctx, 2)
            return getattr(quota, method)(ctx)

        @db_api.retry_transaction
        def operation(ctx):
            outer_attempts.append(True)
            with db_api.writer.using(ctx):
                db_api.sources.create(ctx, {'type': 'image'})
                return write(ctx)

        with mock.patch.object(db_api, '_lock_project', side_effect=contend):
            result = operation(ctx) if nested else write(ctx)
        # Exact-volume release locks again through the nested release helper.
        self.assertEqual([1, 2] if nested else [0, 0], inner_attempts[:2])
        self.assertEqual(2 if nested else 0, len(outer_attempts))
        self.assertEqual(int(nested), len(db_api.sources.list(ctx)))
        usage = quota.usage(ctx)
        if method == 'update':
            self.assertEqual(7, quota.get(ctx)['apps'])
        elif method == 'reset':
            self.assertEqual(quota.defaults()['apps'], quota.get(ctx)['apps'])
            with db_api.reader.using(ctx) as session:
                self.assertIsNone(session.get(models.Quota, method))
        elif method in ('commit', 'rollback'):
            self.assertEqual(1, usage['reserved']['apps'])
            self.assertEqual(3 if method == 'commit' else 2,
                             usage['in_use']['apps'])
        elif method == 'release':
            self.assertEqual(1, usage['in_use']['apps'])
            self.assertEqual(2, usage['reserved']['apps'])
        elif method == 'reconcile':
            self.assertEqual(0, usage['in_use']['apps'])
        else:
            self.assertTrue(result)
            self.assertEqual(0, usage['in_use']['volume_gb'])
            self.assertEqual(1, usage['reserved']['volume_gb'])

    def test_limits_defaults_null_zero_unlimited_and_lowering(self):
        self.config.config(apps=2, group='quotas')
        self.assertEqual(2, quota.get(self.ctx)['apps'])
        quota.update(self.admin, 'project', {'apps': 0})
        self.assertRaises(exception.OverQuota, quota.reserve, self.ctx, apps=1)
        quota.update(self.admin, 'project', {'apps': -1})
        quota.reserve(self.ctx, apps=100)
        quota.update(self.admin, 'project', {'apps': None})
        self.assertEqual(2, quota.get(self.ctx)['apps'])
        self.assertRaises(exception.OverQuota, quota.reserve, self.ctx, apps=1)
        self.assertEqual(100, self.usage().reserved_apps)

    def test_multiresource_overquota_has_no_partial_reservation(self):
        quota.update(self.admin, 'project', {'apps': 1, 'domains': 0})
        with self.assertRaises(exception.OverQuota) as raised:
            quota.reserve(self.ctx, apps=2, domains=1, servers=1)
        self.assertEqual("Quota exceeded for resources: ['apps', 'domains']",
                         str(raised.exception))
        self.assertEqual(0, self.usage().reserved_servers)
        self.assertEqual(0, self.usage().reserved_apps)

    def test_resource_transaction_failure_rolls_back_reservation(self):
        with self.assertRaises(exception.Invalid):
            with db_api.writer.using(self.ctx):
                quota.reserve(self.ctx, environments=1)
                db_api.stacks.create(self.ctx, {})
        with db_api.reader.using(self.ctx):
            self.assertEqual(0, self.ctx.session.query(
                models.QuotaUsage).count())
        self.assertEqual({'environments': 1}, quota.reserve(
            self.ctx, environments=1))

    def test_completion_underflow_is_atomic(self):
        quota.reserve(self.ctx, apps=1, domains=1)
        for complete in (quota.commit, quota.rollback):
            self.assertRaises(exception.Conflict, complete, self.ctx,
                              {'apps': 1, 'domains': 2})
            self.assertEqual(1, self.usage().reserved_apps)
        quota.commit(self.ctx, {'apps': 1, 'domains': 1})
        self.assertRaises(exception.Conflict, quota.commit,
                          self.ctx, {'apps': 1})
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          apps=1, domains=2)
        self.assertEqual(1, self.usage().in_use_apps)

    def test_project_isolation_limits_and_completion(self):
        quota.update(self.admin, 'other', {'apps': 0})
        quota.reserve(self.ctx, apps=1)
        self.assertRaises(exception.OverQuota, quota.reserve,
                          self.other, apps=1)
        self.assertRaises(exception.Conflict, quota.rollback,
                          self.other, {'apps': 1})
        self.assertEqual(1, self.usage().reserved_apps)
        self.assertRaises(exception.NotAuthorized, quota.get,
                          self.ctx, 'other')
        for method in (quota.update, quota.reset):
            args = ({'apps': 1},) if method is quota.update else ()
            self.assertRaises(exception.NotAuthorized, method, self.ctx,
                              'project', *args)
        self.assertRaises(exception.NotAuthorized, quota.get_all, self.ctx)
        self.assertRaises(exception.NotAuthorized, quota.reserve,
                          context.RequestContext(), apps=1)

    def test_reset_preserves_usage_sentinel_and_defaults(self):
        quota.update(self.admin, 'project', {'apps': 5})
        quota.reserve(self.ctx, apps=1)
        before = self.usage().created_at
        quota.reset(self.admin, 'project')
        quota.reset(self.admin, 'project')
        self.assertEqual(before, self.usage().created_at)
        self.assertEqual(1, self.usage().reserved_apps)
        self.assertEqual(10, quota.get(self.ctx)['apps'])
        self.assertEqual([], quota.get_all(self.admin))

    def test_invalid_reservations_and_limits(self):
        for value in (-1, True, None, 1.5, '1'):
            self.assertRaises(exception.Invalid, quota.reserve,
                              self.ctx, apps=value)
        self.assertRaises(exception.Invalid, quota.reserve,
                          self.ctx, unknown=1)
        for value in (-2, True, 1.5, '1'):
            self.assertRaises(exception.Invalid, quota.update,
                              self.admin, 'project', {'apps': value})
        self.assertEqual({}, quota.reserve(self.ctx, apps=0))

    def test_existing_sentinel_counts_live_rows(self):
        env = db_api.stacks.create(self.ctx, dict(
            name='old', plan='small', driver='noop', engine_version='test',
            status=states.ACTIVE, task_state=states.NONE))
        db_api.servers.create(self.ctx, dict(
            stack_id=env.id, name='control', kind='vm', role='control',
            status=states.ACTIVE, task_state=states.NONE))
        db_api.servers.create(self.ctx, dict(
            stack_id=env.id, name='build', kind='baremetal',
            role='build',
            status=states.ACTIVE, task_state=states.NONE))
        db_api.volumes.create(self.ctx, dict(
            stack_id=env.id, name='data', size_gb=10,
            purpose='data', status=states.DELETING))
        deleted = db_api.stacks.create(self.ctx, dict(
            name='deleted', plan='small', driver='noop', engine_version='test',
            status=states.PENDING))
        db_api.stacks.delete(self.ctx, deleted.id)
        quota.update(self.admin, 'project', dict.fromkeys(quota.RESOURCES, 20))
        quota.reserve(self.ctx, environments=1, servers=1, build_servers=1,
                      baremetal_servers=1, volume_gb=1)
        usage = self.usage()
        self.assertEqual(1, usage.in_use_environments)
        self.assertEqual(1, usage.in_use_servers)
        self.assertEqual(1, usage.in_use_build_servers)
        self.assertEqual(1, usage.in_use_baremetal_servers)
        self.assertEqual(10, usage.in_use_volume_gb)
        quota.reserve(self.ctx, servers=1)
        self.assertEqual(1, self.usage().in_use_servers)
        self.assertEqual(2, self.usage().reserved_servers)

    def test_known_stack_and_server_deltas(self):
        self.config.config(data_volume_size_gb=50, group='controller_worker')
        self.assertEqual(dict(environments=1, servers=3, baremetal_servers=1,
                              build_servers=1, volume_gb=50), quota.deltas(
            'stack', {'workers': [{'kind': 'vm'}, {'kind': 'baremetal'}],
                      'build_server': True}))
        self.assertEqual(dict(servers=0, baremetal_servers=0, build_servers=0),
                         quota.deltas('server', {'role': 'control',
                                                 'kind': 'baremetal'}))

    def test_concurrent_first_insert_and_existing_sentinel(self):
        self.config.config(apps=1, group='quotas')
        with tempfile.TemporaryDirectory() as directory:
            facade = enginefacade.transaction_context()
            facade.configure(connection='sqlite:///' + directory + '/db',
                             sqlite_fk=True)
            engine = facade.writer.get_engine()
            self.addCleanup(engine.dispose)
            self.use_facade(facade)
            base_models.Base.metadata.create_all(engine)
            for existing in (False, True):
                barrier = threading.Barrier(2)

                def run():
                    ctx = context.RequestContext(project_id='race')
                    barrier.wait(timeout=10)
                    try:
                        quota.reserve(ctx, apps=1)
                        return 'reserved'
                    except exception.OverQuota:
                        return 'overquota'

                with concurrent.futures.ThreadPoolExecutor(2) as executor:
                    futures = [executor.submit(run) for unused in range(2)]
                    results = [future.result(timeout=15) for future in futures]
                self.assertEqual(['overquota', 'reserved'], sorted(results))
                ctx = context.RequestContext(project_id='race')
                self.assertEqual(1, self.usage(ctx).reserved_apps)
                quota.rollback(ctx, {'apps': 1})
                self.assertEqual(0, self.usage(ctx).reserved_apps)

    def _stack(self, name, status, task):
        stack = objects.Stack(
            context=self.ctx, name=name, plan='small', driver='noop',
            engine_version='test', status=status, task_state=task)
        stack.create()
        return stack

    def test_receiptless_recovery_preserves_committed_capacity(self):
        self.config.config(environments=5, servers=10, group='quotas')
        held = quota.reserve(self.ctx, environments=1, servers=1, volume_gb=5)
        quota.commit(self.ctx, held)
        self._stack('kept', states.ACTIVE, states.NONE)
        quota.reserve(self.ctx, environments=1, servers=2, volume_gb=37)
        orphan = self._stack('orphan', states.PENDING, states.PROVISIONING)
        self.assertTrue(handlers.abandon_stack_create(self.ctx, orphan.id))
        self.assertFalse(handlers.abandon_stack_create(self.ctx, orphan.id))
        usage = quota.usage(self.ctx)
        self.assertEqual(1, usage['in_use']['environments'])
        self.assertEqual(1, usage['in_use']['servers'])
        self.assertEqual(5, usage['in_use']['volume_gb'])
        self.assertFalse(any(usage['reserved'].values()))

    def test_receiptless_recovery_keeps_pending_server_reservation(self):
        self.config.config(environments=5, servers=10, group='quotas')
        quota.reserve(self.ctx, environments=1, servers=3, volume_gb=37)
        orphan = self._stack('orphan', states.PENDING, states.PROVISIONING)
        parent = self._stack('kept', states.ACTIVE, states.NONE)
        objects.Server(
            context=self.ctx, stack_id=parent.id, name='worker', kind='vm',
            role='worker', status=states.PENDING,
            task_state=states.PROVISIONING).create()
        before = quota.usage(self.ctx)
        self.assertRaises(exception.Conflict, handlers.abandon_stack_create,
                          self.ctx, orphan.id)
        self.assertEqual(before, quota.usage(self.ctx))

    def test_receiptless_recovery_keeps_creating_volume_reservation(self):
        self.config.config(environments=5, servers=10, group='quotas')
        quota.reserve(self.ctx, environments=1, servers=3, volume_gb=37)
        orphan = self._stack('orphan', states.PENDING, states.PROVISIONING)
        parent = self._stack('kept', states.ACTIVE, states.NONE)
        objects.Volume(context=self.ctx, stack_id=parent.id, name='data',
                       size_gb=5, purpose='data',
                       status=states.CREATING).create()
        before = quota.usage(self.ctx)
        self.assertRaises(exception.Conflict, handlers.abandon_stack_create,
                          self.ctx, orphan.id)
        self.assertEqual(before, quota.usage(self.ctx))

    def test_reconcile_lowers_orphaned_usage_once(self):
        quota.reserve(self.ctx, volume_gb=112, servers=2, build_servers=1,
                      environments=1)
        quota.commit(self.ctx, dict(volume_gb=112, servers=2,
                                    build_servers=1, environments=1))
        kept = self._stack('kept', states.ACTIVE, states.NONE)
        objects.Volume(
            context=self.ctx, stack_id=kept.id, name='kept', size_gb=12,
            purpose='data', status=states.AVAILABLE).create()
        objects.Server(
            context=self.ctx, stack_id=kept.id, name='worker', kind='vm',
            role='worker', status=states.ACTIVE,
            task_state=states.NONE).create()
        quota.reconcile(self.ctx)
        usage = self.usage()
        self.assertEqual(12, usage.in_use_volume_gb)
        self.assertEqual(1, usage.in_use_servers)
        self.assertEqual(0, usage.in_use_build_servers)
        self.assertEqual(1, usage.in_use_environments)
        quota.reconcile(self.ctx)
        self.assertEqual(12, self.usage().in_use_volume_gb)
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=13)

    def test_reconcile_skips_reservations_and_in_flight_creates(self):
        quota.reserve(self.ctx, volume_gb=40, servers=1)
        quota.commit(self.ctx, {'volume_gb': 40, 'servers': 1})
        quota.reserve(self.ctx, volume_gb=1)
        building = self._stack('building', states.BUILDING,
                               states.PROVISIONING)
        quota.reconcile(self.ctx)
        self.assertEqual(40, self.usage().in_use_volume_gb)
        self.assertEqual(1, self.usage().reserved_volume_gb)
        building.status = states.ERROR
        building.save()
        building.release_task(states.PROVISIONING)
        quota.reconcile(self.ctx)
        # The volume reservation is still held, so volume_gb stays.
        self.assertEqual(40, self.usage().in_use_volume_gb)
        self.assertEqual(0, self.usage().in_use_servers)
        quota.rollback(self.ctx, {'volume_gb': 1})
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage().in_use_volume_gb)
        quota.reconcile(self.ctx)
        self.assertEqual(0, self.usage().in_use_volume_gb)

    def test_release_exact_failed_volume_gb_matches_once(self):
        self.assertFalse(quota.release_exact_failed_volume_gb(self.ctx, 0))
        quota.reserve(self.ctx, volume_gb=50)
        quota.commit(self.ctx, {'volume_gb': 50})
        quota.reserve(self.ctx, volume_gb=1)
        self.assertTrue(quota.release_exact_failed_volume_gb(self.ctx, 50))
        self.assertEqual(0, self.usage().in_use_volume_gb)
        self.assertEqual(1, self.usage().reserved_volume_gb)
        self.assertFalse(quota.release_exact_failed_volume_gb(self.ctx, 50))
        self.assertEqual(0, self.usage().in_use_volume_gb)
        self.assertEqual(1, self.usage().reserved_volume_gb)
        self.assertRaises(exception.Conflict, quota.release, self.ctx,
                          volume_gb=1)

    def test_release_exact_failed_volume_gb_leaves_other_charges(self):
        quota.reserve(self.ctx, volume_gb=40)
        quota.commit(self.ctx, {'volume_gb': 40})
        self.assertFalse(quota.release_exact_failed_volume_gb(self.ctx, 40))
        self.assertEqual(40, self.usage().in_use_volume_gb)
        quota.reserve(self.ctx, volume_gb=1)
        self.assertFalse(quota.release_exact_failed_volume_gb(self.ctx, 50))
        self.assertEqual(40, self.usage().in_use_volume_gb)
        self.assertEqual(1, self.usage().reserved_volume_gb)
        building = self._stack(
            'building', states.BUILDING, states.PROVISIONING)
        self.assertFalse(quota.release_exact_failed_volume_gb(self.ctx, 40))
        self.assertEqual(40, self.usage().in_use_volume_gb)
        building.status = states.ERROR
        building.save()
        building.release_task(states.PROVISIONING)
        volume = objects.Volume(
            context=self.ctx, stack_id=building.id, name='kept', size_gb=40,
            purpose='data', status=states.AVAILABLE)
        volume.create()
        self.assertFalse(quota.release_exact_failed_volume_gb(self.ctx, 40))
        self.assertEqual(40, self.usage().in_use_volume_gb)
        objects.Event(
            context=self.ctx, stack_id=building.id, resource_type='volume',
            resource_id=volume.id, action='create', outcome='failure',
            message='failed').create()
        self.assertTrue(quota.release_exact_failed_volume_gb(self.ctx, 40))
        self.assertEqual(0, self.usage().in_use_volume_gb)
        self.assertEqual(1, self.usage().reserved_volume_gb)

    def test_lower_volume_gb_to_live_keeps_reservation(self):
        quota.reserve(self.ctx, volume_gb=50)
        quota.commit(self.ctx, {'volume_gb': 50})
        quota.reserve(self.ctx, volume_gb=1)
        building = self._stack(
            'building', states.BUILDING, states.PROVISIONING)
        self.assertFalse(quota.lower_volume_gb_to_live(self.ctx))
        self.assertEqual(50, self.usage().in_use_volume_gb)
        self.assertEqual(1, self.usage().reserved_volume_gb)
        building.status = states.ERROR
        building.save()
        building.release_task(states.PROVISIONING)
        kept = self._stack('kept', states.ACTIVE, states.NONE)
        objects.Volume(
            context=self.ctx, stack_id=kept.id, name='kept', size_gb=12,
            purpose='data', status=states.AVAILABLE).create()
        self.assertTrue(quota.lower_volume_gb_to_live(self.ctx))
        self.assertEqual(12, self.usage().in_use_volume_gb)
        self.assertEqual(1, self.usage().reserved_volume_gb)
        self.assertFalse(quota.lower_volume_gb_to_live(self.ctx))
        self.assertEqual(12, self.usage().in_use_volume_gb)
        self.assertEqual(1, self.usage().reserved_volume_gb)


class QuotaControllerTest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.admin_headers = dict(self.headers, **{'X-Roles': 'admin'})
        self.admin = context.RequestContext(
            project_id='project', is_admin=True)
        self.quota_path = '/v1/quotas/project'
        self.env_path = '/v1/stacks/' + self.stack.id
        self.volume = objects.Volume(
            context=self.ctx, stack_id=self.stack.id,
            name='data', size_gb=10, purpose='data', status=states.AVAILABLE)
        self.volume.create()
        self.volume_path = self.env_path + '/volumes/' + self.volume.id

    def usage(self):
        with db_api.reader.using(self.ctx):
            return self.ctx.session.query(models.QuotaUsage).filter_by(
                project_id='project').one()

    def zero_usage(self):
        return {part: dict.fromkeys(quota.RESOURCES, 0)
                for part in ('in_use', 'reserved')}

    def test_show_defaults_admin_update_list_and_delete(self):
        expected = quota.defaults()
        shown = self.request('get', self.quota_path).json
        self.assertEqual(expected, shown['quota'])
        self.assertEqual(self.zero_usage(), shown['usage'])
        self.assertEqual({'quota': expected},
                         self.request('get', '/v1/quotas/defaults').json)
        self.assertEqual({'quotas': []}, self.request(
            'get', '/v1/quotas', headers=self.admin_headers).json)
        result = self.request('put', self.quota_path,
                              {'quota': {'apps': 2, 'domains': -1}},
                              headers=self.admin_headers).json
        self.assertEqual(2, result['quota']['apps'])
        self.assertEqual(-1, result['quota']['domains'])
        self.assertNotIn('usage', result)
        self.assertEqual(set(quota.RESOURCES), set(result['quota']))
        self.request('put', self.quota_path, {'quota': {'apps': None}},
                     headers=self.admin_headers)
        self.assertEqual(10, self.request('get', self.quota_path).json[
            'quota']['apps'])
        rows = self.request('get', '/v1/quotas', headers=self.admin_headers
                            ).json['quotas']
        self.assertEqual('project', rows[0]['project_id'])
        self.assertIsNone(rows[0]['apps'])
        self.assertEqual(-1, rows[0]['domains'])
        self.assertEqual(set(quota.RESOURCES) | {'project_id'}, set(rows[0]))
        quota.reserve(self.ctx, apps=1)
        for unused in range(2):
            response = self.request('delete', self.quota_path,
                                    headers=self.admin_headers, status=204)
            self.assertEqual(b'', response.body)
        shown = self.request('get', self.quota_path).json
        self.assertEqual(expected, shown['quota'])
        self.assertEqual(1, shown['usage']['reserved']['apps'])
        self.assertEqual(1, self.usage().reserved_apps)

    def test_show_usage_zero_counted_and_other_project(self):
        self.assertEqual(self.zero_usage(),
                         self.request('get', self.quota_path).json['usage'])

        quota.update(self.admin, 'project',
                     {'build_servers': 5, 'baremetal_servers': 5})
        quota.reserve(self.ctx, build_servers=2, baremetal_servers=1)
        quota.commit(self.ctx, {'build_servers': 1, 'baremetal_servers': 1})
        usage = self.request('get', self.quota_path).json['usage']
        self.assertEqual(1, usage['in_use']['build_servers'])
        self.assertEqual(1, usage['reserved']['build_servers'])
        self.assertEqual(1, usage['in_use']['baremetal_servers'])
        self.assertEqual(0, usage['reserved']['baremetal_servers'])
        self.assertEqual(0, usage['in_use']['apps'])
        self.assertEqual(0, usage['in_use']['volume_gb'])

        other = context.RequestContext(project_id='other', is_admin=True)
        quota.reserve(other, apps=3, volume_gb=8)
        quota.commit(other, {'apps': 3, 'volume_gb': 8})
        again = self.request('get', self.quota_path).json['usage']
        self.assertEqual(usage, again)

        foreign = self.request('get', '/v1/quotas/other',
                               headers=self.admin_headers).json['usage']
        self.assertEqual(3, foreign['in_use']['apps'])
        self.assertEqual(8, foreign['in_use']['volume_gb'])
        self.assertEqual(0, foreign['in_use']['build_servers'])
        self.assertEqual(0, foreign['reserved']['apps'])
        self.assertEqual(0, foreign['in_use']['servers'])

    def endpoints(self):
        return [('get', '/v1/quotas', None),
                ('get', '/v1/quotas/defaults', None),
                ('get', self.quota_path, None),
                ('put', self.quota_path, {'quota': {'apps': 1}}),
                ('delete', self.quota_path, None)]

    def test_authentication_role_and_microversion_every_quota_route(self):
        for method, path, body in self.endpoints():
            self.request(method, path, body, headers={}, status=401)
            self.request(method, path, body, headers=dict(
                self.headers, **{'X-Roles': 'unrelated'}), status=403)
            self.client.request(path, method=method.upper(), headers=dict(
                self.admin_headers,
                **{'OpenStack-API-Version': 'app-hosting 1.99'}), status=406)
        for method, path, body in self.endpoints():
            if (path != '/v1/quotas/defaults' and
                    (method != 'get' or path == '/v1/quotas')):
                self.request(method, path, body, status=403)

    def test_quota_foreign_project_and_admin_access(self):
        for project in ('other', 'nonexistent'):
            path = '/v1/quotas/' + project
            for method, body in (('get', None),
                                 ('put', {'quota': {'apps': 0}}),
                                 ('delete', None)):
                self.request(method, path, body, status=404)
            self.request('put', path, {'quota': {'apps': 0}},
                         headers=self.admin_headers)
            self.assertEqual(0, self.request('get', path,
                             headers=self.admin_headers).json['quota']['apps'])
        self.assertEqual(10, self.request('get', self.quota_path).json[
            'quota']['apps'])
        rows = self.request('get', '/v1/quotas', headers=self.admin_headers
                            ).json['quotas']
        self.assertEqual(['nonexistent', 'other'],
                         [r['project_id'] for r in rows])

    def test_schema_queries_and_unsupported_methods(self):
        for value in (-2, True, 1.5, '1', [], {}):
            self.request('put', self.quota_path, {'quota': {'apps': value}},
                         headers=self.admin_headers, status=400)
        for body in ({}, {'quota': None}, {'quota': {'project_id': 'other'}},
                     {'quota': {'in_use_apps': 1}}, {'quota': {'unknown': 1}},
                     {'quota': {}, 'unknown': True}):
            self.request('put', self.quota_path, body,
                         headers=self.admin_headers, status=400)
        self.request('delete', self.quota_path, {'quota': {}},
                     headers=self.admin_headers, status=400)
        for method, path, body in self.endpoints():
            self.request(method, path + '?unknown=1', body,
                         headers=self.admin_headers, status=400)
        for method, path in (('post', '/v1/quotas'),
                             ('put', '/v1/quotas/defaults'),
                             ('patch', self.quota_path)):
            self.request(method, path, {'quota': {}},
                         headers=self.admin_headers, status=405)

    def test_overquota_403_on_every_create_hook_and_volume_extend(self):
        cases = [
            ('environments', 'post', '/v1/stacks',
             {'stack': {'name': 'new', 'plan': 'small'}}),
            ('servers', 'post', self.env_path + '/servers', {'server': {
                'name': 'new', 'kind': 'vm', 'role': 'worker',
                'plan': 'small'}}),
            ('baremetal_servers', 'post', self.env_path + '/servers',
             {'server': {
                 'name': 'new', 'kind': 'baremetal', 'role': 'worker',
                 'plan': 'small'}}),
            ('build_servers', 'post', self.env_path + '/servers', {'server': {
                'name': 'new', 'kind': 'vm', 'role': 'build',
                'plan': 'small'}}),
            ('apps', 'post', '/v1/services', self.create_body()),
            ('domains', 'post', self.path + '/domains',
             {'domain': {'fqdn': 'new.example.com'}}),
            ('volume_gb', 'post', self.env_path + '/volumes',
             {'volume': {'name': 'new', 'size_gb': 1}}),
            ('volume_gb', 'patch', self.volume_path,
             {'volume': {'size_gb': 11}})]
        self.config.config(enable_baremetal=True, group='controller_worker')
        for resource, method, path, body in cases:
            limits = dict.fromkeys(quota.RESOURCES, -1)
            limits[resource] = 0
            quota.update(self.admin, 'project', limits)
            response = self.request(method, path, body, status=403)
            self.assertEqual("Quota exceeded for resources: ['%s']" % resource,
                             response.json['faultstring'])
            usage = self.usage()
            for name in quota.RESOURCES:
                self.assertEqual(0, getattr(usage, 'reserved_' + name))
        self.assertEqual(10, objects.Volume.get_by_id(
            self.ctx, self.volume.id).size_gb)
        self.assertEqual(1, len(objects.ServiceList.get_all(self.ctx)))
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_stack_child_capacity_cannot_bypass_quotas(self):
        for resource, options in (
                ('servers', {'workers': [{'kind': 'vm', 'plan': 'small'}]}),
                ('baremetal_servers', {'workers': [
                    {'kind': 'baremetal', 'plan': 'metal'}]}),
                ('build_servers', {'build_server': True}),
                ('volume_gb', {'data_volume_gb': 20})):
            limits = dict.fromkeys(quota.RESOURCES, -1)
            limits[resource] = 0
            quota.update(self.admin, 'project', limits)
            self.config.config(enable_baremetal=True,
                               group='controller_worker')
            self.request('post', '/v1/stacks', {'stack': dict(
                name='new', plan='small', **options)}, status=403)
        self.assertEqual(1, len(objects.StackList.get_all(self.ctx)))

    def test_create_and_extend_reserve_only_new_capacity(self):
        quota.update(self.admin, 'project', {'volume_gb': 15})
        with mock.patch.object(handlers.Handler, 'extend_volume') as handler:
            self.request('patch', self.volume_path,
                         {'volume': {'size_gb': 12}}, status=202)
        ctx = handler.call_args.args[0]
        self.assertEqual({'volume_gb': 2}, ctx.quota_reservation)
        self.assertNotIn('quota_reservation', ctx.to_dict())
        self.assertEqual(10, self.usage().in_use_volume_gb)
        self.assertEqual(2, self.usage().reserved_volume_gb)
        self.request('patch', self.volume_path, {'volume': {'size_gb': 15}},
                     status=202)
        self.assertEqual(5, self.usage().reserved_volume_gb)
        self.request('patch', self.volume_path, {'volume': {'size_gb': 16}},
                     status=403)
        self.assertEqual(15, objects.Volume.get_by_id(
            self.ctx, self.volume.id).size_gb)
        self.request('patch', self.volume_path, {'volume': {'size_gb': 15}},
                     status=400)
        self.assertEqual(5, self.usage().reserved_volume_gb)

    def test_failed_create_does_not_leak_reservations(self):
        quota.update(self.admin, 'project', dict.fromkeys(quota.RESOURCES, -1))
        with mock.patch.object(db_api.services, 'create',
                               side_effect=exception.Invalid()):
            self.request('post', '/v1/services', self.create_body(),
                         status=400)
        self.assertEqual(0, self.usage().reserved_apps)
        self.assertEqual(1, len(objects.SourceList.get_all(self.ctx)))
        self.request('post', self.path + '/domains', {'domain': {
            'fqdn': self.domain.fqdn}}, status=409)
        self.assertEqual(0, self.usage().reserved_domains)
        with mock.patch.object(db_api.volumes, 'create',
                               side_effect=exception.Invalid()):
            self.request('post', self.env_path + '/volumes', {'volume': {
                'name': 'new', 'size_gb': 2}}, status=400)
        self.assertEqual(0, self.usage().reserved_volume_gb)

    def test_handoff_failure_retains_row_and_reservation(self):
        with mock.patch.object(handlers.Handler, 'create_service',
                               side_effect=exception.Conflict()):
            self.request('post', '/v1/services', self.create_body(),
                         status=409)
        self.assertEqual(1, self.usage().reserved_apps)
        self.assertEqual(2, len(objects.ServiceList.get_all(self.ctx)))

    def test_successful_hooks_hold_reservations_and_project_isolation(self):
        quota.update(self.admin, 'project', {'apps': 2})
        quota.update(self.admin, 'other', {'apps': 0})
        self.request('post', '/v1/services', self.create_body(), status=202)
        self.assertEqual(1, self.usage().in_use_apps)
        self.assertEqual(1, self.usage().reserved_apps)
        body = self.create_body()
        body['service']['name'] = 'another'
        self.request('post', '/v1/services', body, status=403)
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        self.request('post', '/v1/services', body, headers=foreign, status=404)
        self.assertEqual(1, self.usage().reserved_apps)

    def test_export_limits_share_project_lock_without_quota_columns(self):
        self.config.config(max_concurrent_per_project=1, max_size_gb=10,
                           group='export')
        path = self.env_path + '/exports'
        self.request('post', path, {'export': {}}, status=202)
        env = objects.Stack.get_by_id(self.ctx, self.stack.id)
        env.release_task(states.RECONCILING)
        self.request('post', path, {'export': {}}, status=403)
        row = objects.ExportList.get_all(self.ctx)[0]
        for status in (states.SNAPSHOTTING, states.UPLOADING, states.ACTIVE):
            row.status = status
            row.save()
        self.volume.size_gb = 11
        self.volume.save()
        self.request('post', path, {'export': {}}, status=403)
        self.assertEqual(1, len(objects.ExportList.get_all(self.ctx)))
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def test_admin_access_uses_operator_admin_policy(self):
        enforcer = policy.get_enforcer()
        enforcer.load_rules()
        enforcer.set_rules(policy_module.Rules.from_dict({
            'ADMIN': 'role:manager',
            'coast:quota:get_all': 'rule:ADMIN',
            'coast:quota:update': 'rule:ADMIN',
            'coast:quota:delete': 'rule:ADMIN'}), overwrite=False)
        self.request('put', '/v1/quotas/other', {'quota': {'apps': 0}})
        result = self.request('get', '/v1/quotas/other').json
        self.assertEqual(0, result['quota']['apps'])
        result = self.request('get', '/v1/quotas').json
        self.assertEqual('other', result['quotas'][0]['project_id'])
        self.request('get', '/v1/quotas/other', headers=self.admin_headers,
                     status=404)
        self.request('delete', '/v1/quotas/other', status=204)

    def test_successful_creates_account_known_capacity(self):
        quota.update(self.admin, 'project', dict.fromkeys(quota.RESOURCES, -1))
        self.config.config(enable_baremetal=True, group='controller_worker')
        with mock.patch.object(handlers.Handler,
                               'create_stack') as handler:
            self.request('post', '/v1/stacks', {'stack': {
                'name': 'new', 'plan': 'small', 'data_volume_gb': 20,
                'workers': [{'kind': 'baremetal', 'plan': 'metal'}],
                'build_server': True}}, status=202)
        usage = self.usage()
        held = dict(environments=1, servers=2, baremetal_servers=1,
                    build_servers=1, volume_gb=20)
        self.assertEqual(held, handler.call_args.args[0].quota_reservation)
        for name, value in held.items():
            self.assertEqual(value, getattr(usage, 'reserved_' + name))
        self.assertEqual(1, usage.in_use_environments)
        self.assertEqual(1, usage.in_use_servers)
        self.assertEqual(10, usage.in_use_volume_gb)
        self.request('post', self.env_path + '/servers', {'server': {
            'name': 'build', 'kind': 'baremetal', 'role': 'build',
            'plan': 'small'}}, status=202)
        usage = self.usage()
        self.assertEqual(3, usage.reserved_servers)
        self.assertEqual(2, usage.reserved_build_servers)
        self.assertEqual(2, usage.reserved_baremetal_servers)
        self.request('post', self.path + '/domains', {'domain': {
            'fqdn': 'new.example.com'}}, status=201)
        self.assertEqual(1, self.usage().in_use_domains)
        self.assertEqual(1, self.usage().reserved_domains)
        self.request('post', self.env_path + '/volumes', {'volume': {
            'name': 'new', 'size_gb': 5}}, status=202)
        self.assertEqual(25, self.usage().reserved_volume_gb)

    def test_concurrent_volume_extension_reserves_actual_increase(self):
        with tempfile.TemporaryDirectory() as directory:
            facade = enginefacade.transaction_context()
            facade.configure(connection='sqlite:///' + directory + '/db',
                             sqlite_fk=True)
            engine = facade.writer.get_engine()
            self.addCleanup(engine.dispose)
            for name in ('reader', 'writer'):
                self.useFixture(fixtures.MonkeyPatch(
                    'coast.db.api.' + name, getattr(facade, name)))
            base_models.Base.metadata.create_all(engine)
            env = objects.Stack(
                context=self.ctx, name='race', plan='small', driver='noop',
                engine_version='test', status=states.ACTIVE,
                task_state=states.NONE)
            env.create()
            volume = objects.Volume(
                context=self.ctx, stack_id=env.id, name='data',
                size_gb=10, purpose='data', status=states.AVAILABLE)
            volume.create()
            quota.update(self.admin, 'project', {'volume_gb': 14})
            path = '/v1/stacks/%s/volumes/%s' % (env.id, volume.id)
            barrier = threading.Barrier(2)

            def run(size):
                barrier.wait(timeout=10)
                response = self.client.patch_json(
                    path, {'volume': {'size_gb': size}},
                    headers=self.headers, expect_errors=True)
                return response.status_int

            with concurrent.futures.ThreadPoolExecutor(2) as executor:
                futures = [executor.submit(run, size) for size in (12, 14)]
                results = [future.result(timeout=15) for future in futures]
            self.assertIn(results, ([202, 202], [400, 202]))
            self.assertEqual(14, objects.Volume.get_by_id(
                self.ctx, volume.id).size_gb)
            self.assertEqual(10, self.usage().in_use_volume_gb)
            self.assertEqual(4, self.usage().reserved_volume_gb)
