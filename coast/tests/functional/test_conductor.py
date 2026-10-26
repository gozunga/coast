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

"""M1 conductor acceptance over fake RPC and a tagged fake cloud."""

from unittest import mock

from coast.common import states
from coast.conductor import deployments
from coast import objects
from coast.tests import conductor


class InfrastructureFlowTest(conductor.ConductorTest):
    def test_new_stack_disables_builtin_terminal(self):
        path = self.stack()
        stack_id = path.rsplit('/', 1)[-1]
        driver = self.manager.drivers['noop']
        self.assertIs(False, driver.terminal_enabled[stack_id])

    def test_stack_server_volume_completion_and_delete(self):
        path = self.stack()
        env = self.request('get', path).json['stack']
        self.assertEqual(states.ACTIVE, env['status'])
        servers = self.request('get', path + '/servers').json['servers']
        worker = next(row for row in servers if row['role'] == 'worker')
        self.assertEqual(states.ACTIVE, worker['status'])
        volume = self.request('post', path + '/volumes', {'volume': {
            'name': 'attached-data', 'size_gb': 5, 'server_id': worker['id']}},
            status=202, complete=True).json['volume']
        vp = path + '/volumes/' + volume['id']
        self.assertEqual(states.IN_USE, self.request('get', vp).json[
            'volume']['status'])
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        for endpoint in (path, path + '/servers', path + '/volumes', vp,
                         path + '/servers/' + worker['id']):
            self.request('get', endpoint, headers=foreign, status=404)
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        self.request('delete', path, headers=reader, status=403)
        self.request('post', path + '/volumes', {'volume': {
            'name': 'denied', 'size_gb': 1}}, headers=reader, status=403)
        self.config.config(volume_gb=15, group='quotas')
        self.request('post', path + '/volumes', {'volume': {
            'name': 'over-quota', 'size_gb': 1}}, status=403)
        self.request('get', path, headers=dict(self.headers, **{
            'OpenStack-API-Version': 'app-hosting 1.99'}), status=406)
        self.request('delete', path, status=202, complete=True)
        self.request('get', path, status=404)
        self.request('get', path + '/servers/' + worker['id'], status=404)
        deleted = objects.Stack.get_by_id(self.ctx, env['id'],
                                          show_deleted=True)
        self.assertEqual(states.DELETED, deleted.status)
        self.assertEqual(states.NONE, deleted.task_state)
        self.assertFalse(objects.ServerList.get_all(self.ctx))
        kept = objects.Server.get_by_id(
            self.ctx, worker['id'], show_deleted=True)
        self.assertIsNotNone(kept.deleted_at)
        self.assertEqual(states.DELETING, kept.status)
        self.assertFalse(objects.VolumeList.get_all(self.ctx))
        self.assertFalse(self.manager.cloud.resources)
        for name, value in self.usage().items():
            if name.startswith(('reserved_', 'in_use_')):
                self.assertEqual(0, value, name)
        emitted = self.manager.notifier.events
        self.assertIn(('coast.environment.create.end', {
            'resource_id': env['id'], 'project_id': 'project',
            'user_id': 'user', 'status': states.ACTIVE}), emitted)

    def test_offboarding_with_history_keeps_server_rows(self):
        path = self.stack()
        env = self.request('get', path).json['stack']
        worker = next(row for row in self.request(
            'get', path + '/servers').json['servers']
            if row['role'] == 'worker')
        source = objects.Source(context=self.ctx, type='git',
                                repo_url='https://example.com/web.git')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=env['id'], name='web',
            source_id=source.id, server_id=worker['id'], ports='3000',
            replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE)
        service.create()
        deployment = objects.Deployment(
            context=self.ctx, service_id=service.id, spec={'ports': '3000'},
            server_id=worker['id'], trigger='api', status=states.ACTIVE)
        deployment.create()
        self.request('delete', path, status=202, complete=True)
        self.request('get', path, status=404)
        self.request('get', path + '/servers', status=404)
        self.request('get', path + '/servers/' + worker['id'], status=404)
        shown = self.request(
            'get', '/v1/services/' + service.id).json['service']
        self.assertEqual(worker['id'], shown['server_id'])
        kept = objects.Server.get_by_id(
            self.ctx, worker['id'], show_deleted=True)
        self.assertIsNotNone(kept.deleted_at)
        self.assertEqual(worker['id'], objects.Deployment.get_by_id(
            self.ctx, deployment.id).server_id)
        self.assertFalse(self.manager.cloud.resources)


