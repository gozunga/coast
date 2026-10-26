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

"""Private Git creation and rotation through fake RPC and the noop driver."""

from unittest import mock

from coast.api import microversions
from coast.common import exception
from coast.common import secrets
from coast.common import states
from coast.drivers import noop
from coast import objects
from coast.tests import conductor


class PrivateGitFlowTest(conductor.ConductorTest):
    def create_private_app(self):
        self.headers['OpenStack-API-Version'] = (
            'app-hosting ' + microversions.DEPLOY_KEY_VERSION)
        path = self.stack()
        stack_id = path.rsplit('/', 1)[-1]
        worker = next(row for row in self.request(
            'get', path + '/servers').json['servers']
            if row['role'] == 'worker')
        created = self.request('post', '/v1/services', {'service': {
            'name': 'private-app', **self.service_parents(stack_id),
            'server_id': worker['id'], 'source': {
                'type': 'git', 'repo_url': 'git@example.com:org/repo.git',
                'deploy_key': {'generate': True}}}},
            status=202, complete=True).json['service']
        service = objects.Service.get_by_id(self.ctx, created['id'])
        source = objects.Source.get_by_id(self.ctx, service.source_id)
        return path, service, source

    def test_create_rotate_and_delete(self):
        incoming = []
        original = noop.NoopDriver.create_app

        def capture(driver, ep, env, app, source, credentials):
            incoming.append(credentials)
            return original(driver, ep, env, app, source, credentials)

        with mock.patch.object(noop.NoopDriver, 'create_app', capture):
            path, service, source = self.create_private_app()
        private = self.store.get(self.ctx, source.credentials_ref)
        self.assertEqual([private], incoming)
        self.assertEqual(states.STOPPED, service.status)
        self.assertFalse(objects.DeploymentList.get_all(
            self.ctx, filters={'service_id': service.id}))
        old_ref = source.credentials_ref
        old_public = source.deploy_public_key
        response = self.request('patch', '/v1/services/' + service.id, {
            'service': {'source': {'deploy_key': {'generate': True}}}})
        source = objects.Source.get_by_id(self.ctx, source.id)
        self.assertNotEqual(old_ref, source.credentials_ref)
        self.assertNotEqual(old_public, source.deploy_public_key)
        self.assertNotIn(old_ref, self.store.values)
        self.assertEqual(source.deploy_public_key,
                         response.json['service']['source'][
                             'deploy_key']['public_key'])
        self.request('delete', '/v1/services/' + service.id, status=202,
                     complete=True)
        self.assertNotIn(source.credentials_ref, self.store.values)

    def test_engine_failure_keeps_old_desired_key(self):
        path, service, source = self.create_private_app()
        driver = self.manager._driver(objects.Stack.get_by_id(
            self.ctx, service.stack_id))
        before = set(self.store.values)
        with mock.patch.object(driver, 'write_deploy_key',
                               side_effect=[RuntimeError('pasted-data'),
                                            None]):
            result = self.request('patch', '/v1/services/' + service.id, {
                'service': {'source': {'deploy_key': {'generate': True}}}},
                status=409)
        saved = objects.Source.get_by_id(self.ctx, source.id)
        self.assertEqual(source.credentials_ref, saved.credentials_ref)
        self.assertEqual(source.deploy_public_key, saved.deploy_public_key)
        self.assertEqual(before, set(self.store.values))
        self.assertNotIn('pasted-data', result.text)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, service.id).task_state)

    def offboarding_app(self):
        # A legacy offboarding workload has no live app group. Build that
        # historical fixture directly, then exercise the real DELETE route.
        path = self.stack()
        stack_id = path.rsplit('/', 1)[-1]
        worker = next(row for row in objects.ServerList.get_all(self.ctx)
                      if row.stack_id == stack_id and row.role == 'worker')
        source = objects.Source(context=self.ctx, type='git',
                                repo_url='git@example.com:org/repo.git')
        source.create()
        service = objects.Service(
            context=self.ctx, stack_id=stack_id, name='legacy-app',
            source_id=source.id, server_id=worker.id, ports='',
            status=states.STOPPED, task_state=states.NONE, auto_deploy=False)
        service.create()
        reference, public = secrets.store_deploy_key(
            self.ctx, service, {'generate': True})
        source.credentials_ref = reference
        source.deploy_public_key = public
        source.save()
        stack = objects.Stack.get_by_id(self.ctx, stack_id)
        driver = self.manager._driver(stack)
        result = driver.create_app(None, stack, service, source,
                                   self.store.get(self.ctx, reference))
        service.engine_ref = result.engine_ref
        service.save()
        key_ref = driver.deploy_keys[(service.stack_id, service.id)]
        self.assertIn(key_ref, driver.key_material)
        return path, service, source, driver, key_ref

    def assert_stack_credentials_deleted(self, path, service, source,
                                         driver, key_ref):
        self.request('get', path, status=404)
        self.assertNotIn(source.credentials_ref, self.store.values)
        self.assertNotIn(key_ref, driver.key_material)
        self.assertNotIn((service.stack_id, service.id), driver.deploy_keys)
        self.assertNotIn((service.stack_id, service.id), driver._apps)

    def test_stack_delete_removes_barbican_and_engine_key(self):
        data = self.offboarding_app()
        self.request('delete', data[0], status=202, complete=True)
        self.assert_stack_credentials_deleted(*data)

    def test_stack_delete_retries_engine_key_cleanup(self):
        data = self.offboarding_app()
        path, service, source, driver, key_ref = data
        with mock.patch.object(driver, 'delete_deploy_key',
                               side_effect=RuntimeError('private-data')):
            self.request('delete', path, status=202, complete=True)
        self.assertEqual(states.ERROR, self.request('get', path).json[
            'stack']['status'])
        self.assertIn(key_ref, driver.key_material)
        self.assertIn(source.credentials_ref, self.store.values)
        self.request('delete', path, status=202, complete=True)
        self.assert_stack_credentials_deleted(*data)

    def test_stack_delete_retries_barbican_cleanup(self):
        data = self.offboarding_app()
        path, service, source, driver, key_ref = data
        with mock.patch.object(self.store, 'delete',
                               side_effect=RuntimeError('private-data')):
            self.request('delete', path, status=202, complete=True)
        self.assertEqual(states.ERROR, self.request('get', path).json[
            'stack']['status'])
        self.assertNotIn(key_ref, driver.key_material)
        self.assertIn(source.credentials_ref, self.store.values)
        self.request('delete', path, status=202, complete=True)
        self.assert_stack_credentials_deleted(*data)

    def test_unowned_rpc_cannot_delete_secret(self):
        path, service, source = self.create_private_app()
        service.acquire_task(states.DEPLOYING)
        reference, public = secrets.store_deploy_key(
            self.ctx, service, {'generate': True})
        self.assertRaises(exception.CoastException,
                          self.manager.rotate_deploy_key,
                          self.ctx, service.id, reference, public)
        self.assertIn(reference, self.store.values)
        self.assertIn(source.credentials_ref, self.store.values)
