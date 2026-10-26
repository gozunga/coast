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

"""Optional HTTP check API and deployment stability regressions."""

from unittest import mock

from coast.common import states
from coast.conductor import deployments
from coast.conductor import runtime_logs
from coast.drivers import base
from coast.drivers.coolify import v4
from coast import objects
from coast.tests import app_api
from coast.tests import deployments as deployment_fixtures


class HealthAPITest(app_api.AppAPITest):
    def test_no_port_views_hide_stored_url_and_dns_target(self):
        self.domain.destroy()
        self.service.ports = None
        self.service.default_domain = 'stored.example.com'
        self.service.save()
        for version in ('1.0', '1.4', '1.5', '1.6'):
            shown = self.request('get', self.path, version=version).json[
                'service']
            listed = self.request('get', '/v1/services', version=version
                                  ).json['services'][0]
            for view in (shown, listed):
                self.assertIsNone(view['ports'])
                self.assertIsNone(view['default_url'])
                self.assertEqual([], view['dns_records'])
        self.request('post', self.path + '/domains',
                     {'domain': {'fqdn': 'worker.example.com'}}, status=400)

    def test_port_views_return_stored_url_and_dns_target(self):
        self.domain.destroy()
        self.service.default_domain = 'stored.example.com'
        self.service.save()
        shown = self.request('get', self.path).json['service']
        listed = self.request('get', '/v1/services').json['services'][0]
        for view in (shown, listed):
            self.assertEqual('https://stored.example.com',
                             view['default_url'])
            self.assertEqual([{'type': 'A', 'name': 'stored.example.com',
                               'value': self.server.floating_ip,
                               'purpose': 'routing'}], view['dns_records'])

    def test_no_port_conductor_ignores_explicit_and_stored_hosts(self):
        from coast.conductor import manager
        owner = manager.Manager(self.conf)
        self.domain.destroy()
        self.service.ports = None
        self.service.default_domain = 'stored.example.com'
        self.assertEqual([], owner._fqdns(self.ctx, self.service))
        self.assertEqual([], owner._fqdns_for(
            self.ctx, self.service, 'supplied.example.com'))
        self.service.template_id = 'template-id'
        self.assertEqual(['https://stored.example.com'],
                         owner._fqdns(self.ctx, self.service))

    def test_new_default_and_microversion_visibility(self):
        created = self.request('post', '/v1/services', self.create_body(),
                               version='1.4', status=202).json['service']
        self.assertEqual('none', created['health_check'])
        self.assertEqual('/', created['health_check_path'])
        self.assertIsNone(created['default_url'])
        self.assertIsNone(created['health_check_port'])
        path = '/v1/services/' + created['id']
        self.assertEqual('none', self.request(
            'get', path, version='1.4').json['service']['health_check'])
        self.assertIn('none', [row['health_check'] for row in self.request(
            'get', '/v1/services', version='1.4').json['services']])
        for version in ('1.0', '1.1', '1.2', '1.3'):
            shown = self.request('get', path, version=version).json['service']
            self.assertNotIn('health_check', shown)
            self.assertNotIn('health_check_port', shown)
            self.assertNotIn('health_check_path', shown)

    def test_old_versions_reject_new_settings(self):
        for version in ('1.0', '1.1', '1.2', '1.3'):
            for field, value in (('health_check', 'none'),
                                 ('health_check_port', 8080),
                                 ('health_check_path', '/ready')):
                self.request('post', '/v1/services',
                             self.create_body(**{field: value}),
                             version=version, status=406)
                self.request('patch', self.path,
                             {'service': {field: value}},
                             version=version, status=406)

    def test_http_defaults_and_updates(self):
        created = self.request('post', '/v1/services', self.create_body(
            health_check='http', ports='8080,9090'), version='1.4',
            status=202).json['service']
        self.assertEqual(8080, created['health_check_port'])
        self.assertEqual('/', created['health_check_path'])
        self.assertIsNotNone(created['default_url'])
        for options in ({'health_check_port': 9090,
                         'health_check_path': '/ready'},
                        {'health_check': 'none', 'ports': ''}):
            result = self.request('patch', '/v1/services/' + created['id'],
                                  {'service': options}, version='1.4').json[
                                      'service']
            for key, value in options.items():
                self.assertEqual(value, result[key])
        self.assertIsNone(result['default_url'])
        self.request('post', '/v1/services/' + created['id'] + '/domains',
                     {'domain': {'fqdn': 'worker.example.com'}}, status=400)

    def test_http_requires_ports_and_schema_values(self):
        for options in ({'health_check': 'tcp'},
                        {'health_check': 'http'},
                        {'health_check': 'http', 'ports': ''},
                        {'health_check': 'http', 'ports': '99999'},
                        {'health_check_port': 0},
                        {'health_check_port': 65536},
                        {'health_check_port': '80'},
                        {'health_check_path': 'ready'},
                        {'health_check_path': '/\r\nInjected: header'}):
            self.request('post', '/v1/services', self.create_body(**options),
                         version='1.4', status=400)
        self.request('patch', self.path, {'service': {'ports': ''}},
                     version='1.4', status=400)

    def test_updates_validate_stored_http_even_without_health_fields(self):
        self.domain.destroy()
        self.service.health_check = 'http'
        self.service.save()
        for ports in ('', None, '  '):
            self.request('patch', self.path, {'service': {'ports': ports}},
                         version='1.4', status=400)
        self.assertEqual(self.service.ports, objects.Service.get_by_id(
            self.ctx, self.service.id).ports)
        self.service.ports = None
        self.service.save()
        self.request('patch', self.path,
                     {'service': {'description': 'metadata only'}},
                     version='1.4', status=400)
        self.request('patch', self.path,
                     {'service': {'health_check': 'none'}}, version='1.4')

    def test_http_probe_path_and_none_bypass(self):
        from coast.conductor import manager
        owner = manager.Manager(self.conf)
        self.config.config(http_check=True, group='conductor')
        self.service.healthcheck_path = '/ready'
        with mock.patch('coast.network.domain_check.http_status',
                        return_value=200) as probe:
            self.assertEqual(200, owner._probe(self.ctx, self.service))
            self.assertEqual('/ready', probe.call_args.kwargs['path'])
            probe.reset_mock()
            self.service.health_check = 'none'
            self.assertIsNone(owner._probe(self.ctx, self.service))
            probe.assert_not_called()

    def test_null_ports_create_update_and_domain_guard(self):
        for index, port in enumerate((None, '', '  ')):
            body = self.create_body(ports=port)
            body['service']['name'] = 'worker-' + str(index)
            created = self.request(
                'post', '/v1/services', body, version='1.5', status=202).json[
                    'service']
            self.assertIsNone(created['default_url'])
            path = '/v1/services/' + created['id']
            self.unlock(created['id'])
            self.request('patch', path, {'service': {'ports': '8080'}},
                         version='1.5')
            result = self.request('patch', path,
                                  {'service': {'ports': None}},
                                  version='1.5').json['service']
            self.assertIsNone(result['ports'])
            self.assertIsNone(result['default_url'])
            self.request('patch', path, {'service': {'ports': '8080'}},
                         version='1.5')
            self.request('post', path + '/domains', {'domain': {
                'fqdn': 'worker-%s.example.com' % index}}, status=201)
            self.request('patch', path, {'service': {'ports': None}},
                         version='1.5', status=400)
        self.request('post', '/v1/services', self.create_body(
            health_check='http', ports=None), version='1.5', status=400)
        self.request('patch', self.path, {'service': {'ports': None}},
                     version='1.5', status=400)

    def test_no_port_driver_settings_clear_routing(self):
        self.service.ports = None
        self.service.health_check = 'none'
        source = objects.Source.get_by_id(self.ctx, self.service.source_id)
        source.build_pack = 'nixpacks'
        settings = v4.app_settings(self.service, source)
        self.assertEqual('', settings['ports_exposes'])
        self.assertEqual('', settings['domains'])
        self.assertFalse(settings['health_check_enabled'])


