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

"""Private registry credentials, guest files and garbage collection."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

import bcrypt
import fixtures

from coast.agent import registry as registry_gc
from coast.api import secret_store
from coast.common import exception
from coast.common import states
from coast.conductor import manager as manager_mod
from coast.conductor import registry
from coast.drivers import base as driver_base
from coast.tests import base


STACK = '11111111-1111-4111-8111-111111111111'
APP = '22222222-2222-4222-8222-222222222222'
REPO = STACK + '/' + APP


class RegistrySecretTest(base.TestCase):
    def test_secret_round_trip_and_htpasswd(self):
        saved = {}

        def call(method, context, payload, identity):
            self.assertEqual('coast:%s:registry:%s' % (STACK, STACK),
                             identity.name)
            self.assertEqual('put', method)
            saved['payload'] = payload
            return 'registry-ref'

        with mock.patch.object(secret_store, 'call', side_effect=call):
            reference = registry.create_secret(
                mock.sentinel.context,
                secret_store.SecretIdentity(STACK, 'registry', STACK))
        self.assertEqual('registry-ref', reference)
        payload = json.loads(saved['payload'])
        self.assertEqual({'username', 'password'}, set(payload))
        self.assertEqual('coast', payload['username'])
        self.assertLessEqual(len(payload['password'].encode()), 72)
        line = registry.htpasswd('coast', payload['password'])
        user, hashed = line.strip().split(':', 1)
        self.assertEqual('coast', user)
        self.assertTrue(hashed.startswith('$2b$'))
        self.assertTrue(bcrypt.checkpw(
            payload['password'].encode(), hashed.encode()))

    def test_bad_secret_does_not_echo_the_payload(self):
        secret = 'x' * 80
        raw = json.dumps({'username': 'coast', 'password': secret})
        with mock.patch.object(secret_store, 'call', return_value=raw):
            error = self.assertRaises(
                exception.Invalid, registry.load_secret, None, 'ref')
        self.assertNotIn(secret, str(error))
        with mock.patch.object(secret_store, 'call', return_value='['):
            error = self.assertRaises(
                exception.Invalid, registry.load_secret, None, 'ref')
        self.assertNotIn('[', str(error))

    def test_repository_name_is_two_lowercase_uuids(self):
        self.assertEqual(REPO, registry.repository(STACK, APP))
        upper = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        self.assertRaises(
            exception.Invalid, registry.repository, upper.upper(), APP)
        self.assertRaises(
            exception.Invalid, registry.repository, '../' + STACK, APP)

    def test_worker_receives_only_the_registry_ca(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ca = Path(directory.name) / 'ca.pem'
        ca.write_text('CA-MATERIAL\n')
        self.config.config(ca_certificate=str(ca), group='certificates')
        self.config.config(registry_port=5000, group='controller_worker')
        stack = SimpleNamespace(registry_secret_ref='ref')
        extra, files = registry.files_for(
            self.conf, stack, SimpleNamespace(role='worker'), '192.0.2.2')
        self.assertEqual('', extra)
        self.assertEqual(
            {'/etc/docker/certs.d/192.0.2.2:5000/ca.crt': 'CA-MATERIAL\n'},
            files)
        self.assertEqual(('', {}), registry.files_for(
            self.conf, SimpleNamespace(registry_secret_ref=None),
            SimpleNamespace(role='worker'), '192.0.2.2'))

    def test_attach_sets_the_per_stack_template(self):
        endpoint = driver_base.EngineEndpoint(
            'https://example.com/api/v1', '', '', '', token='engine-token')
        stack = SimpleNamespace(registry_secret_ref='ref', _context=None)
        controller = SimpleNamespace(private_ip='192.0.2.2')
        raw = json.dumps({'username': 'coast', 'password': 'registry-secret'})
        with mock.patch.object(secret_store, 'call', return_value=raw):
            updated = registry.attach(
                self.conf, endpoint, stack, controller)
        self.assertEqual(
            '192.0.2.2:5000/{environment_id}/{app_id}',
            updated.registry_image_template)
        self.assertEqual('192.0.2.2:5000', updated.registry_host)
        self.assertNotIn('registry-secret', repr(updated))
        self.assertIs(
            endpoint, registry.attach(
                self.conf, endpoint, SimpleNamespace(
                    registry_secret_ref=None), controller))


class RegistryGcTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.conf_path = root / 'coast.conf'
        self.data = root / 'data'
        self.data.mkdir()
        self.files = {}
        for name in ('config.yml', 'htpasswd', 'tls.crt', 'tls.key'):
            path = root / name
            path.write_text('material')
            self.files[name] = path
        self.image = 'registry:3@sha256:' + ('ab' * 32)
        self.conf_path.write_text('[registry]\nimage=' + self.image + '\n')
        self.repos = root / 'repositories'
        self.repos.mkdir()
        (root / 'blobs').mkdir()
        self.outside = root / 'outside'
        self.outside.mkdir()
        (self.outside / 'keep').write_text('safe')
        patcher = mock.patch.multiple(
            registry_gc, CONF=self.conf_path, DATA=self.data,
            CONFIG=self.files['config.yml'], HTPASSWD=self.files['htpasswd'],
            CERT=self.files['tls.crt'], KEY=self.files['tls.key'])
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self, fail_at=None):
        calls = []

        def run(args):
            calls.append(args)
            if fail_at and fail_at in args:
                return None
            return b''

        return calls, run

    def test_empty_list_still_collects(self):
        calls, run = self.runner()
        result = registry_gc.garbage_collect(
            {'repositories': []}, run, self.repos)
        self.assertEqual({'collected': 0}, result)
        self.assertEqual(
            ['inspect', 'stop', 'run', 'start'],
            [args[1] for args in calls])
        self.assertIn('--pull', calls[2])
        self.assertIn('never', calls[2])
        self.assertIn('garbage-collect', calls[2])
        self.assertIn('registry:3', calls[2])
        self.assertNotIn(self.image, calls[2])

    def test_never_pushed_registry_skips_collection(self):
        # distribution garbage-collect exits 1 when repositories or blobs
        # is missing (live DevStack m2rb), so an empty registry is a no-op.
        for missing in ('repositories', 'blobs'):
            with self.subTest(missing=missing):
                path = self.repos.parent / missing
                path.rmdir()
                calls, run = self.runner()
                result = registry_gc.garbage_collect(
                    {'repositories': [REPO]}, run, self.repos)
                self.assertEqual({'collected': 0}, result)
                self.assertEqual(['inspect'], [args[1] for args in calls])
                path.mkdir()

    def test_named_repository_is_removed(self):
        target = self.repos / STACK / APP
        target.mkdir(parents=True)
        (target / 'manifest').write_text('blob')
        calls, run = self.runner()
        result = registry_gc.garbage_collect(
            {'repositories': [REPO]}, run, self.repos)
        self.assertEqual({'collected': 1}, result)
        self.assertFalse(target.exists())
        self.assertEqual('start', calls[-1][1])

    def test_bad_names_and_symlinks_are_rejected(self):
        calls, run = self.runner()
        self.assertRaises(
            ValueError, registry_gc.garbage_collect,
            {'repositories': ['../etc']}, run, self.repos)
        self.assertEqual([], calls)
        (self.repos / STACK).symlink_to(self.outside)
        calls, run = self.runner()
        self.assertRaises(
            ValueError, registry_gc.garbage_collect,
            {'repositories': [REPO]}, run, self.repos)
        self.assertEqual('safe', (self.outside / 'keep').read_text())
        self.assertEqual('start', calls[-1][1])
        self.assertNotIn('run', [args[1] for args in calls])

    def test_missing_container_fails_closed(self):
        calls, run = self.runner(fail_at='inspect')
        self.assertRaises(
            registry_gc.RegistryError, registry_gc.garbage_collect,
            {'repositories': []}, run, self.repos)
        self.assertEqual(['inspect'], [args[1] for args in calls])

    def test_collection_failure_restarts_and_keeps_the_error(self):
        calls, run = self.runner(fail_at='garbage-collect')
        self.assertRaises(
            registry_gc.RegistryError, registry_gc.garbage_collect,
            {'repositories': []}, run, self.repos)
        self.assertEqual('start', calls[-1][1])

    def test_unpinned_image_does_not_run_docker(self):
        self.conf_path.write_text('[registry]\nimage=registry:latest\n')
        calls, run = self.runner()
        self.assertRaises(
            registry_gc.RegistryError, registry_gc.garbage_collect,
            {'repositories': []}, run, self.repos)
        self.assertEqual([], calls)


class RegistryConductorTest(base.TestCase):
    def test_service_delete_collects_one_repository(self):
        service = mock.Mock(engine_ref=None, task_state='NONE')
        service.id = APP
        env = SimpleNamespace(id=STACK, status=states.ACTIVE)
        ctx = mock.sentinel.context
        endpoint = mock.Mock()
        endpoint._committed.return_value = False
        endpoint._delete_engine_cleanup.side_effect = (
            manager_mod.Manager._delete_engine_cleanup)
        endpoint._delete_service_engine.side_effect = lambda *args: (
            manager_mod.Manager._delete_service_engine(endpoint, *args))
        with mock.patch.object(
                manager_mod.objects.Source, 'get_by_id',
                return_value=SimpleNamespace(type='image')), \
                mock.patch.object(
                    manager_mod.objects.DomainList, 'get_all',
                    return_value=[]), \
                mock.patch.object(
                    manager_mod.app_secrets, 'references', return_value=[]), \
                mock.patch.object(
                    manager_mod.objects.DeploymentList, 'get_all',
                    return_value=[]), \
                mock.patch.object(
                    manager_mod.storage_rules, 'drop_service_storages'
                ) as drop, mock.patch.object(
                    manager_mod.file_mounts, 'drop'), mock.patch.object(
                        manager_mod.backups, 'drop_service'), \
                mock.patch.object(
                    manager_mod.managed_dns, 'delete_service'):
            manager_mod.Manager._delete_service(
                endpoint, ctx, service, env, mock.Mock(), {}, None)
        drop.assert_called_once_with(ctx, service)
        endpoint.collect_registry.assert_called_once_with(
            ctx, env, [REPO], strict=True)
        endpoint._endpoint.assert_called_once_with(env)

    def test_timer_sends_an_empty_repository_list(self):
        listed = SimpleNamespace(
            id=STACK, project_id='project', registry_secret_ref='ref')
        env = SimpleNamespace(
            id=STACK, project_id='project', status=states.ACTIVE,
            registry_secret_ref='ref')
        endpoint = mock.Mock()
        with mock.patch.object(
                manager_mod.objects.StackList, 'get_all',
                side_effect=[[listed], []]), \
                mock.patch.object(
                    manager_mod.objects.Stack, 'get_by_id', return_value=env):
            manager_mod.Manager.collect_registries(endpoint)
        endpoint.collect_registry.assert_called_once_with(
            mock.ANY, env, [], strict=False)

    def test_timer_retries_unreachable_engine_at_warning(self):
        env = SimpleNamespace(
            id=STACK, project_id='project', status=states.ACTIVE,
            registry_secret_ref='ref')
        endpoint = mock.Mock()
        endpoint.collect_registry.side_effect = (
            manager_mod.base.EngineUnreachable())
        with mock.patch.object(
                manager_mod.objects.StackList, 'get_all',
                side_effect=[[env], []]), mock.patch.object(
                    manager_mod.objects.Stack, 'get_by_id',
                    return_value=env), mock.patch.object(
                        manager_mod, 'LOG') as log:
            manager_mod.Manager.collect_registries(endpoint)
        log.warning.assert_called_once()
        log.error.assert_not_called()

    def test_collect_posts_to_the_controller_agent(self):
        env = SimpleNamespace(
            id=STACK, driver='coolify_v4', registry_secret_ref='ref',
            _context=mock.Mock())
        server = SimpleNamespace(
            kind='vm', status=states.ACTIVE, mgmt_ip='192.0.2.10')
        endpoint = mock.Mock()
        endpoint.conf = self.conf
        with mock.patch.object(
                manager_mod.objects.ServerList, 'get_all',
                return_value=[server]), \
                mock.patch(
                    'coast.conductor.agent_client.AgentClient') as client:
            manager_mod.Manager.collect_registry(
                endpoint, mock.sentinel.context, env, [REPO], True)
        client.assert_called_once_with(self.conf, '192.0.2.10')
        client.return_value.request.assert_called_once_with(
            'POST', '/v1/registry/gc', {'repositories': [REPO]})

    def test_noop_and_missing_controller_do_not_hide_strict_failure(self):
        env = SimpleNamespace(
            id=STACK, driver='noop', registry_secret_ref='ref')
        endpoint = mock.Mock()
        with mock.patch.object(
                manager_mod.objects.ServerList, 'get_all') as servers:
            manager_mod.Manager.collect_registry(
                endpoint, mock.sentinel.context, env, [], True)
        servers.assert_not_called()
        env.driver = 'coolify_v4'
        logs = self.useFixture(fixtures.FakeLogger())
        with mock.patch.object(
                manager_mod.objects.ServerList, 'get_all', return_value=[]):
            manager_mod.Manager.collect_registry(
                endpoint, mock.sentinel.context, env, [], False)
            self.assertRaises(
                driver_base.DriverError, manager_mod.Manager.collect_registry,
                endpoint, mock.sentinel.context, env, [REPO], True)
        self.assertIn('Registry sweep skipped', logs.output)
        self.assertIn(STACK, logs.output)
