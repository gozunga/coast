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

"""Shared in-process API / fake RPC / SQLite conductor fixture."""

import json
import queue
import uuid

import fixtures
from oslo_db.sqlalchemy import enginefacade
import oslo_messaging as messaging

from coast.api import app as api_app
from coast.api import handlers
from coast.api import root_controller
from coast.api.v1 import appgroup_controllers
from coast.api.v1 import service_controllers
from coast.api.v1 import stack_controllers
from coast.common import context
from coast.common import states
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.db import base_models
from coast.db import models
from coast import objects
from coast.tests import app_api
from coast.tests.unit import test_api
import webtest


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def info(self, ctx, event_type, payload):
        self.events.append((event_type, payload))


class Endpoint(manager.Manager):
    def __init__(self, conf, store):
        super().__init__(conf, notifier=RecordingNotifier())
        self.done = queue.Queue()

    def execute(self, *args, **kwargs):
        try:
            return super().execute(*args, **kwargs)
        finally:
            self.done.put(True)


class ConductorTest(test_api.APITest):
    def setUp(self):
        super().setUp()
        self.config.config(definitions=json.dumps([
            dict(name='small', kind='vm',
                 roles=['control', 'worker', 'build'], flavor='small'),
            dict(name='worker', kind='vm', roles=['worker'], flavor='worker'),
            dict(name='metal', kind='baremetal', roles=['worker'],
                 flavor='metal')]), group='plans')
        directory = self.useFixture(fixtures.TempDir()).path
        facade = enginefacade.transaction_context()
        facade.configure(connection='sqlite:///' + directory + '/coast.db',
                         sqlite_fk=True)
        self.engine = facade.writer.get_engine()
        self.addCleanup(self.engine.dispose)
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.engine)
        self.config.config(driver='noop', group='controller_worker')
        self.config.config(environments=5, servers=10, volume_gb=1000,
                           group='quotas')
        self.store = app_api.MemorySecretStore()
        self.useFixture(fixtures.MonkeyPatch(
            'coast.api.secret_store.STORE', self.store))
        # Fake exchanges keep queued casts across transports in a process.
        # A unique topic prevents old no-op controller tests entering this DB.
        self.useFixture(fixtures.MonkeyPatch(
            'coast.conductor.rpcapi.TOPIC',
            rpcapi.TOPIC + '-' + str(uuid.uuid4())))
        self.transport = messaging.get_rpc_transport(self.conf)
        self.addCleanup(self.transport.cleanup)
        self.manager = Endpoint(self.conf, self.store)
        self.addCleanup(self.manager.close)
        self.server = messaging.get_rpc_server(
            self.transport, messaging.Target(topic=rpcapi.TOPIC,
                                             server=str(uuid.uuid4())),
            [self.manager], executor='threading',
            serializer=rpcapi.Serializer())
        self.server.start()
        self.addCleanup(self.stop_server)
        self.rpc = rpcapi.ConductorAPI(self.transport)
        self.handler = handlers.Handler(self.rpc)
        root = root_controller.V1Controller()
        root.stacks = stack_controllers.Collection(handler=self.handler)
        root.services = service_controllers.Collection(handler=self.handler)
        root.appgroups = appgroup_controllers.Appgroups(
            handler=self.handler)
        root.environments = appgroup_controllers.Environments(
            handler=self.handler)
        # Use the Paste middleware for policy, faults and microversions; only
        # replace the Pecan root for injection, not auth or request handling.
        factory = api_app.factory

        def injected_factory(global_conf, **local_conf):
            if local_conf.get('versioned') == 'true':
                return api_app.setup_app(root)
            return factory(global_conf, **local_conf)

        self.useFixture(fixtures.MonkeyPatch(
            'coast.api.app.factory', injected_factory))
        self.client = webtest.TestApp(api_app.load_app())
        self.ctx = context.RequestContext(project_id='project', user_id='user')
        self.headers = {'X-User-Id': 'user', 'X-Project-Id': 'project',
                        'X-Roles': 'reader,member,manager'}

    def stop_server(self):
        self.server.stop()
        self.server.wait()

    def request(self, method, path, body=None, status=200, headers=None,
                complete=False):
        kwargs = {'status': status, 'headers': headers or self.headers}
        if body is None:
            response = getattr(self.client, method)(path, **kwargs)
        else:
            response = getattr(self.client, method + '_json')(path, body,
                                                              **kwargs)
        if complete:
            # Allow fake-RPC scheduling and SQLite writes under parallel test
            # load, while staying below the overall 60-second test timeout.
            self.manager.done.get(timeout=30)
        if status >= 400:
            self.assert_fault(response, status)
        return response

    def stack(self, **options):
        row = self.request('post', '/v1/stacks', {'stack': {
            'name': 'new', 'plan': 'small',
            'workers': [{'kind': 'vm', 'plan': 'small'}],
            'data_volume_gb': 10, **options}}, status=202,
            complete=True).json['stack']
        return '/v1/stacks/' + row['id']

    def service_parents(self, stack_id):
        appgroups = objects.AppgroupList.get_all(
            self.ctx, filters={'stack_id': stack_id})
        if appgroups:
            appgroup = appgroups[0]
            environment = objects.EnvironmentList.get_all(
                self.ctx, filters={'appgroup_id': appgroup.id})[0]
        else:
            appgroup = objects.Appgroup(
                context=self.ctx, stack_id=stack_id, name='website',
                status=states.ACTIVE, task_state=states.NONE,
                engine_ref='project-uuid')
            appgroup.create()
            environment = objects.Environment(
                context=self.ctx, appgroup_id=appgroup.id,
                name='production', status=states.ACTIVE,
                task_state=states.NONE,
                engine_ref='environment-uuid')
            environment.create()
        return {'appgroup_id': appgroup.id,
                'environment_id': environment.id}

    def usage(self):
        with self.engine.connect() as connection:
            return dict(connection.execute(models.QuotaUsage.__table__.select(
            )).mappings().one())
