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

"""Opt-in template create contract using only documented engine routes."""

from types import SimpleNamespace
import uuid

from coast.drivers import base as engine
from coast.drivers.coolify import v4
from coast.tests.contract.coolify import test_live


class LiveTemplateTest(test_live.LiveCoolifyBase):
    def test_versioned_catalog_and_create_type(self):
        self.env.engine_version = self.driver.get_engine_version(self.ep)
        items = self.driver.list_templates(None, self.env)
        template = self.driver.get_template(
            None, self.env, 'wordpress-with-mariadb')
        self.assertIn(template, items)
        self.assertRaises(engine.EngineNotFound, self.driver.get_template,
                          None, self.env, 'coast-unknown-template')
        # Do not record service responses: they can include generated envs.
        self.driver.recorder = None
        application = SimpleNamespace(engine_ref=self.env.engine_ref)
        environment = SimpleNamespace(id=uuid.uuid4().hex, engine_ref=None)
        self.addCleanup(self.driver.delete_environment, self.ep, self.env,
                        application, environment)
        environment.engine_ref = self.driver.ensure_environment(
            self.ep, self.env, application, environment).engine_ref
        app = SimpleNamespace(
            id=uuid.uuid4().hex, engine_ref=None,
            server_id=self.servers[0].id,
            server_engine_ref=self.servers[0].engine_ref,
            project_engine_ref=application.engine_ref,
            environment_engine_ref=environment.engine_ref)

        def cleanup():
            # Recover an uncertain create by deterministic name, not by DB
            # access, compose inspection or container execution.
            with self.driver._client(self.ep) as c:
                row = v4.find(c, '/services', app)
                if row:
                    v4.remove(c, '/services/' + v4.identifier(row))

        self.addCleanup(cleanup)
        source = SimpleNamespace(type='template', template_id=template['id'])
        result = self.driver.create_app(self.ep, self.env, app, source, None)
        app.engine_ref = result.engine_ref
        self.assertTrue(app.engine_ref)
        app.template_id = template['id']
        mounts = self.driver.list_volume_mounts(self.ep, self.env, app)
        for mount in mounts:
            self.assertEqual({'name', 'source', 'type', 'container_path',
                              'read_only'}, set(mount))
        self.assertTrue(mounts)
        self.assertEqual({}, result.extra)
        self.assertEqual(result, self.driver.create_app(
            self.ep, self.env, app, source, None))
        # Adoption without a recorded UUID must not enqueue a second create.
        app.engine_ref = None
        self.assertEqual(result, self.driver.create_app(
            self.ep, self.env, app, source, None))
