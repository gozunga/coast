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

"""Retained data deletion over the real API, fake RPC, and fake Cinder."""

from unittest import mock

from coast.common import states
from coast.conductor import volume_delete
from coast import objects
from coast.tests import conductor


class RetainedVolumeFlowTest(conductor.ConductorTest):
    def test_detached_data_delete_releases_capacity_and_keeps_receipt(self):
        path = self.stack(workers=[])
        before = self.usage()['in_use_volume_gb']
        volume = self.request('post', path + '/volumes', {'volume': {
            'name': 'retained', 'size_gb': 7}}, status=202,
            complete=True).json['volume']
        vp = path + '/volumes/' + volume['id']
        self.assertEqual(before + 7, self.usage()['in_use_volume_gb'])
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        self.request('delete', vp, headers=reader, status=403)
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        self.request('delete', vp, headers=foreign, status=404)
        self.request('delete', vp, status=202, complete=True)
        self.request('get', vp, status=404)
        self.assertEqual(before, self.usage()['in_use_volume_gb'])
        receipts = objects.EventList.get_all(self.ctx, filters={
            'resource_id': volume['id'], 'action': 'delete.cleanup'})
        self.assertEqual(1, len(receipts))
        self.assertEqual('success', receipts[0].outcome)
        stack = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[-1])
        self.assertEqual(states.NONE, stack.task_state)

    def test_service_project_cinder_dispatch_for_retained_row(self):
        path = self.stack(workers=[])
        before = self.usage()['in_use_volume_gb']
        volume = self.request('post', path + '/volumes', {'volume': {
            'name': 'retained-real-path', 'size_gb': 9}}, status=202,
            complete=True).json['volume']
        stack = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[-1])
        stack.driver = 'coolify_v4'
        stack.save()
        block = volume_delete.NoopCinder(self.manager.cloud, volume['id'])
        with mock.patch.object(volume_delete.controller.ControllerCloud,
                               'proxy', return_value=block):
            self.request('delete', path + '/volumes/' + volume['id'],
                         status=202, complete=True)
        self.request('get', path + '/volumes/' + volume['id'], status=404)
        self.assertEqual(before, self.usage()['in_use_volume_gb'])
