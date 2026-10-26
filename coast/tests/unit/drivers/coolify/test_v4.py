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

"""Offline tests against live fixtures and explicit synthetic edge cases."""

import base64
from contextlib import nullcontext
import copy
import dataclasses
import datetime
from email.utils import format_datetime
import io
import json
import logging
from pathlib import Path
import re
import traceback
from types import SimpleNamespace
from unittest import mock
from urllib import parse

import fixtures
import jsonschema
import requests
import requests_mock

from coast.agent import proxy
from coast.common import engine_versions
from coast.common import states
from coast.conductor import agent_client
from coast.drivers import base as engine
from coast.drivers.coolify import catalog
from coast.drivers.coolify import client
from coast.drivers.coolify import v4
from coast.drivers import loader
from coast.tests import base

FIXTURES = Path(__file__).parent / "fixtures"

# Separate guest allow-list gaps; changing access must be deliberate.
KNOWN_DENIED = {
    "POST /services/{uuid}/envs",
    "PATCH /services/{uuid}/envs",
}


class CoolifyTest(base.TestCase):
    def setUp(self):
        super().setUp()
        scope = mock.patch('coast.agent.namespace.management_namespace',
                           side_effect=lambda name: nullcontext())
        scope.start()
        self.addCleanup(scope.stop)
        self.http = requests_mock.Mocker()
        self.http.start()
        self.addCleanup(self.http.stop)
        self.synthetic = json.loads((FIXTURES / "synthetic.json").read_text())
        self.fixtures = self.synthetic.copy()
        self.fixtures.update(
            json.loads((FIXTURES / "v4.4.3.json").read_text()))
        self.contracts = json.loads((FIXTURES / "requests.json").read_text())
        self.driver = loader.load_driver("coolify_v4", self.conf)
        self.config.config(retry_wait=0, group="coolify")
        self.config.config(agent_client_cert="cert", agent_client_key="key",
                           group="controller_worker")
        self.config.config(ca_certificate="ca", group="certificates")
        self.ep = agent_client.endpoint(self.conf, "192.0.2.10",
                                        token="engine-secret")
        self.env = SimpleNamespace(
            engine_version="4.4.3", id="env", engine_ref="project-uuid")
        self.server = SimpleNamespace(
            id="server",
            hostname="coast-shop-5bad06-worker-1",
            engine_ref="server-uuid",
            private_ip="192.0.2.10",
            role="worker",
        )
        self.service = SimpleNamespace(
            id="app",
            engine_ref="app-uuid",
            server_id="server",
            server_hostname="coast-shop-5bad06-worker-1",
            source_id="source",
            description="Test service",
            ports="8080",
            replicas=1,
            auto_deploy=False,
            cpu_limit=0.5,
            memory_limit_mb=128,
            healthcheck_path="/healthz",
            project_engine_ref="application-project",
            environment_engine_ref="stage-uuid",
        )
        self.source = SimpleNamespace(
            id="source",
            type="git",
            repo_url="https://git.example.com/service.git",
            branch="main",
            build_pack="nixpacks",
            credentials_ref=None,
            base_directory="/",
            dockerfile_path=None,
            compose_path=None,
            image=None,
        )
        # A finished deployment is ACTIVE only once the app runs.
        self.http.get(self.ep.base_url + "/applications/app-uuid",
                      json={"uuid": "app-uuid", "status": "running:healthy"})
        self.deployment = SimpleNamespace(
            id="deployment",
            engine_ref="deployment-uuid",
            source_ref="main",
            image_digest=None,
            spec={
                "source": vars(self.source).copy(),
                "ports": "8080",
                "replicas": 1,
                "cpu_limit": 0.5,
                "memory_limit_mb": 128,
                "healthcheck_path": "/healthz",
            },
        )
        self.addCleanup(self.check_contracts)
        self.addCleanup(self.check_proxy_contract)

    def build_mode_fixture(self):
        # Synthetic, schema-derived responses; not live recordings.
        rows = json.loads((FIXTURES / "build-mode.json").read_text())
        self.mode_rows = {
            "/servers/server-uuid": rows["worker"],
            "/applications/app-uuid": rows["git"]}
        self.mode_patches = []
        self.mode_failure = None

        def observe(request, context):
            path = request.path_url.removeprefix('/api/v1')
            return copy.deepcopy(self.mode_rows[path])

        def patch(request, context):
            path = request.path_url.removeprefix('/api/v1')
            data = request.json()
            self.mode_patches.append((path, data))
            row = self.mode_rows[path]
            for key, value in data.items():
                if key == 'server_role':
                    row['settings'][key] = value
                elif key == 'use_build_server':
                    row['settings']['is_build_server_enabled'] = value
                else:
                    row[key] = value
            if self.mode_failure and self.mode_failure(path, data):
                # The patch applied but its reply failed.
                context.status_code = 400
                return {'message': 'password=secret'}
            return {'message': 'Updated'}

        for path in self.mode_rows:
            self.http.register_uri('GET', self.ep.base_url + path,
                                   json=observe)
            self.http.register_uri('PATCH', self.ep.base_url + path,
                                   json=patch)
        self.config.config(
            build_registry_image_template=(
                'registry.example.com/{environment_id}/{app_id}'),
            group='coolify')

    def switch_build_mode(self, enabled):
        self.driver.set_build_server_mode(
            self.ep, self.env, [self.server], [self.service], enabled)

    def assert_build_mode(self, enabled):
        self.assertEqual('deployment' if enabled else 'both',
                         self.mode_rows['/servers/server-uuid'][
                             'settings']['server_role'])
        self.assertEqual(enabled, self.mode_rows['/applications/app-uuid'][
            'settings']['is_build_server_enabled'])

    def test_build_mode_switch_and_idempotent_retry(self):
        self.build_mode_fixture()
        self.switch_build_mode(True)
        self.assert_build_mode(True)
        self.assertEqual('registry.example.com/env/app',
                         self.mode_rows['/applications/app-uuid'][
                             'docker_registry_image_name'])
        self.assertEqual(2, len(self.mode_patches))
        self.switch_build_mode(True)
        self.assertEqual(2, len(self.mode_patches))
        self.switch_build_mode(False)
        self.assert_build_mode(False)
        self.assertEqual(4, len(self.mode_patches))
        self.switch_build_mode(False)
        self.assertEqual(4, len(self.mode_patches))

    def test_build_mode_lost_reply_compensates_attempted_patch(self):
        self.build_mode_fixture()
        self.mode_failure = lambda path, data: data.get('use_build_server')
        error = self.assertRaises(engine.DriverError,
                                  self.switch_build_mode, True)
        self.assertNotIn('secret', str(error))
        self.assertIn('previous settings restored', str(error))
        self.assert_build_mode(False)
        self.assertFalse(self.mode_rows['/applications/app-uuid'][
            'docker_registry_image_name'])
        self.mode_failure = None
        self.switch_build_mode(True)
        self.assert_build_mode(True)

    def test_build_mode_restore_failure_reports_operator_recovery(self):
        self.build_mode_fixture()
        self.mode_failure = lambda path, data: True
        error = self.assertRaises(engine.DriverError,
                                  self.switch_build_mode, True)
        self.assertIn('operator recovery required', str(error))
        self.assertNotIn('secret', str(error))

    def test_build_mode_snapshot_failure_does_not_patch(self):
        self.build_mode_fixture()
        del self.mode_rows['/applications/app-uuid']['settings']
        self.assertRaises(engine.DriverError, self.switch_build_mode, True)
        self.assertEqual([], self.mode_patches)
        self.assert_build_mode_worker_unchanged()

    def assert_build_mode_worker_unchanged(self):
        self.assertEqual('both', self.mode_rows['/servers/server-uuid'][
            'settings']['server_role'])

    def test_build_mode_missing_registry_fails_before_worker_patch(self):
        self.build_mode_fixture()
        self.config.config(build_registry_image_template='', group='coolify')
        self.assertRaises(engine.DriverError, self.switch_build_mode, True)
        self.assertEqual([], self.mode_patches)
        self.assert_build_mode(False)

    def test_build_mode_disable_lost_reply_restores_enabled_settings(self):
        self.build_mode_fixture()
        self.switch_build_mode(True)
        self.mode_failure = lambda path, data: (
            data.get('use_build_server') is False)
        self.assertRaises(engine.DriverError, self.switch_build_mode, False)
        self.assert_build_mode(True)

    def register(self, method, path, fixture=None, *, body=None, status=200):
        if fixture:
            body = copy.deepcopy(self.fixtures[fixture])
        args = {"status_code": status}
        args["text" if isinstance(body, str) else "json"] = body
        return self.http.register_uri(method, self.ep.base_url + path, **args)

    def check_contracts(self):
        for request in self.http.request_history:
            path = parse.urlsplit(request.url).path.removeprefix("/api/v1")
            contract = None
            for key, value in self.contracts.items():
                method, template = key.split(" ", 1)
                pattern = re.sub(r"\{[^}]+\}", "[^/]+", template)
                if method == request.method and re.fullmatch(pattern, path):
                    contract = copy.deepcopy(value)
                    break
            self.assertIsNotNone(contract, request.method + " " + path)
            query = parse.parse_qs(parse.urlsplit(request.url).query)
            self.assertFalse(set(query) - set(contract["query"]))
            if request.body:
                schema = contract["body"]
                self.assertIsNotNone(schema)
                # Documented either/or, despite contradictory required list.
                required = schema.get("required", [])
                if "environment_name" in required:
                    schema["required"] = [
                        field
                        for field in required
                        if field
                        not in ("environment_name", "environment_uuid")
                    ]
                    schema["anyOf"] = [
                        {"required": ["environment_name"]},
                        {"required": ["environment_uuid"]},
                    ]
                jsonschema.validate(request.json(), schema)

    def check_proxy_contract(self):
        for request in self.http.request_history:
            path = parse.urlsplit(request.url).path.removeprefix("/api/v1")
            denied = any(
                method == request.method and re.fullmatch(
                    re.sub(r"\{[^}]+\}", "[^/]+", template), path)
                for method, template in
                (entry.split(" ", 1) for entry in KNOWN_DENIED))
            self.assertEqual(
                not denied, proxy.allowed(request.method, request.path_url),
                request.method + " " + request.path_url)

    def test_pasted_dockerfile_create_and_compare_public_contract(self):
        fixture = json.loads((FIXTURES / 'pasted-sources.json').read_text())
        self.service.engine_ref = None
        self.http.get(self.ep.base_url + '/applications', json=[])
        self.http.get(self.ep.base_url + '/servers/server', json={
            'uuid': 'server', 'settings': {'server_role': 'deployment'}})
        self.service.server_engine_ref = 'server'
        posted = self.http.post(self.ep.base_url + '/applications/dockerfile',
                                json=fixture['created_app'])
        self.register('GET', '/servers', body=[])
        source = SimpleNamespace(type='dockerfile', credentials_ref=None,
                                 dockerfile=fixture['dockerfile'])
        result = self.driver.create_app(
            self.ep, self.env, self.service, source, None)
        self.assertEqual('app-uuid', result.engine_ref)
        data = posted.last_request.json()
        self.assertEqual(fixture['dockerfile'], base64.b64decode(
            data['dockerfile']).decode('utf-8'))
        self.assertFalse(data['instant_deploy'])
        self.assertFalse(data['autogenerate_domain'])
        self.assertEqual('8080', data['ports_exposes'])
        self.assertNotIn('git_repository', data)
        self.service.engine_ref = result.engine_ref
        self.http.get(self.ep.base_url + '/applications/app-uuid',
                      json={'dockerfile': fixture['dockerfile']})
        self.driver.set_dockerfile(self.ep, self.env, self.service,
                                   fixture['dockerfile'])
        self.assertFalse(any(r.method == 'PATCH' for r in
                             self.http.request_history))
        self.assertFalse(any('/deploy' in r.path for r in
                             self.http.request_history))

    def test_dockerfile_compare_fails_without_writes(self):
        content = 'FROM busybox # private-content-marker\n'
        for current, error in (
                ({'dockerfile': content.rstrip()},
                 engine.DockerfileChangeUnsupported),
                ({}, engine.DriverError),
                ({'dockerfile': None}, engine.DriverError)):
            with self.subTest(current=current):
                self.http.get(self.ep.base_url + '/applications/app-uuid',
                              json=current)
                failure = self.assertRaises(
                    error, self.driver.set_dockerfile,
                    self.ep, self.env, self.service, content)
                self.assertNotIn('private-content-marker', str(failure))
        self.assertTrue(all(r.method == 'GET' for r in
                            self.http.request_history))

    def test_pasted_source_rejections_do_not_log_echoed_content(self):
        for source_type in ('dockerfile', 'compose'):
            for method in ('POST', 'GET' if source_type == 'dockerfile'
                           else 'PATCH'):
                with self.subTest(source_type=source_type, method=method):
                    self.service.engine_ref = None
                    self.service.is_engine_service = source_type == 'compose'
                    self.service.server_engine_ref = 'server'
                    prefix = ('/services' if source_type == 'compose'
                              else '/applications')
                    self.register('GET', '/servers', body=[])
                    self.http.get(self.ep.base_url + prefix, json=[])
                    self.http.get(self.ep.base_url + '/servers/server', json={
                        'uuid': 'server',
                        'settings': {'server_role': 'deployment'}})
                    path = prefix + ('/dockerfile' if source_type ==
                                     'dockerfile' else '')
                    if method != 'POST':
                        self.service.engine_ref = 'resource-uuid'
                        path = prefix + '/resource-uuid'
                    self.http.register_uri(
                        method, self.ep.base_url + path, status_code=422,
                        json={'message': 'private-content-marker', 'errors': {
                            source_type: ['private-content-marker']}})
                    content = 'FROM busybox # private-content-marker'
                    with self.assertLogs('coast.drivers.coolify.client',
                                         level='DEBUG') as logs:
                        if method == 'POST':
                            source = SimpleNamespace(
                                type=source_type, credentials_ref=None,
                                dockerfile=content, compose=content)
                            self.assertRaises(
                                engine.EngineConflict, self.driver.create_app,
                                self.ep, self.env, self.service, source, None)
                        else:
                            self.assertRaises(
                                engine.EngineConflict,
                                getattr(self.driver, 'set_' + source_type),
                                self.ep, self.env, self.service, content)
                    self.assertNotIn('private-content-marker',
                                     ' '.join(logs.output))
                    self.assertIn('error_keys=' + source_type,
                                  ' '.join(logs.output))

    def test_pasted_dockerfile_preserves_expose_inference(self):
        self.service.ports = None
        source = SimpleNamespace(type='dockerfile', dockerfile='FROM busybox')
        self.assertNotIn('ports_exposes', v4.app_settings(
            self.service, source))
        self.deployment.spec['source'] = {'type': 'dockerfile'}
        self.deployment.spec['ports'] = None
        self.assertNotIn('ports_exposes', v4.frozen_settings(
            self.service, self.deployment))

    def test_pasted_compose_uses_service_routes_independent_of_kind(self):
        fixture = json.loads((FIXTURES / 'pasted-sources.json').read_text())
        self.service.engine_ref = None
        self.service.is_engine_service = True
        self.service.kind = 'app'
        self.service.server_engine_ref = 'server'
        self.http.get(self.ep.base_url + '/services', json=[])
        self.http.get(self.ep.base_url + '/servers/server', json={
            'uuid': 'server', 'settings': {'server_role': 'deployment'}})
        posted = self.http.post(self.ep.base_url + '/services',
                                json=fixture['created_service'])
        result = self.driver.create_app(self.ep, self.env, self.service,
                                        {'type': 'compose',
                                         'compose': fixture['compose']}, None)
        self.assertEqual('svc-uuid', result.engine_ref)
        data = posted.last_request.json()
        self.assertEqual(fixture['compose'], base64.b64decode(
            data['docker_compose_raw']).decode('utf-8'))
        self.assertFalse(data['instant_deploy'])
        self.service.engine_ref = result.engine_ref
        path = self.ep.base_url + '/services/svc-uuid'
        self.http.get(path, json=fixture['service'])
        patched = self.http.patch(path, json={})
        self.assertEqual(fixture['service']['docker_compose_raw'],
                         self.driver.get_compose(
                             self.ep, self.env, self.service))
        self.driver.set_compose(self.ep, self.env, self.service,
                                fixture['compose'])
        self.assertEqual(data['docker_compose_raw'],
                         patched.last_request.json()['docker_compose_raw'])
        self.deployment.engine_ref = None
        restarted = self.http.post(path + '/restart', json={})
        self.driver.deploy_app(
            self.ep, self.env, self.service, self.deployment)
        self.assertTrue(restarted.called)
        self.http.delete(path, json={})
        self.driver.delete_app(self.ep, self.env, self.service)

    def test_pasted_rollback_enqueues_frozen_content_as_fresh_deployment(self):
        for kind in ('dockerfile', 'compose'):
            self.deployment.spec['source'] = {'type': kind}
            target = SimpleNamespace(spec={'source': {'type': kind}})
            with mock.patch.object(self.driver, 'deploy_app') as deploy:
                self.driver.rollback(self.ep, self.env, self.service,
                                     target, self.deployment)
                deploy.assert_called_once_with(
                    self.ep, self.env, self.service, self.deployment)

    def test_known_denied_paths_remain_denied_by_agent(self):
        with client.Client(self.ep, self.conf) as transport:
            for entry in sorted(KNOWN_DENIED):
                method, template = entry.split(" ", 1)
                path = re.sub(r"\{[^}]+\}", "resource-uuid", template)
                with self.subTest(method=method, path=path):
                    target = parse.urlsplit(transport.url + path).path
                    self.assertFalse(proxy.allowed(method, target))

    def test_compare_settings_public_fixture(self):
        fixture = json.loads((FIXTURES / 'reconcile.json').read_text())
        self.service.template_id = 'template'
        path = self.ep.base_url + '/services/app-uuid'
        self.http.get(path, json=fixture['service'])
        self.http.get(path + '/envs', json=fixture['envs'])
        variables = {'PASSWORD': engine.EnvVar('fixture-only')}
        desired = ['other.example.com', 'site.example.com']
        self.assertEqual({'env': False, 'domains': False},
                         self.driver.compare_settings(
                             self.ep, self.env, self.service,
                             variables, desired))
        fixture['envs'][0]['value'] = None
        self.http.get(path + '/envs', json=fixture['envs'])
        self.assertIsNone(self.driver.compare_settings(
            self.ep, self.env, self.service, variables, desired)['env'])
        self.assertEqual({'env': True, 'domains': True},
                         self.driver.compare_settings(
                             self.ep, self.env, self.service, {}, []))
        fixture['service']['applications'].append({'name': 'helper'})
        fixture['service']['applications'][0]['fqdn'] = None
        self.http.get(path, json=fixture['service'])
        self.assertIsNone(self.driver.compare_settings(
            self.ep, self.env, self.service, variables, desired)['domains'])
        self.assertTrue(all(r.method == 'GET'
                            for r in self.http.request_history))
        self.assertNotIn('fixture-only', repr(self.driver.compare_settings(
            self.ep, self.env, self.service, variables, desired)))

    def test_compare_application_q52_and_malformed_observations(self):
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path, json={'fqdn': 'http://site.example.com'})
        self.http.get(path + '/envs', json=[])
        self.assertEqual({'env': False, 'domains': True},
                         self.driver.compare_settings(
                             self.ep, self.env, self.service, {},
                             ['site.example.com']))
        self.assertTrue(self.driver.compare_settings(
            self.ep, self.env, self.service,
            {'KEY': engine.EnvVar('secret')}, [])['env'])
        self.http.get(path + '/envs', json={'unexpected': 'secret'})
        self.http.get(path, json={})
        self.assertEqual({'env': None, 'domains': None},
                         self.driver.compare_settings(
                             self.ep, self.env, self.service, {}, []))

    def test_comparison_all_workloads_is_read_only(self):
        fixture = json.loads((FIXTURES / 'reconcile.json').read_text())
        for kind in ('git', 'image', 'compose', 'template'):
            with self.subTest(kind=kind):
                self.service.template_id = ('template' if kind == 'template'
                                            else None)
                resource = ('services' if kind == 'template'
                            else 'applications')
                path = self.ep.base_url + '/' + resource + '/app-uuid'
                row = (copy.deepcopy(fixture['service'])
                       if kind == 'template' else {'fqdn': None})
                envs = fixture['envs'] if kind == 'template' else []
                variables = ({'PASSWORD': engine.EnvVar('fixture-only')}
                             if kind == 'template' else {})
                domains = (['site.example.com', 'other.example.com']
                           if kind == 'template' else [])
                self.http.get(path + '/envs', json=envs)
                for status, expected in (
                        ('running:healthy', states.ACTIVE),
                        ('running:unhealthy', states.ERROR),
                        ('exited', states.STOPPED),
                        ('stopped', states.STOPPED),
                        ('starting', states.DEPLOYING),
                        ('restarting', states.DEPLOYING),
                        ('unknown', states.ERROR)):
                    row['status'] = status
                    self.http.get(path, json=row)
                    self.assertEqual({'env': False, 'domains': False},
                                     self.driver.compare_settings(
                                         self.ep, self.env, self.service,
                                         variables, domains))
                    self.assertEqual(expected, self.driver.get_app_status(
                        self.ep, self.env, self.service).status)
        self.assertTrue(all(r.method == 'GET'
                            for r in self.http.request_history))

    def test_template_env_bulk_and_owned_deletion(self):
        self.service.template_id = "wordpress-with-mariadb"
        path = self.ep.base_url + "/services/app-uuid/envs"
        self.http.get(path, json=[
            {"uuid": "generated", "key": "SERVICE_PASSWORD_DB",
             "value": "generated-secret"},
            {"uuid": "default", "key": "DEFAULT", "value": "default"},
            {"uuid": "old", "key": "OLD", "comment": "coast-managed"},
            {"uuid": "keep", "key": "PASSWORD", "comment": "coast-managed"},
        ])
        self.http.patch(path + "/bulk", json=[{"value": "response-secret"}])
        self.http.delete(path + "/old", status_code=204)
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("coast.drivers.coolify.client")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.driver.set_env(self.ep, self.env, self.service, {
            "PASSWORD": engine.EnvVar("plain-secret\nsecond", is_secret=True)})
        self.assertEqual({"data": [{
            "key": "PASSWORD", "value": "plain-secret\nsecond",
            "is_literal": True, "is_multiline": True,
            "is_shown_once": True, "comment": "coast-managed"}]},
            self.http.request_history[1].json())
        self.assertEqual(["GET", "PATCH", "DELETE"],
                         [item.method for item in self.http.request_history])
        for value in ("plain-secret", "generated-secret", "response-secret"):
            self.assertNotIn(value, output.getvalue())

    def test_template_env_empty_preserves_generated_defaults(self):
        self.service.template_id = "template"
        path = self.ep.base_url + "/services/app-uuid/envs"
        self.http.get(path, json=[
            {"uuid": "generated", "key": "SERVICE_PASSWORD_DB"},
            {"uuid": "old", "key": "OLD", "comment": "coast-managed"}])
        self.http.delete(path + "/old", status_code=204)
        self.driver.set_env(self.ep, self.env, self.service, {})
        self.assertEqual(2, len(self.http.request_history))

    def test_template_env_build_time_rejected_before_http(self):
        self.service.template_id = "template"
        self.assertRaises(engine.DriverError, self.driver.set_env,
                          self.ep, self.env, self.service,
                          {"KEY": engine.EnvVar("secret", build_time=True)})
        self.assertEqual([], self.http.request_history)

    def test_template_domains_use_service_container_name(self):
        self.service.template_id = "template"
        path = self.ep.base_url + "/services/app-uuid"
        self.http.get(path, json={"applications": [
            {"name": "web", "fqdn": "https://old.example.com"},
            {"name": "helper", "fqdn": None}]})
        self.http.patch(path, json={"uuid": "app-uuid"})
        self.driver.set_domains(self.ep, self.env, self.service,
                                ["one.example.com", "two.example.com"])
        self.assertEqual({
            "urls": [{"name": "web", "url":
                      "https://one.example.com:8080,"
                      "https://two.example.com:8080"}],
            "instant_deploy": False}, self.http.last_request.json())
        self.driver.set_domains(self.ep, self.env, self.service, [])
        self.assertEqual({"urls": [{"name": "web", "url": ""}],
                          "instant_deploy": False},
                         self.http.last_request.json())

    def test_template_domain_internal_port_round_trip(self):
        self.service.template_id = 'uptime-kuma'
        self.service.ports = '3001,8080'
        path = self.ep.base_url + '/services/app-uuid'
        row = {'applications': [{'name': 'web', 'fqdn': None}]}
        self.http.get(path, json=row)
        self.http.patch(path, json={})
        self.driver.set_domains(self.ep, self.env, self.service,
                                ['site.example.com'])
        url = self.http.last_request.json()['urls'][0]['url']
        self.assertEqual('https://site.example.com:3001', url)
        row['applications'][0]['fqdn'] = url
        self.http.get(path, json=row)
        self.http.get(path + '/envs', json=[])
        self.assertEqual(
            ['https://site.example.com'], self.driver.list_domains(
                self.ep, self.env, self.service))
        self.assertFalse(self.driver.compare_settings(
            self.ep, self.env, self.service, {},
            ['site.example.com'])['domains'])
        self.assertTrue(self.driver.compare_settings(
            self.ep, self.env, self.service, {},
            ['other.example.com'])['domains'])
        for value in ('site.example.com:3001', 'host/path', 'host@evil'):
            self.assertRaises(engine.DriverError, v4._domain_url, value, 3001)

    def test_template_domain_target_fails_closed(self):
        self.service.template_id = "template"
        path = self.ep.base_url + "/services/app-uuid"
        for applications in ([], [{"name": "one"}, {"name": "two"}],
                             [{"name": "one", "fqdn": "https://a.example.com"},
                              {"name": "two",
                               "fqdn": "https://b.example.com"}]):
            self.http.get(path, json={"applications": applications})
            self.assertRaises(engine.DriverError, self.driver.set_domains,
                              self.ep, self.env, self.service,
                              ["site.example.com"])
        self.assertTrue(all(item.method == "GET"
                            for item in self.http.request_history))

    def test_template_domain_single_application_without_fqdn(self):
        self.service.template_id = "template"
        self.service.ports = None
        path = self.ep.base_url + "/services/app-uuid"
        self.http.get(path, json={"applications": [{"name": "web"}]})
        self.http.patch(path, json={})
        self.driver.set_domains(self.ep, self.env, self.service,
                                ["site.example.com"])
        self.assertEqual(
            "web", self.http.last_request.json()["urls"][0]["name"])
        self.assertEqual(
            "https://site.example.com",
            self.http.last_request.json()["urls"][0]["url"])

    def test_template_status_uses_service_endpoint(self):
        self.service.template_id = "template"
        self.http.get(self.ep.base_url + "/services/app-uuid",
                      json={"status": "running"})
        self.assertEqual(states.ACTIVE, self.driver.get_app_status(
            self.ep, self.env, self.service).status)

    def test_client_mtls_and_close(self):
        self.register("GET", "/version", "version")
        with client.Client(self.ep, self.conf) as c:
            session = c.session
            self.assertEqual(("cert", "key"), session.cert)
            self.assertEqual("ca", session.verify)
            self.assertFalse(session.trust_env)
            self.assertEqual(self.fixtures["version"],
                             c.request("GET", "/version", text=True))
        request = self.http.last_request
        self.assertEqual(
            "Bearer engine-secret", request.headers["Authorization"]
        )
        self.assertEqual(30, request.timeout)
        self.assertFalse(request.allow_redirects)

    def test_client_direct_url_preserves_tls(self):
        self.config.config(
            insecure_direct_url=True,
            api_timeout=5,
            group="coolify",
        )
        for url in ("https://direct.example.com",
                    "https://direct.example.com/api/v1/"):
            with self.subTest(url=url):
                ep = engine.EngineEndpoint(url, "", "", "ca", "engine-secret")
                self.http.register_uri(
                    "GET", "https://direct.example.com/api/v1/version",
                    text=self.fixtures["version"])
                with client.Client(ep, self.conf) as c:
                    self.assertIsNone(c.session.cert)
                    self.assertEqual("ca", c.session.verify)
                    self.assertEqual(self.fixtures["version"], c.request(
                        "GET", "/version", text=True))
                self.assertEqual(5, self.http.last_request.timeout)

    def test_client_invalid_endpoint_and_missing_credentials(self):
        for url in (
            "http://controller.example.com",
            "https://secret@controller.example.com",
            "https://controller.example.com?token=secret",
            "https://controller.example.com#fragment",
        ):
            ep = engine.EngineEndpoint(url, "cert", "key", "ca", "token")
            self.assertRaises(engine.DriverError, client.Client, ep, self.conf)
        for token, cert in ((None, "cert"), ("token", "")):
            ep = engine.EngineEndpoint(
                self.ep.base_url, cert, "key", "ca", token
            )
            self.assertRaises(engine.DriverError, client.Client, ep, self.conf)
        self.assertEqual([], self.http.request_history)

    def test_client_error_mapping_and_no_secrets(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("coast.drivers.coolify.client")
        logger.addHandler(handler)
        level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, level)
        sleep = mock.patch.object(client.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)
        with client.Client(self.ep, self.conf) as c:
            for status, error_type in (
                (400, engine.DriverError),
                (401, engine.EngineAuthError),
                (403, engine.EngineAuthError),
                (404, engine.EngineNotFound),
                (409, engine.EngineConflict),
                (422, engine.EngineConflict),
                (429, engine.EngineRateLimited),
                (503, engine.EngineUnreachable),
                (302, engine.DriverError),
            ):
                self.register("POST", "/deploy", "error", status=status)
                error = self.assertRaises(
                    error_type, c.request, "POST", "/deploy"
                )
                self.assertNotIn("secret", str(error))
        logged = output.getvalue()
        self.assertNotIn("secret", logged)
        self.assertIn("Coolify API fault", logged)
        self.assertIn("path=/deploy", logged)
        self.assertNotIn("response-secret", logged)
        self.assertNotIn("engine-secret", logged)

    def test_client_logs_sanitized_4xx_and_hides_other_bodies(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("coast.drivers.coolify.client")
        logger.addHandler(handler)
        level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, level)
        secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-key-material"
        self.register(
            "POST", "/servers/server-uuid/validate", body={
                "message": "A server with this IP/Domain is already in use.",
                "errors": {
                    "ip": ["The ip field is required."],
                    "private_key": [secret],
                },
                "private_key": secret,
            }, status=400)
        with client.Client(self.ep, self.conf) as c:
            error = self.assertRaises(
                engine.DriverError, c.request, "POST",
                "/servers/server-uuid/validate", data={"install": False})
        self.assertEqual("Coolify API returned HTTP 400", str(error))
        text = output.getvalue()
        self.assertIn("method=POST", text)
        self.assertIn("path=/servers/{id}/validate", text)
        self.assertIn("status=400", text)
        self.assertIn(
            "message=A server with this IP/Domain is already in use.", text)
        self.assertIn("error_keys=ip,private_key", text)
        self.assertIn("detail=The ip field is required.", text)
        self.assertNotIn("server-uuid", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("BEGIN", text)
        self.assertEqual(1, len(self.http.request_history))
        output.truncate(0)
        output.seek(0)
        self.register(
            "POST", "/servers", body="token=engine-secret", status=400)
        with client.Client(self.ep, self.conf) as c:
            self.assertRaises(
                engine.DriverError, c.request, "POST", "/servers")
        self.assertNotIn("engine-secret", output.getvalue())
        self.assertIn("message=-", output.getvalue())
        output.truncate(0)
        output.seek(0)
        self.register(
            "GET", "/version", body="token=engine-secret", status=503)
        with client.Client(self.ep, self.conf) as c:
            self.assertRaises(
                engine.EngineUnreachable, c.request, "GET", "/version")
        self.assertNotIn("Coolify API fault", output.getvalue())
        self.assertNotIn("engine-secret", output.getvalue())

    def test_retries_only_idempotent_methods(self):
        with client.Client(self.ep, self.conf) as c:
            for method, path in (
                ("GET", "/applications/app-uuid"),
                ("PATCH", "/applications/app-uuid"),
                ("DELETE", "/applications/app-uuid"),
            ):
                response = self.http.register_uri(
                    method,
                    self.ep.base_url + path,
                    [
                        {"status_code": 503, "json": self.fixtures["error"]},
                        {"status_code": 200, "json": self.fixtures["app"]},
                    ],
                )
                self.assertEqual(self.fixtures["app"], c.request(method, path))
                self.assertEqual(2, response.call_count)
            response = self.register("POST", "/deploy", "error", status=503)
            self.assertRaises(
                engine.EngineUnreachable, c.request, "POST", "/deploy"
            )
            self.assertEqual(1, response.call_count)
            response = self.register("GET", "/version", "error", status=401)
            self.assertRaises(
                engine.EngineAuthError, c.request, "GET", "/version"
            )
            self.assertEqual(1, response.call_count)

    def test_client_transport_and_json_failures(self):
        with client.Client(self.ep, self.conf) as c:
            response = self.http.register_uri(
                "GET",
                self.ep.base_url + "/version",
                exc=requests.ConnectionError("engine-secret"),
            )
            error = self.assertRaises(
                engine.EngineUnreachable, c.request, "GET", "/version"
            )
            self.assertEqual(3, response.call_count)
            self.assertNotIn(
                "engine-secret",
                "".join(
                    traceback.format_exception(
                        type(error), error, error.__traceback__
                    )
                ),
            )
            self.register("GET", "/version", body="not JSON engine-secret")
            self.assertRaises(engine.DriverError, c.request, "GET", "/version")
            self.register(
                "DELETE", "/applications/app-uuid", body="", status=204
            )
            self.assertIsNone(c.request("DELETE", "/applications/app-uuid"))

    def test_engine_version_and_bootstrap_recovery(self):
        self.register("GET", "/health", "health")
        self.register("GET", "/version", "version")
        self.assertEqual("4.4.3", self.driver.get_engine_version(self.ep))
        self.register(
            "PATCH", "/team", body={"is_build_server_fallback_enabled": False}
        )
        self.register("GET", "/projects", body=[])
        self.register("POST", "/projects", "project_created", status=201)
        self.register("GET", "/projects/project-uuid/environments", body=[])
        self.register(
            "POST", "/projects/project-uuid/environments",
            "environment_created",
            status=201,
        )
        self.register("GET", "/servers", body=[])
        self.env.engine_ref = None
        result = self.driver.bootstrap_environment(self.ep, self.env)
        self.assertEqual("project-uuid", result.engine_ref)
        self.register("GET", "/projects", "projects")
        self.register(
            "GET", "/projects/project-uuid/environments", "environments"
        )
        self.assertEqual(
            result, self.driver.bootstrap_environment(self.ep, self.env)
        )
        self.assertEqual(
            1,
            sum(
                r.method == "POST" and r.path_url == "/api/v1/projects"
                for r in self.http.request_history
            ),
        )
        self.register("GET", "/version", body="v4.5.0 engine-secret")
        error = self.assertRaises(
            engine.UnsupportedEngineVersion, self.driver.get_engine_version,
            self.ep,
        )
        self.assertNotIn("engine-secret", str(error))

    def test_supported_mixed_fleet_bootstraps_with_new_default(self):
        self.config.config(pinned_version='4.4.7', group='coolify')
        for version in ('4.4.3', '4.4.7'):
            self.env.engine_version = version
            self.register('GET', '/health', 'health')
            self.register('GET', '/version', body=version)
            self.register('GET', '/servers', body=[])
            self.register('PATCH', '/team', body={})
            self.register('GET', '/projects', 'projects')
            self.register('GET', '/projects/project-uuid/environments',
                          'environments')
            self.assertEqual(version, self.driver.get_engine_version(self.ep))
            self.driver.bootstrap_environment(self.ep, self.env)
        self.env.engine_version = '4.4.3'
        self.assertRaises(engine.UnsupportedEngineVersion,
                          self.driver.bootstrap_environment,
                          self.ep, self.env)
        self.register('GET', '/version', body='4.4.9')
        self.assertRaises(engine.UnsupportedEngineVersion,
                          self.driver.get_engine_version, self.ep)

    def _allow_terminal_off(self):
        self.register("PATCH", "/servers/server-uuid",
                      body={"uuid": "server-uuid"})

    def test_bootstrap_turns_builtin_terminals_off(self):
        self.register("GET", "/health", "health")
        self.register("GET", "/version", "version")
        self.register(
            "PATCH", "/team",
            body={"is_build_server_fallback_enabled": False})
        self.register("GET", "/projects", "projects")
        self.register(
            "GET", "/projects/project-uuid/environments", "environments")
        self.register("GET", "/servers", "servers")
        self._allow_terminal_off()
        self.driver.bootstrap_environment(self.ep, self.env)
        terminal = [
            request.json() for request in self.http.request_history
            if request.method == "PATCH" and
            request.path.endswith("/servers/server-uuid")]
        self.assertEqual([{"is_terminal_enabled": False}], terminal)

    def test_terminal_disable_skips_an_existing_false_setting(self):
        server = copy.deepcopy(self.fixtures["server"])
        server["settings"]["is_terminal_enabled"] = False
        self.register("GET", "/servers", body=[server])
        self.driver.disable_builtin_terminals(self.ep, self.env)
        self.assertFalse(any(
            request.method == "PATCH"
            for request in self.http.request_history))

    def test_terminal_disable_rejects_an_invalid_server_list(self):
        self.register("GET", "/servers", body={"servers": []})
        self.assertRaises(
            engine.DriverError, self.driver.disable_builtin_terminals,
            self.ep, self.env)

    def test_ensure_and_delete_project(self):
        application = SimpleNamespace(id="app-1", engine_ref=None)
        stack = SimpleNamespace(id="stack", engine_ref=None)
        self.register("GET", "/projects", body=[])
        self.register(
            "POST", "/projects", body={"uuid": "project-1"}, status=201)
        result = self.driver.ensure_project(self.ep, stack, application)
        self.assertEqual("project-1", result.engine_ref)
        application.engine_ref = result.engine_ref
        self.register("GET", "/projects/project-1", body={
            "uuid": "project-1", "name": "coast-app-1"})
        self.assertEqual(
            result, self.driver.ensure_project(self.ep, stack, application))
        posts = [
            item for item in self.http.request_history
            if item.method == "POST"]
        self.assertEqual(1, len(posts))
        self.assertEqual({"name": "coast-app-1"}, posts[0].json())
        missing = SimpleNamespace(id="app-1", engine_ref="missing-uuid")
        self.register(
            "GET", "/projects/missing-uuid", body={"message": "no"},
            status=404)
        self.assertRaises(
            engine.EngineNotFound, self.driver.ensure_project, self.ep,
            stack, missing)
        self.register("GET", "/projects/project-1/environments", body=[
            {"uuid": "env-1", "name": "coast-env-1"}])
        self.assertRaises(
            engine.DriverError, self.driver.delete_project, self.ep,
            stack, application)
        self.register(
            "GET", "/projects/project-1/environments", body=[])
        self.register("DELETE", "/projects/project-1", body={
            "message": "Project deleted."})
        self.driver.delete_project(self.ep, stack, application)
        self.register(
            "GET", "/projects/project-1", body={"message": "no"},
            status=404)
        self.driver.delete_project(self.ep, stack, application)

    def test_ensure_project_adopts_exact_name(self):
        application = SimpleNamespace(id="app-1", engine_ref=None)
        stack = SimpleNamespace(id="stack")
        self.register("GET", "/projects", body=[
            {"uuid": "other", "name": "website"},
            {"uuid": "kept", "name": "coast-app-1"},
        ])
        result = self.driver.ensure_project(self.ep, stack, application)
        self.assertEqual("kept", result.engine_ref)
        self.assertFalse(any(
            item.method == "POST" for item in self.http.request_history))
        self.register("GET", "/projects", body=[
            {"uuid": "one", "name": "coast-app-1"},
            {"uuid": "two", "name": "coast-app-1"},
        ])
        self.assertRaises(
            engine.EngineConflict, self.driver.ensure_project, self.ep,
            stack, application)

    def test_ensure_and_delete_environment(self):
        stack = SimpleNamespace(id="stack")
        application = SimpleNamespace(id="app-1", engine_ref="project-1")
        environment = SimpleNamespace(id="env-1", engine_ref=None)
        bare = SimpleNamespace(id="app-1", engine_ref=None)
        self.assertRaises(
            engine.DriverError, self.driver.ensure_environment, self.ep,
            stack, bare, environment)
        self.register(
            "GET", "/projects/project-1/environments", body=[])
        self.register(
            "POST", "/projects/project-1/environments",
            body={"uuid": "env-uuid"}, status=201)
        result = self.driver.ensure_environment(
            self.ep, stack, application, environment)
        self.assertEqual("env-uuid", result.engine_ref)
        post = next(
            item for item in self.http.request_history
            if item.method == "POST")
        self.assertEqual({"name": "coast-env-1"}, post.json())
        environment.engine_ref = result.engine_ref
        self.register("GET", "/projects/project-1/environments", body=[
            {"uuid": "env-uuid", "name": "coast-env-1"}])
        self.assertEqual(result, self.driver.ensure_environment(
            self.ep, stack, application, environment))
        self.assertEqual(1, sum(
            item.method == "POST" for item in self.http.request_history))
        named = SimpleNamespace(id="env-1", engine_ref=None)
        adopted = self.driver.ensure_environment(
            self.ep, stack, application, named)
        self.assertEqual("env-uuid", adopted.engine_ref)
        gone = SimpleNamespace(id="env-1", engine_ref="missing-env")
        self.assertRaises(
            engine.EngineNotFound, self.driver.ensure_environment,
            self.ep, stack, application, gone)
        self.register(
            "DELETE", "/projects/project-1/environments/env-uuid",
            body={"message": "Environment deleted."})
        self.driver.delete_environment(
            self.ep, stack, application, environment)
        self.register(
            "DELETE", "/projects/project-1/environments/env-uuid",
            body={"message": "no"}, status=404)
        self.driver.delete_environment(
            self.ep, stack, application, environment)
        self.register(
            "DELETE", "/projects/project-1/environments/env-uuid",
            body={"message": "busy"}, status=400)
        self.assertRaises(
            engine.DriverError, self.driver.delete_environment, self.ep,
            stack, application, environment)

    def test_delete_stack_project_and_key_cleanup(self):
        self.register("GET", "/projects/project-uuid/environments", body=[])
        self.register("DELETE", "/projects/project-uuid", "deleted")
        self.register("GET", "/security/keys", "keys")
        self.register("DELETE", "/security/keys/key-uuid", "deleted")
        self.driver.delete_stack_project(self.ep, self.env)
        self.register("DELETE", "/projects/project-uuid", "error", status=404)
        self.driver.delete_stack_project(self.ep, self.env)
        self.env.engine_ref = None
        self.register("GET", "/projects", body=[])
        self.driver.delete_stack_project(self.ep, self.env)

    def _delete_project_case(self, stack_project, rows, status=200):
        project_path = "/projects/project-uuid"
        self.register("GET", project_path, body={"uuid": "project-uuid"})
        self.register("GET", project_path + "/environments", body=rows)
        for row in rows:
            if isinstance(row, dict) and row.get("uuid"):
                self.register(
                    "DELETE", project_path + "/environments/" + row["uuid"],
                    "deleted" if status == 200 else "error", status=status)
        self.register("DELETE", project_path, "deleted")
        self.register("GET", "/security/keys", "keys")
        self.register("DELETE", "/security/keys/key-uuid", "deleted")
        if stack_project:
            return lambda: self.driver.delete_stack_project(self.ep, self.env)
        return lambda: self.driver.delete_project(
            self.ep, self.env, self.env)

    def test_project_delete_removes_empty_production_first(self):
        for stack_project in (False, True):
            with self.subTest(stack_project=stack_project):
                self.http.reset_mock()
                delete = self._delete_project_case(stack_project, [
                    {"name": "production", "uuid": "default-1"},
                    {"name": "production", "uuid": "default-2"}])
                delete()
                deleted = [r.path for r in self.http.request_history
                           if r.method == "DELETE"]
                expected = [
                    "/api/v1/projects/project-uuid/environments/default-1",
                    "/api/v1/projects/project-uuid/environments/default-2",
                    "/api/v1/projects/project-uuid"]
                if stack_project:
                    expected.append("/api/v1/security/keys/key-uuid")
                self.assertEqual(expected, deleted)

    def test_project_delete_keeps_production_with_resources(self):
        # Coolify returns 400 when applications, services or databases remain.
        for stack_project in (False, True):
            with self.subTest(stack_project=stack_project):
                self.http.reset_mock()
                delete = self._delete_project_case(stack_project, [
                    {"name": "production", "uuid": "default-1"}], status=400)
                self.assertRaises(engine.DriverError, delete)
                self.assertEqual([
                    "/api/v1/projects/project-uuid/environments/default-1"],
                    [r.path for r in self.http.request_history
                     if r.method == "DELETE"])

    def test_project_delete_rejects_other_environment_before_cleanup(self):
        for stack_project in (False, True):
            with self.subTest(stack_project=stack_project):
                self.http.reset_mock()
                delete = self._delete_project_case(stack_project, [
                    {"name": "production", "uuid": "default-1"},
                    {"name": "coast-env", "uuid": "env-1"}])
                self.assertRaises(engine.DriverError, delete)
                self.assertFalse(any(r.method == "DELETE"
                                     for r in self.http.request_history))

    def test_project_delete_absent_project_is_idempotent(self):
        for stack_project in (False, True):
            with self.subTest(stack_project=stack_project):
                self.http.reset_mock()
                delete = self._delete_project_case(stack_project, [])
                self.register("GET", "/projects/project-uuid/environments",
                              "error", status=404)
                delete()
                self.assertFalse(any(
                    r.method == "DELETE" and "/projects/" in r.path
                    for r in self.http.request_history))

    def test_register_worker_build_and_resume(self):
        self.register("GET", "/servers", body=[])
        self.register("GET", "/security/keys", body=[])
        self.register("POST", "/security/keys", "key_created", status=201)
        self.register("POST", "/servers", "server_created", status=201)
        self.register(
            "POST", "/servers/server-uuid/validate", "validation", status=201
        )
        self.register("GET", "/servers/server-uuid", "server")
        self._allow_terminal_off()
        self.register("PATCH", "/servers/server-uuid/proxy", "proxy")
        self.register("POST", "/servers/server-uuid/proxy/restart",
                      body={"message": "Proxy restart queued."})
        self.server.engine_ref = None
        result = self.driver.register_server(
            self.ep, self.env, self.server, "private-key"
        )
        self.assertEqual("server-uuid", result.engine_ref)
        payload = next(
            r.json()
            for r in self.http.request_history
            if r.method == "POST" and r.path_url == "/api/v1/servers"
        )
        self.assertEqual(self.server.hostname, payload["name"])
        self.assertEqual("both", payload["server_role"])
        self.assertEqual("traefik", payload["proxy_type"])
        self.assertNotIn("is_terminal_enabled", payload)
        self.register("GET", "/servers", "servers")
        self.assertEqual(
            result,
            self.driver.register_server(
                self.ep, self.env, self.server, "private-key"
            ),
        )
        self.register("GET", "/servers", body=[])
        self.register("GET", "/security/keys", "keys")
        self.server.role = "build"
        self.driver.register_server(
            self.ep, self.env, self.server, "private-key"
        )
        payload = [
            r.json()
            for r in self.http.request_history
            if r.method == "POST" and r.path_url == "/api/v1/servers"
        ][-1]
        self.assertEqual("build", payload["server_role"])
        self.assertEqual("none", payload["proxy_type"])
        self.server.role = "control"
        count = len(self.http.request_history)
        self.assertRaises(
            engine.DriverError, self.driver.register_server,
            self.ep, self.env, self.server,
            "private-key",
        )
        self.assertEqual(count, len(self.http.request_history))

    def test_register_worker_with_usable_build_server(self):
        for usable, expected in ((True, "deployment"), (False, "both")):
            with self.subTest(usable=usable):
                self.register("GET", "/servers", body=[{
                    "uuid": "build-uuid", "name": "coast-build",
                    "settings": {"server_role": "build",
                                 "is_usable": usable}}])
                self.register("GET", "/security/keys", "keys")
                self.register("POST", "/servers", "server_created", status=201)
                self.register("POST", "/servers/server-uuid/validate",
                              "validation", status=201)
                self.register("GET", "/servers/server-uuid", "server")
                self._allow_terminal_off()
                self.register("PATCH", "/servers/server-uuid/proxy", "proxy")
                self.register("POST", "/servers/server-uuid/proxy/restart",
                              body={"message": "Proxy restart queued."})
                self.server.engine_ref = None
                self.driver.register_server(
                    self.ep, self.env, self.server, "private-key")
                payload = [r.json() for r in self.http.request_history
                           if r.method == "POST"
                           and r.url == self.ep.base_url + "/servers"][-1]
                self.assertEqual(expected, payload["server_role"])

    def test_register_waits_for_server_validation(self):
        self.register("POST", "/servers/server-uuid/validate", "validation")
        pending = copy.deepcopy(self.fixtures["server"])
        pending["settings"]["is_usable"] = False
        unreachable = copy.deepcopy(self.fixtures["server"])
        unreachable["settings"]["is_reachable"] = False
        self.http.register_uri(
            "GET", self.ep.base_url + "/servers/server-uuid",
            [{"json": pending}, {"json": unreachable},
             {"json": self.fixtures["server"]}])
        self._allow_terminal_off()
        self.register("PATCH", "/servers/server-uuid/proxy", "proxy")
        self.register("POST", "/servers/server-uuid/proxy/restart",
                      body={"message": "Proxy restart queued."})
        with mock.patch.object(v4.time, "sleep") as sleep:
            result = self.driver.register_server(
                self.ep, self.env, self.server, "private-key")
        self.assertEqual("server-uuid", result.engine_ref)
        self.assertEqual(2, sleep.call_count)
        self.assertEqual(
            ["POST", "GET", "GET", "GET", "PATCH", "PATCH", "POST"],
            [request.method for request in self.http.request_history])

    def test_register_validation_timeout_does_not_restart_proxy(self):
        self.config.config(build_timeout=3, build_poll_interval=2,
                           group="controller_worker")
        for role in ("worker", "build"):
            with self.subTest(role=role):
                self.http.reset_mock()
                self.server.role = role
                self.register("POST", "/servers/server-uuid/validate",
                              "validation")
                pending = copy.deepcopy(self.fixtures["server"])
                pending["settings"]["is_usable"] = False
                self.register("GET", "/servers/server-uuid", body=pending)
                now = [0]

                def sleep(seconds):
                    now[0] += seconds

                with mock.patch.object(v4.time, "monotonic",
                                       side_effect=lambda: now[0]), \
                        mock.patch.object(v4.time, "sleep",
                                          side_effect=sleep) as wait:
                    self.assertRaisesRegex(
                        engine.DriverError, "validation timed out",
                        self.driver.register_server,
                        self.ep, self.env, self.server, "private-key")
                self.assertEqual([mock.call(2), mock.call(1)],
                                 wait.call_args_list)
                self.assertEqual(["POST", "GET", "GET"],
                                 [r.method for r in self.http.request_history])

    def _registry_endpoint(self, **fields):
        values = dict(
            registry_host="192.0.2.10:5000",
            registry_username="coast",
            registry_password="registry-secret-value",
            registry_image_template=(
                "192.0.2.10:5000/{environment_id}/{app_id}"))
        values.update(fields)
        return dataclasses.replace(self.ep, **values)

    def _prepare_registry_login(self):
        self.register("POST", "/servers/server-uuid/validate", "validation")
        self.register("GET", "/servers/server-uuid", "server")
        self._allow_terminal_off()
        self.register("PATCH", "/servers/server-uuid/proxy", "proxy")
        self.register("POST", "/servers/server-uuid/proxy/restart",
                      body={"message": "Proxy restart queued."})

    def _mock_registry_login(self, logged_in=True):
        self.register("POST", "/servers/server-uuid/registries",
                      body={"message": "logged in"})
        self.register("GET", "/servers/server-uuid/registries", body={
            "registries": [{
                "registry": "192.0.2.10:5000",
                "logged_in": logged_in,
                "source": "server",
                "username": "coast",
                "used_by": []}],
            "error": None})

    def _registry_posts(self):
        return [request for request in self.http.request_history
                if request.method == "POST" and
                request.path.endswith("/registries")]

    def test_register_logs_into_the_stack_registry(self):
        password = "registry-secret-value"
        self._prepare_registry_login()
        self.register("GET", "/servers", body=[{
            "uuid": "build-uuid",
            "settings": {"server_role": "build", "is_usable": True}}])
        self._mock_registry_login()
        logs = self.useFixture(fixtures.FakeLogger())
        endpoint = self._registry_endpoint()
        result = self.driver.register_server(
            endpoint, self.env, self.server, "private-key")
        self.assertEqual("server-uuid", result.engine_ref)
        posted = self._registry_posts()
        self.assertEqual(1, len(posted))
        self.assertEqual({
            "registry": "192.0.2.10:5000",
            "username": "coast",
            "password": password}, posted[0].json())
        self.assertNotIn(password, logs.output)
        self.assertNotIn(password, repr(endpoint))
        self.assertEqual(
            ["POST", "GET", "PATCH", "PATCH", "POST", "GET", "POST", "GET"],
            [request.method for request in self.http.request_history])

    def test_worker_skips_registry_login_without_a_build_server(self):
        self._prepare_registry_login()
        self.register("GET", "/servers", body=[{
            "uuid": "host-uuid",
            "is_coolify_host": True,
            "settings": {"server_role": "both", "is_usable": True}}])
        result = self.driver.register_server(
            self._registry_endpoint(), self.env, self.server, "private-key")
        self.assertEqual("server-uuid", result.engine_ref)
        self.assertEqual([], self._registry_posts())

    def test_worker_logs_in_when_build_server_is_not_usable(self):
        self._prepare_registry_login()
        self.register("GET", "/servers", body=[{
            "uuid": "build-uuid",
            "settings": {"server_role": "build", "is_usable": False}}])
        self._mock_registry_login()
        self.driver.register_server(
            self._registry_endpoint(), self.env, self.server, "private-key")
        self.assertEqual(1, len(self._registry_posts()))

    def test_build_server_logs_in_with_no_peer(self):
        self.server.role = "build"
        self.register("POST", "/servers/server-uuid/validate", "validation")
        self.register("GET", "/servers/server-uuid", "server")
        self._allow_terminal_off()
        self._mock_registry_login()
        result = self.driver.register_server(
            self._registry_endpoint(), self.env, self.server, "private-key")
        self.assertEqual("server-uuid", result.engine_ref)
        self.assertEqual(1, len(self._registry_posts()))
        self.assertFalse(any(
            request.method == "GET" and request.path.endswith("/servers")
            for request in self.http.request_history))

    def test_registry_login_rejects_an_invalid_server_list(self):
        self._prepare_registry_login()
        self.register("GET", "/servers", body={"servers": []})
        self.assertRaises(
            engine.DriverError, self.driver.register_server,
            self._registry_endpoint(), self.env, self.server, "private-key")
        self.assertEqual([], self._registry_posts())

    def test_registry_login_confirmation_hides_the_password(self):
        self._prepare_registry_login()
        self.register("GET", "/servers", body=[{
            "uuid": "build-uuid",
            "settings": {"server_role": "build", "is_usable": True}}])
        self._mock_registry_login(logged_in=False)
        error = self.assertRaises(
            engine.DriverError, self.driver.register_server,
            self._registry_endpoint(), self.env, self.server, "private-key")
        self.assertNotIn("registry-secret-value", str(error))

    def test_incomplete_registry_login_does_not_post(self):
        self._prepare_registry_login()
        endpoint = self._registry_endpoint(
            registry_username=None, registry_password=None,
            registry_image_template=None)
        self.assertRaises(
            engine.DriverError, self.driver.register_server,
            endpoint, self.env, self.server, "private-key")
        self.assertFalse(any("/registries" in request.path
                             for request in self.http.request_history))

    def test_build_mode_logs_workers_in_before_patches(self):
        self.build_mode_fixture()
        self._mock_registry_login()
        endpoint = self._registry_endpoint()
        self.driver.set_build_server_mode(
            endpoint, self.env, [self.server], [self.service], True)
        posted = self._registry_posts()
        self.assertEqual(1, len(posted))
        self.assertNotIn("registry-secret-value", repr(self.mode_patches))
        methods = [request.method for request in self.http.request_history]
        self.assertLess(methods.index("POST"), methods.index("PATCH"))
        self.build_mode_fixture()
        self.driver.set_build_server_mode(
            endpoint, self.env, [self.server], [self.service], False)
        self.assertEqual(1, len(self._registry_posts()))

    def test_build_mode_login_failure_does_not_patch(self):
        self.build_mode_fixture()
        self.register("POST", "/servers/server-uuid/registries",
                      body={"message": "Login failed."}, status=400)
        error = self.assertRaises(
            engine.DriverError, self.driver.set_build_server_mode,
            self._registry_endpoint(), self.env, [self.server],
            [self.service], True)
        self.assertNotIn("registry-secret-value", str(error))
        self.assertEqual([], self.mode_patches)
        self.assertEqual("both", self.mode_rows["/servers/server-uuid"][
            "settings"]["server_role"])

    def test_endpoint_registry_template_overrides_config(self):
        self.build_mode_fixture()
        self._mock_registry_login()
        endpoint = self._registry_endpoint()
        self.driver.set_build_server_mode(
            endpoint, self.env, [self.server], [self.service], True)
        self.assertEqual(
            "192.0.2.10:5000/env/app",
            self.mode_rows["/applications/app-uuid"][
                "docker_registry_image_name"])
        self.build_mode_fixture()
        self.driver.set_build_server_mode(
            self.ep, self.env, [self.server], [self.service], True)
        self.assertEqual(
            "registry.example.com/env/app",
            self.mode_rows["/applications/app-uuid"][
                "docker_registry_image_name"])

    def test_server_status_and_idempotent_unregister(self):
        self.register("GET", "/servers/server-uuid", "server")
        self.assertEqual(
            states.ACTIVE,
            self.driver.get_server_status(
                self.ep, self.env, self.server
            ).status,
        )
        self.register("GET", "/servers/server-uuid", body={"settings": {}})
        self.assertEqual(
            states.UNHEALTHY,
            self.driver.get_server_status(
                self.ep, self.env, self.server
            ).status,
        )
        self.register("DELETE", "/servers/server-uuid", "deleted")
        self.driver.unregister_server(self.ep, self.env, self.server)
        self.register("DELETE", "/servers/server-uuid", "error", status=404)
        self.driver.unregister_server(self.ep, self.env, self.server)
        self.server.engine_ref = None
        self.register("GET", "/servers", "servers")
        self.driver.unregister_server(self.ep, self.env, self.server)
        self.register("GET", "/servers", body=[])
        self.driver.unregister_server(self.ep, self.env, self.server)

    def creation(self):
        self.service.engine_ref = None
        self.register("GET", "/applications", body=[])
        self.register("GET", "/servers", "servers")

    def test_create_public_git_recovery_and_settings(self):
        self.creation()
        self.register(
            "POST", "/applications/public", "app_created", status=201
        )
        result = self.driver.create_app(
            self.ep, self.env, self.service, self.source, None
        )
        self.assertEqual("app-uuid", result.engine_ref)
        payload = self.http.last_request.json()
        self.assertEqual({
            "git_repository": "https://git.example.com/service.git",
            "git_branch": "main",
            "git_commit_sha": "HEAD",
            "build_pack": "nixpacks",
            "base_directory": "/",
            "name": "coast-app",
            "description": "Test service",
            "ports_exposes": "8080",
            "limits_cpus": "0.5",
            "limits_memory": "128m",
            "health_check_enabled": True,
            "health_check_port": "8080",
            "health_check_path": "/healthz",
            "is_auto_deploy_enabled": False,
            "project_uuid": "application-project",
            "environment_uuid": "stage-uuid",
            "server_uuid": "server-uuid",
            "instant_deploy": False,
            "autogenerate_domain": False,
            "use_build_server": False,
        }, payload)
        self.assertNotEqual("project-uuid", payload["project_uuid"])
        self.assertNotIn("environment_name", payload)
        self.register("GET", "/applications", "apps")
        self.assertEqual(
            result,
            self.driver.create_app(
                self.ep, self.env, self.service, self.source, None
            ),
        )
        self.service.engine_ref = "app-uuid"
        self.source.build_pack = "compose"
        self.source.compose_path = "/compose.yaml"
        self.register("PATCH", "/applications/app-uuid", "app")
        self.driver.update_app(self.ep, self.env, self.service, self.source)
        self.assertEqual(
            "dockercompose", self.http.last_request.json()["build_pack"]
        )

    def test_create_places_adopted_server_by_recorded_uuid(self):
        self.creation()
        adopted = self.fixtures["adopted_server"]
        self.service.server_engine_ref = adopted["uuid"]
        self.register("GET", "/servers/" + adopted["uuid"], body=adopted)
        self.register(
            "POST", "/applications/public", "app_created", status=201
        )
        result = self.driver.create_app(
            self.ep, self.env, self.service, self.source, None
        )
        self.assertEqual("app-uuid", result.engine_ref)
        payload = self.http.last_request.json()
        self.assertEqual(adopted["uuid"], payload["server_uuid"])
        self.assertNotEqual("server-uuid", payload["server_uuid"])
        direct = "/servers/" + adopted["uuid"]
        self.assertTrue(any(
            request.method == "GET" and request.path_url.endswith(direct)
            for request in self.http.request_history))

    def test_create_places_registered_server_by_name(self):
        self.creation()
        self.service.server_engine_ref = None
        self.register(
            "POST", "/applications/public", "app_created", status=201
        )
        self.driver.create_app(
            self.ep, self.env, self.service, self.source, None
        )
        self.assertEqual(
            "server-uuid", self.http.last_request.json()["server_uuid"]
        )
        self.assertFalse(any(
            request.method == "GET" and "/servers/" in request.path_url
            for request in self.http.request_history))

    def test_missing_recorded_server_does_not_use_name(self):
        self.creation()
        self.service.server_engine_ref = "missing-server-uuid"
        self.register(
            "GET", "/servers/missing-server-uuid", "error", status=404
        )
        error = self.assertRaises(
            engine.EngineNotFound, self.driver.create_app,
            self.ep, self.env, self.service, self.source, None,
        )
        self.assertIn("not found", str(error))
        self.assertFalse(
            any(r.method == "POST" for r in self.http.request_history)
        )

    def test_create_fails_closed_without_name_or_recorded_uuid(self):
        self.service.engine_ref = None
        self.service.server_engine_ref = None
        self.register("GET", "/applications", body=[])
        self.register("GET", "/servers", body=[])
        error = self.assertRaises(
            engine.EngineNotFound, self.driver.create_app,
            self.ep, self.env, self.service, self.source, None,
        )
        self.assertIn("not found", str(error))
        self.assertFalse(
            any(r.method == "POST" for r in self.http.request_history)
        )

    def test_recorded_build_server_is_not_a_placement_target(self):
        adopted = copy.deepcopy(self.fixtures["adopted_server"])
        adopted["settings"]["server_role"] = "build"
        self.service.engine_ref = None
        self.service.server_engine_ref = adopted["uuid"]
        self.register("GET", "/applications", body=[])
        self.register("GET", "/servers/" + adopted["uuid"], body=adopted)
        error = self.assertRaises(
            engine.EngineNotFound, self.driver.create_app,
            self.ep, self.env, self.service, self.source, None,
        )
        self.assertIn("not found", str(error))
        self.assertFalse(
            any(r.method == "POST" for r in self.http.request_history)
        )

    def test_missing_parent_refs_fail_before_http(self):
        self.service.engine_ref = None
        image = SimpleNamespace(
            type="image", image="nginx:latest", credentials_ref=None)
        for source in (self.source, image):
            for name in ("project_engine_ref", "environment_engine_ref"):
                service = SimpleNamespace(**vars(self.service))
                delattr(service, name)
                self.assertRaises(
                    engine.DriverError, self.driver.create_app,
                    self.ep, self.env, service, source, None)
                service = SimpleNamespace(**vars(self.service))
                setattr(service, name, None)
                self.assertRaises(
                    engine.DriverError, self.driver.create_app,
                    self.ep, self.env, service, source, None)
        self.assertEqual([], self.http.request_history)

    def test_patch_preserves_engine_normalized_repository(self):
        self.source.repo_url = "https://github.com/example/service.git"
        self.register("PATCH", "/applications/app-uuid", "app")
        self.driver.update_app(self.ep, self.env, self.service, self.source)
        self.assertNotIn("git_repository", self.http.last_request.json())
        self.deployment.spec["source"]["repo_url"] = self.source.repo_url
        self.assertNotIn("git_repository", v4.frozen_settings(
            self.service, self.deployment))
        self.assertEqual(self.source.repo_url,
                         v4.app_settings(self.service, self.source)[
                             "git_repository"])

    def key_rotation_fixture(self):
        return json.loads((FIXTURES / 'deploy-key-rotation.json').read_text())

    def test_rotate_deploy_key_retains_uuid(self):
        fixture = self.key_rotation_fixture()
        self.register('GET', '/security/keys', body=fixture['keys'])
        self.register('PATCH', '/security/keys/key-uuid',
                      body=fixture['updated'])
        old, new = self.driver.prepare_deploy_key(
            self.ep, self.env, self.service, 'new-private-key', 'secret-ref')
        self.assertEqual(('key-uuid', 'key-uuid'), (old, new))
        self.driver.write_deploy_key(
            self.ep, self.env, self.service, new, 'new-private-key')
        self.assertEqual({'private_key': 'new-private-key'},
                         self.http.last_request.json())
        self.assertEqual(['GET', 'PATCH'],
                         [r.method for r in self.http.request_history])
        self.assertTrue(self.driver.SUPPORTS_DEPLOY_KEY_ROTATION)

    def test_failed_rotation_can_restore_old_material_without_rebinding(self):
        fixture = self.key_rotation_fixture()
        self.register('GET', '/security/keys', body=fixture['keys'])
        old, new = self.driver.prepare_deploy_key(
            self.ep, self.env, self.service, 'new-private-key', 'secret-ref')
        self.register('PATCH', '/security/keys/key-uuid',
                      body=fixture['rejected'], status=422)
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger(client.__name__)
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, previous_level)
        error = self.assertRaises(
            engine.DriverError, self.driver.write_deploy_key,
            self.ep, self.env, self.service, new, 'new-private-key')
        self.assertNotIn('pasted-private-data', output.getvalue())
        self.assertNotIn('new-private-key', output.getvalue())
        self.assertNotIn('pasted-private-data', str(error))
        self.assertNotIn('new-private-key', str(error))
        self.register('PATCH', '/security/keys/key-uuid',
                      body=fixture['updated'])
        self.driver.write_deploy_key(
            self.ep, self.env, self.service, old, 'old-private-key')
        self.assertEqual({'private_key': 'old-private-key'},
                         self.http.last_request.json())
        self.assertFalse(any(r.method in ('POST', 'DELETE')
                             for r in self.http.request_history))

    def test_delete_app_removes_all_keys_and_retries_cleanup(self):
        fixture = self.key_rotation_fixture()
        self.register('DELETE', '/applications/app-uuid', 'deleted')
        self.http.get(self.ep.base_url + '/security/keys', [
            {'json': fixture['multiple_keys']},
            {'json': fixture['keys']}])
        self.register('DELETE', '/security/keys/old-key', 'deleted')
        self.register('DELETE', '/security/keys/key-uuid',
                      body=fixture['rejected'], status=422)
        self.assertRaises(engine.DriverError, self.driver.delete_app,
                          self.ep, self.env, self.service)
        self.register('DELETE', '/applications/app-uuid', 'deleted',
                      status=404)
        self.register('DELETE', '/security/keys/key-uuid', 'deleted')
        self.driver.delete_app(self.ep, self.env, self.service)
        removed = [r.path_url for r in self.http.request_history
                   if r.method == 'DELETE']
        self.assertIn('/api/v1/security/keys/old-key', removed)
        self.assertEqual(2, removed.count('/api/v1/security/keys/key-uuid'))
        self.assertNotIn('/api/v1/security/keys/other-key', removed)

    def test_create_private_git_and_image(self):
        self.creation()
        self.source.credentials_ref = "opaque-secret-ref"
        self.register("GET", "/security/keys", body=[])
        self.register("POST", "/security/keys", "key_created", status=201)
        self.register(
            "POST", "/applications/private-deploy-key", "app_created",
            status=201,
        )
        self.driver.create_app(
            self.ep, self.env, self.service, self.source, "private-key"
        )
        payload = self.http.last_request.json()
        self.assertEqual("key-uuid", payload["private_key_uuid"])
        self.assertEqual("application-project", payload["project_uuid"])
        self.assertEqual("stage-uuid", payload["environment_uuid"])
        self.assertNotIn("environment_name", payload)
        self.source.type = "image"
        self.source.image = "registry.example.com:5000/service:v1"
        self.source.credentials_ref = None
        self.register(
            "POST", "/applications/dockerimage", "app_created", status=201
        )
        self.driver.create_app(
            self.ep, self.env, self.service, self.source, None)
        self.assertEqual({
            "docker_registry_image_name": "registry.example.com:5000/service",
            "docker_registry_image_tag": "v1",
            "name": "coast-app",
            "description": "Test service",
            "ports_exposes": "8080",
            "limits_cpus": "0.5",
            "limits_memory": "128m",
            "health_check_enabled": True,
            "health_check_port": "8080",
            "health_check_path": "/healthz",
            "project_uuid": "application-project",
            "environment_uuid": "stage-uuid",
            "server_uuid": "server-uuid",
            "instant_deploy": False,
            "autogenerate_domain": False,
            "use_build_server": False,
        }, self.http.last_request.json())

    def test_compose_never_uses_dedicated_builder(self):
        self.creation()
        self.source.build_pack = 'compose'
        self.source.compose_path = '/docker-compose.yaml'
        self.register('GET', '/servers', body=self.fixtures['servers'] + [{
            'uuid': 'builder', 'settings': {'server_role': 'build'}}])
        self.register('POST', '/applications/public', 'app_created',
                      status=201)
        self.driver.create_app(
            self.ep, self.env, self.service, self.source, None)
        payload = self.http.last_request.json()
        self.assertEqual('dockercompose', payload['build_pack'])
        self.assertFalse(payload['use_build_server'])
        self.assertNotIn('docker_registry_image_name', payload)
        self.service.engine_ref = 'app-uuid'
        # Update and frozen deploy must also repair pre-existing bad settings.
        self.register('PATCH', '/applications/app-uuid', body={})
        self.driver.update_app(self.ep, self.env, self.service, self.source)
        self.assertFalse(self.http.last_request.json()['use_build_server'])
        self.deployment.engine_ref = None
        self.deployment.spec['source'] = vars(self.source).copy()
        self.register('POST', '/deploy', body={'deployments': [{
            'resource_uuid': 'app-uuid', 'deployment_uuid': 'deploy'}]})
        self.driver.deploy_app(
            self.ep, self.env, self.service, self.deployment)
        patches = [r.json() for r in self.http.request_history
                   if r.method == 'PATCH']
        self.assertTrue(patches)
        self.assertTrue(all(p['use_build_server'] is False for p in patches))

    def test_build_mode_keeps_compose_on_worker(self):
        self.build_mode_fixture()
        row = self.mode_rows['/applications/app-uuid']
        row['build_pack'] = 'dockercompose'
        row['settings']['is_build_server_enabled'] = True
        self.config.config(build_registry_image_template='', group='coolify')
        for enabled in (True, True, False):
            self.switch_build_mode(enabled)
            self.assertFalse(row['settings']['is_build_server_enabled'])
            self.assertFalse(row.get('docker_registry_image_name'))

    def test_cleared_env_preserves_generated_buildpack_controls(self):
        path = self.ep.base_url + '/applications/app-uuid'
        controls = [dict(key='NIXPACKS_NODE_VERSION', value='22',
                         is_buildpack_control=True, is_preview=preview,
                         uuid='control-' + str(preview))
                    for preview in (False, True)]
        rows = controls + [{'key': 'CUSTOMER', 'uuid': 'customer',
                            'comment': 'coast-managed'}]
        self.http.get(path, json={'fqdn': None})
        self.http.get(path + '/envs', json=rows)
        self.http.delete(path + '/envs/customer', json={})
        self.driver.set_env(self.ep, self.env, self.service, {})
        deletes = [r.path_url for r in self.http.request_history
                   if r.method == 'DELETE']
        self.assertEqual(['/api/v1/applications/app-uuid/envs/customer'],
                         deletes)
        self.http.get(path + '/envs', json=controls)
        self.assertEqual({'env': False, 'domains': False},
                         self.driver.compare_settings(
                             self.ep, self.env, self.service, {}, []))

    def test_env_comparison_ignores_duplicate_engine_defaults(self):
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path, json={'fqdn': None})
        rows = [{'key': 'DEFAULT', 'is_preview': preview}
                for preview in (False, True)]
        self.http.get(path + '/envs', json=rows)
        self.assertIs(False, self.driver.compare_settings(
            self.ep, self.env, self.service, {}, [])['env'])
        # An engine default cannot satisfy customer intent of the same name.
        self.assertIs(True, self.driver.compare_settings(
            self.ep, self.env, self.service,
            {'DEFAULT': engine.EnvVar('customer')}, [])['env'])
        # Duplicate managed observations still fail closed.
        rows = [dict(row, comment='coast-managed') for row in rows]
        self.http.get(path + '/envs', json=rows)
        self.assertIs(True, self.driver.compare_settings(
            self.ep, self.env, self.service, {}, [])['env'])

    def test_dedicated_build_registry(self):
        self.creation()
        servers = self.fixtures["servers"] + [
            {
                "uuid": "build-uuid",
                "name": "coast-build",
                "settings": {"server_role": "build"},
            }
        ]
        self.register("GET", "/servers", body=servers)
        self.assertRaises(
            engine.DriverError, self.driver.create_app,
            self.ep, self.env, self.service,
            self.source, None,
        )
        self.assertFalse(
            any(r.method == "POST" for r in self.http.request_history)
        )
        self.config.config(
            build_registry_image_template=(
                "registry.example.com/{environment_id}/{app_id}"
            ),
            group="coolify",
        )
        self.register(
            "POST", "/applications/public", "app_created", status=201
        )
        self.driver.create_app(
            self.ep, self.env, self.service, self.source, None)
        payload = self.http.last_request.json()
        self.assertTrue(payload["use_build_server"])
        self.assertEqual(
            "registry.example.com/env/app",
            payload["docker_registry_image_name"],
        )

    def test_inline_dockerfile_build_placement(self):
        for has_builder in (False, True):
            with self.subTest(has_builder=has_builder):
                self.creation()
                source = SimpleNamespace(
                    type='dockerfile', credentials_ref=None,
                    dockerfile='FROM busybox')
                self.register('GET', '/servers', body=(
                    self.fixtures['servers'] + ([{
                        'uuid': 'builder',
                        'settings': {'server_role': 'build'}}]
                        if has_builder else [])))
                self.register('POST', '/applications/dockerfile',
                              'app_created', status=201)
                self.driver.create_app(
                    self._registry_endpoint(), self.env, self.service,
                    source, None)
                payload = self.http.last_request.json()
                self.assertEqual(has_builder, payload['use_build_server'])
                if has_builder:
                    self.assertEqual('192.0.2.10:5000/env/app',
                                     payload['docker_registry_image_name'])
                else:
                    self.assertNotIn('docker_registry_image_name', payload)

    def test_inline_dockerfile_rejects_invalid_build_template(self):
        self.creation()
        self.register('GET', '/servers', body=self.fixtures['servers'] + [{
            'uuid': 'builder', 'settings': {'server_role': 'build'}}])
        source = SimpleNamespace(type='dockerfile', credentials_ref=None,
                                 dockerfile='FROM busybox')
        for template, message in (
                ('', 'dedicated builds require'),
                ('registry.example.com/{app_id}', 'dedicated builds require'),
                ('{environment_id}/{app_id}/{unknown}',
                 'registry image template is invalid')):
            with self.subTest(template=template):
                endpoint = self._registry_endpoint(
                    registry_image_template=template)
                self.assertRaisesRegex(
                    engine.DriverError, message, self.driver.create_app,
                    endpoint, self.env, self.service, source, None)
        self.assertFalse(any(r.method == 'POST'
                             for r in self.http.request_history))

    def test_image_keeps_prebuilt_image_with_build_server(self):
        self.creation()
        self.source.type = 'image'
        self.source.image = 'registry.example.com/service:v1'
        self.register('GET', '/servers', body=self.fixtures['servers'] + [{
            'uuid': 'builder', 'settings': {'server_role': 'build'}}])
        self.register('POST', '/applications/dockerimage', 'app_created',
                      status=201)
        self.driver.create_app(self._registry_endpoint(), self.env,
                               self.service, self.source, None)
        payload = self.http.last_request.json()
        self.assertFalse(payload['use_build_server'])
        self.assertEqual('registry.example.com/service',
                         payload['docker_registry_image_name'])
        self.assertEqual('v1', payload['docker_registry_image_tag'])

    def test_inline_dockerfile_build_mode_switch(self):
        self.build_mode_fixture()
        row = self.mode_rows['/applications/app-uuid']
        row['build_pack'] = 'dockerfile'
        row['dockerfile'] = 'FROM busybox'
        self.switch_build_mode(True)
        self.assert_build_mode(True)
        self.assertEqual('registry.example.com/env/app',
                         row['docker_registry_image_name'])
        self.switch_build_mode(False)
        self.assert_build_mode(False)
        self.assertEqual('registry.example.com/env/app',
                         row['docker_registry_image_name'])

    def test_delete_app_preserves_volume_data_and_removes_key(self):
        self.register("DELETE", "/applications/app-uuid", "deleted")
        self.register(
            "GET", "/security/keys",
            body=[{"uuid": "key-uuid", "name": "coast-app"}],
        )
        self.register("DELETE", "/security/keys/key-uuid", "deleted")
        self.driver.delete_app(self.ep, self.env, self.service)
        request = self.http.request_history[0]
        self.assertEqual(
            ["false"],
            parse.parse_qs(parse.urlsplit(request.url).query)[
                "delete_volumes"
            ],
        )
        self.service.engine_ref = None
        self.register("GET", "/applications", body=[])
        self.driver.delete_app(self.ep, self.env, self.service)

    def test_application_env_drift_is_repaired(self):
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path, json={'fqdn': None})
        self.http.get(path + '/envs', json=[])
        variables = {'KEY': engine.EnvVar('runtime', is_secret=False)}
        self.assertTrue(self.driver.compare_settings(
            self.ep, self.env, self.service, variables, [])['env'])
        created = self.http.post(path + '/envs', json={})
        self.driver.set_env(self.ep, self.env, self.service, variables)
        self.http.get(path + '/envs', json=[created.last_request.json()])
        self.assertFalse(self.driver.compare_settings(
            self.ep, self.env, self.service, variables, [])['env'])

    def test_application_env_unsafe_flags_are_repaired(self):
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path, json={'fqdn': None})
        variables = {'KEY': engine.EnvVar('runtime', is_secret=False)}
        safe = {'uuid': 'normal', 'key': 'KEY', 'value': 'runtime',
                'comment': 'coast-managed', 'is_preview': False,
                'is_literal': True, 'is_multiline': False,
                'is_shown_once': False, 'is_buildtime': False,
                'is_runtime': True}
        for field, value in (('is_buildtime', True), ('is_runtime', False)):
            with self.subTest(field=field):
                unsafe = dict(safe, **{field: value})
                self.http.get(path + '/envs', json=[unsafe])
                self.assertTrue(self.driver.compare_settings(
                    self.ep, self.env, self.service, variables, [])['env'])
                hidden = dict(unsafe, value=None, is_shown_once=True)
                self.http.get(path + '/envs', json=[hidden])
                self.assertTrue(self.driver.compare_settings(
                    self.ep, self.env, self.service, variables, [])['env'])
                patched = self.http.patch(path + '/envs', json={})
                self.driver.set_env(self.ep, self.env, self.service, variables)
                payload = patched.last_request.json()
                self.assertIs(False, payload['is_buildtime'])
                self.assertIs(True, payload['is_runtime'])
                self.assertIs(False, payload['is_preview'])
                self.http.get(path + '/envs', json=[payload])
                self.assertFalse(self.driver.compare_settings(
                    self.ep, self.env, self.service, variables, [])['env'])

    def test_managed_preview_duplicates_are_drift_and_removed(self):
        for template in (False, True):
            with self.subTest(template=template):
                self.service.template_id = 'template' if template else None
                kind = 'services' if template else 'applications'
                path = self.ep.base_url + '/' + kind + '/app-uuid'
                rows = [
                    {'uuid': 'normal', 'key': 'KEY',
                     'comment': 'coast-managed', 'is_preview': False},
                    {'uuid': 'preview', 'key': 'KEY',
                     'comment': 'coast-managed', 'is_preview': True},
                    {'uuid': 'default', 'key': 'DEFAULT', 'is_preview': True}]
                self.http.get(path, json={})
                self.http.get(path + '/envs', json=rows)
                variables = {'KEY': engine.EnvVar('runtime')}
                self.assertTrue(self.driver.compare_settings(
                    self.ep, self.env, self.service, variables, [])['env'])
                self.http.patch(path + '/envs' + ('/bulk' if template else ''),
                                json={})
                deleted = self.http.delete(path + '/envs/preview',
                                           status_code=204)
                self.driver.set_env(self.ep, self.env, self.service, variables)
                self.assertEqual(1, deleted.call_count)
                self.assertEqual('DELETE', self.http.last_request.method)

    def test_application_env_removes_new_preview_copy(self):
        path = self.ep.base_url + '/applications/app-uuid/envs'
        self.http.get(path, [
            {'json': []},
            {'json': [
                {'uuid': 'normal', 'key': 'KEY', 'is_preview': False,
                 'comment': 'coast-managed'},
                {'uuid': 'preview', 'key': 'KEY', 'is_preview': True,
                 'comment': 'coast-managed'}]}])
        self.http.post(path, json={})
        self.http.delete(path + '/preview', status_code=204)
        self.driver.set_env(self.ep, self.env, self.service,
                            {'KEY': engine.EnvVar('runtime', build_time=True)})
        self.assertEqual(['GET', 'POST', 'GET', 'DELETE'],
                         [r.method for r in self.http.request_history])

    def test_application_env_build_time_payload_and_drift(self):
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path, json={'fqdn': None})
        for build_time in (True, False):
            for method in ('POST', 'PATCH'):
                with self.subTest(build_time=build_time, method=method):
                    rows = [] if method == 'POST' else [{'key': 'KEY'}]
                    self.http.get(path + '/envs', json=rows)
                    written = self.http.register_uri(
                        method, path + '/envs', json={})
                    variables = {'KEY': engine.EnvVar(
                        'value', is_secret=False, build_time=build_time)}
                    self.driver.set_env(
                        self.ep, self.env, self.service, variables)
                    payload = written.last_request.json()
                    self.assertIs(build_time, payload['is_buildtime'])
                    self.assertIs(True, payload['is_runtime'])
                    self.assertIs(False, payload['is_preview'])
                    self.http.get(path + '/envs', json=[payload])
                    self.assertFalse(self.driver.compare_settings(
                        self.ep, self.env, self.service, variables, [])['env'])
                    payload['is_buildtime'] = not build_time
                    self.http.get(path + '/envs', json=[payload])
                    self.assertTrue(self.driver.compare_settings(
                        self.ep, self.env, self.service, variables, [])['env'])

    def test_application_env_create_update_and_owned_deletion(self):
        path = self.ep.base_url + '/applications/app-uuid/envs'
        self.http.get(path, json=[
            {'uuid': 'default', 'key': 'DEFAULT'},
            {'uuid': 'existing', 'key': 'KEY', 'is_preview': False},
            {'uuid': 'preview', 'key': 'NEW', 'is_preview': True},
            {'uuid': 'old', 'key': 'OLD', 'comment': 'coast-managed'}])
        self.http.patch(path, json={})
        self.http.post(path, json={})
        self.http.delete(path + '/old', status_code=204)
        self.driver.set_env(self.ep, self.env, self.service, {
            'KEY': engine.EnvVar('runtime\nvalue', is_secret=True),
            'NEW': engine.EnvVar('new-value')})
        self.assertEqual({
            'key': 'KEY', 'value': 'runtime\nvalue', 'is_literal': True,
            'is_multiline': True, 'is_shown_once': True, 'is_preview': False,
            'comment': 'coast-managed', 'is_buildtime': False,
            'is_runtime': True},
            self.http.request_history[1].json())
        self.assertEqual(['GET', 'PATCH', 'POST', 'GET', 'DELETE'],
                         [r.method for r in self.http.request_history])
        self.assertEqual({
            'key': 'NEW', 'value': 'new-value', 'is_literal': True,
            'is_multiline': False, 'is_shown_once': False, 'is_preview': False,
            'comment': 'coast-managed', 'is_buildtime': False,
            'is_runtime': True},
            self.http.request_history[2].json())
        self.driver.set_env(self.ep, self.env, self.service, {})
        self.assertEqual(['GET', 'DELETE'],
                         [r.method for r in self.http.request_history[5:]])

    def test_mount_reconciliation_and_rejection(self):
        self.register("GET", "/applications/app-uuid/storages", "mounts")
        self.register(
            "DELETE", "/applications/app-uuid/storages/persistent-old",
            "deleted",
        )
        self.register(
            "DELETE", "/applications/app-uuid/storages/file-keep", "deleted"
        )
        self.register(
            "DELETE", "/applications/app-uuid/storages/file-old", "deleted"
        )
        self.register(
            "POST", "/applications/app-uuid/storages", body={}, status=201
        )
        self.driver.set_volume_mounts(
            self.ep, self.env, self.service,
            [
                engine.Mount("app", "/data"),
                engine.Mount("new", "/new"),
            ],
        )
        payload = self.http.last_request.json()
        self.assertEqual(
            {
                "type": "persistent",
                "name": "new",
                "mount_path": "/new",
            },
            payload,
        )
        self.register("GET", "/applications/app-uuid/storages", "storages")
        for row in self.fixtures["storages"]["file_storages"]:
            self.register("DELETE", "/applications/app-uuid/storages/" +
                          row["uuid"], "deleted")
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [])

    def test_matching_persistent_name_is_kept(self):
        self.register(
            "GET", "/applications/app-uuid/storages",
            body={"persistent_storages": [{
                "uuid": "keep", "name": "app-uuid-app",
                "mount_path": "/data"}], "file_storages": []})
        before = len(self.http.request_history)
        self.driver.set_volume_mounts(
            self.ep, self.env, self.service,
            [engine.Mount("app", "/data")])
        methods = [item.method for item in
                   self.http.request_history[before:]]
        self.assertEqual(["GET"], methods)

    def test_service_actions_and_observed_states(self):
        for action in ("start", "stop", "restart"):
            self.register("POST", "/applications/app-uuid/" + action, "action")
            getattr(self.driver, action + "_app")(self.ep,
                                                  self.env, self.service)
        for observed, expected in [
            ("running:healthy", states.ACTIVE),
            ("running:unhealthy", states.ERROR),
            ("exited", states.STOPPED),
            ("restarting", states.DEPLOYING),
            ("unknown engine-secret", states.ERROR),
        ]:
            self.register(
                "GET", "/applications/app-uuid", body={"status": observed}
            )
            result = self.driver.get_app_status(
                self.ep, self.env, self.service)
            self.assertEqual(expected, result.status)
            self.assertIsNone(result.detail)

    def test_deploy_frozen_settings_resolved_ref_and_replay(self):
        self.deployment.engine_ref = None
        self.deployment.source_ref = "a" * 40
        self.service.ports = "9999"
        self.register("PATCH", "/applications/app-uuid", "app")
        self.register("POST", "/deploy", "deploy")
        result = self.driver.deploy_app(
            self.ep, self.env, self.service, self.deployment
        )
        self.assertEqual("deployment-uuid", result.engine_ref)
        self.assertEqual({}, result.extra)
        payload = [r for r in self.http.request_history
                   if r.method == "PATCH"][0].json()
        self.assertEqual("8080", payload["ports_exposes"])
        self.assertEqual("a" * 40, payload["git_commit_sha"])
        self.deployment.engine_ref = result.engine_ref
        self.register("GET", "/deployments/deployment-uuid", "deployment")
        self.deployment.spec = {}  # Known refs do not reapply settings.
        result = self.driver.deploy_app(
            self.ep, self.env, self.service, self.deployment
        )
        self.assertEqual("a" * 40, result.extra["resolved_ref"])
        status = self.driver.get_deployment_status(
            self.ep, self.env, self.service, self.deployment
        )
        self.assertEqual(states.ACTIVE, status.status)
        self.assertEqual("a" * 40, status.detail)
        self.assertEqual("a" * 40, status.resolved_ref)

    def test_deploy_port_change_resends_domains_for_proxy_labels(self):
        self.deployment.engine_ref = None
        self.deployment.spec = dict(self.deployment.spec, ports="80")
        self.register("GET", "/applications/app-uuid", body={
            "ports_exposes": "8080", "fqdn": "https://web.example.test"})
        self.register("PATCH", "/applications/app-uuid", "app")
        self.register("POST", "/deploy", "deploy")
        self.driver.deploy_app(
            self.ep, self.env, self.service, self.deployment)
        payload = [r for r in self.http.request_history
                   if r.method == "PATCH"][0].json()
        self.assertEqual("80", payload["ports_exposes"])
        self.assertEqual("https://web.example.test", payload["domains"])

    def test_deploy_same_port_does_not_touch_domains(self):
        self.deployment.engine_ref = None
        self.deployment.spec = dict(self.deployment.spec, ports="80")
        self.register("GET", "/applications/app-uuid", body={
            "ports_exposes": "80", "fqdn": "https://web.example.test"})
        self.register("PATCH", "/applications/app-uuid", "app")
        self.register("POST", "/deploy", "deploy")
        self.driver.deploy_app(
            self.ep, self.env, self.service, self.deployment)
        payload = [r for r in self.http.request_history
                   if r.method == "PATCH"][0].json()
        self.assertNotIn("domains", payload)

    def test_resolved_ref_rejects_unresolved_commits_and_image_tags(self):
        for commit in (None, "HEAD", "main", "a" * 39, "a" * 41,
                       "g" * 40, "password=engine-secret"):
            with self.subTest(commit=commit):
                row = copy.deepcopy(self.fixtures["deployment"])
                row["commit"] = commit
                # A tag is not a digest, even if it resembles a SHA.
                row["docker_registry_image_tag"] = "b" * 40
                self.register("GET", "/deployments/deployment-uuid", body=row)
                result = self.driver.deploy_app(
                    self.ep, self.env, self.service, self.deployment)
                status = self.driver.get_deployment_status(
                    self.ep, self.env, self.service, self.deployment)
                self.assertEqual({}, result.extra)
                self.assertIsNone(status.resolved_ref)
                self.assertIsNone(status.detail)

    def test_deployment_states_and_cancel(self):
        for observed, expected in [
            ("queued", states.PENDING),
            ("in_progress", states.DEPLOYING),
            ("finished", states.ACTIVE),
            ("failed", states.FAILED),
            ("cancelled", states.CANCELLED),
            ("cancelled-by-user", states.CANCELLED),
            ("unknown", states.FAILED),
        ]:
            row = copy.deepcopy(self.fixtures["deployment"])
            row["status"] = observed
            self.register("GET", "/deployments/deployment-uuid", body=row)
            self.assertEqual(
                expected,
                self.driver.get_deployment_status(
                    self.ep, self.env, self.service, self.deployment
                ).status,
            )
        self.register("GET", "/deployments/deployment-uuid", "queued")
        self.register("POST", "/deployments/deployment-uuid/cancel", "deleted")
        self.driver.cancel_deployment(
            self.ep, self.env, self.service, self.deployment
        )
        self.assertEqual("POST", self.http.last_request.method)
        self.register("GET", "/deployments/deployment-uuid", "deployment")
        self.driver.cancel_deployment(
            self.ep, self.env, self.service, self.deployment
        )
        self.assertEqual("GET", self.http.last_request.method)

    def test_rollback_uses_resolved_commit_and_image_tag(self):
        new = copy.deepcopy(self.deployment)
        new.engine_ref = None
        self.register("GET", "/deployments/deployment-uuid", "deployment")
        self.register("PATCH", "/applications/app-uuid", "app")
        self.register("POST", "/applications/app-uuid/rollback", "rollback")
        result = self.driver.rollback(
            self.ep, self.env, self.service, self.deployment, new
        )
        self.assertEqual("rollback-uuid", result.engine_ref)
        self.assertEqual({"commit": "a" * 40}, self.http.last_request.json())
        self.deployment.spec["source"].update(
            type="image", image="registry.example.com/service:v1"
        )
        self.deployment.source_ref = "v1"
        # Live 4.4.3 image deployments report no registry image tag. Image
        # rollback re-applies the frozen tag and enqueues one deploy.
        row = copy.deepcopy(self.fixtures["deployment"])
        row["docker_registry_image_tag"] = None
        self.register("GET", "/deployments/deployment-uuid", body=row)
        self.register("POST", "/deploy", "deploy")
        posts = len([r for r in self.http.request_history
                     if r.method == "POST"])
        result = self.driver.rollback(
            self.ep, self.env, self.service, self.deployment, new)
        history = self.http.request_history
        patch = [r for r in history if r.method == "PATCH"][-1]
        self.assertEqual("v1", patch.json()["docker_registry_image_tag"])
        self.assertEqual("POST", history[-1].method)
        self.assertIn("/deploy", history[-1].path)
        self.assertEqual(posts + 1, len([r for r in history
                                         if r.method == "POST"]))
        self.assertTrue(result.engine_ref)
        new.engine_ref = "deployment-uuid"
        self.assertEqual(
            "deployment-uuid",
            self.driver.rollback(
                self.ep, self.env, self.service, self.deployment, new
            ).engine_ref,
        )
        self.register("GET", "/deployments/deployment-uuid", "queued")
        new.engine_ref = None
        self.assertRaises(
            engine.DriverError, self.driver.rollback,
            self.ep, self.env, self.service,
            self.deployment, new,
        )

    def test_template_deployment_logs_use_service_endpoint_and_masking(self):
        self.service.template_id = "uptime-kuma"
        self.deployment.engine_ref = "service-deployment-ref"
        self.register("GET", "/services/app-uuid", body={"applications": [
            {"name": "web", "fqdn": "https://example.com"},
            {"name": "helper"}], "databases": [{"name": "db"}]})
        response = self.register("GET", "/services/app-uuid/logs", body={
            "logs": "2026-10-10T01:00:00Z old\n"
                    "2026-10-10T01:00:01Z engine-secret coast-private "
                    "generated-private public-value password=hidden"})
        self.register("GET", "/services/app-uuid/envs", body=[
            {"key": "GENERATED", "value": "generated-private"},
            {"key": "PRIVATE", "is_shown_once": True}])
        masks = {"PRIVATE": engine.EnvVar("coast-private", is_secret=True)}
        lines = list(self.driver.get_deployment_log(
            self.ep, self.env, self.service, self.deployment,
            since="2026-10-10T01:00:00Z", masks=masks))
        self.assertEqual(1, len(lines))
        self.assertEqual("stdout", lines[0].stream)
        self.assertIn("public-value", lines[0].line)
        for secret in ("engine-secret", "coast-private", "generated-private",
                       "hidden"):
            self.assertNotIn(secret, lines[0].line)
        self.assertEqual(["web"], response.last_request.qs[
            "sub_service_name"])
        self.assertEqual(["true"], response.last_request.qs["show_timestamps"])
        self.assertFalse(any("/deployments/" in r.path for r in
                             self.http.request_history))

    def test_template_logs_handle_missing_or_ambiguous_targets(self):
        self.service.template_id = "template"
        for row in ({}, {"applications": [
                {"name": "one"}, {"name": "two"}]}):
            self.register("GET", "/services/app-uuid", body=row)
            self.assertEqual([], list(self.driver.get_deployment_log(
                self.ep, self.env, self.service, self.deployment)))
        self.register("GET", "/services/app-uuid", body={
            "databases": [{"name": "db"}]})
        response = self.register("GET", "/services/app-uuid/logs", status=404,
                                 body={})
        self.assertEqual([], list(self.driver.get_deployment_log(
            self.ep, self.env, self.service, self.deployment)))
        self.assertEqual(1, response.call_count)
        self.assertEqual(["db"], response.last_request.qs["sub_service_name"])
        self.register("GET", "/services/app-uuid", status=404, body={})
        self.assertEqual([], list(self.driver.get_deployment_log(
            self.ep, self.env, self.service, self.deployment)))
        self.assertFalse(any("/deployments/" in r.path for r in
                             self.http.request_history))

    def test_template_runtime_logs_suppress_unknown_shown_once_values(self):
        self.service.template_id = "template"
        self.register("GET", "/services/app-uuid", body={
            "applications": [{"name": "web"}]})
        response = self.register("GET", "/services/app-uuid/logs", body={
            "logs": "2026-10-10T01:00:00Z unknown-secret"})
        self.register("GET", "/services/app-uuid/envs", body=[
            {"key": "GENERATED", "is_shown_once": True}])
        lines = list(self.driver.get_app_logs(
            self.ep, self.env, self.service, tail=20))
        self.assertEqual("***", lines[0].line)
        self.assertEqual(["20"], response.last_request.qs["lines"])

    def test_template_log_route_denied_by_older_agent_returns_empty(self):
        self.service.template_id = "template"
        self.register("GET", "/services/app-uuid", body={
            "applications": [{"name": "web"}]})
        for body in ({}, {"message": "Forbidden"},
                     {"message": "Blocked by agent"}):
            response = self.register("GET", "/services/app-uuid/logs",
                                     status=403, body=body)
            self.assertEqual([], list(self.driver.get_deployment_log(
                self.ep, self.env, self.service, self.deployment)))
            self.assertEqual(1, response.call_count)
        self.assertFalse(any("/deployments/" in request.path
                             for request in self.http.request_history))

    def test_template_forbidden_metadata_and_env_remain_auth_errors(self):
        self.service.template_id = "template"
        self.register("GET", "/services/app-uuid/logs", body={"logs": ""})
        self.register("GET", "/services/app-uuid/envs", status=403, body={})
        self.register("GET", "/services/app-uuid", body={
            "applications": [{"name": "web"}]})
        self.assertRaises(engine.EngineAuthError,
                          self.driver.get_deployment_log,
                          self.ep, self.env, self.service, self.deployment)
        self.register("GET", "/services/app-uuid", status=403, body={})
        self.assertRaises(engine.EngineAuthError,
                          self.driver.get_deployment_log,
                          self.ep, self.env, self.service, self.deployment)

    def test_template_log_auth_and_connection_failures_are_not_hidden(self):
        self.service.template_id = "template"
        self.register("GET", "/services/app-uuid", body={
            "applications": [{"name": "web"}]})
        for status, error in ((401, engine.EngineAuthError),
                              (503, engine.EngineUnreachable)):
            response = self.register("GET", "/services/app-uuid/logs",
                                     status=status, body={})
            self.assertRaises(error, self.driver.get_deployment_log,
                              self.ep, self.env, self.service, self.deployment)
            self.assertEqual(1 if status == 401 else
                             self.conf.coolify.retry_attempts,
                             response.call_count)

    def test_rate_limit_retries_all_methods_without_logging_bodies(self):
        log_patch = mock.patch.object(client, '_log_4xx')
        logged = log_patch.start()
        self.addCleanup(log_patch.stop)
        with mock.patch.object(client.time, 'sleep') as sleep:
            for method in ('GET', 'POST', 'PATCH', 'DELETE'):
                path = ('/deploy' if method == 'POST' else
                        '/applications/app-uuid')
                response = self.http.register_uri(
                    method, self.ep.base_url + path, [
                        {'status_code': 429,
                         'headers': {'Retry-After': '2'}},
                        {'json': {'uuid': 'created'}}])
                with client.Client(self.ep, self.conf) as transport:
                    self.assertEqual({'uuid': 'created'}, transport.request(
                        method, path))
                self.assertEqual(2, response.call_count)
                sleep.assert_called_with(2)
        logged.assert_not_called()

    def test_rate_limited_post_does_not_retry_uncertain_next_outcome(self):
        with mock.patch.object(client.time, 'sleep'):
            with client.Client(self.ep, self.conf) as transport:
                for failure in ({'status_code': 503},
                                {'exc': requests.Timeout('secret')}):
                    endpoint = self.http.post(self.ep.base_url + '/deploy', [
                        {'status_code': 429}, failure, {'json': {}}])
                    error = self.assertRaises(
                        engine.EngineUnreachable, transport.request,
                        'POST', '/deploy')
                    self.assertNotIn('secret', str(error))
                    self.assertEqual(2, endpoint.call_count)

    def test_rate_limit_budget_and_jitter(self):
        self.config.config(retry_wait=1, rate_limit_attempts=3,
                           group='coolify')
        response = self.register('POST', '/deploy', status=429, body={})
        with mock.patch.object(client.time, 'sleep') as sleep:
            with client.Client(self.ep, self.conf) as transport:
                error = self.assertRaises(engine.EngineRateLimited,
                                          transport.request, 'POST', '/deploy')
            self.assertIn('retry budget exhausted', str(error))
            self.assertNotIsInstance(error, engine.EngineUnreachable)
            self.assertEqual(3, response.call_count)
            self.assertEqual(2, sleep.call_count)
            first, last = (call.args[0] for call in sleep.call_args_list)
            self.assertGreaterEqual(first, 0.5)
            self.assertLessEqual(first, 1)
            # Without Retry-After the last retry waits out the budget.
            self.assertGreater(last, 54)
            self.assertLessEqual(last, 60 - client.FINAL_LEAD)

    def test_rate_limit_without_retry_after_outlasts_engine_window(self):
        # Run 7: the agent dropped Retry-After, eight jittered retries
        # spent about 33 s and gave up inside the engine's 60 s window.
        clock = [0.0]
        response = self.http.get(self.ep.base_url + '/servers', [
            {'status_code': 429}] * 7 + [{'json': []}])

        def sleep(delay):
            self.assertGreaterEqual(delay, 0)
            clock[0] += delay

        with mock.patch.object(client.time, 'monotonic',
                               side_effect=lambda: clock[0]):
            with mock.patch.object(client.time, 'sleep',
                                   side_effect=sleep):
                with client.Client(self.ep, self.conf) as transport:
                    self.assertEqual([], transport.request('GET', '/servers'))
        self.assertEqual(8, response.call_count)
        self.assertEqual(
            self.conf.coolify.rate_limit_timeout - client.FINAL_LEAD,
            clock[0])

    def test_rate_limit_final_retry_is_the_last(self):
        clock = [0.0]
        response = self.register('POST', '/deploy', status=429, body={})

        def sleep(delay):
            clock[0] += delay

        with mock.patch.object(client.time, 'monotonic',
                               side_effect=lambda: clock[0]):
            with mock.patch.object(client.time, 'sleep',
                                   side_effect=sleep):
                with client.Client(self.ep, self.conf) as transport:
                    self.assertRaises(engine.EngineRateLimited,
                                      transport.request, 'POST', '/deploy')
        self.assertEqual(self.conf.coolify.rate_limit_attempts,
                         response.call_count)
        self.assertEqual(
            self.conf.coolify.rate_limit_timeout - client.FINAL_LEAD,
            clock[0])

    def _late_wakeup_calls(self, oversleep):
        clock = [0.0]
        response = self.register('GET', '/servers', status=429, body={})

        def sleep(delay):
            clock[0] += delay + oversleep  # the scheduler wakes up late

        with mock.patch.object(client.time, 'monotonic',
                               side_effect=lambda: clock[0]):
            with mock.patch.object(client.time, 'sleep',
                                   side_effect=sleep):
                with client.Client(self.ep, self.conf) as transport:
                    calls = []
                    real = transport._request

                    def counted(*args):
                        calls.append(clock[0])
                        return real(*args)

                    transport._request = counted
                    self.assertRaises(engine.EngineRateLimited,
                                      transport.request, 'GET', '/servers')
        self.assertEqual(len(calls), response.call_count)
        return calls

    def test_rate_limit_late_wakeup_never_retries_past_budget(self):
        budget = self.conf.coolify.rate_limit_timeout
        for oversleep in (0.01, 0.25, 0.49, 0.5, 0.75, 1.5, 5):
            with self.subTest(oversleep=oversleep):
                calls = self._late_wakeup_calls(oversleep)
                self.assertTrue(calls)
                self.assertTrue(all(t < budget for t in calls),
                                (oversleep, calls))

    def test_rate_limit_subsecond_oversleep_keeps_final_retry(self):
        # A held final retry still happens when the wake-up is late by
        # less than FINAL_LEAD, and it starts before the deadline.
        calls = self._late_wakeup_calls(client.FINAL_LEAD / 2)
        budget = self.conf.coolify.rate_limit_timeout
        self.assertEqual(self.conf.coolify.rate_limit_attempts, len(calls))
        self.assertLess(calls[-1], budget)
        self.assertGreater(calls[-1], budget - client.FINAL_LEAD)

    def _transient_only(self, oversleep=0.0, failures=1):
        clock = [0.0]
        response = self.http.get(self.ep.base_url + '/servers', [
            {'exc': requests.ConnectionError('boom')}] * failures + [
            {'json': {'ok': True}}])
        calls = []

        def sleep(delay):
            clock[0] += delay + oversleep

        with mock.patch.object(client.time, 'monotonic',
                               side_effect=lambda: clock[0]):
            with mock.patch.object(client.time, 'sleep', side_effect=sleep):
                with client.Client(self.ep, self.conf) as transport:
                    real = transport._request

                    def counted(*args):
                        calls.append(clock[0])
                        return real(*args)

                    transport._request = counted
                    try:
                        result = transport.request('GET', '/servers')
                    except engine.EngineUnreachable as exc:
                        result = exc
        self.assertEqual(len(calls), response.call_count)
        return result, calls

    def test_transient_retries_within_budget(self):
        self.config.config(retry_wait=1, rate_limit_timeout=60,
                           group='coolify')
        result, calls = self._transient_only(failures=2)
        self.assertEqual({'ok': True}, result)
        self.assertEqual([0.0, 1.0, 3.0], calls)

    def test_transient_retry_never_starts_past_budget(self):
        # No 429 at all: a transient retry that would start at or after
        # the deadline is not made.
        self.config.config(retry_wait=1, rate_limit_timeout=0.5,
                           group='coolify')
        result, calls = self._transient_only()
        self.assertIsInstance(result, engine.EngineUnreachable)
        self.assertEqual([0.0], calls)

    def test_transient_late_wakeup_never_retries_past_budget(self):
        self.config.config(retry_wait=1, rate_limit_timeout=60,
                           group='coolify')
        for oversleep in (0, 0.5, 30, 58.5, 59, 60, 100):
            with self.subTest(oversleep=oversleep):
                result, calls = self._transient_only(oversleep, failures=2)
                self.assertTrue(all(t < 60 for t in calls),
                                (oversleep, calls))
                if calls[-1] == 0.0 or len(calls) < 3:
                    self.assertIsInstance(result, engine.EngineUnreachable)
                else:
                    self.assertEqual({'ok': True}, result)

    def _held_retry_then_transient(self, retry_wait):
        self.config.config(retry_wait=retry_wait, group='coolify')
        clock = [0.0]
        response = self.http.get(self.ep.base_url + '/servers', [
            {'status_code': 429}] * 7 + [
            {'exc': requests.ConnectionError('boom')},
            {'json': {'ok': True}}])

        def sleep(delay):
            clock[0] += delay

        # Early rate-limit sleeps are jittered; keep them short so the
        # held retry is reached deterministically.
        with mock.patch.object(client.random.SystemRandom, 'uniform',
                               return_value=0.0):
            with mock.patch.object(client.time, 'monotonic',
                                   side_effect=lambda: clock[0]):
                with mock.patch.object(client.time, 'sleep',
                                       side_effect=sleep):
                    with client.Client(self.ep, self.conf) as transport:
                        try:
                            return transport.request('GET', '/servers'), \
                                clock[0], response
                        except engine.EngineUnreachable as exc:
                            return exc, clock[0], response

    def test_rate_limit_held_retry_keeps_transient_retry_in_budget(self):
        # Transient handling is unchanged after the held retry: it still
        # retries while that retry starts before the deadline.
        result, clock, response = self._held_retry_then_transient(0.1)
        self.assertEqual({'ok': True}, result)
        self.assertEqual(9, response.call_count)
        self.assertLess(clock, self.conf.coolify.rate_limit_timeout)

    def test_rate_limit_held_retry_no_transient_retry_past_deadline(self):
        result, clock, response = self._held_retry_then_transient(1)
        self.assertIsInstance(result, engine.EngineUnreachable)
        self.assertEqual(8, response.call_count)
        self.assertEqual(
            self.conf.coolify.rate_limit_timeout - client.FINAL_LEAD, clock)

    def test_rate_limit_held_retry_lands_after_engine_window(self):
        # The engine's fixed window opened before this request (its first
        # attempt was rejected) and rejects every attempt until it closes.
        clock = [0.0]
        window_closes = (self.conf.coolify.rate_limit_timeout -
                         client.FINAL_LEAD - 0.1)
        starts = []

        def engine_reply(request, context):
            starts.append(clock[0])
            if clock[0] < window_closes:
                context.status_code = 429
                return {}
            return []

        self.http.get(self.ep.base_url + '/servers', json=engine_reply)

        def sleep(delay):
            clock[0] += delay

        with mock.patch.object(client.time, 'monotonic',
                               side_effect=lambda: clock[0]):
            with mock.patch.object(client.time, 'sleep',
                                   side_effect=sleep):
                with client.Client(self.ep, self.conf) as transport:
                    self.assertEqual([], transport.request('GET', '/servers'))
        self.assertTrue(all(t < window_closes for t in starts[:-1]), starts)
        self.assertGreaterEqual(starts[-1], window_closes)
        self.assertLess(starts[-1], self.conf.coolify.rate_limit_timeout)

    def test_rate_limit_does_not_retry_before_long_retry_after(self):
        response = self.http.post(self.ep.base_url + '/deploy',
                                  status_code=429,
                                  headers={'Retry-After': '120'})
        with mock.patch.object(client.time, 'sleep') as sleep:
            with client.Client(self.ep, self.conf) as transport:
                self.assertRaises(engine.EngineRateLimited, transport.request,
                                  'POST', '/deploy')
            self.assertEqual(1, response.call_count)
            sleep.assert_not_called()

    def test_rate_limit_elapsed_budget_stops_after_sleep(self):
        response = self.register('POST', '/deploy', status=429, body={})
        with mock.patch.object(client.time, 'monotonic',
                               side_effect=(0, 0, 61)):
            with mock.patch.object(client.time, 'sleep') as sleep:
                with client.Client(self.ep, self.conf) as transport:
                    self.assertRaises(engine.EngineRateLimited,
                                      transport.request, 'POST', '/deploy')
        self.assertEqual(1, response.call_count)
        self.assertEqual(1, sleep.call_count)

    def test_retry_after_dates_and_invalid_headers(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        future = format_datetime(now + datetime.timedelta(seconds=30))
        self.assertGreater(client._retry_after(future), 28)
        self.assertLessEqual(client._retry_after(future), 30)
        self.assertEqual(0, client._retry_after(format_datetime(
            now - datetime.timedelta(seconds=30))))
        for value in (None, '', '-1', 'nan', 'inf', '1.5', 'invalid'):
            self.assertIsNone(client._retry_after(value))

    def test_client_does_not_retry_missing_resources(self):
        with client.Client(self.ep, self.conf) as transport:
            for method in ("GET", "PATCH", "DELETE"):
                response = self.register(
                    method, "/applications/app-uuid", status=404, body={})
                self.assertRaises(engine.EngineNotFound, transport.request,
                                  method, "/applications/app-uuid")
                self.assertEqual(1, response.call_count)

    def test_logs_filter_tail_and_mask_secrets(self):
        self.register("GET", "/deployments/deployment-uuid",
                      body=self.synthetic["deployment"])
        self.register("GET", "/applications/app-uuid/envs",
                      body=self.synthetic["envs"])
        lines = list(
            self.driver.get_deployment_log(
                self.ep, self.env, self.service,
                self.deployment,
                since="2026-10-10T01:00:00Z",
            )
        )
        self.assertEqual(1, len(lines))
        self.assertEqual("Deploy complete", lines[0].line)
        self.assertEqual("build", lines[0].stream)
        self.register(
            "GET", "/applications/app-uuid/logs",
            body={
                "logs": (
                    "2026-10-10T01:00:00Z old-value engine-secret "
                    "password=secret"
                )
            },
        )
        lines = list(
            self.driver.get_app_logs(self.ep, self.env, self.service, tail=1)
        )
        self.assertNotIn("old-value", lines[0].line)
        self.assertNotIn("engine-secret", lines[0].line)
        self.assertNotIn("password=secret", lines[0].line)
        self.register(
            "GET", "/applications/app-uuid/envs",
            body=[{"is_shown_once": True}],
        )
        lines = list(self.driver.get_app_logs(self.ep, self.env, self.service))
        self.assertEqual("***", lines[0].line)

    def test_logs_mask_known_secrets_without_suppressing_output(self):
        masks = {
            "PRIVATE": engine.EnvVar(value="coast-private", is_secret=True),
            "PUBLIC": engine.EnvVar(value="public-value", is_secret=False),
            "EMPTY": engine.EnvVar(value="", is_secret=True),
            "PREFIX": engine.EnvVar(value="coast", is_secret=True),
        }
        output = "ready coast-private public-value engine-secret password=bad"
        self.register("GET", "/applications/app-uuid/envs", body=[
            {"key": "PRIVATE", "is_shown_once": True},
            {"key": "PUBLIC", "value": "public-value"},
        ])
        self.register("GET", "/applications/app-uuid/logs", body={
            "logs": "2026-10-10T01:00:00Z " + output})
        self.register("GET", "/deployments/deployment-uuid", body={
            "logs": json.dumps([{
                "timestamp": "2026-10-10T01:00:00Z", "output": output}])})
        for read in (
                lambda: self.driver.get_app_logs(
                    self.ep, self.env, self.service, masks=masks),
                lambda: self.driver.get_deployment_log(
                    self.ep, self.env, self.service, self.deployment,
                    masks=masks)):
            self.assertEqual(
                ["ready *** public-value *** password=***"],
                [line.line for line in read()])
        # A Coast-known secret does not make an engine-only key safe.
        self.register("GET", "/applications/app-uuid/envs", body=[
            {"key": "PRIVATE", "is_shown_once": True},
            {"key": "ENGINE_ONLY", "is_shown_once": True},
        ])
        self.assertEqual(["***"], [
            line.line for line in self.driver.get_app_logs(
                self.ep, self.env, self.service, masks=masks)])
        self.assertEqual(["***"], [
            line.line for line in self.driver.get_deployment_log(
                self.ep, self.env, self.service, self.deployment,
                masks=masks)])

    def test_hidden_build_lines_stay_visible_and_masked(self):
        logs = json.loads((FIXTURES / "hidden-build-log.json").read_text())
        self.register(
            "GET", "/deployments/deployment-uuid",
            body={"logs": json.dumps(logs)})
        self.register(
            "GET", "/applications/app-uuid/envs",
            body=[{"key": "TOKEN", "value": "engine-secret"}])
        lines = list(self.driver.get_deployment_log(
            self.ep, self.env, self.service, self.deployment))
        self.assertEqual([
            ("2026-10-10T01:00:00.000000Z", "build",
             "#5 [2/6] RUN npm ci\nexporting layers"),
            ("2026-10-10T01:00:01.000000Z", "build",
             "password=*** *** done"),
            ("2026-10-10T01:00:02.000000Z", "build",
             "Build step in progress."),
            ("2026-10-10T01:00:04.000000Z", "build",
             "Deployment finished."),
        ], [(line.ts, line.stream, line.line) for line in lines])
        rendered = "\n".join(line.line for line in lines)
        for secret in ("super-private-key", "leak-token", "git clone",
                       "docker exec", "build-secret", "engine-secret"):
            self.assertNotIn(secret, rendered)
        for line in lines:
            self.assertEqual(
                {"line", "stream", "ts"},
                {item.name for item in dataclasses.fields(line)})
        self.register(
            "GET", "/applications/app-uuid/envs",
            body=[{"key": "TOKEN", "value": "engine-secret",
                   "is_shown_once": True}])
        lines = list(self.driver.get_deployment_log(
            self.ep, self.env, self.service, self.deployment))
        self.assertEqual(["***"] * 5, [line.line for line in lines])
        rendered = "\n".join(line.line for line in lines)
        self.assertNotIn("Build step in progress.", rendered)
        for secret in ("super-private-key", "leak-token", "git clone",
                       "docker exec", "build-secret", "engine-secret",
                       "npm ci"):
            self.assertNotIn(secret, rendered)

    def test_live_log_and_mount_response_shapes(self):
        self.register("GET", "/deployments/deployment-uuid", "deployment")
        self.register("GET", "/applications/app-uuid/envs", body=[])
        lines = list(self.driver.get_deployment_log(
            self.ep, self.env, self.service, self.deployment))
        self.assertTrue(lines)
        self.assertTrue(all(
            isinstance(line, engine.LogLine) for line in lines))
        self.register("GET", "/applications/app-uuid/logs", "logs")
        self.assertTrue(list(self.driver.get_app_logs(
            self.ep, self.env, self.service)))
        captured = json.loads((FIXTURES / "live.json").read_text())
        storage = next(pair["response"]["body"] for pair in captured["pairs"]
                       if pair["request"]["method"] == "GET" and
                       pair["request"]["path"] ==
                       "/applications/app-uuid/storages" and
                       pair["response"]["body"]["file_storages"])
        self.register("GET", "/applications/app-uuid/storages", body=storage)
        row = storage["file_storages"][0]
        self.register("DELETE", "/applications/app-uuid/storages/" +
                      row["uuid"], "deleted")
        self.register("POST", "/applications/app-uuid/storages",
                      body={}, status=201)
        self.driver.set_volume_mounts(
            self.ep, self.env, self.service,
            [engine.Mount("contract", row["mount_path"])])
        self.assertEqual("persistent", self.http.last_request.json()["type"])
        self.assertNotIn("fs_path", self.http.last_request.json())
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [])
        self.assertEqual("DELETE", self.http.last_request.method)

    def test_compose_domains_write_compare_and_clear(self):
        row = json.loads((FIXTURES / 'compose-settings.json').read_text())
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path + '/envs', json=[])
        self.http.get(path, json=row)
        self.http.patch(path, json={'uuid': 'app-uuid'})
        self.http.post(path + '/restart', json={})
        domains = ['site.example.com', 'other.example.com']
        self.assertTrue(self.driver.compare_settings(
            self.ep, self.env, self.service, {}, domains)['domains'])
        self.driver.set_domains(self.ep, self.env, self.service, domains)
        payload = [r for r in self.http.request_history
                   if r.method == 'PATCH'][-1].json()
        self.assertEqual({'docker_compose_domains': [{
            'name': 'web',
            'domain': 'https://site.example.com,https://other.example.com'}]},
            payload)
        # Replay the pinned GET shape after the documented PATCH assignment.
        row['docker_compose_domains'] = json.dumps({
            item['name']: {'domain': item['domain']}
            for item in payload['docker_compose_domains']})
        self.http.get(path, json=row)
        self.assertEqual({'env': False, 'domains': False},
                         self.driver.compare_settings(
                             self.ep, self.env, self.service, {},
                             [name.upper() for name in reversed(domains)]))
        self.assertEqual(states.ACTIVE, self.driver.get_app_status(
            self.ep, self.env, self.service).status)
        row['docker_compose_domains'] = {
            'web': {'domain': 'https://site.example.com'},
            'helper': {'domain': 'https://other.example.com'}}
        self.http.get(path, json=row)
        self.driver.set_domains(self.ep, self.env, self.service, [])
        self.assertEqual({'docker_compose_domains': [
            {'name': 'web', 'domain': ''}, {'name': 'helper', 'domain': ''}]},
            [r for r in self.http.request_history
             if r.method == 'PATCH'][-1].json())
        row['docker_compose_domains'] = {'web': {'domain': ''}}
        self.http.get(path, json=row)
        self.driver.set_domains(self.ep, self.env, self.service, domains)
        payload = [r for r in self.http.request_history
                   if r.method == 'PATCH'][-1].json()
        self.assertEqual('web', payload['docker_compose_domains'][0]['name'])
        self.check_contracts()
        self.check_proxy_contract()

    def test_compose_domains_unknown_target_never_writes(self):
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path + '/envs', json=[])
        for value in (None, [], {}, '{}', 'not-json', 7,
                      {'web': {'domain': True}}, {'web': {}},
                      {'web': {'domain': ''}, 'helper': {'domain': None}},
                      {'web': {'domain': 'https://one.example.com'},
                       'helper': {'domain': 'https://two.example.com'}}):
            with self.subTest(value=value):
                row = {'build_pack': 'dockercompose',
                       'docker_compose_domains': value,
                       'fqdn': 'https://site.example.com'}
                self.http.get(path, json=row)
                self.assertIsNone(self.driver.compare_settings(
                    self.ep, self.env, self.service, {},
                    ['site.example.com'])['domains'])
                self.assertRaises(engine.DriverError, self.driver.set_domains,
                                  self.ep, self.env, self.service,
                                  ['site.example.com'])
        self.http.get(path, json={'build_pack': 'dockercompose'})
        self.assertIsNone(self.driver.compare_settings(
            self.ep, self.env, self.service, {}, [])['domains'])
        self.assertRaises(engine.DriverError, self.driver.set_domains,
                          self.ep, self.env, self.service, [])
        self.assertTrue(all(r.method == 'GET'
                            for r in self.http.request_history))

    def test_compose_domains_empty_map_and_strict_urls(self):
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path + '/envs', json=[])
        for value in (None, [], {}, '{}', '[]'):
            self.http.get(path, json={
                'build_pack': 'dockercompose',
                'docker_compose_domains': value})
            self.assertFalse(self.driver.compare_settings(
                self.ep, self.env, self.service, {}, [])['domains'])
            self.driver.set_domains(self.ep, self.env, self.service, [])
        for url in ('http://site.example.com',
                    'https://site.example.com:8443',
                    'https://site.example.com/other'):
            self.http.get(path, json={
                'build_pack': 'dockercompose', 'docker_compose_domains': {
                    'web': {'domain': url}}, 'fqdn': None})
            self.assertTrue(self.driver.compare_settings(
                self.ep, self.env, self.service, {},
                ['site.example.com'])['domains'])
        self.assertTrue(all(r.method == 'GET'
                            for r in self.http.request_history))

    def test_domain_changes_apply_once_for_running_workloads(self):
        for kind in ('application', 'compose', 'template'):
            with self.subTest(kind=kind):
                self.service.template_id = ('template' if kind == 'template'
                                            else None)
                path = self.ep.base_url + ('/services/' if kind == 'template'
                                           else '/applications/') + 'app-uuid'
                row = {'status': 'running:healthy', 'fqdn': None}
                if kind == 'compose':
                    row.update(build_pack='dockercompose',
                               docker_compose_domains={'web': {'domain': ''}})
                if kind == 'template':
                    row['applications'] = [{'name': 'web', 'fqdn': None}]

                def read(request, context):
                    return copy.deepcopy(row)

                def write(request, context):
                    data = request.json()
                    if kind == 'template':
                        row['applications'][0]['fqdn'] = data['urls'][0]['url']
                    elif kind == 'compose':
                        row['docker_compose_domains'] = {
                            item['name']: {'domain': item['domain']}
                            for item in data['docker_compose_domains']}
                    else:
                        row['fqdn'] = data['domains']
                    return {}

                self.http.get(path, json=read)
                patch = self.http.patch(path, json=write)
                restart = self.http.post(path + '/restart', json={})
                for names in (['site.example.com', 'other.example.com'],
                              ['site.example.com'], []):
                    self.driver.set_domains(
                        self.ep, self.env, self.service, names)
                    count = restart.call_count
                    self.assertEqual(patch.call_count, count)
                    self.driver.set_domains(
                        self.ep, self.env, self.service,
                        [name.upper() for name in reversed(names)])
                    self.assertEqual(count, restart.call_count)
                    self.assertEqual(count, patch.call_count)
                self.assertEqual(3, restart.call_count)
                row['status'] = 'exited'
                self.driver.set_domains(self.ep, self.env, self.service,
                                        ['stopped.example.com'])
                self.assertEqual(3, restart.call_count)
                self.assertEqual(4, patch.call_count)

    def test_domain_restart_failure_restores_drift_for_retry(self):
        path = self.ep.base_url + '/applications/app-uuid'
        self.http.get(path, json={'status': 'running:healthy',
                                  'fqdn': 'https://old.example.com'})
        patch = self.http.patch(path, json={})
        self.http.post(path + '/restart', status_code=422, json={})
        self.assertRaises(engine.DriverError, self.driver.set_domains,
                          self.ep, self.env, self.service,
                          ['new.example.com'])
        self.assertEqual(2, patch.call_count)
        self.assertEqual({'domains': 'https://old.example.com'},
                         patch.last_request.json())

    def test_domains_replace_and_clear(self):
        self.register("GET", "/applications/app-uuid", "app")
        self.register("POST", "/applications/app-uuid/restart", "app")
        self.register("PATCH", "/applications/app-uuid", "app")
        self.driver.set_domains(
            self.ep, self.env, self.service,
            ["service.example.com", "other.example.com"],
        )
        self.assertEqual(
            "https://service.example.com,https://other.example.com",
            [r for r in self.http.request_history
             if r.method == 'PATCH'][-1].json()["domains"],
        )
        self.register("GET", "/applications/app-uuid", body={
            "status": "running:healthy",
            "fqdn": "https://service.example.com,https://other.example.com"})
        self.driver.set_domains(self.ep, self.env, self.service, [])
        payload = [r for r in self.http.request_history
                   if r.method == 'PATCH'][-1].json()
        self.assertEqual("", payload["domains"])

    def test_image_source_with_separate_tag(self):
        self.creation()
        self.source.type = "image"
        self.source.image = "registry.example.com:5000/web"
        self.source.tag = "_v2.0-rc"
        self.register("POST", "/applications/dockerimage", "app_created",
                      status=201)
        self.driver.create_app(self.ep, self.env, self.service,
                               self.source, None)
        payload = self.http.last_request.json()
        self.assertEqual("registry.example.com:5000/web",
                         payload["docker_registry_image_name"])
        self.assertEqual("_v2.0-rc", payload["docker_registry_image_tag"])
        count = len(self.http.request_history)
        for credentials, ref in (("hidden", None), (None, "opaque-ref")):
            self.source.credentials_ref = ref
            self.assertRaises(engine.DriverError, self.driver.create_app,
                              self.ep, self.env, self.service, self.source,
                              credentials)
            self.assertEqual(count, len(self.http.request_history))

    def test_unsupported_inputs_fail_before_http(self):
        self.service.server_id = None
        self.assertRaises(engine.DriverError, self.driver.create_app,
                          self.ep, self.env, self.service, self.source, None)
        self.service.server_id = 'server'
        self.source.type = "unsupported"
        self.assertRaises(
            engine.DriverError, self.driver.update_app,
            self.ep, self.env, self.service,
            self.source,
        )
        self.source.type = "image"
        self.source.image = "repo@sha256:secret"
        self.assertRaises(
            engine.DriverError, self.driver.create_app,
            self.ep, self.env, self.service,
            self.source, None,
        )
        self.deployment.engine_ref = None
        self.deployment.image_digest = "sha256:secret"
        self.assertRaises(
            engine.DriverError, self.driver.deploy_app,
            self.ep, self.env, self.service,
            self.deployment,
        )
        self.assertRaises(
            engine.DriverError, self.driver.set_domains,
            self.ep, self.env, self.service,
            ["https://bad.example.com/secret"],
        )
        self.assertRaises(
            engine.DriverError, self.driver.get_app_logs,
            self.ep, self.env, self.service,
            tail=0,
        )
        self.assertRaises(
            engine.DriverError, self.driver.set_volume_mounts,
            self.ep, self.env, self.service,
            [engine.Mount("data", "relative")],
        )
        self.assertRaises(
            engine.DriverError, self.driver.set_volume_mounts,
            self.ep, self.env, self.service,
            [engine.Mount("bad/name", "/data")],
        )
        self.assertEqual([], self.http.request_history)

    def test_ambiguous_or_missing_resource_refs_are_sanitized(self):
        with client.Client(self.ep, self.conf) as c:
            self.register(
                "GET", "/applications", body=self.fixtures["apps"] * 2
            )
            self.assertRaises(
                engine.EngineConflict, v4.find, c,
                "/applications", self.service,
            )
            self.register("GET", "/applications", body={})
            self.assertRaises(
                engine.DriverError, v4.find, c, "/applications", self.service
            )
        self.assertRaises(engine.DriverError, v4.ref, None)
        self.assertRaises(
            engine.DriverError, v4.identifier, {"token": "secret"}
        )

    def test_finished_deployment_waits_for_running_app(self):
        self.http.get(self.ep.base_url + "/deployments/deployment-uuid",
                      json={"deployment_uuid": "deployment-uuid",
                            "status": "finished"})
        app = self.ep.base_url + "/applications/app-uuid"
        for status, expected in (
                ("exited", states.DEPLOYING), ("starting", states.DEPLOYING),
                ("running:unhealthy", states.DEPLOYING),
                ("running:unknown", states.ACTIVE),
                ("running:healthy", states.ACTIVE)):
            self.http.get(app, json={"status": status})
            with self.subTest(status=status):
                self.assertEqual(expected, self.driver.get_deployment_status(
                    self.ep, self.env, self.service, self.deployment).status)
        self.http.get(self.ep.base_url + "/deployments/deployment-uuid",
                      json={"deployment_uuid": "deployment-uuid",
                            "status": "failed"})
        self.assertEqual(states.FAILED, self.driver.get_deployment_status(
            self.ep, self.env, self.service, self.deployment).status)

    def test_domain_urls_keep_scheme_and_case(self):
        self.register("POST", "/applications/app-uuid/restart", "app")
        self.register("PATCH", "/applications/app-uuid", "app")
        self.driver.set_domains(
            self.ep, self.env, self.service,
            ["HTTPS://Example.COM", "http://Plain.Example.com"])
        self.assertEqual(
            "https://Example.COM,http://Plain.Example.com",
            [r for r in self.http.request_history
             if r.method == 'PATCH'][-1].json()["domains"])

    def test_list_domains_reads_fqdn_and_a_missing_app(self):
        self.register("GET", "/applications/app-uuid", body={
            "uuid": "app-uuid",
            "fqdn": "https://One.Example.com,https://Two.Example.com"})
        self.assertEqual(
            ["https://One.Example.com", "https://Two.Example.com"],
            self.driver.list_domains(self.ep, self.env, self.service))
        self.register("GET", "/applications/app-uuid", "error", status=404)
        self.assertIsNone(
            self.driver.list_domains(self.ep, self.env, self.service))

    def test_update_metadata_sends_the_engine_name(self):
        self.register("PATCH", "/applications/app-uuid", "app")
        self.driver.update_metadata(self.ep, self.env, self.service)
        self.assertEqual(
            {"name": "coast-app", "description": "Test service"},
            self.http.last_request.json())
        self.service.template_id = "template"
        count = len(self.http.request_history)
        self.assertIsNone(self.driver.update_metadata(
            self.ep, self.env, self.service))
        self.assertEqual(count, len(self.http.request_history))

    def test_create_sends_the_owned_default_domain(self):
        self.creation()
        self.service.default_domain = "web-af32.192-0-2-248.sslip.io"
        self.register(
            "POST", "/applications/public", "app_created", status=201)
        self.driver.create_app(
            self.ep, self.env, self.service, self.source, None)
        payload = self.http.last_request.json()
        self.assertEqual(
            "https://web-af32.192-0-2-248.sslip.io", payload["domains"])
        self.assertIs(False, payload["autogenerate_domain"])

    def test_no_ports_disable_health_and_remove_routes_on_deploy(self):
        self.service.ports = ''
        self.service.health_check = 'none'
        self.service.health_check_port = None
        self.deployment.spec['ports'] = ''
        self.deployment.spec['health_check'] = 'none'
        self.http.get(self.ep.base_url + '/applications/app-uuid', json={
            'ports_exposes': '8080', 'fqdn': 'https://web.example.com'})
        self.http.patch(self.ep.base_url + '/applications/app-uuid', json={})
        self.http.post(self.ep.base_url + '/deploy',
                       json=self.fixtures['deploy'])
        self.deployment.engine_ref = None
        self.driver.deploy_app(
            self.ep, self.env, self.service, self.deployment)
        payload = next(req.json() for req in self.http.request_history
                       if req.method == 'PATCH')
        self.assertEqual('', payload['ports_exposes'])
        self.assertFalse(payload['health_check_enabled'])
        self.assertEqual('', payload['domains'])

    def test_http_health_port_is_sent_as_engine_string(self):
        self.service.health_check = 'http'
        self.service.health_check_port = 9090
        result = v4.app_settings(self.service, self.source)
        self.assertTrue(result['health_check_enabled'])
        self.assertEqual('9090', result['health_check_port'])

    def test_volume_observations_include_host_sources_without_content(self):
        self.service.template_id = None
        self.register('GET', '/applications/app-uuid/storages', body={
            'persistent_storages': [
                {'name': 'data', 'mount_path': '/data', 'id': 10}],
            'file_storages': [{'content': 'secret', 'fs_path': '/private',
                               'mount_path': '/config:ro'}]})
        rows = self.driver.list_volume_mounts(
            self.ep, self.env, self.service)
        self.assertEqual([dict(name='data', source='data', type='volume',
                               container_path='/data', read_only=False),
                          dict(name='/private', source='/private', type='bind',
                               container_path='/config', read_only=True)],
                         rows)
        self.assertNotIn('secret', str(rows))

    def test_template_observation_includes_all_mounts_and_modes(self):
        self.service.template_id = 'beszel'
        self.register('GET', '/services/app-uuid', body={
            'docker_compose_raw': (
                'services:\n  agent:\n    image: example/agent\n'
                '    volumes:\n'
                '      - /var/run/docker.sock:/var/run/docker.sock:ro\n'
                '      - data:/data\n'
                '      - type: bind\n        source: /srv/data\n'
                '        target: /host\n        read_only: true\n')})
        rows = self.driver.list_volume_mounts(
            self.ep, self.env, self.service)
        self.assertEqual(['bind', 'volume', 'bind'],
                         [row['type'] for row in rows])
        self.assertEqual([True, False, True],
                         [row['read_only'] for row in rows])
        self.assertEqual('/var/run/docker.sock', rows[0]['source'])

    def test_bind_mount_read_only_reconcile_and_folder_creation(self):
        self.service.template_id = None
        path = '/applications/app-uuid/storages'
        self.register('GET', path, body={'file_storages': [{
            'uuid': 'old', 'fs_path': '/srv/old',
            'mount_path': '/data', 'is_directory': True}]})
        self.register('DELETE', path + '/old', body={})
        self.register('POST', path, body={}, status=201)
        mount = engine.Mount(None, '/data', True, type='bind',
                             host_path='/srv/coast-files/data')
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [mount])
        self.assertEqual(dict(type='file', fs_path='/srv/coast-files/data',
                              is_directory=True, mount_path='/data:ro'),
                         self.http.last_request.json())
        self.register('GET', path, body={'file_storages': [{
            'uuid': 'keep', 'fs_path': '/srv/coast-files/data',
            'mount_path': '/data:ro', 'is_directory': True}]})
        start = len(self.http.request_history)
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [mount])
        self.driver.set_file_mounts(self.ep, self.env, self.service, [])
        self.assertEqual(['GET', 'GET'], [
            r.method for r in self.http.request_history[start:]])

    def test_named_volume_read_only_is_preserved(self):
        self.service.template_id = None
        path = '/applications/app-uuid/storages'
        self.register('GET', path, body={})
        self.register('POST', path, body={}, status=201)
        self.driver.set_volume_mounts(self.ep, self.env, self.service,
                                      [engine.Mount('data', '/data', True)])
        self.assertEqual(dict(type='persistent', name='data',
                              mount_path='/data:ro'),
                         self.http.last_request.json())

    def test_template_user_mount_apply_remove_and_idempotence(self):
        import base64
        import yaml
        self.service.template_id = 'beszel'
        path = self.ep.base_url + '/services/app-uuid'
        original = {'services': {'web': {'image': 'example/web', 'volumes': [
            '/var/run/docker.sock:/var/run/docker.sock:ro']}}}
        state = {'docker_compose_raw': yaml.safe_dump(original)}
        self.http.get(path, json=lambda request, context: state)

        def save(request, context):
            state['docker_compose_raw'] = base64.b64decode(
                request.json()['docker_compose_raw']).decode('utf-8')
            return {}

        posted = self.http.patch(path, json=save)
        mount = engine.Mount(None, '/data', True, type='bind',
                             host_path='/srv/data', id='coast-mount')
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [mount])
        document = yaml.safe_load(state['docker_compose_raw'])
        mounts = document['services']['web']['volumes']
        self.assertEqual(original['services']['web']['volumes'][0], mounts[0])
        self.assertEqual(dict(type='bind', source='/srv/data', target='/data',
                              read_only=True, bind={'create_host_path': True},
                              **{'x-coast-storage': 'coast-mount'}), mounts[1])
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [mount])
        self.assertEqual(1, posted.call_count)
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [])
        self.assertEqual(original, yaml.safe_load(state['docker_compose_raw']))
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [
            engine.Mount('data', '/data', True, id='volume-mount')])
        document = yaml.safe_load(state['docker_compose_raw'])
        self.assertEqual({'data': {}}, document['volumes'])
        self.assertEqual('volume', document['services']['web']['volumes'][1][
            'type'])
        document['services']['db'] = {'image': 'example/db'}
        state['docker_compose_raw'] = yaml.safe_dump(document)
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [])
        cleaned = yaml.safe_load(state['docker_compose_raw'])
        self.assertEqual({'image': 'example/db'}, cleaned['services']['db'])
        self.assertEqual(original['services']['web'],
                         cleaned['services']['web'])

    def test_template_mount_conflicts_fail_before_writes(self):
        self.service.template_id = 'beszel'
        path = '/services/app-uuid'
        mount = engine.Mount('data', '/data')
        for raw in ('services: {web: {image: web}, db: {image: db}}',
                    'services: {web: {image: web, volumes: [data:/data]}}'):
            self.register('GET', path, body={'docker_compose_raw': raw})
            self.assertRaises(engine.EngineConflict,
                              self.driver.set_volume_mounts,
                              self.ep, self.env, self.service, [mount])
        self.assertEqual({'GET'},
                         {r.method for r in self.http.request_history})

    def test_volume_reconcile_preserves_unrelated_content_files(self):
        self.service.template_id = None
        path = '/applications/app-uuid/storages'
        self.register('GET', path, body={
            'persistent_storages': [{'name': 'data', 'mount_path': '/data'}],
            'file_storages': [{'uuid': 'content',
                               'mount_path': '/etc/app.conf',
                               'fs_path': '/data/resource/config',
                               'is_directory': False}]})
        self.driver.set_volume_mounts(self.ep, self.env, self.service,
                                      [engine.Mount('data', '/data')])
        self.assertEqual(['GET'],
                         [request.method for request in
                          self.http.request_history])

    def test_named_storage_edits_preserve_managed_config_files(self):
        path = '/applications/app-uuid/storages'
        self.register('GET', path, body={'file_storages': [{
            'uuid': 'config', 'fs_path': '/data/resource/coast-files/id'}]})
        self.driver.set_volume_mounts(self.ep, self.env, self.service, [])
        self.assertEqual(['GET'],
                         [r.method for r in self.http.request_history])

    def test_content_file_storage_uses_only_generated_source_paths(self):
        path = '/applications/app-uuid/storages'
        self.register('GET', path, body={'file_storages': [{
            'uuid': 'old', 'fs_path': '/data/resource/coast-files/old'}]})
        self.register('DELETE', path + '/old', body={})
        self.register('POST', path, body={})
        mount = engine.FileMount('mount-id', '/etc/site.conf', 'private')
        self.assertNotIn('private', repr(mount))
        self.driver.set_file_mounts(self.ep, self.env, self.service, [mount])
        self.assertEqual({
            'type': 'file', 'mount_path': '/etc/site.conf',
            'content': 'private',
            'is_directory': False, 'fs_path': 'coast-files/mount-id'},
            self.http.last_request.json())
        self.driver.set_file_mounts(self.ep, self.env, self.service, [])
        self.assertEqual('DELETE', self.http.last_request.method)

    def test_template_content_files_resolve_target_before_writes(self):
        self.service.template_id = 'wordpress-with-mariadb'
        self.register('GET', '/services/app-uuid', body={
            'applications': [{'name': 'web', 'uuid': 'web-child'}],
            'databases': [{'name': 'db', 'uuid': 'db-child'}]})
        path = '/services/app-uuid/storages'
        self.register('GET', path, body={'file_storages': []})
        self.register('POST', path, body={})
        mount = engine.FileMount(
            'mount-id', '/etc/site.conf', 'private', 'web')
        self.driver.set_file_mounts(self.ep, self.env, self.service, [mount])
        self.assertEqual('web-child',
                         self.http.last_request.json()['resource_uuid'])
        for name in (None, 'missing'):
            before = len(self.http.request_history)
            self.assertRaises(engine.DriverError, self.driver.set_file_mounts,
                              self.ep, self.env, self.service,
                              [dataclasses.replace(mount, resource_name=name)])
            requests = self.http.request_history[before:]
            self.assertEqual(['GET'], [r.method for r in requests])
        self.register('GET', '/services/app-uuid', body={
            'databases': [{'name': 'db', 'uuid': 'db-child'}]})
        self.driver.set_file_mounts(self.ep, self.env, self.service,
                                    [dataclasses.replace(
                                        mount, resource_name=None)])
        self.assertEqual('db-child',
                         self.http.last_request.json()['resource_uuid'])

    def test_file_driver_rejects_unsafe_paths_before_network(self):
        for path in ('relative', '/a/../b'):
            self.assertRaises(engine.DriverError, self.driver.set_file_mounts,
                              self.ep, self.env, self.service,
                              [engine.FileMount('id', path, 'private')])
        self.assertEqual([], self.http.request_history)

    def test_storage_fault_does_not_log_echoed_content(self):
        logs = self.useFixture(fixtures.FakeLogger(
            name='coast.drivers.coolify.client'))
        self.register('GET', '/applications/app-uuid/storages', status=422,
                      body={'message': 'Private file content',
                            'errors': {'content': ['Private file content']}})
        self.assertRaises(engine.DriverError, self.driver.set_file_mounts,
                          self.ep, self.env, self.service, [])
        self.assertNotIn('Private file content', logs.output)


