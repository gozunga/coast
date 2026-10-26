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

"""API -> fake RPC -> Coolify fixtures -> conductor recovery acceptance."""

import threading
import time

from oslo_service import loopingcall

from coast.common import states
from coast.conductor import manager
from coast.conductor import service
from coast import objects
from coast.tests import deployments


class DeploymentRecoveryFlowTest(deployments.DeploymentTest):
    def test_api_reference_recovery_after_conductor_restart(self):
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['queued'])
        path = '/v1/services/' + self.service.id + '/deployments'
        row = self.request('post', path, {'deployment': {}},
                           status=202, complete=True).json['deployment']
        item = path + '/' + row['id']
        shown = self.request('get', item).json['deployment']
        self.assertEqual(states.DEPLOYING, shown['status'])
        self.assertEqual('deployment-uuid', objects.Deployment.get_by_id(
            self.ctx, row['id']).engine_ref)
        self.request('get', item, headers={**self.headers,
                     'X-Project-Id': 'other'}, status=404)
        self.request('post', item + '/action', {'cancel': {}},
                     headers={**self.headers, 'X-Roles': 'reader'}, status=403)
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['deployment'])
        replacement = manager.Manager(
            self.conf, notifier=self.manager.notifier)
        self.addCleanup(replacement.close)
        tasks = service.DeploymentTasks(self.conf, replacement)
        tasks.run_periodic_tasks(None)
        self.settled(row['id'], states.ACTIVE)
        shown = self.request('get', item).json['deployment']
        self.assertEqual(states.ACTIVE, shown['status'])
        self.assertEqual('a' * 40, shown['resolved_ref'])
        shown = self.request(
            'get', '/v1/services/' + self.service.id).json['service']
        self.assertEqual(row['id'], shown['current_deployment_id'])
        self.assertEqual(1, self.enqueue.call_count)
        receipts = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row['id'], 'outcome': 'success'})
        self.assertEqual(1, len(receipts))

    def test_service_timer_recovers_known_reference(self):
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=self.fixtures['queued'])
        row = self.request(
            'post', '/v1/services/' + self.service.id + '/deployments',
            {'deployment': {}}, status=202,
            complete=True).json['deployment']
        completed = threading.Event()
        recorder = self.manager.notifier

        class Notifier:
            def info(self, ctx, name, body):
                recorder.info(ctx, name, body)
                if (name == 'coast.deployment.create.end' and
                        body['resource_id'] == row['id']):
                    completed.set()

        replacement = manager.Manager(self.conf, notifier=Notifier())
        self.config.config(deploy_reconcile_interval=1, group='conductor')
        worker = service.Service(self.conf, endpoint=replacement)
        worker.start()
        try:
            self.assertEqual(states.DEPLOYING, objects.Deployment.get_by_id(
                self.ctx, row['id']).status)
            self.http.get(self.url + '/deployments/deployment-uuid',
                          json=self.fixtures['deployment'])
            deadline = time.monotonic() + 10

            def await_completion():
                if completed.is_set():
                    raise loopingcall.LoopingCallDone()
                if time.monotonic() > deadline:
                    raise RuntimeError('Deployment timer did not complete')

            # Use oslo.service's wait so its configured backend can run.
            # A native Event.wait blocks the eventlet backend's hub.
            wait = loopingcall.FixedIntervalLoopingCall(await_completion)
            wait.start(interval=0.1).wait()
            self.settled(row['id'], states.ACTIVE)
            self.assertIsNone(objects.Stack.get_by_id(
                self.ctx, self.env.id).conductor_host)
            self.assertEqual(1, self.enqueue.call_count)
        finally:
            worker.stop()

    def test_failed_deployment_service_status_follows_previous_runtime(self):
        path = '/v1/services/' + self.service.id
        first = self.request('post', path + '/deployments',
                             {'deployment': {}}, status=202,
                             complete=True).json['deployment']
        self.settled(first['id'], states.ACTIVE)
        self.http.get(self.url + '/deployments/deployment-uuid',
                      json=dict(self.fixtures['deployment'], status='failed'))
        for runtime, expected in (('running:healthy', states.ACTIVE),
                                  ('exited', states.ERROR)):
            with self.subTest(runtime=runtime):
                self.http.get(self.url + '/applications/app-uuid',
                              json={'status': runtime})
                failed = self.request('post', path + '/deployments',
                                      {'deployment': {}}, status=202,
                                      complete=True).json['deployment']
                self.settled(failed['id'], states.FAILED)
                shown = self.request('get', path).json['service']
                self.assertEqual(expected, shown['status'])
                self.assertEqual(first['id'], shown['current_deployment_id'])
