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

"""API -> no-op handoff with SQLite and fake secret storage, no RPC."""

from unittest import mock

from coast.api import handlers
from coast.common import states
from coast import objects
from coast.tests import app_api


class AppFlowTest(app_api.AppAPITest):
    def test_create_deploy_rollback_env_domain_and_delete(self):
        # No-op handlers accept work; explicitly simulate later completion.
        body = self.create_body(ports='8080', env={
            'PUBLIC': {'value': 'production'},
            'PASSWORD': {'value': 'secret-v1', 'secret': True}})
        created = self.request(
            'post', '/v1/services', body, status=202).json['service']
        path = '/v1/services/' + created['id']
        service = self.unlock(created['id'])
        old = objects.Deployment.get_by_id(
            self.ctx, created['deployment_id'])
        old.status = states.DEPLOYING
        old.save()
        old.status = states.ACTIVE
        old.save()
        service.status = states.ACTIVE
        service.current_deployment_id = old.id
        service.save()
        self.request('patch', path + '/env', {'env': {
            'PASSWORD': {'value': 'secret-v2'}}})
        with mock.patch.object(handlers.Handler, 'create_deployment') as seam:
            deployed = self.request('post', path + '/deployments',
                                    {'deployment': {'source_ref': 'release'}},
                                    status=202).json['deployment']
        # Prove persisted desired state rather than only checking a mock call.
        new = objects.Deployment.get_by_id(self.ctx, deployed['id'])
        self.assertEqual(new.id, seam.call_args.args[1].id)
        self.assertEqual('secret-v2', self.store.get(
            self.ctx, new.spec['env']['PASSWORD']['value_ref']))
        self.assertEqual('secret-v1', self.store.get(
            self.ctx, old.spec['env']['PASSWORD']['value_ref']))
        service = self.unlock(service.id)
        old.status = states.SUPERSEDED
        old.save()
        new.status = states.DEPLOYING
        new.save()
        new.status = states.ACTIVE
        new.save()
        service.current_deployment_id = new.id
        service.save()
        rolled = self.request(
            'post', path + '/deployments/' + old.id + '/action',
            {'rollback': {}}, status=202).json['deployment']
        restored = objects.Deployment.get_by_id(self.ctx, rolled['id'])
        self.assertEqual(old.spec, restored.spec)
        self.assertEqual(old.id, restored.rollback_of)
        self.unlock(service.id)
        domain = self.request('post', path + '/domains', {'domain': {
            'fqdn': 'flow.example.com'}}, status=201).json['domain']
        self.request('post', path + '/domains/' + domain['id'] + '/action',
                     {'verify': {}}, status=202)
        self.assertEqual(states.PENDING_VERIFICATION, objects.Domain.get_by_id(
            self.ctx, domain['id']).status)
        self.unlock(service.id)
        env = self.request('get', path + '/env').json['env']
        self.assertIsNone(env['PASSWORD']['value'])
        self.assertEqual('production', env['PUBLIC']['value'])
        self.request('delete', path, status=202)
        self.assertEqual(states.DELETING, objects.Service.get_by_id(
            self.ctx, service.id).status)
