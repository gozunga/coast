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

from concurrent import futures
import threading
import time
from unittest import mock
import uuid

import fixtures

from coast.common import context
from coast.common import exception
from coast.db import api
from coast.db import base_models
from coast.health_manager import keys
from coast.tests import base


class StackKeyLookupTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.owner = context.RequestContext(project_id='tenant')
        self.stack = api.stacks.create(self.owner, dict(
            id=str(uuid.uuid4()), name='stack', plan='test', driver='noop',
            engine_version='test', status='PENDING',
            heartbeat_key_ref='opaque-ref'))
        self.key = b'k' * 32
        self.lookup = keys.StackKeyLookup(self.conf)
        self.store = mock.Mock()
        self.store.get.return_value = self.key.hex()
        self.useFixture(fixtures.MonkeyPatch(
            'coast.api.secret_store.STORE', self.store))
        self.clock = self.useFixture(fixtures.MockPatchObject(
            keys.time, 'monotonic', return_value=100)).mock

    def test_cache_hit_and_service_context(self):
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.clock.return_value = 129.9
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.assertEqual(1, self.store.get.call_count)
        ctx, ref = self.store.get.call_args.args
        self.assertTrue(ctx.is_admin)
        self.assertIsNone(ctx.auth_token)
        self.assertEqual('opaque-ref', ref)

    def test_expiry_does_not_slide_on_hit(self):
        self.lookup(self.stack.id)
        self.clock.return_value = 129
        self.lookup(self.stack.id)
        self.clock.return_value = 130
        self.store.get.return_value = (b'x' * 32).hex()
        self.assertEqual(b'x' * 32, self.lookup(self.stack.id))
        self.assertEqual(2, self.store.get.call_count)

    def test_unknown_and_deleted_stack(self):
        self.assertRaises(ValueError, self.lookup, str(uuid.uuid4()))
        self.lookup(self.stack.id)
        api.stacks.delete(self.owner, self.stack.id)
        self.assertRaises(ValueError, self.lookup, self.stack.id)
        self.assertNotIn(self.stack.id, self.lookup.cache)
        self.assertEqual(1, self.store.get.call_count)

    def test_missing_ref_invalidates_cache(self):
        self.lookup(self.stack.id)
        api.stacks.update(self.owner, self.stack.id,
                          {'heartbeat_key_ref': None})
        self.assertRaises(ValueError, self.lookup, self.stack.id)
        self.assertNotIn(self.stack.id, self.lookup.cache)
        self.assertEqual(1, self.store.get.call_count)

    def test_changed_ref_fetches_new_key(self):
        self.lookup(self.stack.id)
        api.stacks.update(self.owner, self.stack.id,
                          {'heartbeat_key_ref': 'replacement'})
        self.store.get.return_value = (b'x' * 32).hex()
        self.assertEqual(b'x' * 32, self.lookup(self.stack.id))
        self.assertEqual(2, self.store.get.call_count)

    def test_invalid_payloads_are_not_cached(self):
        for payload in (None, b'k' * 32, '', 'aa' * 31, 'aa' * 33,
                        'zz' * 32, 'aa' * 31 + '  '):
            with self.subTest(payload=payload):
                self.store.get.return_value = payload
                self.assertRaises(ValueError, self.lookup, self.stack.id)
                self.assertNotIn(self.stack.id, self.lookup.cache)
        self.store.get.return_value = self.key.hex()
        self.assertEqual(self.key, self.lookup(self.stack.id))

    def test_expired_backend_failure_uses_last_good_key_and_recovers(self):
        self.lookup(self.stack.id)
        self.clock.return_value = 130
        self.store.get.side_effect = RuntimeError(self.key.hex())
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.assertIn(self.stack.id, self.lookup.cache)
        self.store.get.side_effect = None
        self.clock.return_value = 160
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.assertEqual(3, self.store.get.call_count)

    def test_db_failure_invalidates_cached_key(self):
        self.lookup(self.stack.id)
        with mock.patch.object(api.stacks, 'get', side_effect=RuntimeError):
            self.assertRaises(ValueError, self.lookup, self.stack.id)
        self.assertNotIn(self.stack.id, self.lookup.cache)

    def test_no_key_in_logs_or_public_exception(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.lookup(self.stack.id)
        self.clock.return_value = 130
        self.store.get.side_effect = RuntimeError(self.key.hex())
        self.clock.return_value = 4000
        error = self.assertRaises(ValueError, self.lookup, self.stack.id)
        self.assertIsInstance(error, keys.KeyUnavailable)
        self.assertTrue(error.__suppress_context__)
        self.assertNotIn(self.key.hex(), logs.output)
        self.assertNotIn('opaque-ref', logs.output)
        self.store.get.side_effect = None
        self.store.get.return_value = 'sensitive-invalid-key'
        self.assertRaises(ValueError, self.lookup, self.stack.id)
        self.assertNotIn('sensitive-invalid-key', logs.output)

    def test_cache_is_bounded(self):
        self.lookup.CACHE_SIZE = 1
        self.lookup(self.stack.id)
        other = api.stacks.create(self.owner, dict(
            id=str(uuid.uuid4()), name='other', plan='test', driver='noop',
            engine_version='test', status='PENDING',
            heartbeat_key_ref='other-ref'))
        self.lookup(other.id)
        self.assertEqual([other.id], list(self.lookup.cache))

    def test_stale_key_limit_does_not_slide_and_failure_recovers(self):
        self.lookup(self.stack.id)
        self.store.get.side_effect = RuntimeError('backend credentials')
        for now in (130, 3700):
            self.clock.return_value = now
            self.assertEqual(self.key, self.lookup(self.stack.id))
        self.clock.return_value = 3730
        self.assertRaises(keys.KeyUnavailable, self.lookup, self.stack.id)
        self.store.get.side_effect = None
        self.store.get.return_value = (b'x' * 32).hex()
        self.clock.return_value = 3760
        self.assertEqual(b'x' * 32, self.lookup(self.stack.id))

    def test_uncached_outage_warns_once_per_minute_and_recovers(self):
        logs = self.useFixture(fixtures.FakeLogger())
        self.store.get.side_effect = RuntimeError('backend credentials')
        for now in (100, 110, 159):
            self.clock.return_value = now
            self.assertRaises(keys.KeyUnavailable, self.lookup, self.stack.id)
        self.assertEqual(1, logs.output.count(self.stack.id))
        self.clock.return_value = 160
        self.assertRaises(keys.KeyUnavailable, self.lookup, self.stack.id)
        self.assertEqual(2, logs.output.count(self.stack.id))
        self.clock.return_value = 190
        self.store.get.side_effect = None
        self.assertEqual(self.key, self.lookup(self.stack.id))

    def test_changed_reference_never_reuses_previous_key_on_error(self):
        self.lookup(self.stack.id)
        api.stacks.update(self.owner, self.stack.id,
                          {'heartbeat_key_ref': 'replacement'})
        self.store.get.side_effect = RuntimeError('backend credentials')
        self.assertRaises(keys.KeyUnavailable, self.lookup, self.stack.id)
        self.assertNotIn(self.stack.id, self.lookup.cache)

    def test_slow_outage_bounds_wait_and_repeated_packets_then_recovers(self):
        self.lookup(self.stack.id)
        self.clock.return_value = 130
        release = threading.Event()
        entered = threading.Event()
        self.addCleanup(release.set)

        def slow_get(*args):
            entered.set()
            release.wait(2)
            release.set()
            raise RuntimeError('backend credentials')

        self.store.get.side_effect = slow_get
        started = time.perf_counter()
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.assertTrue(entered.is_set())
        # The request remains blocked while heartbeats continue using the key.
        for _ in range(50):
            self.assertEqual(self.key, self.lookup(self.stack.id))
        self.assertLess(time.perf_counter() - started, 1)
        self.assertEqual(2, self.store.get.call_count)
        pending = next(iter(self.lookup.refreshes.values()))
        release.set()
        self.assertRaises(exception.SecretStoreUnavailable,
                          pending.result, timeout=1)
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.store.get.side_effect = None
        self.store.get.return_value = (b'x' * 32).hex()
        self.clock.return_value = 159.9
        for _ in range(50):
            self.assertEqual(self.key, self.lookup(self.stack.id))
        self.assertEqual(2, self.store.get.call_count)
        self.clock.return_value = 160
        self.assertEqual(b'x' * 32, self.lookup(self.stack.id))
        self.assertEqual(3, self.store.get.call_count)

    def test_slow_uncached_lookup_defers_health_without_repeated_waits(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_get(*args):
            release.wait(2)
            return self.key.hex()

        self.store.get.side_effect = slow_get
        started = time.perf_counter()
        for _ in range(50):
            self.assertRaises(keys.KeyUnavailable, self.lookup, self.stack.id)
        self.assertLess(time.perf_counter() - started, 1)
        self.assertEqual(1, self.store.get.call_count)
        pending = next(iter(self.lookup.refreshes.values()))
        release.set()
        self.assertEqual(self.key.hex(), pending.result(timeout=1))
        self.assertEqual(self.key, self.lookup(self.stack.id))

    def test_refresh_concurrency_is_bounded(self):
        self.lookup.MAX_REFRESHES = 1
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_get(*args):
            release.wait(2)
            return self.key.hex()

        self.store.get.side_effect = slow_get
        self.assertRaises(keys.KeyUnavailable, self.lookup, self.stack.id)
        other = api.stacks.create(self.owner, dict(
            id=str(uuid.uuid4()), name='other', plan='test', driver='noop',
            engine_version='test', status='PENDING',
            heartbeat_key_ref='other-ref'))
        self.assertRaises(keys.KeyUnavailable, self.lookup, other.id)
        self.assertEqual(1, self.store.get.call_count)
        pending = next(iter(self.lookup.refreshes.values()))
        release.set()
        pending.result(timeout=1)
        self.assertEqual(self.key, self.lookup(other.id))

    def test_delayed_success_survives_another_stacks_refresh(self):
        other = api.stacks.create(self.owner, dict(
            id=str(uuid.uuid4()), name='other', plan='test', driver='noop',
            engine_version='test', status='PENDING',
            heartbeat_key_ref='other-ref'))
        release = threading.Event()
        self.addCleanup(release.set)

        def delayed_get(ctx, reference):
            if reference == 'opaque-ref':
                release.wait(2)
            return self.key.hex()

        self.store.get.side_effect = delayed_get
        self.assertRaises(keys.KeyUnavailable, self.lookup, self.stack.id)
        pending = self.lookup.refreshes[(self.stack.id, 'opaque-ref')]
        release.set()
        pending.result(timeout=1)
        self.assertEqual(self.key, self.lookup(other.id))
        # A successful refresh is consumed even before its retry deadline,
        # without a second backend request or another dropped heartbeat.
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.assertEqual(2, self.store.get.call_count)

    def test_completed_refresh_eviction_is_bounded_and_allows_refetch(self):
        self.lookup.CACHE_SIZE = 1
        self.lookup.MAX_REFRESHES = 1
        stacks = [self.stack]
        for name in ('second', 'third'):
            stacks.append(api.stacks.create(self.owner, dict(
                id=str(uuid.uuid4()), name=name, plan='test', driver='noop',
                engine_version='test', status='PENDING',
                heartbeat_key_ref=name + '-ref')))
        gates = {stack.heartbeat_key_ref: threading.Event()
                 for stack in stacks[:2]}
        for gate in gates.values():
            self.addCleanup(gate.set)

        def delayed_get(ctx, reference):
            if reference in gates:
                gates[reference].wait(2)
            return self.key.hex()

        self.store.get.side_effect = delayed_get
        for stack in stacks[:2]:
            self.assertRaises(keys.KeyUnavailable, self.lookup, stack.id)
            pending = self.lookup.refreshes[
                (stack.id, stack.heartbeat_key_ref)]
            gates[stack.heartbeat_key_ref].set()
            pending.result(timeout=1)
        self.assertEqual(2, len(self.lookup.refreshes))
        self.assertEqual(self.key, self.lookup(stacks[2].id))
        self.assertNotIn((self.stack.id, self.stack.heartbeat_key_ref),
                         self.lookup.refreshes)
        self.assertIn((stacks[1].id, stacks[1].heartbeat_key_ref),
                      self.lookup.refreshes)
        self.assertNotIn(self.stack.id, self.lookup.retries)
        self.assertEqual(self.key, self.lookup(stacks[1].id))
        self.assertEqual(3, self.store.get.call_count)
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.assertEqual(4, self.store.get.call_count)

    def test_refresh_finishing_at_wait_timeout_retains_its_result(self):
        # The backend can complete just after result() times out, before the
        # lookup examines the future. Its successful payload must survive.
        pending = futures.Future()

        def completed_after_timeout(timeout):
            pending.set_result(self.key.hex())
            raise futures.TimeoutError()

        self.lookup.refreshes[(self.stack.id, 'opaque-ref')] = pending
        with mock.patch.object(pending, 'result',
                               side_effect=completed_after_timeout):
            self.assertRaises(keys.KeyUnavailable, self.lookup, self.stack.id)
        self.assertEqual(self.key, self.lookup(self.stack.id))
        self.store.get.assert_not_called()