class TemplateDriverTest(base.TestCase):
    def setUp(self):
        super().setUp()
        scope = mock.patch('coast.agent.namespace.management_namespace',
                           side_effect=lambda name: nullcontext())
        scope.start()
        self.addCleanup(scope.stop)
        self.http = requests_mock.Mocker()
        self.http.start()
        self.addCleanup(self.http.stop)
        self.driver = loader.load_driver('coolify_v4', self.conf)
        self.addCleanup(CoolifyTest.check_proxy_contract, self)
        self.ep = engine.EngineEndpoint(
            'https://controller.example.com/api/v1', 'cert', 'key', 'ca',
            'engine-secret')
        self.env = SimpleNamespace(
            id='stack', engine_version='4.4.3', engine_ref='stack-project')
        self.source = SimpleNamespace(type='template',
                                      template_id='wordpress-with-mariadb')
        self.service = SimpleNamespace(
            id='app', engine_ref=None, server_id='server',
            project_engine_ref='application-project',
            environment_engine_ref='stage-uuid',
            server_engine_ref='server-uuid')

    def test_offline_catalog_version_and_fresh_results(self):
        item = self.driver.get_template(
            None, self.env, 'wordpress-with-mariadb')
        item['required_env'].append('MUTATED')
        self.assertNotIn('MUTATED', self.driver.get_template(
            None, self.env, 'wordpress-with-mariadb')['required_env'])
        self.assertEqual([], self.http.request_history)
        self.assertRaises(engine.EngineNotFound, self.driver.get_template,
                          None, self.env, 'absent')
        for version in ('4.4.4', None, '../../escape'):
            self.env.engine_version = version
            self.assertRaises(engine.UnsupportedEngineVersion,
                              self.driver.list_templates, None, self.env)

    def test_pinned_compose_fails_closed_for_unknown_version_or_template(self):
        self.assertRaises(engine.EngineNotFound, catalog.compose,
                          self.env.engine_version, 'absent')
        self.assertRaises(engine.UnsupportedEngineVersion, catalog.compose,
                          '../../escape', self.source.template_id)

    def test_template_create_exact_request_and_no_generated_values(self):
        url = self.ep.base_url
        self.http.get(url + '/services', json=[])
        self.http.get(url + '/servers/server-uuid',
                      json={'uuid': 'server-uuid', 'settings': {}})
        self.http.post(url + '/services', json={
            'uuid': 'new-service', 'SERVICE_PASSWORD_DB': 'generated-secret'})
        self.service.destination_engine_ref = 'destination-uuid'
        with self.assertLogs('coast.drivers.coolify.client',
                             level='DEBUG') as logs:
            result = self.driver.create_app(
                self.ep, self.env, self.service, {
                    'type': 'template',
                    'template_id': 'wordpress-with-mariadb'}, None)
        self.assertEqual('new-service', result.engine_ref)
        self.assertEqual({}, result.extra)
        payload = self.http.last_request.json()
        self.assertEqual({
            'name': 'coast-app',
            'docker_compose_raw': payload['docker_compose_raw'],
            'project_uuid': 'application-project',
            'environment_uuid': 'stage-uuid', 'server_uuid': 'server-uuid',
            'instant_deploy': False, 'destination_uuid': 'destination-uuid'},
            payload)
        self.assertNotIn('type', payload)
        self.assertIn('WORDPRESS_DB_HOST=mariadb', base64.b64decode(
            payload['docker_compose_raw'], validate=True).decode())
        self.assertNotEqual('stack-project', payload['project_uuid'])
        self.assertNotIn('environment_name', payload)
        self.assertNotIn('generated-secret', str(logs.output))

    def test_template_create_when_live_bundle_has_removed_pinned_type(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' /
                              'template-type-rejection.json').read_text())
        self.http.get(self.ep.base_url + '/services', json=[])
        self.http.get(self.ep.base_url + '/servers/server-uuid',
                      json={'uuid': 'server-uuid', 'settings': {}})
        self.assertNotIn(self.source.template_id,
                         fixture['response']['valid_service_types'])

        def create(request, context):
            payload = request.json()
            if 'type' in payload:
                self.assertEqual(fixture['request'], payload)
                context.status_code = fixture['status']
                return fixture['response']
            self.assertNotIn('type', payload)
            raw = base64.b64decode(payload['docker_compose_raw'],
                                   validate=True).decode()
            self.assertIn('WORDPRESS_DB_HOST=mariadb', raw)
            self.assertIn('MYSQL_ROOT_PASSWORD=$SERVICE_PASSWORD_ROOT', raw)
            context.status_code = 201
            return {'uuid': 'new-service'}

        self.http.post(self.ep.base_url + '/services', json=create)
        for version in engine_versions.SUPPORTED_ENGINE_VERSIONS:
            self.env.engine_version = version
            before = len(self.http.request_history)
            result = self.driver.create_app(
                self.ep, self.env, self.service, self.source, None)
            self.assertEqual('new-service', result.engine_ref)
            self.assertEqual(['GET', 'GET', 'POST'], [
                r.method for r in self.http.request_history[before:]])

    def test_template_create_keeps_predefined_network_join(self):
        self.http.get(self.ep.base_url + '/services', json=[])
        self.http.get(self.ep.base_url + '/servers/server-uuid',
                      json={'uuid': 'server-uuid', 'settings': {}})
        self.http.post(self.ep.base_url + '/services',
                       json={'uuid': 'new-service'})
        cases = (('pgadmin', True), ('databasus', True),
                 ('redis-insight', True), ('wordpress-with-mariadb', False))
        for version in engine_versions.SUPPORTED_ENGINE_VERSIONS:
            self.env.engine_version = version
            for template_id, joined in cases:
                self.source.template_id = template_id
                self.driver.create_app(
                    self.ep, self.env, self.service, self.source, None)
                payload = self.http.last_request.json()
                self.assertNotIn('type', payload)
                self.assertEqual(
                    catalog.compose(version, template_id),
                    payload['docker_compose_raw'])
                self.assertIs(joined, payload.get(
                    'connect_to_docker_network', False), template_id)

    def test_adopts_existing_service_and_requires_explicit_parent_refs(self):
        self.http.get(self.ep.base_url + '/services', json=[
            {'name': 'coast-app', 'uuid': 'existing-service'}])
        result = self.driver.create_app(
            self.ep, self.env, self.service, self.source, None)
        self.assertEqual('existing-service', result.engine_ref)
        self.assertEqual(1, len(self.http.request_history))
        del self.service.environment_engine_ref
        self.assertRaises(engine.DriverError, self.driver.create_app,
                          self.ep, self.env, self.service, self.source, None)

    def test_missing_parent_refs_fail_before_http(self):
        for name in ('project_engine_ref', 'environment_engine_ref'):
            service = SimpleNamespace(
                id='app', engine_ref=None, server_id='server',
                project_engine_ref='application-project',
                environment_engine_ref='stage-uuid',
                server_engine_ref='server-uuid')
            delattr(service, name)
            self.assertRaises(
                engine.DriverError, self.driver.create_app,
                self.ep, self.env, service, self.source, None)
        self.assertEqual([], self.http.request_history)

    def test_unknown_template_rejected_before_http(self):
        self.source.template_id = 'absent'
        self.assertRaises(engine.EngineNotFound, self.driver.create_app,
                          self.ep, self.env, self.service, self.source, None)
        self.assertEqual([], self.http.request_history)

    def test_template_actions_and_delete_use_service_routes(self):
        url = self.ep.base_url + '/services/svc-uuid'
        self.service.template_id = 'uptime-kuma'
        self.service.engine_ref = 'svc-uuid'
        for action in ('start', 'stop', 'restart'):
            self.http.post(url + '/' + action, json={})
            getattr(self.driver, action + '_app')(
                self.ep, self.env, self.service)
            self.assertEqual(url + '/' + action, self.http.last_request.url)
        self.http.delete(url, json={})
        self.driver.delete_app(self.ep, self.env, self.service)
        self.assertEqual('DELETE', self.http.last_request.method)
        self.assertEqual(['false'],
                         self.http.last_request.qs['delete_volumes'])
        self.assertNotIn('/applications', str(
            [r.url for r in self.http.request_history]))
        self.assertNotIn('/security/keys', str(
            [r.url for r in self.http.request_history]))

    def test_template_delete_finds_unrecorded_service_by_name(self):
        self.service.template_id = 'uptime-kuma'
        self.http.get(self.ep.base_url + '/services', json=[
            {'name': 'coast-app', 'uuid': 'found-uuid'}])
        self.http.delete(self.ep.base_url + '/services/found-uuid', json={})
        self.driver.delete_app(self.ep, self.env, self.service)
        self.assertEqual('DELETE', self.http.last_request.method)
        self.http.get(self.ep.base_url + '/services', json=[])
        self.driver.delete_app(self.ep, self.env, self.service)
        self.assertEqual('GET', self.http.last_request.method)

    def test_template_deploy_starts_or_restarts_and_waits_for_running(self):
        url = self.ep.base_url + '/services/svc-uuid'
        self.service.template_id = 'uptime-kuma'
        self.service.engine_ref = 'svc-uuid'
        deployment = SimpleNamespace(engine_ref=None)
        self.http.get(url, json={'status': 'exited'})
        self.http.post(url + '/start', json={'message': 'queued'})
        result = self.driver.deploy_app(
            self.ep, self.env, self.service, deployment)
        self.assertEqual('svc-uuid', result.engine_ref)
        self.assertEqual(url + '/start', self.http.last_request.url)
        self.http.get(url, json={'status': 'running:healthy'})
        self.http.post(url + '/restart', json={'message': 'queued'})
        self.driver.deploy_app(self.ep, self.env, self.service, deployment)
        self.assertEqual(url + '/restart', self.http.last_request.url)
        # A recorded reference is never re-sent.
        count = len(self.http.request_history)
        self.driver.deploy_app(self.ep, self.env, self.service,
                               SimpleNamespace(engine_ref='svc-uuid'))
        self.assertEqual(count, len(self.http.request_history))
        deployment.engine_ref = 'svc-uuid'
        for status, expected in (
                ('exited', states.DEPLOYING), ('starting', states.DEPLOYING),
                ('running:unhealthy', states.DEPLOYING),
                ('degraded:unhealthy', states.DEPLOYING),
                ('running:healthy', states.ACTIVE),
                ('running:unknown', states.ACTIVE)):
            self.http.get(url, json={'status': status})
            with self.subTest(status=status):
                self.assertEqual(expected, self.driver.get_deployment_status(
                    self.ep, self.env, self.service, deployment).status)
        self.http.post(url + '/stop', json={})
        self.driver.cancel_deployment(
            self.ep, self.env, self.service, deployment)
        self.assertEqual(url + '/stop', self.http.last_request.url)
        self.assertRaises(engine.DriverError, self.driver.rollback,
                          self.ep, self.env, self.service, deployment,
                          deployment)
        self.assertNotIn('/applications', str(
            [r.url for r in self.http.request_history]))

    def test_compose_read_write_public_schema(self):
        fixture = json.loads((FIXTURES / 'compose-edit.json').read_text())
        self.service.engine_ref = 'svc-uuid'
        self.service.template_id = self.source.template_id
        path = self.ep.base_url + '/services/svc-uuid'
        self.http.get(path, json=fixture)
        patched = self.http.patch(path, json={'uuid': 'svc-uuid'})
        raw = self.driver.get_compose(self.ep, self.env, self.service)
        self.assertEqual(fixture['docker_compose_raw'], raw)
        self.driver.set_compose(self.ep, self.env, self.service, raw)
        self.assertEqual({'instant_deploy': False, 'docker_compose_raw':
                          base64.b64encode(raw.encode()).decode()},
                         patched.last_request.json())
        self.assertFalse(any(request.method == 'POST'
                             for request in self.http.request_history))
        self.http.get(path, json={'docker_compose_raw': None})
        self.assertRaises(engine.DriverError, self.driver.get_compose,
                          self.ep, self.env, self.service)

    def test_compose_git_application_fails_closed(self):
        self.assertRaises(engine.DriverError, self.driver.get_compose,
                          self.ep, self.env, self.service)
        self.assertRaises(engine.DriverError, self.driver.set_compose,
                          self.ep, self.env, self.service, 'services: {}')
        self.assertEqual([], self.http.request_history)

    def test_compose_rejection_does_not_log_echoed_content(self):
        self.service.engine_ref = 'svc-uuid'
        self.service.template_id = self.source.template_id
        path = self.ep.base_url + '/services/svc-uuid'
        self.http.patch(path, status_code=422,
                        json={'message': 'violet',
                              'errors': {'docker_compose_raw': ['violet']}})
        with self.assertLogs('coast.drivers.coolify.client',
                             level='DEBUG') as logs:
            self.assertRaises(engine.EngineConflict, self.driver.set_compose,
                              self.ep, self.env, self.service,
                              'services: {web: {image: violet}}')
        self.assertNotIn('violet', ' '.join(logs.output))
