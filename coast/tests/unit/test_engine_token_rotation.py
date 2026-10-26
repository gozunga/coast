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

"""Offline token rotation checkpoints and fencing."""

import datetime
from unittest import mock

import fixtures
from oslo_utils import timeutils

from coast.api import secret_store
from coast.common import context
from coast.common import engine_versions
from coast.common import exception
from coast.common import states
from coast.conductor import engine_token_rotation as rotation
from coast.conductor import manager
from coast.db import api
from coast.db import base_models
from coast import objects
from coast.tests import base


class RotationTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.patch(api, 'CONF', self.conf)
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(project_id='project')
        self.stack = objects.Stack(
            context=self.ctx, name='stack', driver='coolify_v4',
            engine_version=engine_versions.DEFAULT, plan='test',
            status=states.ACTIVE, task_state=states.NONE,
            engine_token_ref='old', engine_token_name=rotation.BOOTSTRAP_NAME)
        self.stack.create()
        objects.Server(context=self.ctx, stack_id=self.stack.id,
                       name='controller', kind='vm', role='control',
                       status=states.ACTIVE, task_state=states.NONE,
                       mgmt_ip='192.0.2.1').create()
        self.owner = manager.Manager(self.conf, notifier=mock.Mock())
        self.payloads = {'old': 'old-token'}
        self.client = mock.Mock()
        self.client.request.return_value = {'status': 'ok'}
        self.client.rotate_token.return_value = 'replacement-token'
        self.patch(rotation.agent_client, 'AgentClient',
                   mock.Mock(return_value=self.client))
        self.store = mock.Mock()
        self.store.put.side_effect = self.put
        self.store.delete.side_effect = lambda ctx, ref: (
            self.payloads.pop(ref, None))
        self.patch(secret_store, 'STORE', self.store)

    def put(self, ctx, value, identity):
        reference = 'new-' + str(self.store.put.call_count)
        self.payloads[reference] = value
        return reference

    def fresh(self):
        return objects.Stack.get_by_id(self.ctx, self.stack.id)

    def run_rotation(self):
        rotation.rotate(self.owner, self.ctx, self.stack.id)

    def test_happy_path_and_no_backup_field(self):
        self.assertNotIn('engine_backup_ref', objects.Stack.fields)
        self.run_rotation()
        stack = self.fresh()
        self.assertEqual('new-1', stack.engine_token_ref)
        self.assertIsNone(stack.engine_token_previous_ref)
        self.assertIsNone(stack.engine_token_rotation_id)
        self.assertEqual(states.NONE, stack.task_state)
        self.assertEqual({'new-1': 'replacement-token'}, self.payloads)
        self.client.revoke_token.assert_called_once()

    def test_failures_at_each_boundary_retry_converges(self):
        for method, target in (('rotate_token', self.client),
                               ('put', self.store),
                               ('revoke_token', self.client),
                               ('delete', self.store)):
            with self.subTest(method=method):
                original = getattr(target, method).side_effect
                getattr(target, method).side_effect = RuntimeError('private')
                self.assertRaises(exception.CoastException, self.run_rotation)
                stack = self.fresh()
                self.assertIn(stack.engine_token_ref, self.payloads)
                self.assertIsNotNone(stack.engine_token_rotation_id)
                getattr(target, method).side_effect = original
                self.run_rotation()
                self.assertIsNone(self.fresh().engine_token_rotation_id)
                self.assertEqual(states.NONE, self.fresh().task_state)

    def test_process_death_after_every_external_phase_and_checkpoint(self):
        class Crash(BaseException):
            pass

        for phase in ('create', 'put', 'publish', 'revoke', 'delete', 'clear'):
            with self.subTest(phase=phase):
                if phase in ('publish', 'clear'):
                    original = rotation.checkpoint

                    def die(*args, **kwargs):
                        result = original(*args, **kwargs)
                        key = ('engine_token_ref' if phase == 'publish' else
                               'engine_token_rotation_id')
                        if key in kwargs:
                            raise Crash()
                        return result
                    patcher = mock.patch.object(rotation, 'checkpoint', die)
                else:
                    target, method = {
                        'create': (self.client, 'rotate_token'),
                        'put': (self.store, 'put'),
                        'revoke': (self.client, 'revoke_token'),
                        'delete': (self.store, 'delete')}[phase]
                    original = getattr(target, method).side_effect
                    value = getattr(target, method).return_value

                    def die(*args, **kwargs):
                        if original:
                            original(*args, **kwargs)
                        raise Crash()
                    patcher = mock.patch.object(
                        target, method, side_effect=die, return_value=value)
                with patcher:
                    self.assertRaises(Crash, self.run_rotation)
                # The currently published reference always resolves, even when
                # the old secret was already deleted before process death.
                stack = self.fresh()
                self.assertIn(stack.engine_token_ref, self.payloads)
                with api.writer.using(self.ctx):
                    row = api.stacks._get(self.ctx, stack.id, lock=True)
                    row.task_started_at = (timeutils.utcnow() -
                                           datetime.timedelta(days=1))
                self.run_rotation()
                self.assertIsNone(self.fresh().engine_token_rotation_id)
                self.assertEqual(states.NONE, self.fresh().task_state)

    def test_in_flight_task_refuses_without_agent_calls(self):
        self.stack.acquire_task(states.UPGRADING)
        self.assertRaises(exception.Conflict, self.run_rotation)
        self.client.rotate_token.assert_not_called()

    def test_unnamed_token_refused_without_claim_or_backfill(self):
        self.stack.engine_token_name = None
        self.stack.engine_token_created_at = (
            timeutils.utcnow() - datetime.timedelta(days=31))
        self.stack.save()
        with mock.patch.object(objects.Stack, 'acquire_task') as acquire:
            for _attempt in range(2):
                self.assertRaises(exception.Conflict, self.run_rotation)
                rotation.tick(self.owner)
                stack = self.fresh()
                self.assertEqual(states.NONE, stack.task_state)
                self.assertIsNone(stack.engine_token_rotation_id)
                self.assertIsNone(stack.task_owner)
                self.assertIsNone(stack.task_started_at)
                self.assertIsNone(stack.engine_token_name)
                self.assertEqual('old', stack.engine_token_ref)
            acquire.assert_not_called()
        self.client.rotate_token.assert_not_called()
        self.client.revoke_token.assert_not_called()
        self.store.put.assert_not_called()

    def test_service_task_refuses(self):
        source = api.sources.create(self.ctx, dict(type='docker_image'))
        api.services.create(self.ctx, dict(
            stack_id=self.stack.id, name='app', status=states.ACTIVE,
            auto_deploy=False, source_id=source.id,
            task_state=states.DEPLOYING))
        self.assertRaises(exception.Conflict, self.run_rotation)
        self.client.rotate_token.assert_not_called()

    def test_concurrent_rotation_refused_then_expired_claim_resumed(self):
        stack, owner = rotation.claim(self.owner, self.ctx, self.stack.id)
        self.assertRaises(exception.Conflict, self.run_rotation)
        with api.writer.using(self.ctx):
            row = api.stacks._get(self.ctx, stack.id, lock=True)
            row.task_started_at = (timeutils.utcnow() -
                                   datetime.timedelta(days=1))
        self.run_rotation()
        self.assertIsNone(self.fresh().engine_token_rotation_id)
        self.assertRaises(exception.Conflict, rotation.checkpoint,
                          self.ctx, stack.id, owner, fault='stale')

    def test_periodic_selection(self):
        self.stack.engine_token_created_at = timeutils.utcnow()
        self.stack.save()
        rotation.tick(self.owner)
        self.client.rotate_token.assert_not_called()
        self.stack.engine_token_created_at -= datetime.timedelta(days=31)
        self.stack.save()
        rotation.tick(self.owner)
        self.client.rotate_token.assert_called_once()
        self.config.config(engine_token_rotation_days=0, group='secrets')
        self.client.reset_mock()
        rotation.tick(self.owner)
        self.client.rotate_token.assert_not_called()

    def test_unhealthy_controller_skipped(self):
        self.stack.engine_token_created_at = (
            timeutils.utcnow() - datetime.timedelta(days=31))
        self.stack.save()
        self.client.request.return_value = {'status': 'error'}
        with mock.patch.object(objects.Stack, 'acquire_task') as acquire:
            rotation.tick(self.owner)
            acquire.assert_not_called()
        self.client.request.assert_called_once_with('GET', '/health')
        self.client.rotate_token.assert_not_called()
        self.assertEqual(states.NONE, self.fresh().task_state)
        self.assertIsNone(self.fresh().engine_token_rotation_id)

    def test_global_concurrency_bound(self):
        self.config.config(rotation_max_concurrent=1, group='secrets')
        rotation.claim(self.owner, self.ctx, self.stack.id)
        other = objects.Stack(
            context=self.ctx, name='other', driver='coolify_v4',
            engine_version=engine_versions.DEFAULT, plan='test',
            status=states.ACTIVE, task_state=states.NONE,
            engine_token_ref='other',
            engine_token_name=rotation.BOOTSTRAP_NAME)
        other.create()
        self.assertRaises(exception.Conflict, rotation.claim,
                          self.owner, self.ctx, other.id)
