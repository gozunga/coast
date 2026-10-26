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

import inspect
import io
import logging
from types import SimpleNamespace
from unittest import mock

from stevedore import extension

from coast.common import states
from coast.drivers import base as driver_base
from coast.drivers import loader
from coast.drivers import noop
from coast.tests import base
from coast.tests import driver_conformance


class NoopConformanceTest(driver_conformance.DriverConformanceMixin,
                          base.TestCase):
    def setUp(self):
        super().setUp()
        self.ssh_private_key = 'test-key'
        self.driver = loader.load_driver('noop', self.conf)
        self.ep = driver_base.EngineEndpoint(
            'https://controller.example.com/engine', 'cert', 'key', 'ca',
            'test-token')
        for name in ('env', 'server', 'service', 'source', 'deployment',
                     'new_deployment'):
            setattr(self, name, SimpleNamespace(id=name, engine_ref=None))

    def test_noop_compose_roundtrip_and_isolation(self):
        d, ep = self.driver, self.ep
        d.create_app(ep, self.env, self.service, self.source, None)
        self.assertIn('services:', d.get_compose(ep, self.env, self.service))
        content = 'services: {web: {image: nginx}}'
        d.set_compose(ep, self.env, self.service, content)
        self.assertEqual(content, d.get_compose(ep, self.env, self.service))
        self.assertRaises(driver_base.EngineNotFound, d.get_compose,
                          ep, SimpleNamespace(id='foreign'), self.service)

    def test_noop_settings_drift_and_isolation(self):
        d, ep = self.driver, self.ep
        variables = {'KEY': driver_base.EnvVar('secret', is_secret=True)}
        d.create_app(ep, self.env, self.service, self.source, None)
        self.assertEqual({'env': True, 'domains': True}, d.compare_settings(
            ep, self.env, self.service, variables, ['site.example.com']))
        d.set_env(ep, self.env, self.service, variables)
        d.set_domains(ep, self.env, self.service, ['SITE.example.com'])
        self.assertEqual({'env': False, 'domains': False}, d.compare_settings(
            ep, self.env, self.service, variables, ['site.example.com']))
        self.assertNotIn('secret', repr(d._settings))
        variables['KEY'] = driver_base.EnvVar('changed', is_secret=True)
        self.assertTrue(d.compare_settings(
            ep, self.env, self.service, variables, [])['env'])
        self.assertRaises(driver_base.EngineNotFound, d.compare_settings,
                          ep, SimpleNamespace(id='foreign'), self.service,
                          variables, [])

    def test_registered_drivers(self):
        manager = extension.ExtensionManager('coast.drivers')
        self.assertEqual({'noop', 'coolify_v4'}, set(manager.names()))
        for item in manager:
            self.assertTrue(issubclass(item.plugin, driver_base.CoastDriver))
            self.assertFalse(inspect.isabstract(item.plugin))
            for name in driver_base.CoastDriver.__abstractmethods__:
                expected = inspect.signature(
                    getattr(driver_base.CoastDriver, name))
                actual = inspect.signature(getattr(item.plugin, name))
                self.assertEqual(list(expected.parameters),
                                 list(actual.parameters))
            self.assertTrue(item.plugin.SUPPORTED_ENGINE_VERSIONS)

    def test_noop_build_mode_is_idempotent_and_validates_before_write(self):
        self.driver.register_server(self.ep, self.env, self.server, 'key')
        self.driver.create_app(self.ep, self.env, self.service,
                               self.source, None)
        for enabled in (True, True, False, False):
            self.driver.set_build_server_mode(
                self.ep, self.env, [self.server], [self.service], enabled)
            self.assertEqual(enabled, self.driver._build_modes[self.env.id])
        missing = SimpleNamespace(id='missing', engine_ref=None)
        self.assertRaises(driver_base.EngineNotFound,
                          self.driver.set_build_server_mode,
                          self.ep, self.env, [self.server], [missing], True)
        self.assertFalse(self.driver._build_modes[self.env.id])

    def test_abc_cannot_be_instantiated(self):
        self.assertRaises(TypeError, driver_base.CoastDriver, self.conf)

    def test_load_failure_is_sanitized(self):
        self.assertRaises(driver_base.DriverError,
                          loader.load_driver, 'missing', self.conf)
        with mock.patch.object(loader.driver, 'DriverManager',
                               side_effect=RuntimeError('test-token')):
            error = self.assertRaises(driver_base.DriverError,
                                      loader.load_driver, 'noop', self.conf)
        self.assertNotIn('test-token', str(error))

    def test_invalid_plugins_are_rejected(self):
        with mock.patch.object(loader.driver, 'DriverManager') as manager:
            manager.return_value.driver = object()
            self.assertRaises(driver_base.DriverError,
                              loader.load_driver, 'invalid', self.conf)
            manager.return_value.driver = noop.NoopDriver(self.conf)
            manager.return_value.driver.SUPPORTED_ENGINE_VERSIONS = ()
            self.assertRaises(driver_base.UnsupportedEngineVersion,
                              loader.load_driver, 'invalid', self.conf)

    def test_secret_values_have_no_repr(self):
        self.assertNotIn('test-token', repr(self.ep))
        self.assertNotIn('test-value', repr(driver_base.EnvVar('test-value')))

    def test_noop_accepts_template_source(self):
        source = {'type': 'template',
                  'template_id': 'wordpress-with-mariadb'}
        result = self.driver.create_app(
            self.ep, self.env, self.service, source, None)
        self.assertEqual('noop-app-%s' % self.service.id, result.engine_ref)
        self.assertEqual(states.STOPPED, self.driver.get_app_status(
            self.ep, self.env, self.service).status)

    def test_bootstrap_disables_builtin_terminal(self):
        self.driver.bootstrap_environment(self.ep, self.env)
        self.assertIs(False, self.driver.terminal_enabled[self.env.id])

    def test_refs_survive_fresh_driver(self):
        result = self.driver.bootstrap_environment(self.ep, self.env)
        fresh = noop.NoopDriver(self.conf)
        self.assertEqual(
            result, fresh.bootstrap_environment(self.ep, self.env))
        self.env.engine_ref = 'existing-ref'
        self.assertEqual('existing-ref', self.driver.bootstrap_environment(
            self.ep, self.env).engine_ref)

    def test_stack_and_instance_isolation(self):
        d, ep = self.driver, self.ep
        other = SimpleNamespace(id='other', engine_ref=None)
        d.create_app(ep, self.env, self.service, self.source, None)
        self.assertRaises(driver_base.EngineNotFound,
                          d.get_app_status, ep, other, self.service)
        d.create_app(ep, other, self.service, self.source, None)
        d.start_app(ep, self.env, self.service)
        self.assertEqual(states.STOPPED,
                         d.get_app_status(ep, other, self.service).status)
        d.delete_stack_project(ep, self.env)
        self.assertRaises(driver_base.EngineNotFound,
                          d.get_app_status, ep, self.env, self.service)
        self.assertEqual(states.STOPPED,
                         d.get_app_status(ep, other, self.service).status)
        self.assertRaises(driver_base.EngineNotFound,
                          noop.NoopDriver(self.conf).get_app_status,
                          ep, other, self.service)

    def test_project_and_environment_are_idempotent(self):
        d, ep = self.driver, self.ep
        stack = SimpleNamespace(id='stack')
        app = SimpleNamespace(id='application', engine_ref=None)
        environment = SimpleNamespace(id='coolify-env', engine_ref=None)
        first = d.ensure_project(ep, stack, app)
        again = SimpleNamespace(id='application', engine_ref=None)
        self.assertEqual(first.engine_ref,
                         d.ensure_project(ep, stack, again).engine_ref)
        app.engine_ref = first.engine_ref
        self.assertEqual(first, d.ensure_project(ep, stack, app))
        self.assertRaises(driver_base.EngineNotFound, d.ensure_project,
                          ep, stack, SimpleNamespace(
                              id='application', engine_ref='gone'))
        created = d.ensure_environment(ep, stack, app, environment)
        self.assertRaises(driver_base.DriverError, d.delete_project,
                          ep, stack, app)
        environment.engine_ref = created.engine_ref
        self.assertEqual(created, d.ensure_environment(
            ep, stack, app, environment))
        d.delete_environment(ep, stack, app, environment)
        d.delete_environment(ep, stack, app, environment)
        d.delete_project(ep, stack, app)
        d.delete_project(ep, stack, app)

    def test_completed_deployment_cancel_and_logs(self):
        d, ep, env = self.driver, self.ep, self.env
        d.create_app(ep, env, self.service, self.source, None)
        result = d.deploy_app(ep, env, self.service, self.deployment)
        self.assertEqual('0' * 40, result.extra['resolved_ref'])
        repeated = d.deploy_app(ep, env, self.service, self.deployment)
        self.assertEqual(result.extra, repeated.extra)
        d.cancel_deployment(ep, env, self.service, self.deployment)
        self.assertEqual(states.ACTIVE, d.get_deployment_status(
            ep, env, self.service, self.deployment).status)
        self.assertEqual([], list(d.get_deployment_log(
            ep, env, self.service, self.deployment, since='2026-10-09')))
        self.assertEqual(
            [], list(d.get_app_logs(ep, env, self.service, tail=1)))
        d.stop_app(ep, env, self.service)
        d.create_app(ep, env, self.service, self.source, None)
        self.assertEqual(
            states.STOPPED, d.get_app_status(ep, env, self.service).status)

    def test_real_plugin_failure_does_not_log_secret(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger('stevedore')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        entry_point = mock.Mock()
        entry_point.name = 'broken'
        entry_point.load.side_effect = RuntimeError('test-token')
        with mock.patch.object(extension.ExtensionManager, 'list_entry_points',
                               return_value=[entry_point]):
            error = self.assertRaises(driver_base.DriverError,
                                      loader.load_driver, 'broken', self.conf)
        self.assertNotIn('test-token', str(error))
        self.assertNotIn('test-token', output.getvalue())
