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

"""Resize admission and serial replacement contracts."""

import json
from types import SimpleNamespace
from unittest import mock

from coast.api import handlers
from coast.api.v1 import resize
from coast.common import quota
from coast.common import states
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.conductor import server_failover
from coast import objects
from coast.tests.unit import test_controllers


class ResizeTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.26'
        self.headers['X-Roles'] = 'member'
        self.server.flavor_id = 'old'
        self.server.save()
        self.config.config(definitions=json.dumps([dict(
            name='large', flavor='new', kind='vm',
            roles=['worker', 'build', 'control'])]), group='plans')
        self.flavors = {
            'old': SimpleNamespace(id='old', name='small', vcpus=1,
                                   ram=1024, disk=10, ephemeral=0),
            'new': SimpleNamespace(id='new', name='large', vcpus=2,
                                   ram=2048, disk=10, ephemeral=0)}
        patcher = mock.patch.object(resize.controller, 'ControllerCloud')
        cloud = patcher.start()
        self.addCleanup(patcher.stop)
        cloud.return_value.proxy.return_value.find_flavor.side_effect = (
            lambda value: self.flavors[value])
        patcher = mock.patch.object(handlers.rpcapi.ConductorAPI, 'cast')
        self.cast = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, status=202, action='resize', headers=None, body=None):
        path = self.path if action == 'resize_workers' else self.server_path
        return self.client.post_json(path + '/action', body or {
            action: {'flavor': 'new'}}, headers=headers or self.headers,
            status=status)

    def test_member_resize_and_rpc_target(self):
        response = self.post()
        self.assertEqual('FAILOVER', response.json['server']['status'])
        self.assertEqual('old', objects.Server.get_by_id(
            self.ctx, self.server.id).flavor_id)
        args = self.cast.call_args.args
        self.assertEqual('resize_server', args[1])
        self.assertEqual(states.ACTIVE, args[3]['admission_status'])
        self.assertEqual('new', args[3]['targets'][self.server.id][
            'target_flavor'])

    def test_microversion_schema_and_authorization(self):
        for version in ('1.0', '1.25'):
            headers = dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting ' + version})
            self.post(400, headers=headers)
        self.post(400, body={'resize': {}})
        self.post(400, body={'resize': {'flavor': 'new', 'extra': 1}})
        self.post(403, headers=dict(self.headers, **{'X-Roles': 'reader'}))
        self.post(404, headers=dict(self.headers, **{'X-Project-Id': 'other'}))
        self.post(401, headers={'OpenStack-API-Version': 'app-hosting 1.26'})
        self.cast.assert_not_called()

    def test_equal_smaller_and_mixed_dimensions_refused(self):
        for cpu, ram in ((1, 1024), (1, 512), (2, 512)):
            self.flavors['new'].vcpus = cpu
            self.flavors['new'].ram = ram
            self.assertIn('smaller worker', self.post(400).json['faultstring'])
        self.cast.assert_not_called()

    def test_busy_stack_server_and_inactive_refused(self):
        for row in (self.env, self.server):
            row.acquire_task(states.RECONCILING)
            self.post(409)
            row.release_task(states.RECONCILING)
        self.server.status = states.UNHEALTHY
        self.server.save()
        self.post(409)
        self.cast.assert_not_called()

    def test_quota_refusal_and_no_capacity_delta(self):
        with mock.patch.object(quota, 'usage', return_value={
                'in_use': dict.fromkeys(quota.RESOURCES, 100),
                'reserved': dict.fromkeys(quota.RESOURCES, 0)}):
            self.post(403)
        self.post()
        self.assertEqual(dict.fromkeys(quota.RESOURCES, 0),
                         quota.usage(self.ctx)['reserved'])

    def test_lowered_real_quota_refuses_without_claiming(self):
        self.config.config(servers=0, group='quotas')
        self.post(403)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_flavor_name_and_plan_name_mapping(self):
        self.flavors['large'] = self.flavors['new']
        for flavor in ('large', 'new'):
            self.assertEqual({'target_flavor': 'new', 'target_plan': 'large'},
                             resize.selection(self.server, flavor))
        self.assertEqual('new', resize.selection(self.server, 'large')[
            'target_flavor'])

    def test_baremetal_and_wrong_role_are_refused(self):
        self.server.kind = 'baremetal'
        self.server.save()
        self.post(400)
        self.cast.assert_not_called()

    def test_vm_target_plan_excludes_server_role(self):
        self.config.config(definitions=json.dumps([dict(
            name='large', flavor='new', kind='vm', roles=['build'])]),
            group='plans')
        self.assertEqual('vm', self.server.kind)
        self.assertEqual('worker', self.server.role)
        self.post(400)
        self.cast.assert_not_called()

    def workers(self):
        workers = [self.server]
        for suffix in ('two', 'three'):
            server = objects.Server(
                context=self.ctx, stack_id=self.env.id, name=suffix,
                kind='vm', role='worker', flavor_id='old',
                status=states.ACTIVE, task_state=states.NONE)
            server.create()
            workers.append(server)
        return workers

    def refused_workers(self, status, headers=None):
        rows = [objects.Stack.get_by_id(self.ctx, self.env.id)] + list(
            objects.ServerList.get_all(self.ctx, filters={
                'stack_id': self.env.id}))
        before = [(row.status, row.task_state) for row in rows]
        with mock.patch.object(objects.Stack, 'acquire_task') as stack_claim:
            with mock.patch.object(objects.Server, 'acquire_task') as claim:
                response = self.post(status, action='resize_workers',
                                     headers=headers)
        stack_claim.assert_not_called()
        claim.assert_not_called()
        self.cast.assert_not_called()
        for row, expected in zip(rows, before):
            saved = type(row).get_by_id(self.ctx, row.id)
            self.assertEqual(expected, (saved.status, saved.task_state))
        return response

    def test_resize_workers_microversion_and_authorization_refused(self):
        self.workers()
        for version in ('1.0', '1.25'):
            with self.subTest(version=version):
                self.refused_workers(400, dict(self.headers, **{
                    'OpenStack-API-Version': 'app-hosting ' + version}))
        for roles in ('reader', 'unrelated'):
            with self.subTest(roles=roles):
                self.refused_workers(403, dict(self.headers, **{
                    'X-Roles': roles}))
        self.refused_workers(404, dict(self.headers, **{
            'X-Project-Id': 'other'}))
        self.refused_workers(401, {
            'OpenStack-API-Version': 'app-hosting 1.26'})

    def test_resize_workers_quota_refused(self):
        self.workers()
        self.config.config(servers=2, group='quotas')
        response = self.refused_workers(403)
        self.assertIn('Quota exceeded', response.json['faultstring'])
        self.assertEqual(dict.fromkeys(quota.RESOURCES, 0),
                         quota.usage(self.ctx)['reserved'])

    def test_resize_workers_busy_and_inactive_refused(self):
        workers = self.workers()
        for row in [self.env] + workers:
            with self.subTest(resource=row.id, busy=True):
                row.acquire_task(states.RECONCILING)
                self.refused_workers(409)
                row.release_task(states.RECONCILING)
            with self.subTest(resource=row.id, busy=False):
                row.status = (states.DEGRADED if row is self.env else
                              states.UNHEALTHY)
                row.save()
                self.refused_workers(409)
                row.status = states.ACTIVE
                row.save()

    def test_resize_workers_one_equal_or_larger_worker_refuses_all(self):
        workers = self.workers()
        self.flavors['larger'] = SimpleNamespace(
            id='larger', name='larger', vcpus=4, ram=4096,
            disk=10, ephemeral=0)
        for flavor in ('new', 'larger'):
            with self.subTest(current_flavor=flavor):
                workers[-1].flavor_id = flavor
                workers[-1].save()
                response = self.refused_workers(400)
                self.assertIn('smaller worker', response.json['faultstring'])

    def test_handoff_failure_releases_claim_and_fences_delayed_cast(self):
        self.cast.side_effect = RuntimeError('handoff failed')
        self.post(500)
        saved = objects.Server.get_by_id(self.ctx, self.server.id)
        self.assertEqual(states.ACTIVE, saved.status)
        self.assertEqual(states.NONE, saved.task_state)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        args = self.cast.call_args.args
        owner = manager.Manager(self.conf)
        self.addCleanup(owner.close)
        owner.execute(self.ctx, args[1], args[2], args[3],
                      revision=rpcapi.revision(args[2]))
        self.assertEqual('old', objects.Server.get_by_id(
            self.ctx, self.server.id).flavor_id)

    def test_serial_workers_stop_at_first_failure(self):
        for suffix in ('two', 'three'):
            objects.Server(context=self.ctx, stack_id=self.env.id,
                           name=suffix, kind='vm', role='worker',
                           flavor_id='old', status=states.ACTIVE,
                           task_state=states.NONE).create()
        self.env.driver = 'coolify_v4'
        self.env.save()
        self.post(action='resize_workers')
        args = self.cast.call_args.args
        order = []

        def replace(flow, ctx, server, options, revision=None):
            order.append(server.id)
            self.assertTrue(all(objects.Server.get_by_id(
                ctx, previous).status == states.ACTIVE for previous in
                order[:-1]))
            self.assertEqual(states.FAILING_OVER, objects.Stack.get_by_id(
                ctx, self.env.id).task_state)
            flow.succeeded = len(order) < 2
            server.status = states.ACTIVE if flow.succeeded else states.ERROR
            server.save()
            server.release_task(states.FAILING_OVER)
        owner = manager.Manager(self.conf)
        self.addCleanup(owner.close)
        with mock.patch.object(server_failover.FailoverServer, 'execute',
                               autospec=True, side_effect=replace):
            owner.execute(self.ctx, args[1], args[2], args[3],
                          revision=rpcapi.revision(args[2]))
        self.assertEqual(sorted(args[3]['targets'])[:2], order)
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.env.id, 'action': 'resize_workers',
            'resource_type': 'stack'})
        self.assertEqual(['failure'], [event.outcome for event in events])
        self.assertEqual(order[:1], events[0].detail['completed_server_ids'])
        self.assertEqual(order[1], events[0].detail['failed_server_id'])
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
