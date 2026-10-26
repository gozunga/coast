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

"""Deployment recovery branches, crash checkpoints and ownership fences."""

from concurrent import futures
import datetime
import threading
from unittest import mock
import uuid

from oslo_db import exception as db_exception
from oslo_utils import timeutils
import requests
from sqlalchemy import event

from coast.common import context
from coast.common import exception
from coast.common import states
from coast.conductor import deployments
from coast.conductor import manager
from coast.conductor import reconcile
from coast.conductor import rpcapi
from coast.conductor import runtime_logs
from coast.conductor import service
from coast.db import api as db_api
from coast.db import models
from coast.drivers import base
from coast import objects
from coast.tests import deployments as fixtures


class RecoveryTest(fixtures.DeploymentTest):
    def setUp(self):
        super().setUp()
        sleeper = mock.patch('coast.conductor.runtime_logs.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_real_deployment_deadlocks_retry_whole_transaction(self):
        for helper in ('_claim_lock', '_stack', '_event'):
            with self.subTest(helper=helper):
                deployment_id, message = self.accepted()
                original = getattr(manager.Manager, helper)
                attempts = []

                def deadlock_once(owner, *args, **kwargs):
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise db_exception.DBDeadlock()
                    return original(owner, *args, **kwargs)

                before = self.enqueue.call_count
                with mock.patch.object(manager.Manager, helper,
                                       new=deadlock_once):
                    self.manager.execute(self.ctx, **message)
                self.assertGreaterEqual(len(attempts), 2)
                self.assertEqual(before + 1, self.enqueue.call_count)
                self.settled(deployment_id, states.ACTIVE)

    def test_real_deployment_exhausted_deadlocks_release_claim(self):
        for helper in ('_claim_lock', '_stack', '_event'):
            with self.subTest(helper=helper):
                deployment_id, message = self.accepted()
                original = getattr(manager.Manager, helper)
                attempts = []

                def deadlock_four_times(owner, *args, **kwargs):
                    attempts.append(True)
                    if len(attempts) <= 4:
                        raise db_exception.DBDeadlock()
                    return original(owner, *args, **kwargs)

                before = self.enqueue.call_count
                with mock.patch.object(manager.Manager, helper,
                                       new=deadlock_four_times):
                    self.assertRaises(exception.Conflict,
                                      self.manager.execute,
                                      self.ctx, **message)
                self.settled(deployment_id, states.FAILED)
                self.assertEqual(before, self.enqueue.call_count)
                current = objects.Service.get_by_id(
                    self.ctx, self.service.id)
                self.assertIsNone(current.task_owner)
                self.assertIsNone(current.task_started_at)
                # A new request remains executable after exhaustion.
                next_id, next_message = self.accepted()
                self.manager.execute(self.ctx, **next_message)
                self.settled(next_id, states.ACTIVE)

    def test_real_deployment_failure_preserves_superseding_claim(self):
        deployment_id, message = self.accepted()

        def superseded(*args):
            current = objects.Service.get_by_id(self.ctx, self.service.id)
            current.release_task(current.task_state)
            current.acquire_task(states.RECONCILING, owner='domain_change')
            raise RuntimeError('setup failed')

        with mock.patch.object(deployments.Recovery, '_prepare',
                               side_effect=superseded), mock.patch.object(
                                   deployments.safe_log,
                                   'log_failure') as log:
            self.assertRaises(RuntimeError, self.manager.execute,
                              self.ctx, **message)
        log.assert_called_once()
        self.assertEqual(message['operation'],
                         log.call_args.kwargs['operation'])
        self.assertEqual(self.service.id, log.call_args.kwargs['resource'])
        self.assertEqual(self.ctx.request_id,
                         log.call_args.kwargs['request_id'])
        current = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.RECONCILING, current.task_state)
        self.assertEqual('domain_change', current.task_owner)
        self.assertEqual(states.PENDING, objects.Deployment.get_by_id(
            self.ctx, deployment_id).status)
        self.assertEqual(0, self.enqueue.call_count)

    def test_later_secret_is_synced_before_deploy(self):
        ref = 'https://secrets.example.com/later'
        self.store.values[ref] = (self.ctx.project_id, 'runtime-only')
        secret = objects.Secret(
            self.ctx, project_id=self.ctx.project_id,
            service_id=self.service.id, key='RABBITMQ_USERNAME', version=1,
            value_ref=ref, is_secret=True, build_time=False)
        secret.create()
        path = self.url + '/applications/app-uuid/envs'
        created = self.http.post(path, json={})

        def deploy(request, response):
            self.assertEqual(1, created.call_count)
            self.assertEqual('runtime-only',
                             created.last_request.json()['value'])
            return self.fixtures['deploy']

        self.http.post(self.url + '/deploy', json=deploy)
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.settled(deployment_id, states.ACTIVE)

    def test_application_reconcile_repairs_runtime_env_drift(self):
        self.service.status = states.ACTIVE
        self.service.save()
        path = self.url + '/applications/app-uuid'
        self.http.get(path, json={'status': 'running:healthy', 'fqdn': None})
        variables = {'KEY': base.EnvVar('runtime', is_secret=False)}

        def applied(request, response):
            self.http.get(path + '/envs', json=[request.json()])
            return {}

        created = self.http.post(path + '/envs', json=applied)
        with mock.patch.object(self.manager, '_variables',
                               return_value=variables), mock.patch.object(
                                   self.manager, '_fqdns', return_value=[]):
            self.assertFalse(reconcile.Reconciler(self.manager).run(
                self.service))
        self.assertEqual(1, created.call_count)
        self.assertEqual(0, self.enqueue.call_count)

    def dockerfile_deployment(self, current, fault=None, previous=True):
        if previous:
            first, message = self.accepted()
            self.manager.execute(self.ctx, **message)
            self.settled(first, states.ACTIVE)
        content = 'FROM busybox # private-content-marker\n'
        ref = 'https://secrets.example.com/dockerfile'
        self.store.values[ref] = (self.ctx.project_id, content)
        self.service.dockerfile_ref = ref
        self.service.save()
        self.http.get(self.url + '/applications/app-uuid', json={
            'uuid': 'app-uuid', 'status': 'running:healthy', **current})
        before = self.enqueue.call_count
        deployment_id, message = self.accepted()
        with self.assertLogs('coast.drivers.coolify.client',
                             level='DEBUG') as logs:
            self.manager.execute(self.ctx, **message)
        row = self.settled(deployment_id,
                           states.FAILED if fault else states.ACTIVE)
        self.assertEqual(fault, row.fault)
        self.assertEqual(before + (0 if fault else 1),
                         self.enqueue.call_count)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        self.assertFalse(any(r.method == 'PATCH' and
                             'dockerfile' in (r.json() or {})
                             for r in self.http.request_history))
        self.assertNotIn('private-content-marker', ' '.join(logs.output))
        self.assertNotIn('private-content-marker', str(row.fault))
        self.assertNotIn('private-content-marker',
                         str(self.manager.notifier.events))

    def test_unchanged_dockerfile_deploys_without_patch(self):
        self.dockerfile_deployment({
            'dockerfile': 'FROM busybox # private-content-marker\n'})

    def test_first_dockerfile_deployment_skips_patch(self):
        self.dockerfile_deployment({
            'dockerfile': 'FROM busybox # private-content-marker\n'},
            previous=False)

    def test_changed_dockerfile_fails_before_deploy(self):
        self.dockerfile_deployment(
            {'dockerfile': 'FROM busybox # private-content-marker'},
            "This app can't use a changed Dockerfile. "
            "Create a new app from the new Dockerfile, "
            "or deploy from a Git repository.")

    def test_missing_dockerfile_fails_before_deploy(self):
        self.dockerfile_deployment({}, deployments.COMPOSE_SYNC_FAILED)

    def test_env_sync_failure_never_enqueues_or_exposes_values(self):
        self.http.get(self.url + '/applications/app-uuid/envs',
                      exc=requests.ConnectionError('secret-value'))
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        row = self.settled(deployment_id, states.FAILED)
        self.assertEqual(deployments.ENV_SYNC_FAILED, row.fault)
        self.assertEqual(0, self.enqueue.call_count)
        self.assertNotIn('secret-value', str(self.manager.notifier.events))

    def test_synchronous_env_sync_failure_has_safe_fault(self):
        self.env.driver = 'noop'
        self.env.engine_version = 'noop-1.0'
        self.env.save()
        driver = self.manager._driver(self.env)
        deployment_id, message = self.accepted()
        with mock.patch.object(driver, 'set_env', side_effect=(
                base.DriverError('secret-value'))), mock.patch.object(
                    driver, 'deploy_app') as deploy:
            self.manager.execute(self.ctx, **message)
        row = self.settled(deployment_id, states.FAILED)
        self.assertEqual(deployments.ENV_SYNC_FAILED, row.fault)
        deploy.assert_not_called()

    def second_precision_storage(self):
        original = deployments.prepare

        def prepare(*args, **kwargs):
            checkpoint = original(*args, **kwargs)
            # Round only storage, not the checkpoint returned to enqueue.
            db_api.deployments.update(args[1], checkpoint.id, {
                'started_at': checkpoint.started_at.replace(microsecond=0)})
            return checkpoint

        storage = mock.patch.object(deployments, 'prepare',
                                    side_effect=prepare)
        storage.start()
        self.addCleanup(storage.stop)
        # Always start with a fractional clock value, independent of timing.
        now = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None, microsecond=123456)
        clock = mock.patch.object(deployments.timeutils, 'utcnow',
                                  return_value=now)
        clock.start()
        self.addCleanup(clock.stop)

    def test_enqueue_with_second_precision_storage(self):
        self.second_precision_storage()
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        row = self.settled(deployment_id, states.ACTIVE)
        self.assertEqual(0, row.started_at.microsecond)
        self.assertEqual('deployment-uuid', row.engine_ref)
        self.assertEqual(1, self.enqueue.call_count)

    def test_unknown_enqueue_with_second_precision_storage(self):
        self.second_precision_storage()
        deployment_id, message = self.accepted()
        enqueue = self.http.post(self.url + '/deploy', exc=requests.Timeout())
        self.manager.execute(self.ctx, **message)
        row = self.settled(deployment_id, states.FAILED)
        self.assertEqual(deployments.UNKNOWN, row.fault)
        self.assertIsNone(row.engine_ref)
        self.assertEqual(1, enqueue.call_count)

    def test_initial_deploy_with_second_precision_storage(self):
        self.second_precision_storage()
        self.initial_setup()
        created = self.request(
            'post', '/v1/services', self.service_body(name='initial'),
            status=202, complete=True).json['service']
        row = objects.Deployment.get_by_id(self.ctx, created['deployment_id'])
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ACTIVE, row.status)
        self.assertEqual('deployment-uuid', row.engine_ref)
        self.assertEqual(states.ACTIVE, service.status)
        self.assertEqual(states.NONE, service.task_state)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(2, self.usage()['in_use_apps'])
        self.assertEqual(1, self.enqueue.call_count)

    def test_checkpoint_and_reference_commit_before_network(self):
        deployment_id, message = self.accepted()

        def durable(request, response):
            row = objects.Deployment.get_by_id(self.ctx, deployment_id)
            self.assertEqual(states.DEPLOYING, row.status)
            self.assertEqual(deployments.ENQUEUE, row.fault)
            self.assertIsNotNone(row.started_at)
            # The checkpoint committed before enqueue acquired its fence.
            # Independent writers now wait until engine writes finish; the
            # paused-enqueue failover regression covers that serialization.
            return self.fixtures['deploy']

        def observed(request, response):
            row = objects.Deployment.get_by_id(self.ctx, deployment_id)
            self.assertEqual('deployment-uuid', row.engine_ref)
            with db_api.writer.using(self.ctx) as session:
                db_api._lock_project(session, self.ctx.project_id)
            return self.fixtures['deployment']

        self.http.post(self.url + '/deploy', json=durable)
        self.http.get(self.url + '/deployments/deployment-uuid', json=observed)
        self.send_rpc(message)
        row = self.settled(deployment_id, states.ACTIVE)
        self.assertEqual('a' * 40, row.resolved_ref)
        self.assertEqual(deployment_id, objects.Service.get_by_id(
            self.ctx, self.service.id).current_deployment_id)
        before = len(self.http.request_history)
        self.manager.execute(self.ctx, **message)
        self.manager.reconcile_deployments()
        self.assertEqual(before, len(self.http.request_history))

    def send_rpc(self, message):
        self.rpc.client.cast(self.ctx, 'execute', **message)
        self.manager.done.get(timeout=10)

    def test_ambiguous_enqueues_never_repeat_and_faults_are_safe(self):
        failures = [requests.Timeout('password=secret'),
                    requests.ConnectionError('token=secret'), 500, 503,
                    {'deployments': []}, 'invalid-json']
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                deployment_id, message = self.accepted()
                if isinstance(failure, Exception):
                    kwargs = {'exc': failure}
                elif isinstance(failure, int):
                    kwargs = {'status_code': failure,
                              'json': self.fixtures['error']}
                elif isinstance(failure, str):
                    kwargs = {'text': failure}
                else:
                    kwargs = {'json': failure}
                enqueue = self.http.post(self.url + '/deploy', **kwargs)
                self.manager.execute(self.ctx, **message)
                row = self.settled(deployment_id, states.FAILED)
                self.assertEqual(deployments.UNKNOWN, row.fault)
                self.manager.execute(self.ctx, **message)
                self.manager.reconcile_deployments()
                self.assertEqual(1, enqueue.call_count)
                events = objects.EventList.get_all(self.ctx, filters={
                    'resource_id': deployment_id, 'outcome': 'failure'})
                self.assertEqual(1, len(events))
                self.assertEqual(deployments.UNKNOWN, events[0].message)
                self.assertNotIn('secret', row.fault)
                self.assertIn('coast.deployment.create.error',
                              [name for name, body in
                               self.manager.notifier.events])

    def test_crash_mid_enqueue_waits_for_timeout_then_fails(self):
        deployment_id, message = self.accepted()
        enqueue = self.http.post(self.url + '/deploy', exc=SystemExit())
        with self.assertRaises(SystemExit):
            self.manager.execute(self.ctx, **message)
        self.manager.reconcile_deployments()
        self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
            self.ctx, deployment_id).status)
        self.age(deployment_id)
        replacement = manager.Manager(
            self.conf, notifier=self.manager.notifier)
        tasks = service.DeploymentTasks(self.conf, replacement)
        tasks.run_periodic_tasks(None)
        self.settled(deployment_id, states.FAILED)
        self.assertEqual(1, enqueue.call_count)

    def test_settle_tasks_settle_deployment(self):
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.manager.done.get(timeout=10)
        tasks = service.SettleTasks(self.conf, self.manager)
        tasks.run_periodic_tasks(None)
        self.settled(deployment_id, states.ACTIVE)

    def test_settle_skips_while_a_sweep_runs(self):
        with mock.patch.object(deployments, 'sweep') as sweep:
            self.assertTrue(self.manager._settle_lock.acquire())
            try:
                self.manager.settle_deployments()
            finally:
                self.manager._settle_lock.release()
            sweep.assert_not_called()
            self.manager.settle_deployments()
            sweep.assert_called_once_with(self.manager)

    def test_settle_poll_interval_default(self):
        self.assertEqual(15, self.conf.conductor.settle_poll_interval)

    def test_crash_after_reference_recovers_without_enqueue(self):
        deployment_id, message = self.accepted()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      exc=SystemExit())
        with self.assertRaises(SystemExit):
            self.manager.execute(self.ctx, **message)
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        self.assertEqual('deployment-uuid', row.engine_ref)
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['deployment'])
        replacement = manager.Manager(
            self.conf, notifier=self.manager.notifier)
        tasks = service.DeploymentTasks(self.conf, replacement)
        tasks.run_periodic_tasks(None)
        self.settled(deployment_id, states.ACTIVE)
        self.assertEqual(1, self.enqueue.call_count)

    def test_known_ref_pending_then_failed_keeps_previous(self):
        first, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        deployment_id, message = self.accepted()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['queued'])
        self.manager.execute(self.ctx, **message)
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        self.assertEqual(states.DEPLOYING, row.status)
        failed = dict(self.fixtures['deployment'], status='failed')
        self.http.get(self.url + '/deployments/deployment-uuid', json=failed)
        self.manager.reconcile_deployments()
        self.settled(deployment_id, states.FAILED)
        self.assertEqual(first, objects.Service.get_by_id(
            self.ctx, self.service.id).current_deployment_id)
        self.assertEqual(states.ACTIVE, objects.Deployment.get_by_id(
            self.ctx, first).status)

    def test_transient_poll_preserves_reference_until_deadline(self):
        deployment_id, message = self.accepted()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      exc=requests.Timeout('password=secret'))
        self.manager.execute(self.ctx, **message)
        self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
            self.ctx, deployment_id).status)
        self.age(deployment_id)
        self.manager.reconcile_deployments()
        self.settled(deployment_id, states.FAILED)
        self.assertEqual(1, self.enqueue.call_count)

    def test_rate_limited_poll_preserves_reference_and_recovers(self):
        deployment_id, message = self.accepted()
        endpoint = self.http.get(self.url + '/deployments/deployment-uuid',
                                 status_code=429)
        with mock.patch.object(deployments.safe_log, 'log_failure') as logged:
            self.manager.execute(self.ctx, **message)
            current = objects.Deployment.get_by_id(self.ctx, deployment_id)
            self.assertEqual(states.DEPLOYING, current.status)
            self.assertEqual('deployment-uuid', current.engine_ref)
            self.assertEqual(self.conf.coolify.rate_limit_attempts,
                             endpoint.call_count)
            self.http.get(self.url + '/deployments/deployment-uuid',
                          json=self.fixtures['deployment'])
            self.manager.reconcile_deployments()
            self.settled(deployment_id, states.ACTIVE)
            logged.assert_not_called()
        self.assertEqual(1, self.enqueue.call_count)

    def test_persistent_rate_limited_poll_has_clear_fault_and_one_log(self):
        deployment_id, message = self.accepted()
        endpoint = self.http.get(self.url + '/deployments/deployment-uuid',
                                 status_code=429)
        with mock.patch.object(deployments.safe_log, 'log_failure',
                               wraps=deployments.safe_log.log_failure) as log:
            self.manager.execute(self.ctx, **message)
            current = objects.Deployment.get_by_id(self.ctx, deployment_id)
            self.assertEqual(states.DEPLOYING, current.status)
            self.assertEqual('deployment-uuid', current.engine_ref)
            log.assert_not_called()
            self.age(deployment_id)
            self.manager.reconcile_deployments()
            current = self.settled(deployment_id, states.FAILED)
            self.assertEqual(manager.RATE_LIMIT_FAULT, current.fault)
            self.assertEqual('deployment-uuid', current.engine_ref)
            self.assertEqual(2 * self.conf.coolify.rate_limit_attempts,
                             endpoint.call_count)
            self.manager.reconcile_deployments()
            self.assertEqual(1, log.call_count)
            self.assertIn('Engine polling rate limited', log.call_args.args[1])
        self.assertEqual(1, self.enqueue.call_count)

    def test_rate_limited_readiness_waits_then_recovers_or_logs_once(self):
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                self.config.config(stable_seconds=0, stable_samples=2,
                                   stable_sample_interval=0,
                                   group='conductor')
                deployment_id, message = self.accepted()
                running = {'uuid': 'app-uuid', 'status': 'running:healthy'}
                endpoint = self.http.get(
                    self.url + '/applications/app-uuid',
                    [{'json': running}, {'status_code': 429}])
                with mock.patch.object(
                        deployments.safe_log, 'log_failure',
                        wraps=deployments.safe_log.log_failure) as log:
                    self.manager.execute(self.ctx, **message)
                    current = objects.Deployment.get_by_id(
                        self.ctx, deployment_id)
                    self.assertEqual(states.DEPLOYING, current.status)
                    self.assertEqual('deployment-uuid', current.engine_ref)
                    self.assertNotIn(deployment_id, deployments.CRASH_SINCE)
                    self.assertEqual(1 + self.conf.coolify.rate_limit_attempts,
                                     endpoint.call_count)
                    log.assert_not_called()
                    if exhausted:
                        self.age(deployment_id)
                        self.http.get(
                            self.url + '/applications/app-uuid',
                            [{'json': running}, {'status_code': 429}])
                    else:
                        self.http.get(self.url + '/applications/app-uuid',
                                      json=running)
                    self.manager.reconcile_deployments()
                    current = self.settled(
                        deployment_id,
                        states.FAILED if exhausted else states.ACTIVE)
                    self.manager.reconcile_deployments()
                    if exhausted:
                        self.assertEqual(manager.RATE_LIMIT_FAULT,
                                         current.fault)
                        self.assertEqual(1, log.call_count)
                        self.assertIn('Engine polling rate limited',
                                      log.call_args.args[1])
                    else:
                        log.assert_not_called()

    def test_queued_timeout_and_missing_reference_fail_closed(self):
        for kwargs in ({'json': self.fixtures['queued']},
                       {'status_code': 404, 'json': self.fixtures['error']}):
            with self.subTest(kwargs=kwargs):
                deployment_id, message = self.accepted()
                self.http.get(self.url + '/deployments/deployment-uuid',
                              **kwargs)
                self.manager.execute(self.ctx, **message)
                self.age(deployment_id)
                self.manager.reconcile_deployments()
                self.settled(deployment_id, states.FAILED)

    def test_rollback_pins_commit_and_preserves_history(self):
        first, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        second, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        rollback = self.http.post(self.url + '/applications/app-uuid/rollback',
                                  json=self.fixtures['rollback'])
        self.http.get(self.url + '/deployments/rollback-uuid',
                      json=dict(self.fixtures['deployment'],
                                deployment_uuid='rollback-uuid'))
        restored, message = self.accepted(first)
        self.manager.execute(self.ctx, **message)
        self.settled(restored, states.ACTIVE)
        self.assertEqual({'commit': 'a' * 40}, rollback.last_request.json())
        self.assertEqual(states.ROLLED_BACK, objects.Deployment.get_by_id(
            self.ctx, second).status)

    def test_wrong_project_and_lost_task_never_touch_engine(self):
        deployment_id, message = self.accepted()
        other = context.RequestContext(project_id='other')
        with self.assertRaises(exception.NotFound):
            self.manager.execute(other, **message)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        service.release_task(states.DEPLOYING)
        self.manager.execute(self.ctx, **message)
        self.assertEqual(0, self.enqueue.call_count)
        self.assertEqual(states.PENDING, objects.Deployment.get_by_id(
            self.ctx, deployment_id).status)

    def test_cancel_during_poll_cannot_activate_or_release_new_task(self):
        deployment_id, message = self.accepted()

        def cancel(request, response):
            self.request(
                'post', '/v1/services/' + self.service.id + '/deployments/' +
                deployment_id + '/action', {'cancel': {}},
                status=202, complete=True)
            return self.fixtures['deployment']

        self.http.get(self.url + '/deployments/deployment-uuid', json=cancel)
        # Avoid recursive callback when the cancel driver observes status.
        driver = self.manager._driver(self.env)
        with mock.patch.object(driver, 'cancel_deployment', return_value=None):
            self.manager.execute(self.ctx, **message)
        self.settled(deployment_id, states.CANCELLED)
        self.assertIsNone(objects.Service.get_by_id(
            self.ctx, self.service.id).current_deployment_id)

    def initial_setup(self):
        # Q94: git create uses the appgroup and environment engine refs.
        # The API create body does not store environment_id yet, so stamp
        # the parent on the service row before the conductor runs.
        appgroup = objects.Appgroup(
            context=self.ctx, stack_id=self.env.id, name='site',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='appgroup-project')
        appgroup.create()
        environment = objects.Environment(
            context=self.ctx, appgroup_id=appgroup.id, name='stage',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='stage-uuid')
        environment.create()

        def stamp(_mapper, _connection, target):
            # The insert is the real Service.create path. Do not replace
            # the remotable method.
            if (target.stack_id == self.env.id and
                    not target.environment_id):
                target.environment_id = environment.id

        event.listen(models.Service, 'before_insert', stamp)
        self.addCleanup(event.remove, models.Service, 'before_insert',
                        stamp)
        self.http.get(self.url + '/applications', json=[])
        # The stored engine_ref is the Coolify uuid. The engine name is not
        # coast-<server id>.
        server = dict(self.fixtures['server'], name='existing-worker')
        self.worker.engine_ref = server['uuid']
        self.worker.save()
        self.http.get(self.url + '/servers/' + server['uuid'], json=server)
        self.http.get(self.url + '/servers', json=[server])
        self.http.post(self.url + '/applications/public',
                       json=self.fixtures['app_created'])
        self.http.get(self.url + '/applications/app-uuid',
                      json=self.fixtures['app'])
        self.http.get(self.url + '/applications/app-uuid/envs', json=[])
        self.http.get(self.url + '/applications/app-uuid/storages',
                      json=dict(self.fixtures['storages'],
                                file_storages=[], persistent_storages=[]))

    def test_initial_service_quota_commits_even_when_enqueue_is_unknown(self):
        self.initial_setup()
        enqueue = self.http.post(self.url + '/deploy', exc=requests.Timeout())
        created = self.request(
            'post', '/v1/services', self.service_body(name='initial'),
            status=202, complete=True).json['service']
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(2, self.usage()['in_use_apps'])
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ERROR, service.status)
        self.assertEqual(states.NONE, service.task_state)
        row = objects.Deployment.get_by_id(self.ctx, created['deployment_id'])
        self.assertEqual(states.FAILED, row.status)
        self.assertEqual(deployments.UNKNOWN, row.fault)
        self.manager.reconcile_deployments()
        self.assertEqual(1, enqueue.call_count)
        self.assertEqual(2, self.usage()['in_use_apps'])

    def test_initial_setup_failure_rolls_back_service_quota(self):
        self.initial_setup()
        self.http.post(self.url + '/applications/public', status_code=503,
                       json=self.fixtures['error'])
        self.http.get(self.url + '/applications/app-uuid',
                      json=self.fixtures['app'])
        created = self.request(
            'post', '/v1/services', self.service_body(name='initial'),
            status=202, complete=True).json['service']
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(1, self.usage()['in_use_apps'])
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, created['id']).status)
        self.assertEqual(states.FAILED, objects.Deployment.get_by_id(
            self.ctx, created['deployment_id']).status)
        self.assertEqual(0, self.enqueue.call_count)

    def test_delayed_enqueue_cannot_release_a_new_operation(self):
        deployment_id, message = self.accepted()
        # Pause before enqueue takes its transaction. Once it has the fence,
        # recovery must wait (covered by the paused-enqueue failover test).
        with mock.patch.object(deployments.Recovery, 'enqueue') as enqueue:
            self.manager.execute(self.ctx, **message)
        checkpoint = enqueue.call_args.args[1]
        self.age(deployment_id)
        self.manager.reconcile_deployments()
        self.settled(deployment_id, states.FAILED)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        service.acquire_task(states.RECONCILING)
        env = objects.Stack.get_by_id(self.ctx, self.env.id)
        env.acquire_task(states.RECONCILING)
        deployments.Recovery(self.manager).enqueue(self.ctx, checkpoint)
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        self.assertEqual(states.FAILED, row.status)
        self.assertIsNone(row.engine_ref)
        self.assertEqual(0, self.enqueue.call_count)
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.assertEqual(states.RECONCILING, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)

    def test_unknown_cancel_waits_for_enqueue_grace_then_releases(self):
        deployment_id, message = self.accepted()
        self.http.post(self.url + '/deploy', exc=SystemExit())
        with self.assertRaises(SystemExit):
            self.manager.execute(self.ctx, **message)
        self.request('post',
                     '/v1/services/' + self.service.id + '/deployments/' +
                     deployment_id + '/action', {'cancel': {}},
                     status=202, complete=True)
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.age(deployment_id)
        self.manager.reconcile_deployments()
        row = self.settled(deployment_id, states.CANCELLED)
        self.assertEqual(deployments.UNKNOWN, row.fault)
        self.assertIn('coast.deployment.update.end',
                      [name for name, body in self.manager.notifier.events])

    def test_sweep_can_retry_after_one_row_failure(self):
        deployment_id, message = self.accepted()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['queued'])
        self.manager.execute(self.ctx, **message)
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['deployment'])
        original = deployments.Recovery.reconcile
        first = [True]

        def transient(recovery, ctx, id):
            if first[0]:
                first[0] = False
                raise exception.Conflict()
            return original(recovery, ctx, id)

        with mock.patch.object(deployments.Recovery, 'reconcile', transient):
            self.manager.reconcile_deployments()
            self.manager.reconcile_deployments()
        self.settled(deployment_id, states.ACTIVE)

    def test_cancel_before_enqueue_checkpoint_releases_without_post(self):
        deployment_id, message = self.accepted()
        self.request('post',
                     '/v1/services/' + self.service.id + '/deployments/' +
                     deployment_id + '/action', {'cancel': {}},
                     status=202, complete=True)
        self.settled(deployment_id, states.CANCELLED)
        self.manager.execute(self.ctx, **message)
        self.assertEqual(0, self.enqueue.call_count)

    def test_cancel_initial_deployment_before_setup_keeps_service_quota(
            self):
        self.initial_setup()
        messages = []

        def capture(ctx, operation, resource, options=None):
            messages.append(dict(operation=operation,
                                 resource=resource.obj_clone(),
                                 options=options or {},
                                 reservation={'apps': 1},
                                 revision=rpcapi.revision(resource)))

        with mock.patch.object(self.rpc, 'cast', side_effect=capture):
            created = self.request(
                'post', '/v1/services', self.service_body(name='initial'),
                status=202).json['service']
        self.request(
            'post', '/v1/services/' + created['id'] + '/deployments/' +
            created['deployment_id'] + '/action', {'cancel': {}},
            status=202, complete=True)
        self.assertEqual(1, self.usage()['reserved_apps'])
        self.manager.execute(self.ctx, **messages[0])
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.NONE, service.task_state)
        self.assertEqual(states.STOPPED, service.status)
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(2, self.usage()['in_use_apps'])
        row = objects.Deployment.get_by_id(self.ctx, created['deployment_id'])
        self.assertEqual(states.CANCELLED, row.status)
        self.assertIsNotNone(row.finished_at)
        self.assertEqual(0, self.enqueue.call_count)

    def test_concurrent_recovery_completes_one_receipt(self):
        deployment_id, message = self.accepted()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['queued'])
        self.manager.execute(self.ctx, **message)
        barrier = threading.Barrier(2)
        driver = self.manager._driver(self.env)

        def observed(*args):
            barrier.wait(timeout=10)
            return base.ObservedStatus(states.ACTIVE, resolved_ref='a' * 40)

        def reconcile():
            ctx = context.RequestContext(project_id=self.ctx.project_id)
            deployments.Recovery(self.manager).reconcile(ctx, deployment_id)

        with mock.patch.object(driver, 'get_deployment_status', observed):
            with futures.ThreadPoolExecutor(max_workers=2) as pool:
                work = [pool.submit(reconcile) for unused in range(2)]
                for result in work:
                    result.result(timeout=15)
        self.settled(deployment_id, states.ACTIVE)
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': deployment_id, 'outcome': 'success'})
        self.assertEqual(1, len(events))
        self.assertEqual(1, self.enqueue.call_count)

    def test_busy_stack_fails_without_releasing_its_lock(self):
        deployment_id, message = self.accepted()
        self.env.acquire_task(states.UPGRADING)
        self.manager.execute(self.ctx, **message)
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        self.assertEqual(states.FAILED, row.status)
        self.assertEqual(manager.FAULT, row.fault)
        self.assertIsNotNone(row.finished_at)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.assertEqual(states.UPGRADING, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        self.assertEqual(0, self.enqueue.call_count)
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(1, self.usage()['in_use_apps'])

    def test_service_shows_deploying_until_terminal(self):
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'],
                                status='in_progress'))
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'], status='failed'))
        deployments.sweep(self.manager)
        self.settled(deployment_id, states.FAILED)
        # No verified previous release: failure is reflected on the service.
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, self.service.id).status)

    def start_action(self, action):
        self.http.post(self.url + '/applications/app-uuid/' + action,
                       json={})
        self.request('post', '/v1/services/' + self.service.id + '/action',
                     {action: {}}, status=202, complete=True)
        return objects.Service.get_by_id(self.ctx, self.service.id)

    def test_start_and_restart_wait_for_running(self):
        app = self.url + '/applications/app-uuid'
        self.assertEqual(states.STOPPED, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        self.http.get(app, json={'status': 'exited'})
        self.assertEqual(states.STARTING, self.start_action('start').status)
        deployments.sweep(self.manager)
        self.assertEqual(states.STARTING, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        self.http.get(app, json={'status': 'running:unknown'})
        deployments.sweep(self.manager)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        # Restart never trusts the stale engine status at once.
        self.http.get(app, json={'status': 'running:healthy'})
        self.assertEqual(states.STARTING, self.start_action('restart').status)
        deployments.sweep(self.manager)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_start_timeout_marks_error(self):
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'running:unhealthy'})
        self.assertEqual(states.STARTING, self.start_action('start').status)
        self.config.config(start_timeout=1, group='conductor')
        later = timeutils.utcnow() + datetime.timedelta(seconds=5)
        with mock.patch.object(deployments.timeutils, 'utcnow',
                               return_value=later):
            deployments.sweep(self.manager)
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, self.service.id).status)

    def test_start_timeout_ignores_later_writes(self):
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'exited'})
        self.assertEqual(states.STARTING, self.start_action('start').status)
        self.config.config(start_timeout=60, group='conductor')
        later = timeutils.utcnow() + datetime.timedelta(seconds=120)
        event = objects.Event(
            context=self.ctx, stack_id=self.service.stack_id,
            resource_type='app', resource_id=self.service.id,
            action='start', outcome='success')
        event.id = str(uuid.uuid4())
        event.create()
        with mock.patch.object(deployments.timeutils, 'utcnow',
                               return_value=later):
            # An env save bumps updated_at; the start still expires.
            row = objects.Service.get_by_id(self.ctx, self.service.id)
            row.settings_changed_at = later
            row.save()
            deployments.sweep(self.manager)
        row = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(states.ERROR, row.status)
        self.assertEqual(states.NONE, row.task_state)

    def test_http_check_gates_active_after_running(self):
        self.config.config(http_check=True, group='conductor')
        self.worker.floating_ip = '203.0.113.10'
        self.worker.save()
        self.service.ports = '80'
        self.service.save()
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'running:healthy'})
        with mock.patch('coast.network.domain_check.http_status',
                        return_value=502) as probe:
            self.assertEqual(states.STARTING,
                             self.start_action('start').status)
            deployments.sweep(self.manager)
            self.assertEqual(states.STARTING, objects.Service.get_by_id(
                self.ctx, self.service.id).status)
            probe.return_value = 200
            deployments.sweep(self.manager)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        host, address = probe.call_args.args
        self.assertEqual('203.0.113.10', address)
        self.assertIn(self.service.id[:4], host)

    def second_service(self, name='other'):
        created = self.request(
            'post', '/v1/services', self.service_body(deploy=False, name=name),
            status=202, complete=True).json['service']
        row = objects.Service.get_by_id(self.ctx, created['id'])
        row.engine_ref = 'app-uuid-2'
        row.status = states.ACTIVE
        row.save()
        self.http.get(self.url + '/applications/app-uuid-2',
                      json={'status': 'running:healthy'})
        self.http.get(self.url + '/applications/app-uuid-2/envs', json=[])
        self.http.get(self.url + '/applications/app-uuid-2/storages',
                      json={'file_storages': [], 'persistent_storages': []})
        self.http.patch(self.url + '/applications/app-uuid-2', json={})
        return row

    def test_second_service_actions_during_deploy(self):
        # Q149: a deploy on A must not block stop/start of B.
        other = self.second_service()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'],
                                status='in_progress'))
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.manager.done.get(timeout=10)
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        self.http.post(self.url + '/applications/app-uuid-2/stop', json={})
        self.http.get(self.url + '/applications/app-uuid-2',
                      json={'status': 'exited'})
        self.request('post', '/v1/services/' + other.id + '/action',
                     {'stop': {}}, status=202, complete=True)
        self.assertEqual(states.STOPPED, objects.Service.get_by_id(
            self.ctx, other.id).status)
        self.http.post(self.url + '/applications/app-uuid-2/start', json={})
        self.request('post', '/v1/services/' + other.id + '/action',
                     {'start': {}}, status=202, complete=True)
        self.assertEqual(states.STARTING, objects.Service.get_by_id(
            self.ctx, other.id).status)
        # Same service: a second start while the first still holds the task
        # is a clean 409, never ERROR.
        self.request('post', '/v1/services/' + other.id + '/action',
                     {'start': {}}, status=409)
        self.assertEqual(states.STARTING, objects.Service.get_by_id(
            self.ctx, other.id).status)
        # Finish the first deploy; A becomes ACTIVE, B stays STARTING.
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'],
                                status='finished'))
        deployments.sweep(self.manager)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        self.assertEqual(states.STARTING, objects.Service.get_by_id(
            self.ctx, other.id).status)

    def test_running_app_with_proxy_502_fails_fast_with_port_hint(self):
        self.config.config(http_check=True, deploy_unreachable_timeout=120,
                           group='conductor')
        self.worker.floating_ip = '203.0.113.10'
        self.worker.save()
        self.service.ports = '8080'
        self.service.save()
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.manager.done.get(timeout=10)
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'],
                                status='finished'))
        deployments.PROXY_ERRORS_SINCE.pop(deployment_id, None)
        clock = [1000.0]
        with mock.patch('coast.network.domain_check.http_status',
                        return_value=502), \
                mock.patch.object(deployments.time, 'monotonic',
                                  side_effect=lambda: clock[0]):
            deployments.sweep(self.manager)
            self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
                self.ctx, deployment_id).status)
            clock[0] += 119
            deployments.sweep(self.manager)
            self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
                self.ctx, deployment_id).status)
            clock[0] += 2
            deployments.sweep(self.manager)
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        self.assertEqual(states.FAILED, row.status)
        self.assertEqual('App is running but nothing answered on port 8080. '
                         'Check the port setting.', row.fault)
        self.assertNotIn(deployment_id, deployments.PROXY_ERRORS_SINCE)

    def test_finished_rollout_crash_loop_fails_with_hint(self):
        self.config.config(deploy_unreachable_timeout=120, group='conductor')
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'restarting:unknown'})
        self.http.get(self.url + '/applications/app-uuid/logs',
                      json={'logs': '2026-10-16T18:00:00Z boom'})
        self.http.get(self.url + '/applications/app-uuid/envs', json=[])
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.manager.done.get(timeout=10)
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'],
                                status='finished'))
        deployments.CRASH_SINCE.clear()
        clock = [1000.0]
        with mock.patch.object(deployments.time, 'monotonic',
                               side_effect=lambda: clock[0]):
            deployments.sweep(self.manager)
            self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
                self.ctx, deployment_id).status)
            clock[0] += 121
            deployments.sweep(self.manager)
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        self.assertEqual(states.FAILED, row.status)
        self.assertIn('keeps restarting', row.fault)
        self.assertIn('Last output:', row.fault)
        self.assertNotIn(deployment_id, deployments.CRASH_SINCE)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_known_variable_masking_reaches_live_and_stored_logs(self):
        for key, value, is_secret in (
                ('PRIVATE', 'coast-private', True),
                ('PUBLIC', 'public-value', False)):
            ref = 'https://secrets.example.com/' + key
            self.store.values[ref] = (self.ctx.project_id, value)
            objects.Secret(
                self.ctx, project_id=self.ctx.project_id,
                service_id=self.service.id, key=key, version=1,
                value_ref=ref, is_secret=is_secret, build_time=False).create()
        self.http.get(self.url + '/applications/app-uuid/envs', json=[
            {'key': 'PRIVATE', 'is_shown_once': True},
            {'key': 'PUBLIC', 'value': 'public-value'}])
        output = 'ready coast-private public-value'
        self.http.get(self.url + '/applications/app-uuid/logs', json={
            'logs': '2026-10-16T18:00:00Z ' + output})
        deployment_id, _message = self.accepted()
        deployment = objects.Deployment.get_by_id(self.ctx, deployment_id)
        deployment.engine_ref = 'deployment-uuid'
        deployment.save()
        self.http.get(self.url + '/deployments/deployment-uuid', json={
            'logs': '[{"timestamp":"2026-10-16T18:00:00Z",'
                    '"output":"ready coast-private public-value"}]'})
        for resource, identifier in (
                ('service', self.service.id), ('deployment', deployment.id)):
            result = self.manager.get_logs(self.ctx, resource, identifier)
            self.assertEqual('ready *** public-value',
                             result['lines'][0]['line'])
        deployments.Recovery(self.manager).runtime_logs(
            deployment, self.service, self.env, attempts=1)
        saved = objects.Deployment.get_by_id(self.ctx, deployment.id)
        self.assertEqual('ready *** public-value',
                         runtime_logs.stored(saved)['lines'][0]['line'])
        self.assertIn('ready *** public-value',
                      deployments.RUNTIME_LOGS[deployment.id])
        deployment.runtime_log = None
        deployment.save()
        loop = reconcile.Reconciler(self.manager)
        captured = loop.capture_logs(
            self.ctx, self.service, states.DEPLOYING,
            self.manager._driver(self.env), self.manager._endpoint(self.env),
            self.env)
        loop.persist_logs(*captured)
        loop.update_status(
            self.ctx, self.service, states.DEPLOYING,
            self.manager._driver(self.env), self.manager._endpoint(self.env),
            self.env)
        saved = objects.Deployment.get_by_id(self.ctx, deployment.id)
        self.assertEqual('ready *** public-value',
                         runtime_logs.stored(saved)['lines'][0]['line'])

    def test_logs_of_restarting_container_explain_why(self):
        runtime_logs.LAST.clear()
        self.http.get(self.url + '/applications/app-uuid/logs',
                      status_code=400, json={'message': 'not running'})
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'restarting:unknown'})
        with mock.patch.object(runtime_logs.time, 'sleep'):
            result = self.manager.get_logs(
                self.ctx, 'service', self.service.id)
        self.assertEqual([], result['lines'])
        self.assertIn('restarting', result['unavailable'])
        self.assertIn('background', result['unavailable'])

    def test_logs_of_restarting_container_use_brief_running_window(self):
        runtime_logs.LAST.clear()
        self.http.get(self.url + '/applications/app-uuid/envs', json=[])
        logs = self.url + '/applications/app-uuid/logs'
        self.http.get(logs, [
            {'status_code': 400, 'json': {'message': 'not running'}},
            {'status_code': 400, 'json': {'message': 'not running'}},
            {'json': {'logs': '2026-10-16T18:00:00Z boom: missing var'}}])
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'restarting:unknown'})
        with mock.patch.object(runtime_logs.time, 'sleep'):
            result = self.manager.get_logs(
                self.ctx, 'service', self.service.id)
        self.assertIn('recent runs', result['note'])
        self.assertIn('boom: missing var', result['lines'][-1]['line'])
        # Later reads fall back to the captured output.
        self.http.get(logs, status_code=400, json={'message': 'x'})
        with mock.patch.object(runtime_logs.time, 'sleep'):
            result = self.manager.get_logs(
                self.ctx, 'service', self.service.id)
        self.assertIn('last run (captured', result['note'])
        self.assertIn('boom: missing var', result['lines'][-1]['line'])

    def test_same_service_start_conflict_is_409(self):
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'exited'})
        self.http.post(self.url + '/applications/app-uuid/start', json={})
        self.request('post', '/v1/services/' + self.service.id + '/action',
                     {'start': {}}, status=202, complete=True)
        self.assertEqual(states.STARTING, objects.Service.get_by_id(
            self.ctx, self.service.id).status)
        # Hold the task artificially: a concurrent start must 409 at the API.
        self.service = objects.Service.get_by_id(self.ctx, self.service.id)
        self.service.acquire_task(states.RECONCILING)
        self.request('post', '/v1/services/' + self.service.id + '/action',
                     {'start': {}}, status=409)
        self.assertEqual(states.STARTING, objects.Service.get_by_id(
            self.ctx, self.service.id).status)

    def previous_release(self):
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.settled(deployment_id, states.ACTIVE)
        return deployment_id

    def assert_failed_service(self, deployment_id, expected, previous=None):
        self.settled(deployment_id, states.FAILED)
        row = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(expected, row.status)
        self.assertEqual(previous, row.current_deployment_id)

    def test_failed_rollout_crash_loop_clears_previous_active(self):
        previous = self.previous_release()
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'restarting:unknown'})
        deployment_id, message = self.accepted()
        driver = self.manager._driver(self.env)
        with mock.patch.object(deployments.Recovery, 'crashing',
                               return_value=True), \
                mock.patch.object(deployments.Recovery, 'runtime_logs'), \
                mock.patch.object(driver, 'get_app_status',
                                  return_value=base.ObservedStatus(
                                      states.ACTIVE)):
            self.manager.execute(self.ctx, **message)
        self.assert_failed_service(deployment_id, states.ERROR, previous)

    def test_engine_failure_keeps_verified_previous_release_active(self):
        previous = self.previous_release()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'], status='failed'))
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.assert_failed_service(deployment_id, states.ACTIVE, previous)

    def test_failed_rollout_observes_runtime_without_transaction(self):
        previous = self.previous_release()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'], status='failed'))
        deployment_id, message = self.accepted()
        driver = self.manager._driver(self.env)

        sessions = []

        def stopped(*args):
            try:
                sessions.append(self.ctx.session)
            except db_exception.NoEngineContextEstablished:
                sessions.append(None)
            return base.ObservedStatus(states.STOPPED)

        with mock.patch.object(driver, 'get_app_status', side_effect=stopped):
            self.manager.execute(self.ctx, **message)
        self.assertEqual([None], sessions)
        self.assert_failed_service(deployment_id, states.ERROR, previous)

    def test_timeout_clears_unverified_previous_active(self):
        previous = self.previous_release()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['queued'])
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.age(deployment_id)
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'exited'})
        self.manager.reconcile_deployments()
        self.assert_failed_service(deployment_id, states.ERROR, previous)

    def test_unknown_enqueue_does_not_restore_stale_active(self):
        previous = self.previous_release()
        self.http.post(self.url + '/deploy', exc=requests.Timeout())
        deployment_id, message = self.accepted()
        driver = self.manager._driver(self.env)
        sessions = []

        def unavailable(*args):
            try:
                sessions.append(self.ctx.session)
            except db_exception.NoEngineContextEstablished:
                sessions.append(None)
            raise base.EngineUnreachable('password=hidden')

        with mock.patch.object(driver, 'get_app_status',
                               side_effect=unavailable):
            self.manager.execute(self.ctx, **message)
        self.assertEqual([None], sessions)
        self.assert_failed_service(deployment_id, states.ERROR, previous)

    def test_engine_failure_replaces_stale_active_service_status(self):
        previous = self.previous_release()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['queued'])
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        row = objects.Service.get_by_id(self.ctx, self.service.id)
        row.status = states.ACTIVE
        row.save()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'], status='failed'))
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'exited'})
        self.manager.reconcile_deployments()
        self.assert_failed_service(deployment_id, states.ERROR, previous)

    def test_timeout_keeps_running_previous_release_active(self):
        previous = self.previous_release()
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['queued'])
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.age(deployment_id)
        self.manager.reconcile_deployments()
        self.assert_failed_service(deployment_id, states.ACTIVE, previous)
