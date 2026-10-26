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

"""Service drift, isolation, eligibility, bounds and retry regressions."""

import datetime
import time
from unittest import mock

import yaml

from coast.common import exception
from coast.common import states
from coast.conductor import deployments
from coast.conductor import reconcile
from coast.conductor import runtime_logs
from coast.drivers import base
from coast.drivers.coolify import v4
from coast import objects
from coast.tests import conductor


class ReconcileTest(conductor.ConductorTest):
    def setUp(self):
        super().setUp()
        self.stack()
        worker = next(row for row in objects.ServerList.get_all(self.ctx)
                      if row.role == 'worker')
        body = {'service': {
            **self.service_parents(worker.stack_id), 'name': 'web',
            'server_id': worker.id, 'source': {
                'type': 'git', 'repo_url': 'https://example.com/web.git'}}}
        created = self.request('post', '/v1/services', body, status=202,
                               complete=True).json['service']
        self.row = objects.Service.get_by_id(self.ctx, created['id'])
        self.stack_row = objects.Stack.get_by_id(self.ctx, worker.stack_id)
        self.loop = self.manager.service_reconciler
        sleeper = mock.patch('coast.conductor.runtime_logs.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.driver = self.manager._driver(self.stack_row)
        self.ep = self.manager._endpoint(self.stack_row)

    def test_engine_compare_and_status_reads_leave_task_free(self):
        self.loop.run(self.row)
        compare = self.driver.compare_settings
        status = self.driver.get_app_status

        def unlocked(method):
            def read(*args, **kwargs):
                self.assertEqual(states.NONE, objects.Service.get_by_id(
                    self.ctx, self.row.id).task_state)
                return method(*args, **kwargs)
            return read

        with mock.patch.object(self.driver, 'compare_settings',
                               side_effect=unlocked(compare)), \
                mock.patch.object(self.driver, 'get_app_status',
                                  side_effect=unlocked(status)):
            self.assertFalse(self.loop.run(self.row))
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.row.id).task_state)

    def test_real_storage_observation_does_not_hold_task(self):
        self._real_storage_reconcile(False)

    def test_real_storage_writes_use_task_after_unlocked_observation(self):
        self._real_storage_reconcile(True)

    def _real_storage_reconcile(self, drift):
        if drift:
            objects.Storage(
                context=self.ctx, stack_id=self.stack_row.id, type='bind',
                host_path='/srv/data', read_only=True, name=None, size_gb=None,
                service_id=self.row.id, server_id=self.row.server_id,
                container_path='/data', status=states.IN_USE,
                origin='api').create()
        driver = v4.CoolifyV4Driver(self.conf)
        fqdns = self.manager._fqdns(self.ctx, self.row)
        client = mock.MagicMock()
        client.__enter__.return_value = client
        reads, writes = [], []

        def request(method, path, **kwargs):
            if method == 'POST':
                self.assertEqual(states.RECONCILING,
                                 objects.Service.get_by_id(
                                     self.ctx, self.row.id).task_state)
                writes.append(kwargs['data'])
                return {}
            self.assertEqual('GET', method)
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, self.row.id).task_state)
            reads.append(path)
            if path.endswith('/storages'):
                time.sleep(0.01)
                return {'persistent_storages': [], 'file_storages': []}
            if path == '/servers' or path.endswith('/envs'):
                return []
            return {'fqdn': ','.join(fqdns), 'status': 'running'}

        client.request.side_effect = request
        with mock.patch.object(self.manager, '_driver', return_value=driver), \
                mock.patch.object(driver, '_client', return_value=client):
            self.assertFalse(self.loop.run(self.row))
        self.assertTrue(any(path.endswith('/storages') for path in reads))
        self.assertEqual(1 if drift else 0, len(writes))
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.row.id).task_state)

    def test_engine_observation_is_discarded_after_concurrent_edit(self):
        def edit(*args):
            current = objects.Service.get_by_id(self.ctx, self.row.id)
            current.description = 'updated while reading'
            current.save()
            return {'env': True, 'domains': True}

        with mock.patch.object(self.driver, 'compare_settings',
                               side_effect=edit), \
                mock.patch.object(self.driver, 'set_env') as set_env, \
                mock.patch.object(self.driver, 'set_domains') as set_domains:
            self.assertFalse(self.loop.run(self.row))
        set_env.assert_not_called()
        set_domains.assert_not_called()
        current = objects.Service.get_by_id(self.ctx, self.row.id)
        self.assertEqual('updated while reading', current.description)
        self.assertEqual(states.NONE, current.task_state)

    def test_disabled_default_route_is_removed_and_reenabled(self):
        self.row.ports = '80'
        self.row.default_domain = 'default.example.com'
        self.row.default_domain_enabled = False
        self.row.save()
        domain = objects.Domain(
            context=self.ctx, service_id=self.row.id,
            fqdn='shop.example.com', primary=True, tls='none',
            auto_dns=False, dns_mode='manual', verification_token='token',
            status=states.ACTIVE)
        domain.create()
        self.driver.set_domains(self.ep, self.stack_row, self.row,
                                ['https://default.example.com',
                                 'http://shop.example.com'])
        self.assertFalse(self.loop.run(self.row))
        self.assertEqual(['http://shop.example.com'], self.driver.list_domains(
            self.ep, self.stack_row, self.row))
        self.assertIsNone(self.manager._advertised_host(self.ctx, self.row))
        self.manager._sync_default_domain(
            self.ctx, self.row, self.stack_row, self.driver)
        self.assertEqual(['http://shop.example.com'], self.driver.list_domains(
            self.ep, self.stack_row, self.row))
        self.row.default_domain_enabled = True
        self.row.save()
        self.assertFalse(self.loop.run(self.row))
        self.assertIn('http://shop.example.com', self.driver.list_domains(
            self.ep, self.stack_row, self.row))
        self.assertIn('https://' + self.row.default_domain,
                      self.driver.list_domains(
                          self.ep, self.stack_row, self.row))

    def events(self):
        return list(objects.EventList.get_all(
            self.ctx, filters={'resource_id': self.row.id,
                               'action': 'reconcile'}))

    def test_unrestricted_compose_survives_deploy_and_reconcile(self):
        raw = yaml.safe_dump({'services': {'web': {
            'image': 'nginx', 'privileged': True, 'network_mode': 'host',
            'pid': 'host', 'volumes': [
                '/host:/data',
                '/var/run/docker.sock:/var/run/docker.sock']}}})
        deployment = objects.Deployment.get_by_id(
            self.ctx, self.row.current_deployment_id)
        deployment.spec = {'compose_ref': self.store.put(self.ctx, raw)}
        for template in ('beszel', None):
            with self.subTest(template=template):
                self.row.template_id = template
                self.row.save()
                self.manager._push_compose(
                    self.ctx, self.driver, self.ep, self.stack_row,
                    self.row, deployment)
                self.assertEqual(raw, self.driver.get_compose(
                    self.ep, self.stack_row, self.row))
                self.loop.run(self.row)
                self.assertFalse(any(event.outcome == 'failure'
                                     for event in self.events()))

    def test_reconcile_applies_current_bind_mounts_without_restart(self):
        worker = objects.Server.get_by_id(self.ctx, self.row.server_id)
        mount = objects.Storage(
            context=self.ctx, stack_id=self.stack_row.id, type='bind',
            host_path='/srv/data', read_only=True, name=None, size_gb=None,
            service_id=self.row.id, server_id=worker.id,
            container_path='/data', status=states.IN_USE, origin='api')
        mount.create()
        self.assertFalse(self.loop.run(self.row))
        observed = self.driver.list_volume_mounts(
            self.ep, self.stack_row, self.row)
        self.assertEqual([dict(type='bind', source='/srv/data', name=None,
                               container_path='/data', read_only=True)],
                         observed)
        mount.read_only = False
        mount.save()
        self.assertFalse(self.loop.run(self.row))
        self.assertFalse(self.driver.list_volume_mounts(
            self.ep, self.stack_row, self.row)[0]['read_only'])
        mount.destroy()
        self.assertFalse(self.loop.run(self.row))
        self.assertEqual([], self.driver.list_volume_mounts(
            self.ep, self.stack_row, self.row))
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.row.id).status)

    def test_reconcile_turns_builtin_terminal_off(self):
        self.driver.terminal_enabled[self.stack_row.id] = True
        self.loop.run(self.row)
        self.assertIs(False, self.driver.terminal_enabled[self.stack_row.id])

    def test_repair_and_clean_pass(self):
        self.driver.set_domains(self.ep, self.stack_row, self.row,
                                ['drift.example.com'])
        self.assertFalse(self.loop.run(self.row))
        self.assertEqual({'env': False, 'domains': False},
                         self.driver.compare_settings(
                             self.ep, self.stack_row, self.row, {},
                             self.manager._fqdns(self.ctx, self.row)))
        self.assertEqual(['start', 'success'],
                         [event.outcome for event in self.events()])
        self.assertFalse(self.loop.run(self.row))
        self.assertEqual(2, len(self.events()))
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.row.id).task_state)

    def test_env_repair_and_running_mismatch_never_restarts(self):
        variables = {'PASSWORD': base.EnvVar('tenant-secret', is_secret=True)}
        self.driver.stop_app(self.ep, self.stack_row, self.row)
        with mock.patch.object(self.manager, '_variables',
                               return_value=variables):
            self.assertTrue(self.loop.run(self.row))
        self.assertEqual(states.STOPPED, self.driver.get_app_status(
            self.ep, self.stack_row, self.row).status)
        self.assertEqual(states.STOPPED, objects.Service.get_by_id(
            self.ctx, self.row.id).status)
        self.assertIn('STOPPED', self.events()[-1].message)
        self.assertNotIn('tenant-secret', str(self.events()))
        self.assertNotIn('tenant-secret', str(self.manager.notifier.events))
        self.assertFalse(self.driver.compare_settings(
            self.ep, self.stack_row, self.row, variables,
            self.manager._fqdns(self.ctx, self.row))['env'])

    def test_busy_and_inactive_rows_are_skipped(self):
        for row, field, value in (
                (self.row, 'task_state', states.RECONCILING),
                (self.stack_row, 'task_state', states.RECONCILING),
                (self.stack_row, 'status', states.DEGRADED),
                (self.row, 'status', states.STOPPED)):
            old = getattr(row, field)
            if field == 'task_state':
                row.acquire_task(value)
            else:
                setattr(row, field, value)
                row.save()
            self.assertFalse(self.loop.run(self.row))
            self.assertEqual([], self.events())
            if field == 'task_state':
                row.release_task(value)
            else:
                setattr(row, field, old)
                row.save()

    def test_unknown_and_engine_errors_release_task(self):
        for observed in (None, base.EngineUnreachable('secret-url')):
            kwargs = ({'return_value': {'env': None, 'domains': None}}
                      if observed is None else {'side_effect': observed})
            with mock.patch.object(self.driver, 'compare_settings', **kwargs):
                self.assertTrue(self.loop.run(self.row))
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, self.row.id).task_state)
        self.assertNotIn('secret-url', str(self.events()))

    def test_foreign_project_and_cas_conflict(self):
        foreign = self.row.obj_clone()
        foreign.project_id = 'foreign'
        self.assertRaises(exception.NotFound, self.loop.run, foreign)
        with mock.patch.object(objects.Service, 'acquire_task',
                               side_effect=exception.Conflict):
            self.assertFalse(self.loop.run(self.row))
        self.assertEqual([], self.events())

    def test_bounded_cursor_backoff_and_budget(self):
        rows = [self.row.obj_clone() for _ in range(3)]
        for i, row in enumerate(rows):
            row.id = str(i)
        with mock.patch.object(objects.ServiceList, 'get_all',
                               return_value=rows), mock.patch.object(
                reconcile, 'LIMIT', 3), mock.patch.object(
                self.loop, 'run', return_value=True) as run:
            self.loop.sweep()
            self.assertEqual('2', self.loop.marker)
            self.assertEqual(3, len(self.loop.retries))
            self.loop.sweep()
            self.assertEqual(3, run.call_count)
            with mock.patch.object(reconcile.time, 'monotonic',
                                   side_effect=[0, 31]):
                self.loop.sweep()
            self.assertEqual(3, run.call_count)
        self.assertTrue(all(due > 0 for due, _ in self.loop.retries.values()))

    def test_crash_loop_and_stable_recovery(self):
        self.config.config(deploy_unreachable_timeout=30, stable_seconds=20,
                           group='conductor')
        self.clock = 0
        with mock.patch.object(reconcile.timeutils, 'utcnow',
                               side_effect=lambda: (
                                   datetime.datetime(2026, 1, 1) +
                                   datetime.timedelta(seconds=self.clock))), \
                mock.patch.object(self.driver, 'get_app_status') as status:
            status.return_value = base.ObservedStatus(states.DEPLOYING)
            self.assertTrue(self.loop.run(self.row))
            self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
                self.ctx, self.row.id).status)
            self.clock = 30
            self.loop.run(self.row)
            row = objects.Service.get_by_id(self.ctx, self.row.id)
            self.assertEqual(states.ERROR, row.status)
            self.assertIn('restarting', row.reconcile_fault)
            self.assertIn('restarting', self.request(
                'get', '/v1/services/' + row.id, headers={
                    **self.headers, 'OpenStack-API-Version':
                    'app-hosting 1.5'}).json[
                    'service']['reconcile_fault'])
            status.return_value = base.ObservedStatus(states.ACTIVE)
            self.clock = 31
            self.loop.run(self.row)
            self.assertEqual(states.ERROR, objects.Service.get_by_id(
                self.ctx, row.id).status)
            # Restarting resets continuous recovery, even after brief running.
            self.clock = 40
            status.return_value = base.ObservedStatus(states.DEPLOYING)
            self.loop.run(self.row)
            status.return_value = base.ObservedStatus(states.ACTIVE)
            self.clock = 41
            self.loop.run(self.row)
            self.clock = 61
            self.loop.run(self.row)
            row = objects.Service.get_by_id(self.ctx, row.id)
            self.assertEqual(states.ACTIVE, row.status)
            self.assertIsNone(row.reconcile_fault)
            self.assertEqual(states.NONE, row.task_state)

    def test_engine_error_and_unrelated_error(self):
        with mock.patch.object(
                self.driver, 'get_app_status',
                return_value=base.ObservedStatus(states.ERROR)):
            self.loop.run(self.row)
        row = objects.Service.get_by_id(self.ctx, self.row.id)
        self.assertEqual(states.ERROR, row.status)
        row.reconcile_fault = None
        row.save()
        with mock.patch.object(self.driver, 'get_app_status') as status:
            self.loop.run(row)
            status.assert_called_once()
        row = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, row.status)
        self.assertIsNone(row.reconcile_fault)

    def test_sweep_recovers_error_service(self):
        self.row.status = states.ERROR
        self.row.reconcile_fault = 'App container failed.'
        self.row.save()
        self.loop.sweep()
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.row.id).status)

    def test_successful_deploy_can_clear_old_reconcile_fault(self):
        self.row.reconcile_fault = 'App container failed.'
        self.row.save()
        self.loop.run(self.row)
        self.assertIsNone(objects.Service.get_by_id(
            self.ctx, self.row.id).reconcile_fault)

    def test_failed_read_resets_recovery_confirmation(self):
        self.config.config(stable_seconds=20, group='conductor')
        self.row.status = states.ERROR
        self.row.reconcile_fault = 'App container failed.'
        self.row.save()
        with mock.patch.object(reconcile.timeutils, 'utcnow',
                               return_value=datetime.datetime(2026, 1, 1)):
            self.loop.run(self.row)
        with mock.patch.object(self.driver, 'get_app_status',
                               side_effect=base.EngineUnreachable()):
            self.loop.run(self.row)
        with mock.patch.object(
                reconcile.timeutils, 'utcnow',
                return_value=datetime.datetime(2026, 1, 1, 0, 0, 30)):
            self.loop.run(self.row)
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, self.row.id).status)

    def test_short_running_sample_does_not_reset_crash_deadline(self):
        self.config.config(deploy_unreachable_timeout=30, stable_seconds=20,
                           group='conductor')
        with mock.patch.object(self.driver, 'get_app_status') as status, \
                mock.patch.object(reconcile.timeutils, 'utcnow') as clock:
            status.return_value = base.ObservedStatus(states.DEPLOYING)
            clock.return_value = datetime.datetime(2026, 1, 1) + (
                datetime.timedelta(seconds=0))
            self.loop.run(self.row)
            status.return_value = base.ObservedStatus(states.ACTIVE)
            clock.return_value = datetime.datetime(2026, 1, 1) + (
                datetime.timedelta(seconds=10))
            self.loop.run(self.row)
            status.return_value = base.ObservedStatus(states.DEPLOYING)
            clock.return_value = datetime.datetime(2026, 1, 1) + (
                datetime.timedelta(seconds=30))
            self.loop.run(self.row)
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, self.row.id).status)

    def test_log_reads_and_retry_sleeps_leave_task_free(self):
        def unlocked(*args, **kwargs):
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, self.row.id).task_state)
            return []

        capture = runtime_logs.capture
        sleep = mock.Mock(side_effect=unlocked)

        def capture_with_sleep(*args, **kwargs):
            return capture(*args, sleep=sleep, **kwargs)

        for observed in (states.DEPLOYING, states.ERROR):
            with self.subTest(observed=observed), mock.patch.object(
                    self.driver, 'get_app_status',
                    return_value=base.ObservedStatus(observed)), \
                    mock.patch.object(self.driver, 'get_app_logs',
                                      side_effect=unlocked) as read, \
                    mock.patch.object(runtime_logs, 'capture',
                                      side_effect=capture_with_sleep):
                self.loop.run(self.row)
                self.assertGreater(read.call_count, 1)
                self.assertGreater(sleep.call_count, 0)

    def test_slow_log_capture_allows_delete_and_discards_observation(self):
        self.row.ports = '80'
        self.row.save()
        domain = self.request(
            'post', '/v1/services/' + self.row.id + '/domains',
            {'domain': {'fqdn': 'logs.example.com'}}, status=201,
            complete=True).json['domain']
        latest = runtime_logs.latest(self.ctx, self.row.id)

        def read(*args, **kwargs):
            time.sleep(0.01)
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, self.row.id).task_state)
            self.request('delete', '/v1/services/' + self.row.id +
                         '/domains/' + domain['id'], status=202,
                         complete=True)
            return [base.LogLine('', 'stderr', 'stale observation')]

        with mock.patch.object(
                self.driver, 'get_app_status',
                return_value=base.ObservedStatus(states.ERROR)), \
                mock.patch.object(self.driver, 'get_app_logs',
                                  side_effect=read):
            self.assertFalse(self.loop.run(self.row))
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.row.id).status)
        self.assertNotIn('stale observation', str(runtime_logs.stored(
            objects.Deployment.get_by_id(self.ctx, latest.id))))

    def test_capture_burst_before_restart_confirmation(self):
        self.config.config(deploy_unreachable_timeout=150, group='conductor')
        latest = runtime_logs.latest(self.ctx, self.row.id)
        self.assertIsNotNone(latest)
        with mock.patch.object(
                self.driver, 'get_app_status',
                return_value=base.ObservedStatus(states.DEPLOYING)), \
                mock.patch.object(self.driver, 'get_app_logs', side_effect=[
                    [], base.DriverError(),
                    [base.LogLine('', 'stderr', 'missing setting')]]):
            self.loop.run(self.row)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.row.id).status)
        self.assertEqual('missing setting', runtime_logs.stored(
            objects.Deployment.get_by_id(self.ctx, latest.id))['lines'][0][
                'line'])

    def test_background_capture_for_deployment_owned_error(self):
        self.row.status = states.ERROR
        self.row.reconcile_fault = None
        self.row.save()
        with mock.patch.object(
                self.driver, 'get_app_status',
                return_value=base.ObservedStatus(states.ERROR)), \
                mock.patch.object(self.driver, 'get_app_logs', side_effect=[
                    [], [base.LogLine('', 'stderr', 'deployment failed')]]):
            self.loop.run(self.row)
        saved = runtime_logs.latest(self.ctx, self.row.id)
        self.assertEqual('deployment failed', runtime_logs.stored(
            saved)['lines'][0]['line'])
        row = objects.Service.get_by_id(self.ctx, self.row.id)
        self.assertEqual(states.ERROR, row.status)
        self.assertIsNone(row.reconcile_fault)

    def test_alternating_workers_share_crash_and_recovery_windows(self):
        self.config.config(deploy_unreachable_timeout=150, stable_seconds=20,
                           group='conductor')
        other = reconcile.Reconciler(self.manager)
        start = datetime.datetime(2026, 1, 1)
        with mock.patch.object(self.driver, 'get_app_status') as status, \
                mock.patch.object(reconcile.timeutils, 'utcnow') as clock:
            status.return_value = base.ObservedStatus(states.DEPLOYING)
            for seconds, loop in ((0, self.loop), (120, other),
                                  (151, self.loop)):
                clock.return_value = start + datetime.timedelta(
                    seconds=seconds)
                loop.run(self.row)
            row = objects.Service.get_by_id(self.ctx, self.row.id)
            self.assertEqual(states.ERROR, row.status)
            self.assertIsNone(row.unhealthy_since)
            status.return_value = base.ObservedStatus(states.ACTIVE)
            for seconds, loop in ((160, other), (181, self.loop)):
                clock.return_value = start + datetime.timedelta(
                    seconds=seconds)
                loop.run(self.row)
            row = objects.Service.get_by_id(self.ctx, self.row.id)
            self.assertEqual(states.ACTIVE, row.status)
            self.assertIsNone(row.running_since)
            self.assertIsNone(row.reconcile_fault)

    def test_restart_keeps_crash_confirmation(self):
        self.config.config(deploy_unreachable_timeout=150, group='conductor')
        start = datetime.datetime(2026, 1, 1)
        with mock.patch.object(self.driver, 'get_app_status', return_value=(
                base.ObservedStatus(states.DEPLOYING))), mock.patch.object(
                reconcile.timeutils, 'utcnow', return_value=start) as clock:
            self.loop.run(self.row)
            row = objects.Service.get_by_id(self.ctx, self.row.id)
            self.assertEqual(start, row.unhealthy_since.replace(tzinfo=None))
            restarted = reconcile.Reconciler(self.manager)
            clock.return_value = start + datetime.timedelta(seconds=151)
            restarted.run(self.row)
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, self.row.id).status)

    def test_newer_failed_crash_loop_bypasses_window(self):
        objects.Deployment(context=self.ctx, service_id=self.row.id,
                           status=states.FAILED,
                           trigger='api', spec={},
                           fault=(deployments.CRASH_LOOP %
                                  'restarting')).create()
        self.config.config(deploy_unreachable_timeout=150, group='conductor')
        with mock.patch.object(self.driver, 'get_app_status', return_value=(
                base.ObservedStatus(states.DEPLOYING))):
            self.loop.run(self.row)
        row = objects.Service.get_by_id(self.ctx, self.row.id)
        self.assertEqual(states.ERROR, row.status)
        self.assertIn('restarting', row.reconcile_fault)

    def test_non_crash_failure_does_not_bypass_window(self):
        objects.Deployment(context=self.ctx, service_id=self.row.id,
                           status=states.FAILED,
                           trigger='api', spec={},
                           fault='Build failed.').create()
        with mock.patch.object(self.driver, 'get_app_status', return_value=(
                base.ObservedStatus(states.DEPLOYING))):
            self.loop.run(self.row)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.row.id).status)

    def test_current_failed_deployment_does_not_bypass_window(self):
        latest = objects.Deployment(
            context=self.ctx, service_id=self.row.id, status=states.FAILED,
            trigger='api', spec={},
            fault=deployments.CRASH_LOOP % 'restarting')
        latest.create()
        self.row.current_deployment_id = latest.id
        self.row.save()
        with mock.patch.object(self.driver, 'get_app_status', return_value=(
                base.ObservedStatus(states.DEPLOYING))):
            self.loop.run(self.row)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, self.row.id).status)

    def test_status_change_clears_confirmation(self):
        self.row.unhealthy_since = datetime.datetime(2026, 1, 1)
        self.row.running_since = datetime.datetime(2026, 1, 1)
        self.row.save()
        self.row.status = states.STOPPED
        self.row.save()
        row = objects.Service.get_by_id(self.ctx, self.row.id)
        self.assertIsNone(row.unhealthy_since)
        self.assertIsNone(row.running_since)

    def test_restart_keeps_recovery_confirmation(self):
        self.config.config(stable_seconds=20, group='conductor')
        self.row.status = states.ERROR
        self.row.reconcile_fault = 'App container failed.'
        self.row.save()
        start = datetime.datetime(2026, 1, 1)
        with mock.patch.object(reconcile.timeutils, 'utcnow',
                               return_value=start) as clock:
            self.loop.run(self.row)
            clock.return_value = start + datetime.timedelta(seconds=21)
            reconcile.Reconciler(self.manager).run(self.row)
        row = objects.Service.get_by_id(self.ctx, self.row.id)
        self.assertEqual(states.ACTIVE, row.status)
        self.assertIsNone(row.running_since)

    def test_network_repair_without_services_and_task_release(self):
        loop = self.manager.network_reconciler
        stack = self.stack_row
        stack.driver = 'coolify_v4'
        stack.save()
        control = next(s for s in objects.ServerList.get_all(
            self.ctx, filters={'stack_id': stack.id}) if s.role == 'control')
        control.private_ip = '192.0.2.2'
        control.save()
        with mock.patch.object(reconcile.network_stack,
                               'EnvironmentNetwork') as factory, mock.patch(
                'coast.conductor.reconcile.objects.ServiceList.get_all',
                side_effect=AssertionError('No services required')):
            loop.run(stack)
            factory.return_value.reconcile.assert_called_once_with(
                self.ctx.project_id, stack.id, '192.0.2.2', False,
                deadline=None, ctx=mock.ANY)
            factory.return_value.reconcile.side_effect = RuntimeError('quota')
            self.assertRaises(RuntimeError, loop.run, stack)
        # The repair never takes the stack task, so nothing can strand.
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, stack.id).task_state)

    def test_network_sweep_is_separate_and_time_bounded(self):
        self.config.config(group='controller_worker',
                           network_repair_timeout=1)
        loop = self.manager.network_reconciler
        with mock.patch.object(loop, 'run') as run, mock.patch(
                'coast.conductor.reconcile.time.monotonic',
                side_effect=[0, 0, 5, 5, 5]):
            loop.sweep()
        self.assertEqual(1, run.call_count)
        self.assertEqual(1, run.call_args.kwargs['deadline'])
        with mock.patch.object(loop, 'sweep') as sweep:
            self.manager.reconcile_services()
            sweep.assert_not_called()
            self.manager.reconcile_networks()
            sweep.assert_called_once_with()

    def test_network_repair_skips_busy_and_noop_stacks(self):
        with mock.patch.object(reconcile.network_stack,
                               'EnvironmentNetwork') as factory:
            self.manager.network_reconciler.run(self.stack_row)
            self.stack_row.driver = 'coolify_v4'
            self.stack_row.save()
            self.stack_row.acquire_task(states.DELETING)
            self.manager.network_reconciler.run(self.stack_row)
            factory.assert_not_called()

    def test_network_repair_ignores_unbooted_build_servers(self):
        stack = self.stack_row
        stack.driver = 'coolify_v4'
        stack.save()
        control = next(s for s in objects.ServerList.get_all(
            self.ctx, filters={'stack_id': stack.id}) if s.role == 'control')
        control.private_ip = '192.0.2.2'
        control.save()
        build = objects.Server(
            context=self.ctx, stack_id=stack.id, name='build', role='build',
            kind='vm', status=states.ACTIVE, task_state=states.NONE)
        build.create()
        with mock.patch.object(reconcile.network_stack,
                               'EnvironmentNetwork') as factory:
            self.manager.network_reconciler.run(stack)
            factory.return_value.reconcile.assert_called_once_with(
                self.ctx.project_id, stack.id, '192.0.2.2', False,
                deadline=None, ctx=mock.ANY)
            factory.reset_mock()
            build.compute_id = 'nova-build'
            build.save()
            self.manager.network_reconciler.run(stack)
            factory.return_value.reconcile.assert_called_once_with(
                self.ctx.project_id, stack.id, '192.0.2.2', True,
                deadline=None, ctx=mock.ANY)
