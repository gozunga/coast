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

"""Snapshot expiry through the conductor timer with a fake Cinder."""

import datetime
from types import SimpleNamespace
from unittest import mock

from openstack import exceptions as sdk_exceptions

from coast.common import context
from coast.common import exception
from coast.conductor import service
from coast.conductor import stack_teardown
from coast.db import api
from coast.db import models
from coast import objects
from coast.tests import conductor


class SnapshotRetentionTest(conductor.ConductorTest):
    def test_periodic_cleanup_preserves_other_project_null_expiry(self):
        path = self.stack()
        stack_id = path.rsplit('/', 1)[-1]
        expired = objects.StackDeleteSnapshot(
            context=self.ctx, stack_id=stack_id, volume_id='removed-volume',
            cinder_volume_id='removed-cinder', cinder_snapshot_id='expired',
            status='available', retention_days=0,
            expires_at=datetime.datetime.now(datetime.timezone.utc) -
            datetime.timedelta(seconds=1))
        expired.create()
        self.request('delete', path, status=202, complete=True)
        with api.writer.using(self.ctx) as session:
            session.query(models.Stack).filter_by(id=stack_id).update(
                {'driver': 'coolify'})
        other = context.RequestContext(project_id='other')
        stack = objects.Stack(
            context=other, name='other', driver='coolify', plan='small',
            engine_version='test', status='ACTIVE', task_state='NONE')
        stack.create()
        kept = objects.StackDeleteSnapshot(
            context=other, stack_id=stack.id, volume_id='other-volume',
            cinder_volume_id='other-cinder', cinder_snapshot_id='kept',
            status='available', retention_days=None, expires_at=None)
        kept.create()
        block = mock.Mock()
        block.get_snapshot.side_effect = sdk_exceptions.ResourceNotFound(
            'gone')
        cloud_contexts = []

        def cloud(conf, owner):
            cloud_contexts.append(owner.project_id)
            return SimpleNamespace(proxy=lambda name: block)

        with mock.patch.object(stack_teardown.controller, 'ControllerCloud',
                               side_effect=cloud):
            tasks = service.DeploymentTasks(self.conf, self.manager)
            tasks.reconcile(None)
            tasks.reconcile(None)
        self.assertEqual([self.ctx.project_id], cloud_contexts)
        self.assertRaises(exception.NotFound,
                          objects.StackDeleteSnapshot.get_by_id,
                          self.ctx, expired.id)
        self.assertEqual('kept', objects.StackDeleteSnapshot.get_by_id(
            other, kept.id).cinder_snapshot_id)
