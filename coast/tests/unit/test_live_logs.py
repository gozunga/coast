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

"""Live log RPC, project isolation and secret masking regressions."""

import json
from unittest import mock

import fixtures
import requests

from coast.common import context
from coast.common import exception
from coast.common import states
from coast.conductor import runtime_logs
from coast.drivers import base
from coast import objects
from coast.tests import deployments


class LiveLogsTest(deployments.DeploymentTest):
    def setUp(self):
        super().setUp()
        self.http.get(self.url + '/applications/app-uuid/envs', json=[])
        self.path = '/v1/services/' + self.service.id
        # Keep fake RPC's polling sleep intact: patching time.sleep on
        # the shared module turns its listener into a busy loop.
        clock = mock.Mock(wraps=runtime_logs.time, sleep=mock.Mock())
        self.useFixture(fixtures.MonkeyPatch(
            'coast.conductor.runtime_logs.time', clock))

    def test_deployment_log_returns_driver_lines_and_completion(self):
        dep_id = self.request('post', self.path + '/deployments', {
            'deployment': {}}, status=202, complete=True).json[
                'deployment']['id']
        self.manager.reconcile_deployments()
        response = self.request(
            'get', self.path + '/deployments/' + dep_id + '/log').json['log']
        self.assertEqual(dep_id, response['deployment_id'])
        self.assertTrue(response['complete'])
        self.assertTrue(response['lines'])
        self.assertEqual({'ts', 'stream', 'line'},
                         set(response['lines'][0]))
        foreign = context.RequestContext(project_id='other')
        self.assertRaises(exception.NotFound, self.manager.get_logs,
                          foreign, 'deployment', dep_id)
        self.request('get', self.path + '/deployments/' + dep_id + '/log',
                     headers={**self.headers, 'X-Project-Id': 'other'},
                     status=404)

    def test_service_logs_filter_since_tail_and_mask_token(self):
        self.http.get(self.url + '/applications/app-uuid/logs', json={
            'logs': '2026-10-10T01:00:00Z before\n'
                    '2026-10-10T01:00:01Z engine-secret after'})
        result = self.request('get', self.path +
                              '/logs?tail=1&since=2026-10-10T01:00:00Z').json
        self.assertEqual(1, len(result['logs']['lines']))
        line = result['logs']['lines'][0]
        self.assertIn('after', line['line'])
        self.assertNotIn('engine-secret', str(result))
        foreign = context.RequestContext(project_id='other')
        self.assertRaises(exception.NotFound, self.manager.get_logs,
                          foreign, 'service', self.service.id)
        self.request('get', self.path + '/logs',
                     headers={**self.headers, 'X-Project-Id': 'other'},
                     status=404)
        self.assertEqual([], self.request('get', self.path +
                         '/logs?tail=0').json['logs']['lines'])

    def _engine(self, path, **kwargs):
        # The last registered matcher wins.
        self.http.get(self.url + path, **kwargs)

    def _assert_log_fault(self, api_path, status, secret, cause=None,
                          logger_name=None):
        # logger_name limits the check to the API fault log. The RPC
        # server still records the exception text in its own log.
        logs = self.useFixture(fixtures.FakeLogger(name=logger_name))
        result = self.request('get', api_path, status=status)
        title = ('Service Unavailable' if status == 503
                 else 'Internal Server Error')
        self.assertEqual(title, result.json['faultstring'])
        self.assertNotIn(secret, result.text)
        if self.env.engine_token_ref:
            self.assertNotIn(self.env.engine_token_ref, result.text)
        self.assertNotIn(secret, logs.output)
        if cause is not None:
            self.assertIn(cause, logs.output)
        return result

    def test_service_logs_return_503_when_engine_is_unreachable(self):
        secret = 'password=secret'
        cases = (
            {'exc': requests.ConnectionError(secret)},
            {'status_code': 503, 'json': {'message': secret}},
        )
        for kwargs in cases:
            self._engine('/applications/app-uuid/logs', **kwargs)
            self._assert_log_fault(
                self.path + '/logs', 503, secret,
                'coast.drivers.base.EngineUnreachable')

    def test_deployment_log_returns_503_when_engine_is_unreachable(self):
        secret = 'password=secret'
        dep_id = self.request('post', self.path + '/deployments', {
            'deployment': {}}, status=202, complete=True).json[
                'deployment']['id']
        self.manager.reconcile_deployments()
        self._engine('/deployments/deployment-uuid',
                     exc=requests.ConnectionError(secret))
        self._assert_log_fault(
            self.path + '/deployments/' + dep_id + '/log', 503, secret,
            'coast.drivers.base.EngineUnreachable')

    def test_service_logs_return_503_when_engine_rejects_credential(self):
        secret = 'engine-secret'
        self._engine('/applications/app-uuid/logs',
                     status_code=401, json={'message': secret})
        self._assert_log_fault(
            self.path + '/logs', 503, secret,
            'coast.drivers.base.EngineAuthError')

    def test_service_logs_return_503_when_engine_token_is_missing(self):
        self.env.engine_token_ref = None
        self.env.save()
        self._assert_log_fault(
            self.path + '/logs', 503, 'Engine token reference is required',
            'coast.drivers.base.EngineAuthError',
            logger_name='coast.api.errors')

    def test_service_logs_return_500_for_invalid_engine_response(self):
        secret = 'engine-secret'
        self._engine('/applications/app-uuid/logs',
                     status_code=400, json={'message': secret})
        self._assert_log_fault(self.path + '/logs', 500, secret)

    def test_missing_engine_log_resources_return_empty_complete(self):
        dep_id, _ = self.accepted()
        row = objects.Deployment.get_by_id(self.ctx, dep_id)
        row.engine_ref = 'missing'
        row.save()
        for path, api_path in (
                ('/applications/app-uuid/logs', self.path + '/logs'),
                ('/deployments/missing', self.path + '/deployments/' +
                 dep_id + '/log')):
            response = self.http.get(self.url + path, status_code=404,
                                     json={'message': 'engine-secret'})
            result = self.request('get', api_path).json
            log = result.get('logs', result.get('log'))
            self.assertEqual([], log['lines'])
            self.assertTrue(log['complete'])
            self.assertEqual(1, response.call_count)
            self.assertNotIn('engine-secret', str(result))

    def test_template_deployment_logs_are_complete_during_rollout(self):
        dep_id, _ = self.accepted()
        row = objects.Deployment.get_by_id(self.ctx, dep_id)
        row.engine_ref = 'app-uuid'
        row.save()
        self.service.template_id = 'uptime-kuma'
        self.service.save()
        self.http.get(self.url + '/services/app-uuid', json={
            'applications': [{'name': 'uptime-kuma'}]})
        self.http.get(self.url + '/services/app-uuid/envs', json=[])
        response = self.http.get(self.url + '/services/app-uuid/logs', json={
            'logs': '2026-10-10T01:00:00Z engine-secret runtime ready'})
        path = self.path + '/deployments/' + dep_id + '/log'
        result = self.request('get', path).json['log']
        self.assertTrue(result['complete'])
        self.assertEqual('stdout', result['lines'][0]['stream'])
        self.assertNotIn('engine-secret', str(result))
        self.assertEqual(1, response.call_count)
        response = self.http.get(self.url + '/services/app-uuid/logs',
                                 status_code=404, json={})
        result = self.request('get', path).json['log']
        self.assertEqual([], result['lines'])
        self.assertTrue(result['complete'])
        self.assertEqual(1, response.call_count)
        self.assertFalse(any('/deployments/app-uuid' in request.path
                             for request in self.http.request_history))

    def test_old_agent_template_log_rejection_is_empty_complete_once(self):
        dep_id, _ = self.accepted()
        row = objects.Deployment.get_by_id(self.ctx, dep_id)
        row.engine_ref = 'app-uuid'
        row.save()
        self.service.template_id = 'uptime-kuma'
        self.service.save()
        self.http.get(self.url + '/services/app-uuid', json={
            'applications': [{'name': 'uptime-kuma'}]})
        for api_path, envelope in (
                (self.path + '/deployments/' + dep_id + '/log', 'log'),
                (self.path + '/logs', 'logs')):
            response = self.http.get(self.url + '/services/app-uuid/logs',
                                     status_code=403,
                                     json={'message': 'Forbidden'})
            result = self.request('get', api_path).json[envelope]
            self.assertEqual([], result['lines'])
            self.assertTrue(result['complete'])
            self.assertEqual(1, response.call_count)
        self.assertFalse(any('/deployments/app-uuid' in request.path
                             for request in self.http.request_history))

    def test_pending_deployment_has_empty_log(self):
        dep_id, _ = self.accepted()
        result = self.request('get', self.path + '/deployments/' +
                              dep_id + '/log').json['log']
        self.assertEqual({'deployment_id': dep_id, 'complete': False,
                          'lines': []}, result)
        self.assertEqual(states.PENDING,
                         objects.Deployment.get_by_id(self.ctx, dep_id).status)

    def test_delete_failed_create_without_engine_reference(self):
        self.service.engine_ref = None
        self.service.status = states.ERROR
        self.service.save()
        self.http.get(self.url + '/applications', json=[])
        self.http.get(self.url + '/security/keys', json=[])
        self.request('delete', self.path, status=202, complete=True)
        self.request('get', self.path, status=404)
        self.assertEqual(0, self.usage()['in_use_apps'])

    def failed_with_output(self):
        dep_id, message = self.accepted()
        row = objects.Deployment.get_by_id(self.ctx, dep_id)
        runtime_logs.persist(row, [base.LogLine(
            '', 'stderr', 'worker failed: password=hidden')])
        row.status = states.FAILED
        row.save()
        runtime_logs.LAST.clear()
        return row

    def test_stored_output_survives_cache_loss_and_empty_engine(self):
        row = self.failed_with_output()
        driver = self.manager._driver(self.env)
        for resource, resource_id in (('service', self.service.id),
                                      ('deployment', row.id)):
            method = ('get_app_logs' if resource == 'service'
                      else 'get_deployment_log')
            for failure in (None, base.EngineUnreachable(),
                            base.EngineAuthError(), base.DriverError()):
                kwargs = ({'return_value': []} if failure is None else
                          {'side_effect': failure})
                with mock.patch.object(driver, method, **kwargs):
                    result = self.manager.get_logs(
                        self.ctx, resource, resource_id)
                self.assertTrue(result['captured_at_failure'])
                self.assertIn('failure time', result['note'])
                self.assertIn('worker failed', result['lines'][0]['line'])
                self.assertNotIn('hidden', str(result))
        self.assertRaises(exception.NotFound, self.manager.get_logs,
                          context.RequestContext(project_id='other'),
                          'deployment', row.id)
        self.assertRaises(exception.NotFound, self.manager.get_logs,
                          context.RequestContext(project_id='other'),
                          'service', self.service.id)

    def test_runtime_field_microversion_and_bounded_persistence(self):
        row = self.failed_with_output()
        path = self.path + '/deployments/' + row.id
        self.assertNotIn('runtime_log', self.request(
            'get', path, headers={**self.headers, 'OpenStack-API-Version':
                                  'app-hosting 1.4'}).json['deployment'])
        self.assertEqual(row.runtime_log, self.request(
            'get', path, headers={**self.headers, 'OpenStack-API-Version':
                                  'app-hosting 1.5'}).json[
                                      'deployment']['runtime_log'])
        rows = self.request('get', self.path + '/deployments',
                            headers={**self.headers, 'OpenStack-API-Version':
                                     'app-hosting 1.5'}).json['deployments']
        self.assertEqual(row.runtime_log, rows[0]['runtime_log'])
        selected = self.request(
            'get', path + '?fields=runtime_log', headers={
                **self.headers, 'OpenStack-API-Version': 'app-hosting 1.5'})
        self.assertEqual(row.runtime_log,
                         selected.json['deployment']['runtime_log'])
        self.request('get', path + '?fields=runtime_log', headers={
            **self.headers, 'OpenStack-API-Version': 'app-hosting 1.4'},
            status=400)
        runtime_logs.persist(row, [base.LogLine('', 'stderr', 'é' * 40000)])
        stored = objects.Deployment.get_by_id(self.ctx, row.id).runtime_log
        self.assertLessEqual(len(stored.encode('utf-8')), 65535)
        self.assertTrue(stored)
        runtime_logs.persist(row, [])
        self.assertEqual(stored, objects.Deployment.get_by_id(
            self.ctx, row.id).runtime_log)

    def test_fresh_logs_take_priority_over_failure_tail(self):
        self.failed_with_output()
        self.http.get(self.url + '/applications/app-uuid/logs',
                      json={'logs': '2026-10-10T01:00:00Z recovered'})
        result = self.manager.get_logs(
                self.ctx, 'service', self.service.id)
        self.assertNotIn('captured_at_failure', result)
        self.assertEqual('recovered', result['lines'][0]['line'])

    def test_failure_output_read_by_logs_endpoint_is_durable(self):
        row = self.failed_with_output()
        row.runtime_log = None
        row.save()
        self.service.status = states.ERROR
        self.service.save()
        driver = self.manager._driver(self.env)
        lines = [base.LogLine('2026-10-10T01:00:00Z', 'stderr', 'new failure')]
        with mock.patch.object(driver, 'get_app_logs', return_value=lines):
            self.manager.get_logs(self.ctx, 'service', self.service.id)
        runtime_logs.LAST.clear()
        with mock.patch.object(driver, 'get_app_logs', return_value=[]):
            result = self.manager.get_logs(
                self.ctx, 'service', self.service.id)
        self.assertTrue(result['captured_at_failure'])
        self.assertEqual('new failure', result['lines'][0]['line'])

    def test_empty_reads_return_newest_captured_deployment(self):
        old = self.failed_with_output()
        newest = objects.Deployment(
            context=self.ctx, service_id=self.service.id, trigger='api',
            spec={}, status=states.PENDING)
        newest.create()
        runtime_logs.persist(newest, [base.LogLine('', 'stderr', 'new crash')])
        runtime_logs.LAST.clear()
        driver = self.manager._driver(self.env)
        with mock.patch.object(driver, 'get_app_logs', return_value=[]):
            result = self.manager.get_logs(
                self.ctx, 'service', self.service.id)
        self.assertEqual(newest.id, result['deployment_id'])
        self.assertTrue(result['captured_at'])
        self.assertEqual('new crash', result['lines'][0]['line'])
        self.assertNotEqual(old.id, result['deployment_id'])

    def test_retry_empty_then_error_then_catch_output_and_persist(self):
        row = self.failed_with_output()
        driver = self.manager._driver(self.env)
        with mock.patch.object(driver, 'get_app_logs', side_effect=[
                [], base.DriverError(),
                [base.LogLine('', 'stderr', 'caught once')]]):
            result = self.manager.get_logs(
                self.ctx, 'service', self.service.id)
        self.assertEqual('caught once', result['lines'][0]['line'])
        runtime_logs.LAST.clear()
        with mock.patch.object(driver, 'get_app_logs', return_value=[]):
            result = self.manager.get_logs(
                self.ctx, 'service', self.service.id)
        self.assertTrue(result['captured_at'])
        self.assertIn('caught once', str(result['lines']))
        self.assertIn('worker failed', str(result['lines']))
        self.assertIn('caught once', objects.Deployment.get_by_id(
            self.ctx, row.id).runtime_log)

    def test_append_deduplicates_overlapping_and_stale_capture_objects(self):
        row = self.failed_with_output()
        stale = objects.Deployment.get_by_id(self.ctx, row.id)
        runtime_logs.persist(row, [base.LogLine('', 'stderr', 'one\ntwo')])
        runtime_logs.persist(stale, [base.LogLine('', 'stderr', 'two\nthree')])
        runtime_logs.persist(stale, [base.LogLine('', 'stderr', 'one\ntwo')])
        lines = runtime_logs.stored(objects.Deployment.get_by_id(
            self.ctx, row.id))['lines']
        self.assertEqual(['one', 'two', 'three'],
                         [line['line'] for line in lines[-3:]])

    def test_retry_burst_bounds_attempts_and_elapsed_time(self):
        clock = [0.0]
        delays = []

        def sleep(seconds):
            delays.append(seconds)
            clock[0] += seconds

        def read():
            clock[0] += 0.75
            return []

        with mock.patch.object(runtime_logs.time, 'monotonic',
                               side_effect=lambda: clock[0]):
            self.assertEqual([], runtime_logs.burst(read, sleep=sleep))
        self.assertLessEqual(clock[0], 5)
        self.assertLessEqual(sum(delays), 4.5)

    def test_stored_engine_timestamps_survive_later_capture(self):
        row = self.failed_with_output()
        timestamp = '2026-10-16T21:48:05.785629+00:00'
        lines = [base.LogLine(timestamp, 'stdout', 'missing setting')]
        runtime_logs.persist(row, lines)
        runtime_logs.persist(row, lines)
        saved = objects.Deployment.get_by_id(self.ctx, row.id)
        result = runtime_logs.stored(saved)['lines']
        selected = [line for line in result
                    if line['line'] == 'missing setting']
        self.assertEqual([dict(ts=timestamp, stream='stdout',
                               line='missing setting')], selected)

    def test_untimed_repeats_use_one_capture_timestamp(self):
        row = self.failed_with_output()
        lines = [base.LogLine('', 'stderr', 'missing setting')] * 4
        lines.append(base.LogLine('', 'stdout', 'next message'))
        runtime_logs.persist(row, lines)
        saved = objects.Deployment.get_by_id(self.ctx, row.id)
        result = runtime_logs.stored(saved)['lines'][-2:]
        self.assertEqual('missing setting (repeated 4 times)',
                         result[0]['line'])
        self.assertEqual(result[0]['ts'], result[1]['ts'])
        self.assertEqual('stdout', result[1]['stream'])
        runtime_logs.persist(row, lines)
        self.assertEqual(saved.runtime_log, objects.Deployment.get_by_id(
            self.ctx, row.id).runtime_log)

    def test_same_message_at_distinct_engine_times_is_retained(self):
        row = self.failed_with_output()
        for timestamp in ('2026-10-16T21:48:05Z', '2026-10-16T21:49:05Z'):
            runtime_logs.persist(row, [base.LogLine(
                timestamp, 'stderr', 'missing setting')])
        saved = objects.Deployment.get_by_id(self.ctx, row.id)
        lines = runtime_logs.stored(saved)['lines'][-2:]
        self.assertEqual(2, len(lines))
        self.assertNotEqual(lines[0]['ts'], lines[1]['ts'])
        self.assertEqual(lines[0]['line'], lines[1]['line'])

    def test_structured_output_without_capture_time_is_rejected(self):
        row = self.failed_with_output()
        for timestamp in ('', '2026-10-10T01:00:00Z'):
            row.runtime_log = json.dumps(dict(
                ts=timestamp, stream='stderr', line='failure'))
            self.assertRaises(KeyError, runtime_logs.stored, row)

    def test_plain_text_output_is_rejected(self):
        row = self.failed_with_output()
        row.runtime_log = 'old failure\nold tail'
        self.assertRaises(ValueError, runtime_logs.stored, row)

    def test_escaped_oversized_log_retains_valid_record(self):
        row = self.failed_with_output()
        runtime_logs.persist(row, [base.LogLine('', 'stderr', '"' * 70000)])
        saved = objects.Deployment.get_by_id(self.ctx, row.id)
        self.assertLessEqual(len(saved.runtime_log.encode('utf-8')), 65535)
        result = runtime_logs.stored(saved)
        self.assertTrue(result['lines'][-1]['line'])
        self.assertEqual({'"'}, set(result['lines'][-1]['line']))

    def test_overlapping_capture_keeps_largest_observed_repeat_count(self):
        row = self.failed_with_output()
        line = base.LogLine('', 'stderr', 'missing setting')
        runtime_logs.persist(row, [line])
        runtime_logs.persist(row, [line] * 3)
        runtime_logs.persist(row, [line] * 2)
        saved = objects.Deployment.get_by_id(self.ctx, row.id)
        self.assertEqual('missing setting (repeated 3 times)',
                         runtime_logs.stored(saved)['lines'][-1]['line'])
