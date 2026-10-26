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

"""Pinned comparison contracts; no automatic restart or task takeover."""

import os
from types import SimpleNamespace
import uuid

from coast.common import states
from coast.drivers import base as engine
from coast.drivers.coolify import v4
from coast.tests import base
from coast.tests.contract.coolify import test_live
from coast.tests.contract.coolify.version import EXPECTED_VERSION


class ComparisonContractTest(test_live.LiveCoolifyBase):
    def setUp(self):
        super().setUp()
        # Compose responses may contain generated values. Never record here.
        self.driver.recorder = None

    def test_git_comparison(self):
        self.check_application('git')

    def test_image_comparison(self):
        self.check_application('image')

    def test_compose_comparison(self):
        self.check_application('compose')

    def check_application(self, kind):
        if kind == 'image':
            source = SimpleNamespace(type='image', image='nginx:alpine',
                                     credentials_ref=None)
        else:
            source = SimpleNamespace(
                type='git', repo_url=os.environ.get(
                    'COAST_CONTRACT_COOLIFY_GIT_URL',
                    'https://github.com/coollabsio/coolify-examples'),
                branch='main', build_pack=(
                    'compose' if kind == 'compose' else 'nixpacks'),
                base_directory=(
                    '/internal/compose-parser-tests' if kind == 'compose'
                    else '/javascript/runtimes/nodejs'),
                compose_path=(
                    '/docker-compose-simple.yaml' if kind == 'compose'
                    else None), dockerfile_path=None, credentials_ref=None)
        self.prepare_source(source)
        app = SimpleNamespace(
            id=uuid.uuid4().hex, engine_ref=None,
            server_id=self.servers[0].id,
            server_engine_ref=self.servers[0].engine_ref,
            description='Settings comparison contract',
            ports='3000' if kind == 'git' else '80', replicas=1,
            auto_deploy=False, cpu_limit=1, memory_limit_mb=512,
            healthcheck_path=None)
        self.bind_parent_refs(app)
        self.addCleanup(self.driver.delete_app, self.ep, self.env, app)
        app.engine_ref = self.driver.create_app(
            self.ep, self.env, app, source, None).engine_ref
        path = '/applications/' + v4.ref(app.engine_ref)
        if kind == 'compose':
            # Deployment loads the repo compose file through existing APIs.
            self.deploy(app, source)
            # Test-owned fixture has named api/analytics containers. Seed one
            # known target; production must not invent this selection.
            self.api('PATCH', path, data={'docker_compose_domains': [
                {'name': 'api', 'domain': ''}]})

        def compare(variables, domains):
            return self.driver.compare_settings(
                self.ep, self.env, app, variables, domains)

        # Runtime variables are managed; unmarked engine defaults survive.
        self.driver.set_env(self.ep, self.env, app, {})
        self.driver.set_domains(self.ep, self.env, app, [])
        self.assertEqual({'env': False, 'domains': False}, compare({}, []))
        self.api('POST', path + '/envs', data={
            'key': 'COAST_CONTRACT', 'value': 'nonsecret',
            'comment': 'coast-managed'})
        self.assertTrue(compare({}, [])['env'])
        desired = {'COAST_CONTRACT': engine.EnvVar('nonsecret')}
        self.driver.set_env(self.ep, self.env, app, desired)
        self.assertFalse(compare(desired, [])['env'])
        self.assertTrue(compare({}, [])['env'])
        self.driver.set_env(self.ep, self.env, app, {})
        self.assertFalse(compare({}, [])['env'])

        domain = app.id + '.example.com'
        self.driver.set_domains(self.ep, self.env, app, [domain])
        self.assertFalse(compare({}, [domain.upper()])['domains'])
        self.assertTrue(compare({}, [])['domains'])
        self.assertTrue(compare({}, ['other.example.com'])['domains'])
        # API port normalization is not a comparison test: observe HTTP
        # drift live, and check strict port/path comparison offline.
        value = 'http://' + domain
        data = ({'docker_compose_domains': [{'name': 'api', 'domain': value}]}
                if kind == 'compose' else {'domains': value})
        self.api('PATCH', path, data=data)
        self.assertTrue(compare({}, [domain])['domains'])
        self.driver.set_domains(self.ep, self.env, app, [])
        self.assertEqual({'env': False, 'domains': False}, compare({}, []))

        # Lifecycle actions belong to test setup, never comparison itself.
        if kind != 'compose':
            self.deploy(app, source)
        self.assertEqual(states.ACTIVE, self.driver.get_app_status(
            self.ep, self.env, app).status)
        self.driver.stop_app(self.ep, self.env, app)
        self.wait(lambda: self.driver.get_app_status(
            self.ep, self.env, app), states.STOPPED)
        for _ in range(2):
            compare({}, [])
            self.assertEqual(states.STOPPED, self.driver.get_app_status(
                self.ep, self.env, app).status)


class TemplateComparisonContractTest(base.TestCase):
    def test_read_only_template_comparison(self):
        prefix = 'COAST_CONTRACT_COOLIFY_'
        values = {key: os.environ.get(prefix + key)
                  for key in ('URL', 'TOKEN_FILE', 'TEMPLATE_UUID')}
        if not all(values.values()):
            self.skipTest('Read-only template contract settings not supplied')
        driver = test_live.ContractDriver(self.conf)
        ep = test_live.endpoint(self.conf, values)
        self.assertEqual(EXPECTED_VERSION, driver.get_engine_version(ep))
        app = SimpleNamespace(engine_ref=values['TEMPLATE_UUID'],
                              template_id='wordpress-with-mariadb')
        path = '/services/' + v4.ref(app.engine_ref)
        # Existing disposable template: GET only, no creation or cleanup.
        with driver._client(ep) as transport:
            envs = transport.request('GET', path + '/envs')
            row = transport.request('GET', path)
        stack = SimpleNamespace()
        drift = driver.compare_settings(ep, stack, app, {}, [])
        self.assertEqual(any(r.get('comment') == 'coast-managed'
                             for r in envs), drift['env'])
        targets = row['applications']
        self.assertTrue(targets, 'Template must contain an application')
        self.assertEqual(any(r.get('fqdn') for r in targets), drift['domains'])
        # A fresh desired key cannot match; do not write it to the template.
        missing = 'COAST_CONTRACT_' + uuid.uuid4().hex.upper()
        self.assertTrue(driver.compare_settings(
            ep, stack, app, {missing: engine.EnvVar('nonsecret')}, [])['env'])
        status, _, health = row.get('status', '').partition(':')
        expected = {'running': states.ACTIVE, 'exited': states.STOPPED,
                    'stopped': states.STOPPED, 'starting': states.DEPLOYING,
                    'restarting': states.DEPLOYING}.get(status, states.ERROR)
        if status == 'running' and health == 'unhealthy':
            expected = states.ERROR
        self.assertEqual(expected,
                         driver.get_app_status(ep, stack, app).status)
