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

"""Offline regression coverage for live contract transport and cleanup."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fixtures
import requests_mock

from coast.agent import proxy
from coast.common import states
from coast.drivers import base as engine
from coast.tests import base
from coast.tests.contract.coolify import test_live


class LiveSetupTest(base.TestCase):
    def setUp(self):
        super().setUp()
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.__enter__()
        self.addCleanup(environment.__exit__, None, None, None)
        directory = self.useFixture(fixtures.TempDir()).path
        token = Path(directory) / 'token'
        token.write_text('test-token')
        self.values = {'URL': 'https://controller.example.com:9443/api/v1',
                       'TOKEN_FILE': str(token), 'WORKER_UUIDS': 'worker',
                       'PRIVATE_KEY_UUID': 'key'}
        for key, value in self.values.items():
            os.environ['COAST_CONTRACT_COOLIFY_' + key] = value
        for key in ('CLIENT_CERT', 'CLIENT_KEY', 'CA_CERT'):
            os.environ['COAST_CONTRACT_COOLIFY_' + key] = '/private/' + key
        os.environ['COAST_CONTRACT_COOLIFY_FALLBACK_ENABLED'] = 'false'

    def test_git_fixture_skips_without_dedicated_registry(self):
        case = test_live.LiveCoolifyBase()
        case.config = self.config
        case.driver = test_live.ContractDriver(self.conf)
        case.ep = engine.EngineEndpoint(
            'https://example.com', '', '', '', None)
        with mock.patch.object(case, 'api', return_value=[{
                'settings': {'server_role': 'build'}}]):
            self.assertRaises(self.skipException, case.prepare_source,
                              SimpleNamespace(type='git'))
            # Image contracts need no registry even in a mixed-role fleet.
            case.prepare_source(SimpleNamespace(type='image'))

    def test_git_fixture_uses_operator_registry_template(self):
        template = 'registry.example.com/{environment_id}/{app_id}'
        os.environ['COAST_CONTRACT_COOLIFY_BUILD_REGISTRY_TEMPLATE'] = template
        case = test_live.LiveCoolifyBase()
        case.config = self.config
        case.driver = test_live.ContractDriver(self.conf)
        case.ep = engine.EngineEndpoint(
            'https://example.com', '', '', '', None)
        with mock.patch.object(case, 'api', return_value=[{
                'settings': {'server_role': 'build'}}]):
            case.prepare_source(SimpleNamespace(type='git'))
        self.assertEqual(template, case.driver._image_template(case.ep))

    def test_agent_uses_production_client(self):
        ep = test_live.endpoint(self.conf, self.values)
        driver = test_live.ContractDriver(self.conf)
        with requests_mock.Mocker() as http:
            http.get(ep.base_url + '/version', json='engine-version')
            with driver._client(ep) as transport:
                self.assertEqual((ep.client_cert, ep.client_key),
                                 transport.session.cert)
                self.assertEqual(ep.ca_cert, transport.session.verify)
                self.assertEqual('engine-version',
                                 transport.request('GET', '/version'))
        self.assertFalse(self.conf.coolify.insecure_direct_url)

    def test_agent_rejects_http(self):
        self.values['URL'] = 'http://controller.example.com:9443/api/v1'
        ep = test_live.endpoint(self.conf, self.values)
        self.assertRaises(engine.DriverError,
                          test_live.ContractDriver(self.conf)._client, ep)

    def test_agent_credentials_fail_closed(self):
        del os.environ['COAST_CONTRACT_COOLIFY_CLIENT_KEY']
        self.assertRaises(ValueError, test_live.endpoint,
                          self.conf, self.values)

    def test_direct_loopback_is_explicit(self):
        os.environ['COAST_CONTRACT_COOLIFY_TRANSPORT'] = 'direct'
        self.values['URL'] = 'http://127.0.0.1:8000'
        ep = test_live.endpoint(self.conf, self.values)
        with test_live.ContractDriver(self.conf)._client(ep) as transport:
            self.assertEqual('http://127.0.0.1:8000/api/v1', transport.url)
        self.assertTrue(self.conf.coolify.insecure_direct_url)

    def test_invalid_settings_fail_before_live_mutation(self):
        os.environ['COAST_CONTRACT_COOLIFY_TRANSPORT'] = 'unknown'
        self.assertRaises(ValueError, test_live.endpoint,
                          self.conf, self.values)
        del os.environ['COAST_CONTRACT_COOLIFY_FALLBACK_ENABLED']
        self.assertRaises(ValueError, test_live.original_fallback)
        os.environ['COAST_CONTRACT_COOLIFY_FALLBACK_ENABLED'] = 'yes'
        self.assertRaises(ValueError, test_live.original_fallback)

    def test_setup_and_cleanup_use_allowed_routes(self):
        for original in ('true', 'false'):
            self._check_setup_cleanup(original)

    def test_failed_bootstrap_restores_original_flag(self):
        for original in ('true', 'false'):
            self._check_setup_cleanup(original, fail_bootstrap=True)

    def _check_setup_cleanup(self, original, fail_bootstrap=False):
        os.environ['COAST_CONTRACT_COOLIFY_FALLBACK_ENABLED'] = original
        case = test_live.LiveCoolifyBase()
        driver = mock.Mock()
        driver.bootstrap_environment.return_value.engine_ref = 'project'
        driver.get_engine_version.return_value = test_live.EXPECTED_VERSION
        driver.register_server.return_value.engine_ref = 'worker'
        driver.get_server_status.return_value = SimpleNamespace(
            status=states.ACTIVE)
        if fail_bootstrap:
            driver.bootstrap_environment.side_effect = (
                engine.EngineUnreachable('Bootstrap unavailable'))
        calls = []

        def api(method, path, **kwargs):
            self.assertTrue(proxy.allowed(method, '/api/v1' + path))
            calls.append((method, path, kwargs))
            return {'uuid': 'worker', 'ip': '192.0.2.1', 'name': 'worker'}

        with mock.patch.object(test_live, 'ContractDriver',
                               return_value=driver), mock.patch.object(
                                   case, 'api', side_effect=api):
            try:
                if fail_bootstrap:
                    self.assertRaises(engine.EngineUnreachable, case.setUp)
                else:
                    case.setUp()
            finally:
                self.assertTrue(case.doCleanups())
        self.assertIn(('PATCH', '/team', {'data': {
            'is_build_server_fallback_enabled': original == 'true'}}), calls)
        self.assertNotIn(('GET', '/team'), [c[:2] for c in calls])
        driver.delete_stack_project.assert_called_once_with(case.ep, case.env)

    def test_build_role_setup_accepts_agent_transport(self):
        os.environ['COAST_CONTRACT_COOLIFY_BUILD_REGISTRY_TEMPLATE'] = (
            'registry.example.com/{environment_id}/{app_id}')
        os.environ['COAST_CONTRACT_COOLIFY_RUN_SERVER_UUID'] = 'run-worker'
        for cls in (test_live.BuildModeTest, test_live.BuildServersTest):
            with mock.patch.object(
                    test_live.LiveCoolifyBase, 'setUp') as setup:
                cls().setUp()
                setup.assert_called_once_with()

    def test_server_inventory_uses_production_agent_transport(self):
        ep = test_live.endpoint(self.conf, self.values)
        driver = test_live.ContractDriver(self.conf)
        rows = [{'uuid': 'application', 'type': 'application'}]
        with requests_mock.Mocker() as http:
            http.get(ep.base_url + '/servers/worker/resources', json=rows)
            with driver._client(ep) as transport:
                self.assertEqual(rows, transport.request(
                    'GET', '/servers/worker/resources'))
                self.assertEqual((ep.client_cert, ep.client_key),
                                 transport.session.cert)
                self.assertEqual(ep.ca_cert, transport.session.verify)
            self.assertTrue(proxy.allowed(
                http.last_request.method, http.last_request.path_url))

    def test_shared_setup_with_real_bootstrap_obeys_proxy_allowlist(self):
        case = test_live.LiveCoolifyBase()
        project = []
        fallback_writes = []

        def respond(request, context):
            self.assertTrue(proxy.allowed(request.method, request.path_url))
            path = request.path.removeprefix('/api/v1')
            if path == '/health':
                return 'OK'
            if path == '/version':
                return test_live.EXPECTED_VERSION
            if path == '/team':
                fallback_writes.append(request.json())
                return {}
            if path == '/servers':
                return []
            if path == '/servers/worker':
                return {'uuid': 'worker', 'ip': '192.0.2.1', 'name': 'worker'}
            if path == '/projects':
                if request.method == 'POST':
                    project.append(dict(request.json(), uuid='project'))
                    return project[0]
                return project
            if path == '/projects/project/environments':
                return [{'name': 'production'}]
            self.fail('Unexpected contract request')

        driver_class = test_live.ContractDriver
        with requests_mock.Mocker() as http, mock.patch.object(
                driver_class, 'register_server', return_value=(
                    engine.DriverResult(engine_ref='worker'))), \
                mock.patch.object(driver_class, 'get_server_status',
                                  return_value=SimpleNamespace(
                                      status=states.ACTIVE)), \
                mock.patch.object(driver_class, 'delete_stack_project'):
            def response_text(request, context):
                payload = respond(request, context)
                return payload if isinstance(payload, str) else json.dumps(
                    payload)

            http.register_uri(requests_mock.ANY, requests_mock.ANY,
                              text=response_text)
            try:
                case.setUp()
            finally:
                self.assertTrue(case.doCleanups())
        self.assertEqual(1, len(project))
        self.assertEqual([
            {'is_build_server_fallback_enabled': False}] * 3,
            fallback_writes)
        self.assertFalse(proxy.allowed('GET', '/api/v1/team'))
