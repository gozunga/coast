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

"""Container API through project policy, fake RPC, and worker HTTP."""

from unittest import mock

import requests

from coast.agent import containers
from coast.common import context
from coast.common import exception
from coast.drivers import base
from coast import objects
from coast.tests import container_views as data
from coast.tests import deployments


class ServiceContainersTest(deployments.DeploymentTest):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.13'
        self.service.template_id = 'wordpress'
        self.service.save()
        self.worker.mgmt_ip = '192.0.2.5'
        self.worker.save()
        self.path = '/v1/services/' + self.service.id + '/containers'
        self.config.config(project_id='project', role='worker', group='agent')
        self.config.config(agent_client_cert='cert', agent_client_key='key',
                           mgmt_netns='', group='controller_worker')
        self.config.config(ca_certificate='ca', group='certificates')
        self.docker = data.Docker()
        for key, value, hidden in (('TOKEN', 'hidden-value', True),
                                   ('PASSWORD', 'plain', False)):
            objects.Secret(context=self.ctx, service_id=self.service.id,
                           key=key, value_ref=self.store.put(self.ctx, value),
                           is_secret=hidden, build_time=False,
                           version=1).create()
        self.agent_url = 'https://192.0.2.5:%s/v1/containers' % (
            self.conf.controller_worker.agent_port)

        def respond(request, response):
            return containers.read(self.conf, request.json(), self.docker)

        self.agent = self.http.get(self.agent_url, json=respond)

    def test_pasted_compose_uses_service_ownership_for_views(self):
        self.service.template_id = None
        self.service.save()
        source = objects.Source.get_by_id(self.ctx, self.service.source_id)
        source.type = 'compose'
        source.build_pack = None
        source.save()
        self.test_list_detail_logs_and_secret_flags()
        self.assertEqual('service',
                         self.agent.last_request.json()['resource_type'])

    def test_list_detail_logs_and_secret_flags(self):
        headers = dict(self.headers, **{'X-Roles': 'reader'})
        response = self.request('get', self.path, headers=headers)
        self.assertEqual('no-store', response.headers['Cache-Control'])
        names = [row['name'] for row in response.json['containers']]
        self.assertEqual(['db', 'web'], names)
        response = self.request('get', self.path + '/c-web', headers=headers)
        env = response.json['container']['environment']
        self.assertEqual({'TOKEN': '***', 'ALIAS': '***', 'PASSWORD': 'plain'},
                         env)
        self.assertEqual({'cpu': .5, 'memory_bytes': 134217728},
                         response.json['container']['limits'])
        logs = self.request('get', self.path + '/c-web/logs?tail=1&since=' +
                            '2026-10-10T01:00:00Z', headers=headers)
        self.assertEqual('*** PASSWORD=plain', logs.json['logs']['lines'][0][
            'line'])
        self.assertEqual('stderr', logs.json['logs']['lines'][0]['stream'])
        for text in (response.text, logs.text):
            for hidden in ('hidden-value', '/host', 'engine-internal',
                           'coolify.', '192.0.2.5', 'app-uuid'):
                self.assertNotIn(hidden, text)
        self.assertEqual([], self.request('get', self.path +
                         '/c-web/logs?tail=0').json['logs']['lines'])
        self.assertEqual('GET', self.agent.last_request.method)
        self.assertEqual('project',
                         self.agent.last_request.json()['project_id'])

    def test_auth_project_microversion_and_unknown_targets(self):
        for suffix in ('', '/c-web', '/c-web/logs'):
            path = self.path + suffix
            self.request('get', path, headers=dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting 1.12'}), status=404)
            self.client.get(path, headers={
                'OpenStack-API-Version': 'app-hosting 1.13'}, status=401)
            self.request('get', path, headers=dict(self.headers, **{
                'X-Roles': 'unrelated'}), status=403)
            self.request('get', path, headers=dict(self.headers, **{
                'X-Project-Id': 'other'}), status=404)
            self.request('get', path.replace(self.service.id,
                         '00000000-0000-0000-0000-000000000000'), status=404)
        self.assertFalse(self.agent.called)
        self.assertRaises(exception.NotFound, self.manager.get_containers,
                          context.RequestContext(project_id='other'),
                          self.service.id)
        self.request('get', self.path + '/missing', status=404)
        self.request('get', self.path + '/missing/logs', status=404)
        self.request('get', self.path + '/c-web/not-a-route', status=404)
        self.request('get', self.path + '/logs', status=404)

    def test_read_only_queries_and_non_compose(self):
        for suffix in ('', '/c-web', '/c-web/logs'):
            for method in ('post', 'put', 'patch', 'delete'):
                self.request(method, self.path + suffix, status=405)
            self.request('get', self.path + suffix + '?limit=1', status=400)
        for query in ('tail=-1', 'tail=10001', 'tail=no', 'since=invalid'):
            self.request('get', self.path + '/c-web/logs?' + query, status=400)
        self.assertFalse(self.agent.called)
        self.service.template_id = None
        self.service.save()
        self.request('get', self.path, status=409)
        source = objects.Source.get_by_id(self.ctx, self.service.source_id)
        source.build_pack = 'compose'
        source.save()
        self.docker.rows = [{'Id': 'abc123', 'Labels': data.labels(
            kind='application')}]
        self.docker.detail['Config']['Labels'] = data.labels(
            kind='application')
        self.assertEqual(1, len(self.request('get', self.path).json[
            'containers']))

    def test_faults_are_generic_and_missing_worker_is_unavailable(self):
        for status in (502, 503, 504):
            self.http.get(self.agent_url, status_code=status,
                          text='hidden-value engine-internal')
            result = self.request('get', self.path, status=503)
            self.assertEqual('Service Unavailable', result.json['faultstring'])
        self.http.get(self.agent_url, exc=requests.ConnectionError(
            'hidden-value engine-internal'))
        self.request('get', self.path, status=503)
        self.http.get(self.agent_url, json={'containers': 'invalid'})
        result = self.request('get', self.path, status=500)
        self.assertEqual('Internal Server Error', result.json['faultstring'])
        self.assertNotIn('hidden-value', result.text)
        self.service.server_id = None
        self.service.save()
        self.request('get', self.path, status=503)

    def test_worker_cannot_be_from_another_stack_or_role(self):
        self.worker.role = 'build'
        self.worker.save()
        self.request('get', self.path, status=503)
        self.assertFalse(self.agent.called)

    def test_conductor_boundary_drops_unknown_agent_fields(self):
        original = containers.read(self.conf, dict(
            project_id='project', resource_type='service',
            resource_uuid='app-uuid', container='c-web'), self.docker)
        original['container']['host'] = 'engine-internal'
        original['container']['limits']['binds'] = '/host'
        with mock.patch('coast.conductor.agent_client.AgentClient.request',
                        return_value=original):
            response = self.request('get', self.path + '/c-web')
        self.assertNotIn('engine-internal', response.text)
        self.assertNotIn('/host', response.text)

    def test_rpc_typed_engine_fault(self):
        with mock.patch.object(self.rpc, '_sync_call',
                               side_effect=base.EngineUnreachable()):
            self.request('get', self.path, status=503)

    def test_mask_observed_secret_after_desired_value_changes(self):
        self.docker.detail['Config']['Env'] = [
            'TOKEN=previous-secret', 'ALIAS=previous-secret', 'PASSWORD=plain']
        self.docker.raw = data.frame(
            '2026-10-10T01:00:00Z previous-secret hidden-value plain\n')
        result = self.request('get', self.path + '/c-web').json['container']
        self.assertEqual({'TOKEN': '***', 'ALIAS': '***', 'PASSWORD': 'plain'},
                         result['environment'])
        result = self.request('get', self.path + '/c-web/logs').json
        self.assertEqual('*** *** plain', result['logs']['lines'][0]['line'])
        self.assertNotIn('environment', result['logs'])

    def test_other_stack_worker_assignment_is_rejected(self):
        foreign = objects.Stack(
            context=self.ctx, name='other', plan='small', driver='noop',
            engine_version='test', status='ACTIVE', task_state='NONE')
        foreign.create()
        server = objects.Server(
            context=self.ctx, name='other-worker', stack_id=foreign.id,
            kind='vm', role='worker', status='ACTIVE', task_state='NONE',
            mgmt_ip='192.0.2.6')
        server.create()
        self.service.server_id = server.id
        self.assertRaises(exception.Invalid, self.service.save)
        self.assertFalse(self.agent.called)

    def test_retained_file_content_is_masked_in_container_reads(self):
        ref = self.store.put(self.ctx, 'old-file-secret\nnew-file-secret')
        objects.Deployment(
            context=self.ctx, service_id=self.service.id,
            server_id=self.worker.id, trigger='api', status='SUPERSEDED',
            spec={'file_mounts': [{'content_ref': ref}]}).create()
        self.service.file_mount_secret_refs = [ref]
        self.service.save()
        self.docker.detail['Config']['Env'].append('FILE=old-file-secret')
        self.docker.raw = data.frame(
            '2026-10-10T01:00:00Z old-file-secret new-file-secret plain\n')
        detail = self.request('get', self.path + '/c-web').json['container']
        self.assertEqual('***', detail['environment']['FILE'])
        logs = self.request('get', self.path + '/c-web/logs').json['logs']
        self.assertEqual('*** *** plain', logs['lines'][0]['line'])
