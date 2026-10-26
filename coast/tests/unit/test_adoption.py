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

import io
from types import SimpleNamespace
from unittest import mock

import fixtures
from oslo_config import cfg
from stevedore import extension

from coast.api import secret_store
from coast.cmd import manage
from coast.common import config
from coast.common import context
from coast.common import exception
from coast.common import states
from coast.conductor import manager
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast.drivers import base as driver_base
from coast.drivers import noop
from coast import objects
from coast.tests import base


class FakeDriver(noop.NoopDriver):
    SUPPORTED_ENGINE_VERSIONS = ('4.4.3', '4.4.7')


class AdoptionTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        self.useFixture(fixtures.MonkeyPatch(
            'coast.common.quota.cfg.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(project_id='project')
        self.args = SimpleNamespace(
            project_id='project', name='existing',
            url='https://engine.example.com',
            token_ref='https://secrets.example.com/v1/secrets/opaque',
            server_ip='192.0.2.1', server_role='worker',
            engine_ref='engine-project', server_engine_ref='engine-worker')

    def test_atomic_adoption_and_isolation(self):
        env_id, line = manage.adopt_stack(self.conf, self.args)
        env = objects.Stack.get_by_id(self.ctx, env_id)
        self.assertEqual(states.ACTIVE, env.status)
        self.assertEqual(self.args.token_ref, env.engine_token_ref)
        self.assertEqual('engine-project', env.engine_ref)
        self.assertNotIn(self.args.url, str(env.obj_to_primitive()))
        self.assertEqual('adopted_endpoints = %s:%s' % (
            env_id, self.args.url), line)
        self.assertNotIn(self.args.token_ref, line)
        servers = objects.ServerList.get_all(self.ctx)
        self.assertEqual({'control', 'worker'}, {s.role for s in servers})
        self.assertTrue(all(s.status == states.ACTIVE for s in servers))
        with api.reader.using(self.ctx):
            usage = self.ctx.session.query(models.QuotaUsage).one()
            self.assertEqual(2, usage.in_use_servers)
            self.assertEqual(1, usage.in_use_environments)
            self.assertEqual(0, usage.reserved_servers)

    def test_wrong_project_and_duplicate(self):
        env_id, _ = manage.adopt_stack(self.conf, self.args)
        other = context.RequestContext(project_id='other')
        self.assertRaises(exception.NotFound,
                          objects.Stack.get_by_id, other, env_id)
        self.assertRaises(exception.CoastException,
                          manage.adopt_stack, self.conf, self.args)
        self.assertEqual(1, len(objects.StackList.get_all(self.ctx)))

    def test_rollback_on_server_error(self):
        with mock.patch.object(objects.Server, 'create',
                               side_effect=RuntimeError('secret')):
            self.assertRaises(RuntimeError, manage.adopt_stack,
                              self.conf, self.args)
        self.assertEqual([], list(objects.StackList.get_all(self.ctx)))
        self.assertEqual([], list(objects.ServerList.get_all(self.ctx)))

    def test_invalid_inputs_and_quota_leave_no_rows(self):
        for url in ('http://example.com', 'https://user:secret@example.com',
                    'https://example.com?token=secret',
                    'https://example.com/#secret', 'https://example.com,foo'):
            self.args.url = url
            self.assertRaises(driver_base.DriverError,
                              manage.adopt_stack, self.conf, self.args)
        self.args.url = 'https://engine.example.com'
        self.args.token_ref = 'raw-token'
        self.assertRaises(driver_base.DriverError, manage.adopt_stack,
                          self.conf, self.args)
        self.args.token_ref = 'https://secrets.example.com/opaque'
        self.args.server_role = 'build'
        self.config.config(build_servers=0, group='quotas')
        self.assertRaises(exception.OverQuota, manage.adopt_stack,
                          self.conf, self.args)
        self.assertEqual([], list(objects.StackList.get_all(self.ctx)))

    def configured(self):
        env_id, _ = manage.adopt_stack(self.conf, self.args)
        self.config.config(enabled_drivers=['fake'], group='controller_worker')
        self.config.config(insecure_direct_url=True,
                           adopted_endpoints={env_id: self.args.url},
                           group='coolify')
        env = objects.Stack.get_by_id(self.ctx, env_id)
        env.driver = 'fake'
        return manager.Manager(self.conf), env

    def _placed_service(self, env, server, name):
        source = objects.Source(
            context=self.ctx, type='git',
            repo_url='https://example.com/service.git')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=env.id, name=name,
            source_id=source.id, server_id=server.id, ports='3000',
            replicas=1, auto_deploy=False, status=states.PENDING,
            task_state=states.NONE)
        service.create()
        return service

    def _create_with_spy(self, mgr, env, service):
        seen = {}

        class Spy(FakeDriver):
            def create_app(self, ep, env, service, source, source_credentials):
                seen['ref'] = service.server_engine_ref
                return super().create_app(
                    ep, env, service, source, source_credentials)

        store = mock.Mock()
        store.get.return_value = 'plaintext-token'
        with mock.patch.object(secret_store, 'STORE', store):
            mgr._create_service(self.ctx, service, env, Spy(self.conf), {}, [])
        fresh = objects.Service.get_by_id(self.ctx, service.id)
        self.assertNotIn('server_engine_ref', fresh.fields)
        return seen['ref']

    def test_create_service_passes_recorded_server_ref(self):
        mgr, env = self.configured()
        worker = next(row for row in objects.ServerList.get_all(self.ctx)
                      if row.role == 'worker')
        service = self._placed_service(env, worker, 'adopted')
        self.assertEqual(
            'engine-worker', self._create_with_spy(mgr, env, service))

    def test_create_service_passes_absent_server_ref(self):
        self.args.server_engine_ref = None
        mgr, env = self.configured()
        worker = next(row for row in objects.ServerList.get_all(self.ctx)
                      if row.role == 'worker')
        self.assertIsNone(worker.engine_ref)
        service = self._placed_service(env, worker, 'registered')
        self.assertIsNone(self._create_with_spy(mgr, env, service))

    def test_registered_fake_driver_and_secret_endpoint(self):
        mgr, env = self.configured()
        ep = mock.Mock(name='fake')
        ep.name = 'fake'
        ep.load.return_value = FakeDriver
        store = mock.Mock()
        store.get.return_value = 'plaintext-token'
        with mock.patch.object(extension.ExtensionManager, 'list_entry_points',
                               return_value=[ep]), mock.patch.object(
                                   secret_store, 'STORE', store):
            drv = mgr._driver(env)
            self.assertIsInstance(drv, FakeDriver)
            endpoint = mgr._endpoint(env)
            self.assertEqual(self.args.url, endpoint.base_url)
            self.assertEqual('plaintext-token', endpoint.token)
            self.assertTrue(endpoint.ca_cert)
            with mock.patch('ssl.get_default_verify_paths') as paths:
                paths.return_value.cafile = None
                paths.return_value.capath = None
                self.assertRaises(driver_base.DriverError, mgr._endpoint, env)
            self.assertNotIn('plaintext-token', repr(endpoint))
            store.get.assert_called_with(env._context, self.args.token_ref)

    def test_fail_closed(self):
        mgr, env = self.configured()
        self.assertRaises(exception.CoastException, mgr._driver, env)
        env.engine_token_ref = None
        self.assertRaises(driver_base.EngineAuthError, mgr._driver, env)
        env.engine_token_ref = self.args.token_ref
        self.config.config(insecure_direct_url=False, group='coolify')
        self.assertRaises(driver_base.DriverError, mgr._driver, env)
        self.config.config(insecure_direct_url=True,
                           adopted_endpoints={env.id: 'http://example.com'},
                           group='coolify')
        self.assertRaises(driver_base.DriverError, mgr._driver, env)
        self.config.config(adopted_endpoints={}, group='coolify')
        self.assertRaises(driver_base.DriverError, mgr._driver, env)
        self.config.config(enabled_drivers=[], group='controller_worker')
        self.assertRaises(driver_base.DriverError, mgr._driver, env)

    def test_unknown_plugin_version_and_empty_secret_fail_closed(self):
        mgr, env = self.configured()
        store = mock.Mock()
        store.get.return_value = 'token'
        plugin = mock.Mock()
        plugin.name = 'fake'
        plugin.load.return_value = FakeDriver
        with mock.patch.object(
                secret_store, 'STORE', store), mock.patch.object(
                extension.ExtensionManager, 'list_entry_points',
                return_value=[]) as entry_points:
            self.assertRaises(driver_base.DriverError, mgr._driver, env)
            entry_points.return_value = [plugin]
            env.engine_version = 'unsupported'
            self.assertRaises(driver_base.UnsupportedEngineVersion,
                              mgr._driver, env)
            for token in (None, '', 123):
                store.get.return_value = token
                self.assertRaises(driver_base.EngineAuthError,
                                  mgr._driver, env)
            store.get.side_effect = RuntimeError('plaintext-token')
            error = self.assertRaises(exception.CoastException,
                                      mgr._driver, env)
            self.assertNotIn('plaintext-token', str(error))

    def test_optional_server_and_config_round_trip(self):
        self.args.server_ip = None
        self.args.server_engine_ref = None
        env_id, _ = manage.adopt_stack(self.conf, self.args)
        self.assertEqual(['control'], [s.role for s in
                         objects.ServerList.get_all(self.ctx)])
        # Mapping text round-trips through oslo.config DictOpt syntax.
        conf_file = self.useFixture(fixtures.TempDir()).path + '/coast.conf'
        with open(conf_file, 'w') as stream:
            stream.write('[coolify]\nadopted_endpoints = %s:%s\n' % (
                env_id, self.args.url))
        conf = cfg.ConfigOpts()
        config.parse_args([], conf=conf, default_config_files=[conf_file])
        self.assertEqual({env_id: self.args.url},
                         conf.coolify.adopted_endpoints)

    def test_main_error_is_sanitized(self):
        with mock.patch.object(manage, 'CONF') as conf, mock.patch.object(
                manage.config, 'parse_args'), mock.patch.object(
                    manage.config, 'setup_logging'), mock.patch.object(
                        manage, 'adopt_stack',
                        side_effect=RuntimeError(self.args.token_ref)):
            conf.command.name = 'adopt-stack'
            error = self.assertRaises(SystemExit, manage.main)
            self.assertNotIn(self.args.token_ref, str(error))

    def test_main_output(self):
        output = io.StringIO()
        with mock.patch.object(manage, 'CONF') as conf, mock.patch.object(
                manage.config, 'parse_args'), mock.patch.object(
                    manage.config, 'setup_logging'), mock.patch.object(
                        manage, 'adopt_stack',
                        return_value=('id', 'adopted_endpoints = id:url')), \
                mock.patch('sys.stdout', output):
            conf.command.name = 'adopt-stack'
            self.assertEqual(0, manage.main())
        self.assertEqual('id\n[coolify]\nadopted_endpoints = id:url\n',
                         output.getvalue())
