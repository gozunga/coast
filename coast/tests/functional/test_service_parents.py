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

"""Public service create through fake RPC and the Coolify HTTP driver."""

from unittest import mock

from coast.common import context
from coast.common import safe_log
from coast.common import states
from coast import objects
from coast.tests import deployments


class ServiceParentFlowTest(deployments.DeploymentTest):
    def test_git_image_and_template_create_use_recorded_parent_refs(self):
        parents = self.service_parents(self.env.id)
        appgroup = objects.Appgroup.get_by_id(
            self.ctx, parents['appgroup_id'])
        appgroup.engine_ref = 'appgroup-project'
        appgroup.save()
        environment = objects.Environment.get_by_id(
            self.ctx, parents['environment_id'])
        environment.engine_ref = 'stage-uuid'
        environment.save()
        self.worker.engine_ref = 'worker-uuid'
        self.worker.save()
        self.http.get(self.url + '/servers/worker-uuid', json={
            'uuid': 'worker-uuid', 'settings': {'server_role': 'deployment'}})
        self.http.get(self.url + '/servers', json=[])
        self.http.get(self.url + '/applications', json=[])
        self.http.get(self.url + '/services', json=[])
        git = self.http.post(self.url + '/applications/public',
                             json={'uuid': 'git-uuid'})
        image = self.http.post(self.url + '/applications/dockerimage',
                               json={'uuid': 'image-uuid'})
        self.http.get(self.url + '/applications/image-uuid',
                      json={'uuid': 'image-uuid', 'build_pack': 'dockerimage',
                            'status': 'running:healthy'})
        self.http.get(self.url + '/applications/image-uuid/envs', json=[])
        self.http.get(self.url + '/applications/image-uuid/storages', json={
            'persistent_storages': [], 'file_storages': []})
        image_patch = self.http.patch(
            self.url + '/applications/image-uuid', json={})
        template = self.http.post(self.url + '/services',
                                  json={'uuid': 'template-uuid'})
        self.http.get(self.url + '/applications/git-uuid',
                      json={'uuid': 'git-uuid', 'build_pack': 'nixpacks'})
        self.http.get(self.url + '/applications/git-uuid/envs', json=[])
        self.http.get(self.url + '/applications/git-uuid/storages', json={
            'persistent_storages': [], 'file_storages': []})
        self.http.patch(self.url + '/applications/git-uuid', json={})
        self.http.get(self.url + '/services/template-uuid/envs', json=[])
        self.http.get(self.url + '/services/template-uuid', json={
            'applications': [{'name': 'wordpress', 'fqdn': None}]})
        self.http.patch(self.url + '/services/template-uuid', json={})
        for name, source, endpoint, ref, template_id in (
                ('git', {'type': 'git',
                         'repo_url': 'https://example.com/x.git',
                         'build_pack': 'nixpacks'}, git, 'git-uuid', None),
                ('image', {'type': 'image',
                           'image': 'registry.example.com:5000/web',
                           'tag': 'release'}, image, 'image-uuid', None),
                ('template', 'template:wordpress-with-mariadb', template,
                 'template-uuid', 'wordpress-with-mariadb')):
            body = {'service': {**parents, 'name': name,
                                'server_id': self.worker.id,
                                'source': source, 'deploy': False}}
            created = self.request('post', '/v1/services', body, status=202,
                                   complete=True).json['service']
            row = objects.Service.get_by_id(self.ctx, created['id'])
            self.assertEqual(states.STOPPED, row.status)
            self.assertEqual(states.NONE, row.task_state)
            self.assertEqual(ref, row.engine_ref)
            self.assertEqual(environment.id, row.environment_id)
            payload = endpoint.last_request.json()
            self.assertEqual('appgroup-project', payload['project_uuid'])
            self.assertEqual('stage-uuid', payload['environment_uuid'])
            self.assertEqual('worker-uuid', payload['server_uuid'])
            shown = self.request(
                'get', '/v1/services/' + row.id).json['service']
            for item in (created, shown):
                self.assertEqual(appgroup.id, item['appgroup_id'])
                self.assertEqual(environment.id, item['environment_id'])
                self.assertEqual(template_id, item['template_id'])
                self.assertNotIn('engine_ref', item)
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(4, self.usage()['in_use_apps'])
        payload = image.last_request.json()
        self.assertEqual('registry.example.com:5000/web',
                         payload['docker_registry_image_name'])
        self.assertEqual('release', payload['docker_registry_image_tag'])
        self.assertFalse(payload['instant_deploy'])
        self.assertFalse(payload['use_build_server'])
        self.assertNotIn('git_repository', payload)
        image_service = objects.ServiceList.get_all(
            self.ctx, filters={'name': 'image'})[0]
        deployed = self.request(
            'post', '/v1/services/' + image_service.id + '/deployments',
            {'deployment': {'source_ref': 'override'}}, status=202,
            complete=True).json['deployment']
        # settled() assumes the single setup service. This flow has four.
        deployment = objects.Deployment.get_by_id(self.ctx, deployed['id'])
        self.assertEqual(states.ACTIVE, deployment.status)
        self.assertIsNotNone(deployment.finished_at)
        image_service = objects.Service.get_by_id(self.ctx, image_service.id)
        self.assertEqual(states.ACTIVE, image_service.status)
        self.assertEqual(states.NONE, image_service.task_state)
        self.assertEqual(states.NONE, objects.Stack.get_by_id(
            self.ctx, self.env.id).task_state)
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(4, self.usage()['in_use_apps'])
        self.assertEqual('override',
                         image_patch.last_request.json()[
                             'docker_registry_image_tag'])
        self.assertEqual('registry.example.com:5000/web',
                         image_patch.last_request.json()[
                             'docker_registry_image_name'])

    def _rate_limited_create(self, persistent=False):
        self.worker.engine_ref = 'worker-uuid'
        self.worker.save()
        self.http.get(self.url + '/servers/worker-uuid', json={
            'uuid': 'worker-uuid', 'settings': {'server_role': 'deployment'}})
        self.http.get(self.url + '/applications', json=[])
        self.http.get(self.url + '/security/keys', json=[])
        replies = [{'status_code': 429}]
        if not persistent:
            replies.append({'json': {'uuid': 'app-uuid'}})
        endpoint = self.http.post(self.url + '/applications/dockerimage',
                                  replies)
        body = self.service_body(name='limited')
        body['service']['source'] = {'type': 'image', 'image': 'nginx',
                                     'tag': 'alpine'}
        created = self.request('post', '/v1/services', body, status=202,
                               complete=True).json['service']
        return objects.Service.get_by_id(self.ctx, created['id']), endpoint

    def test_create_recovers_from_rate_limit_and_becomes_active(self):
        service, endpoint = self._rate_limited_create()
        self.assertEqual(states.ACTIVE, service.status)
        self.assertEqual(states.NONE, service.task_state)
        self.assertEqual(2, endpoint.call_count)
        self.assertEqual(endpoint.request_history[0].json(),
                         endpoint.request_history[1].json())
        self.assertEqual(1, self.enqueue.call_count)
        self.assertFalse(any(request.path.endswith('/servers')
                             for request in self.http.request_history))

    def test_persistent_rate_limit_sets_clear_fault_and_logs_once(self):
        with mock.patch.object(safe_log, 'log_failure',
                               wraps=safe_log.log_failure) as logged:
            service, endpoint = self._rate_limited_create(persistent=True)
        self.assertEqual(states.ERROR, service.status)
        deployment = objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': service.id})[0]
        self.assertIn('rate limit (HTTP 429)', deployment.fault)
        self.assertNotIn('unreachable', deployment.fault)
        events = objects.EventList.get_all(
            self.ctx, filters={'resource_id': service.id,
                               'outcome': 'failure'})
        self.assertTrue(events)
        self.assertIn('rate limit (HTTP 429)', events[0].message)
        self.assertEqual(self.conf.coolify.rate_limit_attempts,
                         endpoint.call_count)
        self.assertEqual(1, logged.call_count)
        self.assertEqual(0, self.enqueue.call_count)

    def test_persistent_deploy_rate_limit_is_a_clear_fault(self):
        endpoint = self.http.post(self.url + '/deploy', status_code=429)
        with mock.patch.object(safe_log, 'log_failure',
                               wraps=safe_log.log_failure) as logged:
            row = self.request(
                'post', '/v1/services/' + self.service.id + '/deployments',
                {'deployment': {}}, status=202,
                complete=True).json['deployment']
        deployment = self.settled(row['id'], states.FAILED)
        self.assertIn('rate limit (HTTP 429)', deployment.fault)
        self.assertNotIn('unknown', deployment.fault)
        self.assertEqual(self.conf.coolify.rate_limit_attempts,
                         endpoint.call_count)
        self.assertEqual(1, logged.call_count)

    def test_invalid_parents_fail_before_rpc_and_writes(self):
        parents = self.service_parents(self.env.id)
        foreign_ctx = context.RequestContext(project_id='other')
        stack = objects.Stack(context=foreign_ctx, name='other', plan='small',
                              driver='noop', engine_version='test',
                              status=states.ACTIVE, task_state=states.NONE)
        stack.create()
        foreign_app = objects.Appgroup(
            context=foreign_ctx, stack_id=stack.id, name='other',
            status=states.ACTIVE, task_state=states.NONE, engine_ref='other')
        foreign_app.create()
        foreign_env = objects.Environment(
            context=foreign_ctx, appgroup_id=foreign_app.id, name='other',
            status=states.ACTIVE, task_state=states.NONE, engine_ref='other')
        foreign_env.create()
        for source in ({'type': 'git',
                        'repo_url': 'https://example.com/x.git'},
                       'template:wordpress-with-mariadb'):
            body = {'service': {**parents, 'name': 'rejected',
                                'source': source,
                                'server_id': self.worker.id, 'env': {
                                    'PASSWORD': {'value': 'not-written'}}}}
            for field, foreign_id in (('appgroup_id', foreign_app.id),
                                      ('environment_id', foreign_env.id)):
                for value in ('missing', foreign_id):
                    invalid = {'service': dict(
                        body['service'], **{field: value})}
                    self.assert_rejected_without_writes(invalid, 404)
                invalid = {'service': dict(body['service'])}
                del invalid['service'][field]
                self.assert_rejected_without_writes(invalid, 400)
                parent_class = (objects.Appgroup
                                if field == 'appgroup_id'
                                else objects.Environment)
                parent = parent_class.get_by_id(self.ctx, parents[field])
                for status, ref in ((states.PENDING, 'ref'),
                                    (states.ACTIVE, None)):
                    parent.status = status
                    parent.engine_ref = ref
                    table = parent.repository.model.__table__
                    with self.engine.begin() as connection:
                        connection.execute(table.update().where(
                            table.c.id == parent.id).values(
                                status=status, engine_ref=ref))
                    self.assert_rejected_without_writes(body, 409)
                parent.status = states.ACTIVE
                parent.engine_ref = 'restored'
                with self.engine.begin() as connection:
                    connection.execute(table.update().where(
                        table.c.id == parent.id).values(
                            status=states.ACTIVE, engine_ref='restored'))

    def test_service_without_worker_rejected_before_rpc(self):
        # Q144: no automatic placement; a null server_id is a clear 400.
        parents = self.service_parents(self.env.id)
        body = {'service': {**parents, 'name': 'unplaced',
                            'server_id': None, 'deploy': True,
                            'source': {
                                'type': 'git',
                                'repo_url': 'https://example.com/x.git'},
                            'env': {'PASSWORD': {'value': 'not-written'}}}}
        self.assert_rejected_without_writes(body, 400)
        result = self.request('post', '/v1/services', body, status=400)
        self.assertIn('server_id is required', result.json['faultstring'])
        self.assertNotIn('not-written', result.text)

    def assert_rejected_without_writes(self, body, status):
        with self.engine.connect() as connection:
            tables = objects.Service.repository.model.metadata.tables.values()
            before = {table.name: list(connection.execute(
                table.select()).mappings()) for table in tables}
        secrets = dict(self.store.values)
        requests = len(self.http.request_history)
        self.request('post', '/v1/services', body, status=status)
        with self.engine.connect() as connection:
            after = {table.name: list(connection.execute(
                table.select()).mappings()) for table in tables}
        self.assertEqual(before, after)
        self.assertEqual(secrets, self.store.values)
        self.assertEqual(requests, len(self.http.request_history))
        self.assertTrue(self.manager.done.empty())