class HealthDeploymentTest(deployment_fixtures.DeploymentTest):
    def setUp(self):
        super().setUp()
        self.config.config(stable_seconds=20, group='conductor')
        for cache in (deployments.RUNNING_SINCE, deployments.CRASH_SINCE,
                      deployments.RUNTIME_LOGS,
                      deployments.PROXY_ERRORS_SINCE):
            cache.clear()
            self.addCleanup(cache.clear)
        self.service.health_check = 'none'
        self.service.save()
        self.clock = 1000.0
        clock = mock.patch.object(deployments.time, 'monotonic',
                                  side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def poll(self, deployment_id, expected):
        self.manager.reconcile_deployments()
        self.assertEqual(expected, objects.Deployment.get_by_id(
            self.ctx, deployment_id).status)

    def test_none_waits_for_stable_runtime_and_skips_http(self):
        deployment_id, message = self.accepted()
        with mock.patch.object(self.manager, '_probe') as probe:
            self.manager.execute(self.ctx, **message)
            self.poll(deployment_id, states.DEPLOYING)
            self.clock += 19
            self.poll(deployment_id, states.DEPLOYING)
            self.clock += 1
            self.poll(deployment_id, states.ACTIVE)
            probe.assert_not_called()
        self.assertNotIn(deployment_id, deployments.RUNNING_SINCE)

    def test_fast_crash_loop_that_looks_running_at_polls_is_not_active(self):
        self.config.config(stable_samples=3, deploy_unreachable_timeout=60,
                           group='conductor')
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        driver = self.manager._driver(self.env)
        flaky = [base.ObservedStatus(states.ACTIVE),
                 base.ObservedStatus(states.DEPLOYING),
                 base.ObservedStatus(states.ACTIVE)] * 10
        with mock.patch.object(driver, 'get_app_status',
                               side_effect=flaky), \
                mock.patch.object(deployments, 'sleep'):
            self.poll(deployment_id, states.DEPLOYING)
            self.clock += 20
            self.poll(deployment_id, states.DEPLOYING)
            self.clock += 61
            self.poll(deployment_id, states.DEPLOYING)
            self.clock += 20
            self.poll(deployment_id, states.FAILED)
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        self.assertIn('keeps restarting', row.fault)

    def test_restart_resets_stability_and_retains_crash_logs(self):
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.clock += 19
        app = self.url + '/applications/app-uuid'
        self.http.get(app, json={'status': 'restarting:unknown'})
        self.poll(deployment_id, states.DEPLOYING)
        driver = self.manager._driver(self.env)
        with mock.patch.object(driver, 'get_app_logs', return_value=iter([
                base.LogLine('', 'stderr', 'runtime error password=hidden')])):
            self.http.get(app, json={'status': 'running:unknown'})
            self.clock += 1
            self.poll(deployment_id, states.DEPLOYING)
        self.assertNotIn('hidden', deployments.RUNTIME_LOGS[deployment_id])
        self.clock += self.conf.conductor.deploy_unreachable_timeout
        self.http.get(app, json={'status': 'exited'})
        with mock.patch.object(driver, 'get_app_logs',
                               side_effect=base.DriverError('token=hidden')), \
                mock.patch.object(deployments, 'sleep') as sleep:
            self.poll(deployment_id, states.FAILED)
            self.assertEqual(9, sleep.call_count)
        fault = objects.Deployment.get_by_id(self.ctx, deployment_id).fault
        self.assertIn('keeps exiting', fault)
        self.assertIn('runtime error', fault)
        self.assertNotIn('hidden', fault)
        self.assertNotIn(deployment_id, deployments.RUNTIME_LOGS)

    def test_http_also_requires_base_stability(self):
        self.service.health_check = 'http'
        self.service.ports = '8080'
        self.service.save()
        deployment_id, message = self.accepted()
        with mock.patch('coast.conductor.manager.Manager._probe',
                        return_value=200) as probe:
            self.manager.execute(self.ctx, **message)
            probe.assert_not_called()
            self.clock += 20
            self.poll(deployment_id, states.ACTIVE)
            probe.assert_called_once()

    def test_http_no_answer_fails_after_timeout(self):
        self.service.health_check = 'http'
        self.service.ports = '8080'
        self.service.save()
        deployment_id, message = self.accepted()
        with mock.patch('coast.conductor.manager.Manager._probe',
                        return_value=0):
            self.manager.execute(self.ctx, **message)
            self.clock += 20
            self.poll(deployment_id, states.DEPLOYING)
            self.clock += self.conf.conductor.deploy_unreachable_timeout
            self.poll(deployment_id, states.FAILED)
        self.assertEqual(deployments.NO_ANSWER % '8080',
                         objects.Deployment.get_by_id(
                             self.ctx, deployment_id).fault)

    def test_start_none_waits_for_stability(self):
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'running:unhealthy'})
        self.http.post(self.url + '/applications/app-uuid/start', json={})
        self.request('post', '/v1/services/' + self.service.id + '/action',
                     {'start': {}}, status=202, complete=True)
        with mock.patch.object(self.manager, '_serving') as probe:
            deployments.settle_starting(
                self.manager, self.ctx, self.service.id)
            self.assertEqual(states.STARTING, objects.Service.get_by_id(
                self.ctx, self.service.id).status)
            self.clock += 20
            deployments.settle_starting(
                self.manager, self.ctx, self.service.id)
            self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
                self.ctx, self.service.id).status)
            probe.assert_not_called()

    def test_runtime_log_retry_keeps_last_success_and_bounds_tail(self):
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        recovery = deployments.Recovery(self.manager)
        deployment = objects.Deployment.get_by_id(self.ctx, deployment_id)
        driver = self.manager._driver(self.env)
        lines = [base.LogLine('', 'stderr', 'x' * 150) for _ in range(30)]
        with mock.patch.object(driver, 'get_app_logs', side_effect=[
                base.DriverError(), base.DriverError(), iter(lines)
                ]) as logs, \
                mock.patch.object(deployments, 'sleep') as sleep:
            recovery.runtime_logs(deployment, self.service, self.env,
                                  attempts=10)
        self.assertEqual(['x' * 150 + ' (repeated 30 times)'],
                         [line['line'] for line in runtime_logs.stored(
                             objects.Deployment.get_by_id(
                                 self.ctx, deployment_id))['lines']])
        self.assertEqual(3, logs.call_count)
        self.assertEqual(2, sleep.call_count)
        self.assertLessEqual(
            len(deployments.RUNTIME_LOGS[deployment_id]), 6000)

    def test_engine_http_failure_uses_no_answer_deadline(self):
        self.service.health_check = 'http'
        self.service.ports = '8080'
        self.service.save()
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'running:unhealthy'})
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.clock += 20
        self.poll(deployment_id, states.DEPLOYING)
        self.clock += self.conf.conductor.deploy_unreachable_timeout
        self.poll(deployment_id, states.FAILED)
        fault = objects.Deployment.get_by_id(self.ctx, deployment_id).fault
        self.assertEqual(deployments.NO_ANSWER % '8080', fault)

    def test_none_ignores_old_engine_http_health(self):
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'running:unhealthy'})
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.clock += 20
        self.poll(deployment_id, states.ACTIVE)

    def test_settle_catches_empty_reads_in_short_running_window(self):
        deployment_id, message = self.accepted()
        self.manager.execute(self.ctx, **message)
        self.http.get(self.url + '/applications/app-uuid',
                      json={'status': 'restarting:unknown'})
        driver = self.manager._driver(self.env)
        with mock.patch.object(driver, 'get_app_logs', side_effect=[
                [], [], [base.LogLine('', 'stderr', 'boot failed')]]), \
                mock.patch.object(deployments, 'sleep'):
            self.poll(deployment_id, states.DEPLOYING)
        self.assertEqual('boot failed', runtime_logs.stored(
            objects.Deployment.get_by_id(
                self.ctx, deployment_id))['lines'][0]['line'])
