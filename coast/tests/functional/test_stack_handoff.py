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

"""Failed and delayed stack handoffs preserve quota and task ownership."""

from unittest import mock

from coast.api import handlers
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import rpcapi
from coast import objects
from coast.tests import conductor


class StackHandoffTest(conductor.ConductorTest):
    body = {'stack': {'name': 'handoff', 'plan': 'small',
                      'workers': [{'kind': 'vm', 'plan': 'small'}],
                      'build_server': True, 'data_volume_gb': 10}}

    def pending(self, name='handoff'):
        with mock.patch.object(self.rpc, 'cast') as cast:
            self.request('post', '/v1/stacks',
                         {'stack': dict(self.body['stack'], name=name)},
                         status=202)
        return cast.call_args.args

    def assert_empty_quota(self):
        usage = quota.usage(self.ctx)
        for counters in usage.values():
            self.assertFalse(any(counters.values()), usage)

    def test_failed_cast_releases_quota_and_allows_delete(self):
        with mock.patch.object(self.rpc, 'cast', side_effect=(
                exception.CoastException('Conductor handoff failed.'))):
            self.request('post', '/v1/stacks', self.body, status=500)
        stack = objects.StackList.get_all(self.ctx)[0]
        self.assertEqual(states.ERROR, stack.status)
        self.assertEqual(states.NONE, stack.task_state)
        self.assert_empty_quota()
        path = '/v1/stacks/' + stack.id
        self.request('delete', path, status=202, complete=True)
        self.request('get', path, status=404)
        self.assert_empty_quota()

    def test_unclaimed_pending_delete_fences_late_cast(self):
        ctx, operation, stack, options = self.pending()
        path = '/v1/stacks/' + stack.id
        other = dict(self.headers, **{'X-Project-Id': 'other'})
        self.request('delete', path, status=404, headers=other)
        self.request('delete', path, status=202, complete=True)
        self.manager.execute(
            ctx, operation, stack, options,
            reservation=ctx.quota_reservation, revision=rpcapi.revision(stack))
        self.assert_empty_quota()
        self.assertEqual([], list(objects.ServerList.get_all(self.ctx)))

    def test_failed_handoff_fences_late_cast_and_repeated_cleanup(self):
        ctx, operation, stack, options = self.pending()
        self.assertTrue(handlers.abandon_stack_create(ctx, stack.id))
        self.assertFalse(handlers.abandon_stack_create(ctx, stack.id))
        self.manager.execute(
            ctx, operation, stack, options,
            reservation=ctx.quota_reservation, revision=rpcapi.revision(stack))
        self.assert_empty_quota()
        self.assertEqual(states.ERROR,
                         objects.Stack.get_by_id(ctx, stack.id).status)

    def test_failed_create_can_retry_at_quota_limit(self):
        self.config.config(environments=1, servers=2, build_servers=1,
                           volume_gb=10, group='quotas')
        with mock.patch.object(self.rpc, 'cast', side_effect=(
                exception.CoastException('Conductor handoff failed.'))):
            self.request('post', '/v1/stacks', self.body, status=500)
        self.request('post', '/v1/stacks',
                     {'stack': dict(self.body['stack'], name='retry')},
                     status=202, complete=True)
        self.assertEqual(1, quota.usage(self.ctx)['in_use']['environments'])
        self.assertFalse(any(quota.usage(self.ctx)['reserved'].values()))

    def test_cleanup_preserves_another_requests_reservation(self):
        self.config.config(build_servers=2, group='quotas')
        ctx, unused, first, options = self.pending()
        other_ctx, unused, second, options = self.pending('second')
        self.assertTrue(handlers.abandon_stack_create(ctx, first.id))
        reserved = quota.usage(ctx)['reserved']
        for name, amount in other_ctx.quota_reservation.items():
            self.assertEqual(amount, reserved[name])
        self.assertEqual(states.PENDING,
                         objects.Stack.get_by_id(ctx, second.id).status)

    def test_owned_stack_keeps_lock_and_quota(self):
        ctx, unused, stack, options = self.pending()
        stack.conductor_host = 'conductor.example.com'
        stack.save()
        before = quota.usage(ctx)
        self.assertFalse(handlers.abandon_stack_create(ctx, stack.id))
        self.request('delete', '/v1/stacks/' + stack.id, status=409)
        self.assertEqual(before, quota.usage(ctx))

    def test_failed_cast_after_claim_preserves_owner_and_quota(self):
        def claim_then_fail(ctx, operation, stack, options):
            current = objects.Stack.get_by_id(ctx, stack.id)
            current.conductor_host = 'conductor.example.com'
            current.save()
            raise exception.CoastException('Conductor handoff failed.')

        with mock.patch.object(self.rpc, 'cast', side_effect=claim_then_fail):
            self.request('post', '/v1/stacks', self.body, status=500)
        stack = objects.StackList.get_all(self.ctx)[0]
        self.assertEqual('conductor.example.com', stack.conductor_host)
        self.assertEqual(states.PENDING, stack.status)
        self.assertEqual(states.PROVISIONING, stack.task_state)
        held = quota.usage(self.ctx)['reserved']
        self.assertEqual(1, held['environments'])
        self.assertEqual(2, held['servers'])
        self.assertEqual(1, held['build_servers'])
        self.assertEqual(10, held['volume_gb'])
        self.request('delete', '/v1/stacks/' + stack.id, status=409)
        self.assertEqual(held, quota.usage(self.ctx)['reserved'])

    def receiptless_pending(self):
        stack = objects.Stack(context=self.ctx, name='unclaimed', plan='small',
                              driver='noop', engine_version='4.4.3',
                              status=states.PENDING,
                              task_state=states.NONE)
        stack.create()
        stack.acquire_task(states.PROVISIONING)
        return stack

    def test_unclaimed_pending_without_receipt_releases_reserved_quota(self):
        # Seed the pre-fix state: held capacity but no acceptance receipt.
        self.config.config(baremetal_servers=1, group='quotas')
        quota.reserve(self.ctx, environments=1, servers=3,
                      baremetal_servers=1, build_servers=1, volume_gb=37)
        stack = self.receiptless_pending()
        self.request('delete', '/v1/stacks/' + stack.id,
                     status=202, complete=True)
        self.request('get', '/v1/stacks/' + stack.id, status=404)
        self.assert_empty_quota()
        self.manager.execute(
            self.ctx, 'create_stack', stack, {},
            reservation=dict(environments=1, servers=3, baremetal_servers=1,
                             build_servers=1, volume_gb=37),
            revision=rpcapi.revision(stack))
        self.manager.execute(self.ctx, 'delete_stack', stack)
        self.assert_empty_quota()

    def test_receiptless_recovery_preserves_unrelated_and_other_project(self):
        quota.reserve(self.ctx, environments=1, servers=3, volume_gb=37,
                      apps=1, domains=1, storage_gb=9)
        other = type(self.ctx)(project_id='other', user_id='user')
        quota.reserve(other, environments=1, servers=2, volume_gb=5)
        before = quota.usage(other)
        stack = self.receiptless_pending()
        self.request('delete', '/v1/stacks/' + stack.id,
                     status=202, complete=True)
        self.assertEqual(before, quota.usage(other))
        held = quota.usage(self.ctx)['reserved']
        self.assertEqual(1, held['apps'])
        self.assertEqual(1, held['domains'])
        self.assertEqual(9, held['storage_gb'])
        self.assertEqual(0, held['servers'])
        self.assertEqual(0, held['volume_gb'])

    def test_receiptless_recovery_waits_for_other_stack_create(self):
        quota.reserve(self.ctx, environments=1, servers=3, volume_gb=37)
        stack = self.receiptless_pending()
        ctx, unused, other, options = self.pending('other')
        before = quota.usage(self.ctx)
        self.request('delete', '/v1/stacks/' + stack.id, status=409)
        self.assertEqual(before, quota.usage(self.ctx))
        current = objects.Stack.get_by_id(self.ctx, stack.id)
        self.assertEqual(states.PROVISIONING, current.task_state)
        handlers.abandon_stack_create(ctx, other.id)
        self.request('delete', '/v1/stacks/' + stack.id,
                     status=202, complete=True)
        self.assert_empty_quota()

    def test_receiptless_recovery_waits_for_volume_hold(self):
        quota.reserve(self.ctx, environments=1, servers=3, volume_gb=37)
        stack = self.receiptless_pending()
        objects.Event(context=self.ctx, stack_id=stack.id,
                      resource_type='volume', resource_id='volume',
                      action='extend', outcome='start', message='5').create()
        before = quota.usage(self.ctx)
        self.request('delete', '/v1/stacks/' + stack.id, status=409)
        self.assertEqual(before, quota.usage(self.ctx))
