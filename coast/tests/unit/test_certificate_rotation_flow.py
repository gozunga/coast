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

"""Project-scoped CAS admission, exact lifetime and receipt fencing."""

import datetime
import ipaddress
import threading
from types import SimpleNamespace
from unittest import mock

import fixtures

from coast.common import context
from coast.common import exception
from coast.common import states
from coast.conductor import certificates
from coast.db import base_models
from coast.drivers import base as driver_base
from coast.health_manager import certificates as health
from coast import objects
from coast.tests import base
from coast.tests import certificates as material


class RotationFlowTest(material.Material, base.TestCase):
    def setUp(self):
        super().setUp()
        self.prepare_material()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(project_id='tenant')
        self.stack = objects.Stack(
            context=self.ctx, name='stack', plan='small', driver='coolify_v4',
            engine_version='4.4.3', status=states.ACTIVE,
            task_state=states.NONE)
        self.stack.create()
        self.server = objects.Server(
            context=self.ctx, id=material.SERVER_ID, stack_id=self.stack.id,
            name='worker', role='worker', kind='vm', status=states.ACTIVE,
            task_state=states.NONE, mgmt_ip=material.ADDRESS,
            agent_cert_serial=self.body()['expected_serial'],
            agent_cert_expires_at=self.load(self.current).not_valid_after_utc)
        self.server.create()
        self.manager = SimpleNamespace(conf=self.conf, notifier=mock.Mock())
        self.client = mock.Mock()
        self.patcher = mock.patch.object(
            certificates.agent_client, 'AgentClient', return_value=self.client)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.old = self.load(self.current)
        self.replacement = self.issue()
        self.new = self.load(self.replacement)
        self.issuer = mock.patch.object(
            certificates.tls, 'issue_server_certificate',
            return_value=self.replacement)
        self.issuer.start()
        self.addCleanup(self.issuer.stop)
        self.client.certificate_connection.side_effect = [self.old, {
            'status': 'rotated', 'serial': str(self.new.serial_number)},
            self.new]

    @staticmethod
    def load(issued):
        return certificates.x509.load_pem_x509_certificate(
            issued.certificate_pem)

    def run_rotation(self):
        certificates.rotate(self.manager, self.ctx, self.server)

    def events(self):
        return objects.EventList.get_all(self.ctx, filters={
            'resource_id': self.server.id, 'action': certificates.ACTION})

    def fresh(self):
        return objects.Server.get_by_id(self.ctx, self.server.id)

    def test_publish_after_fresh_peer_and_duplicate_is_noop(self):
        observed = []

        def connection(server_id, body=None):
            observed.append(self.fresh().agent_cert_serial)
            self.assertEqual(states.RECONCILING, self.fresh().task_state)
            self.assertEqual(material.SERVER_ID, server_id)
            if len(observed) == 1:
                return self.old
            if body is not None:
                self.assertEqual(self.server.agent_cert_serial,
                                 body['expected_serial'])
                return {'status': 'rotated',
                        'serial': str(self.new.serial_number)}
            return self.new

        self.client.certificate_connection.side_effect = connection
        self.run_rotation()
        self.assertEqual([str(self.old.serial_number)] * 3, observed)
        self.assertEqual(str(self.new.serial_number),
                         self.fresh().agent_cert_serial)
        self.assertEqual(states.NONE, self.fresh().task_state)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)
        self.assertEqual({'start', 'success'},
                         {event.outcome for event in self.events()})
        self.run_rotation()
        self.assertEqual(3, len(observed))

    def test_wrong_new_peer_fails_without_publication_or_automatic_retry(self):
        self.client.certificate_connection.side_effect = [self.old, {
            'status': 'rotated', 'serial': str(self.new.serial_number)},
            self.old]
        self.run_rotation()
        self.assertEqual(str(self.old.serial_number),
                         self.fresh().agent_cert_serial)
        self.assertEqual({'start', 'failure'},
                         {event.outcome for event in self.events()})
        self.assertEqual(states.NONE, self.fresh().task_state)
        self.run_rotation()
        self.assertEqual(3, self.client.certificate_connection.call_count)

    def test_uncertain_post_never_reissues_and_never_stores_secrets(self):
        self.client.certificate_connection.side_effect = [
            self.old, ValueError('SECRET')]
        self.run_rotation()
        self.run_rotation()
        self.assertEqual(2, self.client.certificate_connection.call_count)
        for event in self.events():
            self.assertNotIn('SECRET', event.message or '')
            self.assertNotIn('PRIVATE KEY', event.message or '')
        self.assertEqual(str(self.old.serial_number),
                         self.fresh().agent_cert_serial)

    def _assert_sanitized_failure_log(self, logs, cause, phase, secrets):
        output = logs.output
        self.assertIn(cause, output)
        self.assertIn(self.server.id, output)
        self.assertIn(self.stack.id, output)
        self.assertIn(phase + ' the start receipt', output)
        for secret in secrets:
            self.assertNotIn(secret, output)
        failure = [event.message for event in self.events()
                   if event.outcome == 'failure']
        self.assertEqual([certificates.FAULT], failure)
        for event in self.events():
            for secret in secrets:
                self.assertNotIn(secret, event.message or '')
        self.assertEqual(states.NONE, self.fresh().task_state)

    def test_failure_before_start_logs_cause_class_without_secret(self):
        secret = '-----BEGIN PRIVATE KEY----- secret-key-material'
        logs = self.useFixture(fixtures.FakeLogger())

        def connection(server_id, body=None):
            raise ValueError(secret)

        self.client.certificate_connection.side_effect = connection
        self.run_rotation()
        self._assert_sanitized_failure_log(
            logs, 'builtins.ValueError', 'before',
            (secret, 'PRIVATE KEY', 'secret-key-material'))

    def test_failure_after_start_logs_linked_cause_without_secret(self):
        inner = '-----BEGIN PRIVATE KEY----- inner-secret-key'
        outer = '-----BEGIN CERTIFICATE----- outer-secret-pem'
        logs = self.useFixture(fixtures.FakeLogger())

        def connection(server_id, body=None):
            if body is None:
                return self.old
            try:
                raise ipaddress.AddressValueError(inner)
            except ipaddress.AddressValueError:
                raise driver_base.EngineUnreachable(outer) from None

        self.client.certificate_connection.side_effect = connection
        self.run_rotation()
        self._assert_sanitized_failure_log(
            logs, 'ipaddress.AddressValueError', 'after',
            (inner, outer, 'PRIVATE KEY', 'CERTIFICATE',
             'inner-secret-key', 'outer-secret-pem'))
        self.assertNotIn('EngineUnreachable', logs.output)
        self.assertEqual(str(self.old.serial_number),
                         self.fresh().agent_cert_serial)

    def test_skipped_response_is_rejected_without_publication(self):
        self.client.certificate_connection.side_effect = [self.old, {
            'status': 'skipped', 'reason': 'server_ca_missing'}]
        self.run_rotation()
        self.run_rotation()
        self.assertEqual({'start', 'failure'},
                         {event.outcome for event in self.events()})
        self.assertIn(certificates.FAULT,
                      [event.message for event in self.events()])
        self.assertEqual(2, self.client.certificate_connection.call_count)
        self.assertEqual(str(self.old.serial_number),
                         self.fresh().agent_cert_serial)

    def test_not_due_does_not_issue_or_write_events(self):
        self.client.certificate_connection.side_effect = None
        self.client.certificate_connection.return_value = self.old
        with mock.patch.object(certificates, 'due', return_value=False):
            self.run_rotation()
        self.assertEqual(0, len(self.events()))
        self.assertEqual(states.NONE, self.fresh().task_state)
        self.assertEqual(1, self.client.certificate_connection.call_count)

    def test_task_conflicts_and_wrong_project_do_not_call_agent(self):
        for kind, resource in (('server', self.server), ('stack', self.stack)):
            resource.acquire_task(states.DELETING)
            self.run_rotation()
            resource.release_task(states.DELETING)
        other = context.RequestContext(project_id='other')
        self.assertRaises(exception.NotFound, certificates.rotate,
                          self.manager, other, self.server)
        self.assertEqual(0, self.client.certificate_connection.call_count)

    def test_durable_start_fences_interrupted_attempt(self):
        objects.Event(context=self.ctx,
                      id=certificates.receipt_id(self.server),
                      stack_id=self.stack.id, resource_type='server',
                      resource_id=self.server.id, action=certificates.ACTION,
                      outcome='start').create()
        self.run_rotation()
        self.assertEqual(0, self.client.certificate_connection.call_count)

    def test_exact_80_percent_boundary(self):
        start = self.old.not_valid_before_utc
        lifetime = self.old.not_valid_after_utc - start
        boundary = start + lifetime * 0.8
        self.assertFalse(certificates.due(
            self.old, boundary - datetime.timedelta(microseconds=1)))
        self.assertTrue(certificates.due(self.old, boundary))

    def test_health_handoff_scopes_projects_and_skips_busy_or_receipted(self):
        sweep = health.Rotation(self.conf)
        self.client.certificate_connection.side_effect = None
        self.client.certificate_connection.return_value = self.old
        rpc = mock.Mock()
        with mock.patch.object(health.rpcapi, 'ConductorAPI',
                               return_value=rpc):
            self.server.acquire_task(states.DELETING)
            sweep.run()
            rpc.cast.assert_not_called()
            self.server.release_task(states.DELETING)

            def cast(ctx, operation, server):
                self.assertEqual('tenant', ctx.project_id)
                self.assertEqual('rotate_server', operation)
                self.assertEqual(self.server.id, server.id)
                self.assertRaises(AttributeError, lambda: ctx.session)

            rpc.cast.side_effect = cast
            sweep.run()
            self.assertEqual(1, rpc.cast.call_count)
            objects.Event(context=self.ctx,
                          id=certificates.receipt_id(self.server),
                          stack_id=self.stack.id, resource_type='server',
                          resource_id=self.server.id,
                          action=certificates.ACTION,
                          outcome='failure').create()
            sweep.run()
            self.assertEqual(1, rpc.cast.call_count)

    def test_health_sweep_bounds_rows_and_advances_cursor(self):
        for index in range(4):
            objects.Server(context=self.ctx, stack_id=self.stack.id,
                           name='build', kind='vm', role='build',
                           status=states.ACTIVE, task_state=states.NONE,
                           mgmt_ip=material.ADDRESS,
                           agent_cert_serial=(
                               self.server.agent_cert_serial)).create()
        self.client.certificate_connection.side_effect = None
        self.client.certificate_connection.return_value = self.old
        sweep = health.Rotation(self.conf)
        with mock.patch.object(health, 'LIMIT', 2), mock.patch.object(
                health.rpcapi, 'ConductorAPI'):
            sweep.run()
            first = sweep.cursor
            self.assertTrue(first)
            sweep.run()
            self.assertGreater(sweep.cursor, first)
            sweep.run()
            self.assertEqual('', sweep.cursor)
        self.assertEqual(5, self.client.certificate_connection.call_count)

    def test_duplicate_during_connection_cannot_take_over_claim(self):
        connections = []

        def connection(server_id, body=None):
            connections.append(body)
            certificates.rotate(self.manager, self.ctx, self.server)
            if len(connections) == 1:
                return self.old
            if body is not None:
                return {'status': 'rotated',
                        'serial': str(self.new.serial_number)}
            return self.new

        self.client.certificate_connection.side_effect = connection
        self.run_rotation()
        self.assertEqual(3, len(connections))
        self.assertEqual(str(self.new.serial_number),
                         self.fresh().agent_cert_serial)

    def test_rotation_proceeds_while_deployment_holds_stack_lock(self):
        self.stack.acquire_task(states.RECONCILING)
        self.run_rotation()
        stack = objects.Stack.get_by_id(self.ctx, self.stack.id)
        self.assertEqual(states.RECONCILING, stack.task_state)
        self.assertEqual(states.ACTIVE, stack.status)
        self.assertEqual(states.NONE, self.fresh().task_state)
        self.assertEqual(str(self.new.serial_number),
                         self.fresh().agent_cert_serial)

    def test_busy_server_is_skipped(self):
        self.server.acquire_task(states.RECONCILING)
        self.run_rotation()
        self.assertEqual(0, self.client.certificate_connection.call_count)
        self.assertEqual(states.RECONCILING, self.fresh().task_state)
        self.assertEqual(0, len(self.events()))

    def test_delete_and_same_server_failover_stay_excluded(self):
        self.client.certificate_connection.side_effect = None
        self.client.certificate_connection.return_value = self.old
        self.stack.acquire_task(states.DELETING)
        self.run_rotation()
        self.stack.release_task(states.DELETING)
        self.server.acquire_task(states.FAILING_OVER)
        self.run_rotation()
        self.server.release_task(states.FAILING_OVER)
        self.assertEqual(0, self.client.certificate_connection.call_count)
        # Failover of a different server holds the stack task only.
        self.stack.acquire_task(states.FAILING_OVER)
        self.client.certificate_connection.side_effect = [self.old, {
            'status': 'rotated', 'serial': str(self.new.serial_number)},
            self.new]
        self.run_rotation()
        self.assertEqual(states.FAILING_OVER, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)
        self.assertEqual(states.NONE, self.fresh().task_state)
        self.assertEqual(str(self.new.serial_number),
                         self.fresh().agent_cert_serial)

    def test_concurrent_rotate_cas(self):
        self.stack.acquire_task(states.RECONCILING)
        started = threading.Event()
        release = threading.Event()
        calls = []
        errors = []

        def connection(server_id, body=None):
            calls.append(body)
            if len(calls) == 1:
                started.set()
                if not release.wait(10):
                    raise RuntimeError('peer did not finish')
                return self.old
            if body is not None:
                return {'status': 'rotated',
                        'serial': str(self.new.serial_number)}
            return self.new

        def peer():
            local = context.RequestContext(project_id='tenant')
            try:
                if not started.wait(10):
                    raise RuntimeError('rotation did not claim the server')
                held = objects.Server.get_by_id(local, self.server.id)
                self.assertEqual(states.RECONCILING, held.task_state)
                certificates.rotate(self.manager, local, self.server)
                held = objects.Server.get_by_id(local, self.server.id)
                self.assertEqual(states.RECONCILING, held.task_state)
                stack = objects.Stack.get_by_id(local, self.stack.id)
                self.assertEqual(states.RECONCILING, stack.task_state)
            except Exception as exc:
                errors.append(exc)
            finally:
                release.set()

        self.client.certificate_connection.side_effect = connection
        thread = threading.Thread(target=peer)
        thread.start()
        try:
            self.run_rotation()
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(3, len(calls))
        self.assertEqual(str(self.new.serial_number),
                         self.fresh().agent_cert_serial)
        self.assertEqual(states.NONE, self.fresh().task_state)
        self.assertEqual(states.RECONCILING, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def _worker(self, name, expires, stack_id=None):
        return objects.Server(
            context=self.ctx, stack_id=stack_id or self.stack.id, name=name,
            kind='vm', role='worker', status=states.ACTIVE,
            task_state=states.NONE, mgmt_ip=material.ADDRESS,
            agent_cert_serial=self.server.agent_cert_serial,
            agent_cert_expires_at=expires)

    def test_sweep_prefers_nearest_expiry_despite_stack_lock(self):
        soon = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        late = datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc)
        self.server.agent_cert_expires_at = late
        self.server.save()
        unknown = self._worker('unknown', None)
        unknown.create()
        nearer = self._worker('near', soon)
        nearer.create()
        busy = self._worker('busy', soon - datetime.timedelta(days=1))
        busy.create()
        busy.acquire_task(states.RECONCILING)
        deleting = objects.Stack(
            context=self.ctx, name='deleting', plan='small',
            driver='coolify_v4', engine_version='4.4.3',
            status=states.ACTIVE, task_state=states.NONE)
        deleting.create()
        deleting.acquire_task(states.DELETING)
        doomed = self._worker(
            'doomed', soon - datetime.timedelta(days=2), deleting.id)
        doomed.create()
        self.stack.acquire_task(states.RECONCILING)
        seen = []

        def connection(server_id, body=None):
            seen.append(server_id)
            return self.old

        self.client.certificate_connection.side_effect = connection
        sweep = health.Rotation(self.conf)
        with mock.patch.object(health.rpcapi, 'ConductorAPI'):
            sweep.run()
        self.assertEqual(
            [unknown.id, nearer.id, self.server.id], seen)
        self.assertEqual('', sweep.cursor)
        seen.clear()
        paged = health.Rotation(self.conf)
        with mock.patch.object(health, 'LIMIT', 1), mock.patch.object(
                health.rpcapi, 'ConductorAPI'):
            for expected in (unknown.id, nearer.id, self.server.id):
                paged.run()
                self.assertEqual([expected], seen[-1:])
                self.assertEqual(expected, paged.cursor)
            paged.run()
            self.assertEqual('', paged.cursor)
        self.assertEqual(
            [unknown.id, nearer.id, self.server.id], seen)
        self.assertEqual(states.RECONCILING, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)
        self.assertEqual(states.DELETING, objects.Stack.get_by_id(
            self.ctx, deleting.id).task_state)