class WorkloadFlowTest(conductor.ConductorTest):
    def test_app_name_reuse_after_delete(self):
        path = self.stack()
        worker = next(row for row in self.request(
            'get', path + '/servers').json['servers']
            if row['role'] == 'worker')
        body = {'service': {
            **self.service_parents(path.rsplit('/', 1)[1]),
            'name': 'reusable', 'server_id': worker['id'], 'ports': '8080',
            'source': {'type': 'git',
                       'repo_url': 'https://example.com/web.git'}}}
        ids = set()
        for attempt in range(2):
            created = self.request('post', '/v1/services', body,
                                   status=202, complete=True).json['service']
            self.assertNotIn(created['id'], ids)
            ids.add(created['id'])
            conflict = self.request('post', '/v1/services', body, status=409)
            self.assertIn('already exists', conflict.json['faultstring'])
            target = '/v1/services/' + created['id']
            self.request('delete', target, status=202, complete=True)
            self.request('get', target, status=404)

    def test_delete_service_soft_deletes_deployment_history(self):
        path = self.stack()
        worker = next(row for row in self.request(
            'get', path + '/servers').json['servers']
            if row['role'] == 'worker')
        created = self.request('post', '/v1/services', {'service': {
            **self.service_parents(path.rsplit('/', 1)[1]),
            'name': 'web', 'server_id': worker['id'], 'ports': '8080',
            'source': {'type': 'git',
                       'repo_url': 'https://example.com/web.git'}}},
            status=202, complete=True).json['service']
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(created['deployment_id'],
                         service.current_deployment_id)

        target = '/v1/services/' + service.id
        second = self.request('post', target + '/deployments', {
            'deployment': {'source_ref': 'release'}}, status=202,
            complete=True).json['deployment']
        history = {created['deployment_id']: states.SUPERSEDED,
                   second['id']: states.ACTIVE}
        self.assertEqual(2, len(self.request(
            'get', target + '/deployments').json['deployments']))
        self.request('delete', target, status=202, complete=True)

        self.request('get', target, status=404)
        self.request('get', target + '/deployments', status=404)
        deleted = objects.Service.get_by_id(
            self.ctx, service.id, show_deleted=True)
        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(states.DELETING, deleted.status)
        self.assertEqual(states.NONE, deleted.task_state)
        self.assertIsNone(deleted.current_deployment_id)
        self.assertFalse(objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': service.id}))
        for deployment_id, status in history.items():
            self.request('get', target + '/deployments/' + deployment_id,
                         status=404)
            deployment = objects.Deployment.get_by_id(
                self.ctx, deployment_id, show_deleted=True)
            self.assertIsNotNone(deployment.deleted_at)
            self.assertEqual(status, deployment.status)
        self.assertEqual(0, self.usage()['in_use_apps'])

    def test_service_deployment_domain_export_completion(self):
        path = self.stack()
        worker = next(row for row in self.request(
            'get', path + '/servers').json['servers']
            if row['role'] == 'worker')
        created = self.request('post', '/v1/services', {'service': {
            **self.service_parents(path.rsplit('/', 1)[1]),
            'name': 'web', 'server_id': worker['id'], 'ports': '8080',
            'source': {'type': 'git',
                       'repo_url': 'https://example.com/web.git'},
            'env': {'PASSWORD': {'value': 'secret-value', 'secret': True}}}},
            status=202, complete=True).json['service']
        service_path = '/v1/services/' + created['id']
        service = objects.Service.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.ACTIVE, service.status)
        self.assertEqual(states.NONE, service.task_state)
        self.assertEqual(created['deployment_id'],
                         service.current_deployment_id)
        first = objects.Deployment.get_by_id(
            self.ctx, created['deployment_id'])
        self.assertEqual(states.ACTIVE, first.status)
        self.assertEqual('0' * 40, first.resolved_ref)
        shown = self.request(
            'get',
            service_path + '/deployments/' + first.id).json['deployment']
        self.assertEqual(first.resolved_ref, shown['resolved_ref'])
        self.assertIsNotNone(first.started_at)
        self.assertIsNotNone(first.finished_at)
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(1, self.usage()['in_use_apps'])
        second = self.request('post', service_path + '/deployments',
                              {'deployment': {'source_ref': 'release'}},
                              status=202, complete=True).json['deployment']
        self.assertEqual(states.SUPERSEDED, objects.Deployment.get_by_id(
            self.ctx, first.id).status)
        self.assertEqual(states.ACTIVE, objects.Deployment.get_by_id(
            self.ctx, second['id']).status)
        self.assertEqual(second['id'], objects.Service.get_by_id(
            self.ctx, service.id).current_deployment_id)
        rolled = self.request(
            'post', service_path + '/deployments/' + first.id + '/action',
            {'rollback': {}}, status=202, complete=True).json['deployment']
        self.assertEqual(states.ACTIVE, objects.Deployment.get_by_id(
            self.ctx, rolled['id']).status)
        restored = objects.Deployment.get_by_id(self.ctx, rolled['id'])
        self.assertEqual('rollback', restored.trigger)
        self.assertEqual(first.resolved_ref, restored.source_ref)
        self.assertEqual(first.resolved_ref, restored.resolved_ref)
        self.assertEqual(states.ROLLED_BACK, objects.Deployment.get_by_id(
            self.ctx, second['id']).status)
        self.assertEqual(1, len(objects.EventList.get_all(self.ctx, filters={
            'resource_id': rolled['id'], 'action': 'rollback',
            'outcome': 'success'})))
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.request('post', service_path + '/action', {'stop': {}},
                     status=202, complete=True)
        self.assertEqual(states.STOPPED, objects.Service.get_by_id(
            self.ctx, service.id).status)
        self.request('post', service_path + '/action', {'start': {}},
                     status=202, complete=True)
        # STARTING until the deployment poll sees the service running.
        self.assertEqual(states.STARTING, objects.Service.get_by_id(
            self.ctx, service.id).status)
        deployments.sweep(self.manager)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, service.id).status)
        self.request('post', service_path + '/action', {'restart': {}},
                     status=202, complete=True)
        deployments.sweep(self.manager)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, service.id).status)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        domain = self.request('post', service_path + '/domains', {'domain': {
            'fqdn': 'web.example.com'}}, status=201, complete=True).json[
                'domain']
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.PENDING_VERIFICATION, stored.status)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.assertEqual(0, self.usage()['reserved_domains'])
        self.assertEqual(1, self.usage()['in_use_domains'])
        self.request('post', service_path + '/domains/' + domain['id'] +
                     '/action', {'verify': {}}, status=202, complete=True)
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.PENDING_VERIFICATION, stored.status)
        self.assertIsNone(stored.verified_at)
        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        return_value=True), \
                mock.patch('coast.conductor.manager.domain_check.routes_to',
                           return_value=True), \
                mock.patch('coast.conductor.manager.domain_check.tls_ready',
                           return_value=True):
            self.manager.drive_domains()
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.assertEqual(states.ACTIVE, stored.status)
        self.assertIsNotNone(stored.verified_at)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)
        self.request('delete', service_path + '/domains/' + domain['id'],
                     status=202, complete=True)
        self.request('get', service_path + '/domains/' + domain['id'],
                     status=404)
        self.assertEqual(0, self.usage()['in_use_domains'])
        exported = self.request('post', path + '/exports', {'export': {}},
                                status=202, complete=True).json['export']
        export_path = path + '/exports/' + exported['id']
        shown = self.request('get', export_path).json['export']
        self.assertEqual(states.ACTIVE, shown['status'])
        self.assertEqual([], shown['artifacts'])
        self.assertIsNone(shown['object_expires_at'])
        self.assertNotIn('download_url', str(shown))
        self.assertIn(self.manager.cloud.reference('export', exported['id']),
                      self.manager.cloud.resources)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, path.rsplit('/', 1)[1]).task_state)
        again = self.request('post', path + '/exports', {'export': {}},
                             status=202, complete=True).json['export']
        self.assertEqual(states.ACTIVE, objects.Export.get_by_id(
            self.ctx, again['id']).status)
        self.request('post', export_path + '/action', {'renew_url': {}},
                     status=409)
        self.request('delete', export_path, status=202, complete=True)
        self.request('get', export_path, status=404)
        self.assertNotIn(
            self.manager.cloud.reference('export', exported['id']),
            self.manager.cloud.resources)
        self.request('delete', path + '/exports/' + again['id'],
                     status=202, complete=True)
        idle = self.request('post', '/v1/services', {'service': {
            **self.service_parents(path.rsplit('/', 1)[1]),
            'name': 'idle', 'server_id': worker['id'], 'deploy': False,
            'source': {'type': 'git',
                       'repo_url': 'https://example.com/idle.git'}}},
            status=202, complete=True).json['service']
        self.assertNotIn('deployment_id', idle)
        self.assertEqual(states.STOPPED, objects.Service.get_by_id(
            self.ctx, idle['id']).status)
        self.assertFalse(objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': idle['id']}))
        for service_id in (created['id'], idle['id']):
            target = '/v1/services/' + service_id
            self.request('delete', target, status=202, complete=True)
            self.request('get', target, status=404)
            kept = objects.Service.get_by_id(
                self.ctx, service_id, show_deleted=True)
            self.assertIsNotNone(kept.deleted_at)
            self.assertEqual(states.NONE, kept.task_state)
        self.assertEqual(0, self.usage()['in_use_apps'])
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(0, self.usage()['reserved_domains'])
        emitted = [event[0] for event in self.manager.notifier.events]
        for event_type in (
                'coast.app.create.end', 'coast.deployment.create.end',
                'coast.domain.create.end', 'coast.export.create.end'):
            self.assertIn(event_type, emitted)
        text = str(self.manager.notifier.events)
        self.assertNotIn('secret-value', text)
        for event in objects.EventList.get_all(self.ctx):
            self.assertNotIn('secret-value', event.message or '')


class AppgroupDeleteFlowTest(conductor.ConductorTest):
    def test_delete_environment_then_appgroup(self):
        path = self.stack()
        created = self.request(
            'post', path + '/appgroups',
            {'appgroup': {'name': 'website'}},
            status=202, complete=True).json['appgroup']
        app_path = '/v1/appgroups/' + created['id']
        self.assertEqual(
            states.ACTIVE,
            self.request('get', app_path).json['appgroup']['status'])
        env = self.request(
            'post', app_path + '/environments',
            {'environment': {'name': 'live'}},
            status=202, complete=True).json['environment']
        env_path = '/v1/environments/' + env['id']
        self.assertEqual(
            states.ACTIVE,
            self.request('get', env_path).json['environment']['status'])
        self.request('delete', env_path, status=202, complete=True)
        self.request('get', env_path, status=404)
        # Q147: the default production environment goes first too.
        default = [e for e in self.request(
            'get', app_path + '/environments').json['environments']
            if e['name'] == 'production']
        self.assertEqual(1, len(default))
        self.assertEqual(states.ACTIVE, default[0]['status'])
        self.request('delete', '/v1/environments/' + default[0]['id'],
                     status=202, complete=True)
        self.request('delete', app_path, status=202, complete=True)
        self.request('get', app_path, status=404)
        gone_env = objects.Environment.get_by_id(
            self.ctx, env['id'], show_deleted=True)
        gone_app = objects.Appgroup.get_by_id(
            self.ctx, created['id'], show_deleted=True)
        self.assertEqual(states.DELETED, gone_env.status)
        self.assertEqual(states.DELETED, gone_app.status)
        self.assertIsNotNone(gone_env.deleted_at)
        self.assertIsNotNone(gone_app.deleted_at)
