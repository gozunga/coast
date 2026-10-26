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

"""In-process WSGI/SQLite service controller test fixtures (no network)."""

import json

import uuid

import fixtures

from coast.api import microversions
from coast.api import secret_store
from coast.common import context
from coast.common import states
from coast.db import base_models
from coast import objects
from coast.tests.unit import test_api


class MemorySecretStore(secret_store.SecretStore):
    def __init__(self):
        self.values = {}
        self.reads = []

    def put(self, context, value, identity=None):
        if identity is not None:
            assert isinstance(identity, secret_store.SecretIdentity)
        ref = 'test-secret:' + str(uuid.uuid4())
        self.values[ref] = (context.project_id, value)
        return ref

    def get(self, context, reference):
        self.reads.append(reference)
        owner, value = self.values[reference]
        if owner != context.project_id:
            raise AssertionError('Secret owner mismatch')
        return value

    def delete(self, context, reference):
        owner, value = self.values[reference]
        if owner != context.project_id:
            raise AssertionError('Secret owner mismatch')
        del self.values[reference]


class AppAPITest(test_api.APITest):
    def setUp(self):
        super().setUp()
        self.config.config(definitions=json.dumps([
            dict(name='small', kind='vm',
                 roles=['control', 'worker', 'build'], flavor='small'),
            dict(name='worker', kind='vm', roles=['worker'], flavor='worker'),
            dict(name='metal', kind='baremetal', roles=['worker'],
                 flavor='metal')]), group='plans')
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.store = MemorySecretStore()
        self.useFixture(fixtures.MonkeyPatch(
            'coast.api.secret_store.STORE', self.store))
        self.headers = {'X-User-Id': 'user', 'X-Project-Id': 'project',
                        'X-Roles': 'reader,member,manager'}
        self.ctx = context.RequestContext(project_id='project')
        self.stack = objects.Stack(
            context=self.ctx, name='existing', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        self.stack.create()
        self.server = objects.Server(
            context=self.ctx, name='worker',
            stack_id=self.stack.id,
            kind='vm', role='worker', status=states.ACTIVE,
            task_state=states.NONE, floating_ip='203.0.113.25')
        self.server.create()
        self.application = objects.Appgroup(
            context=self.ctx, stack_id=self.stack.id, name='website',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='project-uuid')
        self.application.create()
        self.environment = objects.Environment(
            context=self.ctx, appgroup_id=self.application.id,
            name='production', status=states.ACTIVE, task_state=states.NONE,
            engine_ref='environment-uuid')
        self.environment.create()
        self.source = objects.Source(
            context=self.ctx, type='git',
            repo_url='https://example.com/web.git')
        self.source.create()
        self.service = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            environment_id=self.environment.id, name='web',
            source_id=self.source.id, server_id=self.server.id, ports='3000',
            replicas=1, auto_deploy=False, status=states.ACTIVE,
            task_state=states.NONE)
        self.service.create()
        self.path = '/v1/services/' + self.service.id
        self.deployment = objects.Deployment(
            context=self.ctx, service_id=self.service.id, source_ref='main',
            server_id=self.server.id, spec={'env': {}}, trigger='api',
            image_digest='sha256:test-image',
            status=states.ACTIVE)
        self.deployment.create()
        self.service.current_deployment_id = self.deployment.id
        self.service.save()
        self.dep_path = self.path + '/deployments/' + self.deployment.id
        self.domain = objects.Domain(
            context=self.ctx, service_id=self.service.id,
            fqdn='www.example.com',
            auto_dns=False, dns_mode='manual', tls='letsencrypt',
            verification_token='challenge', status=states.PENDING_VERIFICATION)
        self.domain.create()
        self.domain_path = self.path + '/domains/' + self.domain.id

    def request(self, method, path, body=None, headers=None, status=200,
                version=None):
        headers = dict(self.headers if headers is None else headers)
        expected = '1.0'
        if version is not None:
            headers[microversions.HEADER] = 'app-hosting ' + version
            expected = version
        if body is not None:
            response = getattr(self.client, method + '_json')(
                path, body, headers=headers, status=status)
        else:
            response = getattr(self.client, method)(
                path, headers=headers, status=status)
        if status >= 400:
            self.assert_fault(response, status)
        self.assert_version(response, expected)
        return response

    def create_body(self, **options):
        return {'service': dict(
            name='new', appgroup_id=self.application.id,
            environment_id=self.environment.id,
            server_id=self.server.id, source={
                'type': 'git',
                'repo_url': 'https://example.com/new.git'},
            **options)}

    def unlock(self, service_id=None):
        service = objects.Service.get_by_id(
            self.ctx, service_id or self.service.id)
        if service.task_state != states.NONE:
            service.release_task(service.task_state)
        return service
