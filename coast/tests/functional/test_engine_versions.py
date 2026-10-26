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

"""Persisted mixed fleets through conductor engine setup and observations."""

from unittest import mock

import requests_mock

from coast.common import states
from coast.conductor import agent_client
from coast.conductor import stack_create
from coast.drivers import base
from coast.drivers.coolify import v4
from coast import objects
from coast.tests import conductor


class EngineVersionsFlowTest(conductor.ConductorTest):
    def setUp(self):
        super().setUp()
        self.config.config(pinned_version='4.4.7', group='coolify')
        self.config.config(enabled_drivers=['noop', 'coolify_v4'],
                           agent_client_cert='client.pem',
                           agent_client_key='client.key', mgmt_netns='',
                           group='controller_worker')
        self.config.config(ca_certificate='ca.pem', group='certificates')
        self.driver = v4.CoolifyV4Driver(self.conf)
        self.manager.drivers['coolify_v4'] = self.driver
        self.http = requests_mock.Mocker()
        self.http.start()
        self.addCleanup(self.http.stop)
        self.address_index = 10

    def prepare_engine(self, version):
        # API/fake RPC create the persisted fixture without any cloud access.
        path = self.stack(
            workers=[], name='version-' + str(self.address_index))
        stack = objects.Stack.get_by_id(self.ctx, path.rsplit('/', 1)[1])
        stack.driver = 'coolify_v4'
        stack.engine_version = version
        stack.engine_ref = None
        stack.engine_token_ref = self.store.put(self.ctx, 'engine-token')
        stack.save()
        server = objects.ServerList.get_all(self.ctx, filters={
            'stack_id': stack.id, 'role': 'control'})[0]
        server.mgmt_ip = '192.0.2.' + str(self.address_index)
        self.address_index += 1
        server.save()
        url = agent_client.endpoint(self.conf, server.mgmt_ip).base_url
        self.http.get(url + '/health', text='ok')
        self.http.get(url + '/version', text=version)
        self.http.get(url + '/servers', json=[])
        self.http.patch(url + '/team', json={})
        self.http.get(url + '/projects', json=[])
        self.http.post(url + '/projects', json={'uuid': 'project-uuid'})
        self.http.get(url + '/projects/project-uuid/environments', json=[])
        self.http.post(url + '/projects/project-uuid/environments',
                       json={'uuid': 'production-uuid'})
        flow = stack_create.CreateStack(self.manager)
        flow.ctx = self.ctx
        flow.stack = stack
        flow.server = server
        flow.driver = self.manager._driver(stack)
        return flow, url

    def test_mixed_fleet_bootstraps_and_observes_both_recorded_versions(self):
        stacks = []
        for version in ('4.4.3', '4.4.7'):
            flow, url = self.prepare_engine(version)
            # Run the real conductor engine step, real driver and HTTP client.
            flow.engine()
            self.assertEqual(version, self.manager._engine_version(
                self.driver, flow.stack))
            stacks.append(flow.stack.id)
            self.assertEqual('project-uuid', objects.Stack.get_by_id(
                self.ctx, flow.stack.id).engine_ref)
            self.assertTrue(any(
                request.method == 'POST' and request.json() == {
                    'name': 'production'} and request.url ==
                url + '/projects/project-uuid/environments'
                for request in self.http.request_history))
            self.assertTrue(any(request.method == 'PATCH' and
                                request.url == url + '/team'
                                for request in self.http.request_history))
        self.assertEqual(['4.4.3', '4.4.7'], [
            objects.Stack.get_by_id(self.ctx, identifier).engine_version
            for identifier in stacks])
        self.assertEqual('4.4.7', self.conf.coolify.pinned_version)

    def test_mixed_fleet_creates_new_environments_through_api(self):
        for version in self.driver.SUPPORTED_ENGINE_VERSIONS:
            flow, url = self.prepare_engine(version)
            flow.engine()
            path = '/v1/stacks/' + flow.stack.id + '/appgroups'
            group = self.request('post', path, {'appgroup': {
                'name': 'mixed'}}, status=202, complete=True).json['appgroup']
            self.assertEqual(states.ACTIVE, objects.Appgroup.get_by_id(
                self.ctx, group['id']).status)
            path = '/v1/appgroups/' + group['id'] + '/environments'
            body = {'environment': {'name': 'new-environment'}}
            created = self.request('post', path, body, status=202,
                                   complete=True).json['environment']
            row = objects.Environment.get_by_id(self.ctx, created['id'])
            self.assertEqual(states.ACTIVE, row.status)
            self.assertEqual('production-uuid', row.engine_ref)

    def test_acceptance_fixed_name_rerun_is_refused_before_engine_calls(self):
        # Run 5's ops.py posts the same a56-mixed name on each invocation.
        # Replay with both releases alive and the new-stack pin at 4.4.7.
        fleets = [self.prepare_engine(version)
                  for version in self.driver.SUPPORTED_ENGINE_VERSIONS]
        for flow, url in fleets:
            flow.engine()
            group = self.request(
                'post', '/v1/stacks/' + flow.stack.id + '/appgroups',
                {'appgroup': {'name': 'mixed'}}, status=202,
                complete=True).json['appgroup']
            path = '/v1/appgroups/' + group['id'] + '/environments'
            body = {'environment': {'name': 'a56-mixed'}}
            row = self.request('post', path, body, status=202,
                               complete=True).json['environment']
            self.http.reset_mock()
            with mock.patch.object(self.rpc, 'cast') as cast:
                response = self.request('post', path, body, status=409)
            cast.assert_not_called()
            self.assertEqual([], self.http.request_history)
            fault = response.json['faultstring']
            self.assertIn('a56-mixed', fault)
            self.assertIn('already exists in this app group', fault)
            self.assertIn('Existing environment ID: %s (status ACTIVE)'
                          % row['id'], fault)
            self.assertIn('Use the existing environment or choose a '
                          'different name.', fault)
            listed = self.request('get', path).json['environments']
            matching = [item['id'] for item in listed
                        if item['name'] == 'a56-mixed']
            self.assertEqual([row['id']], matching)
            # Follow the refusal's advice: a fresh name reaches the engine.
            self.request('post', path, {'environment': {'name': 'another'}},
                         status=202, complete=True)
            self.assertTrue(self.http.request_history)

    def test_conductor_refuses_unsupported_live_version(self):
        flow, url = self.prepare_engine('4.4.3')
        self.http.get(url + '/version', text='4.4.9')
        self.assertRaises(base.UnsupportedEngineVersion, flow.engine)
        self.assertRaises(base.UnsupportedEngineVersion,
                          self.manager._engine_version,
                          self.driver, flow.stack)
        self.assertFalse(any(request.method == 'PATCH'
                             for request in self.http.request_history))
        self.assertEqual('4.4.3', objects.Stack.get_by_id(
            self.ctx, flow.stack.id).engine_version)

    def test_manager_refuses_supported_version_differing_from_stack(self):
        for recorded, observed in (('4.4.3', '4.4.7'), ('4.4.7', '4.4.3')):
            flow, url = self.prepare_engine(recorded)
            flow.engine()
            self.http.get(url + '/version', text=observed)
            # The driver accepts this release; the conductor must reject
            # it against the persisted stack rather than the global pin.
            self.assertEqual(observed, self.driver.get_engine_version(
                self.manager._endpoint(flow.stack)))
            self.assertRaises(base.UnsupportedEngineVersion,
                              self.manager._engine_version,
                              self.driver, flow.stack)
            self.assertEqual(recorded, objects.Stack.get_by_id(
                self.ctx, flow.stack.id).engine_version)
