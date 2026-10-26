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

"""Reusable conformance mixin; subclasses supply driver and resource fixtures.

Combine with an oslotest TestCase. Set driver, ep, env, server, service,
source, deployment, new_deployment and ssh_private_key in setUp. Live
drivers can override wait_status to poll until a terminal status is
observed. Do not use production resources.
"""

from coast.common import states
from coast.drivers import base


class DriverConformanceMixin:
    def wait_status(self, getter, *args):
        result = getter(*args)
        self.assertIsInstance(result, base.ObservedStatus)
        return result

    def test_driver_lifecycle(self):
        d, ep, env = self.driver, self.ep, self.env
        self.assertIsInstance(d, base.CoastDriver)
        self.assertIn(d.get_engine_version(ep), d.SUPPORTED_ENGINE_VERSIONS)
        result = d.bootstrap_environment(ep, env)
        self.assertIsInstance(result, base.DriverResult)
        self.assertTrue(result.engine_ref)
        self.assertEqual(result, d.bootstrap_environment(ep, env))
        self.env.engine_ref = result.engine_ref
        result = d.register_server(ep, env, self.server, self.ssh_private_key)
        self.assertEqual(result, d.register_server(
            ep, env, self.server, self.ssh_private_key))
        self.assertIsInstance(result, base.DriverResult)
        self.assertTrue(result.engine_ref)
        self.server.engine_ref = result.engine_ref
        self.assertEqual(states.ACTIVE, self.wait_status(
            d.get_server_status, ep, env, self.server).status)
        result = d.create_app(ep, env, self.service, self.source, None)
        self.assertEqual(result, d.create_app(
            ep, env, self.service, self.source, None))
        self.assertIsInstance(result, base.DriverResult)
        self.assertTrue(result.engine_ref)
        self.service.engine_ref = result.engine_ref
        self.assertEqual(states.STOPPED, self.wait_status(
            d.get_app_status, ep, env, self.service).status)
        d.update_app(ep, env, self.service, self.source)
        d.set_env(ep, env, self.service, {'TEST': base.EnvVar('test-value')})
        d.set_env(ep, env, self.service, {})
        d.set_volume_mounts(ep, env, self.service, [
                            base.Mount('data', '/service')])
        d.set_volume_mounts(ep, env, self.service, [])
        d.set_domains(ep, env, self.service, ['service.example.com'])
        d.set_domains(ep, env, self.service, [])
        result = d.deploy_app(ep, env, self.service, self.deployment)
        self.assertEqual(result, d.deploy_app(
            ep, env, self.service, self.deployment))
        self.assertIsInstance(result, base.DriverResult)
        self.assertTrue(result.engine_ref)
        self.deployment.engine_ref = result.engine_ref
        self.assertEqual(states.ACTIVE, self.wait_status(
            d.get_deployment_status, ep, env, self.service,
            self.deployment).status)
        for line in d.get_deployment_log(ep, env, self.service,
                                         self.deployment):
            self.assertIsInstance(line, base.LogLine)
        for line in d.get_app_logs(ep, env, self.service):
            self.assertIsInstance(line, base.LogLine)
        result = d.rollback(ep, env, self.service, self.deployment,
                            self.new_deployment)
        self.assertIsInstance(result, base.DriverResult)
        self.assertTrue(result.engine_ref)
        self.new_deployment.engine_ref = result.engine_ref
        self.assertEqual(states.ACTIVE, self.wait_status(
            d.get_deployment_status, ep, env, self.service,
            self.new_deployment).status)
        d.stop_app(ep, env, self.service)
        self.assertEqual(states.STOPPED, self.wait_status(
            d.get_app_status, ep, env, self.service).status)
        d.start_app(ep, env, self.service)
        d.restart_app(ep, env, self.service)
        self.assertEqual(states.ACTIVE, self.wait_status(
            d.get_app_status, ep, env, self.service).status)
        d.delete_app(ep, env, self.service)
        d.delete_app(ep, env, self.service)
        d.unregister_server(ep, env, self.server)
        d.unregister_server(ep, env, self.server)
        d.delete_stack_project(ep, env)
        d.delete_stack_project(ep, env)

    def test_cancel_deployment(self):
        """Check cancellation; an immediate engine can already be finished."""
        d, ep, env = self.driver, self.ep, self.env
        env.engine_ref = d.bootstrap_environment(ep, env).engine_ref
        self.server.engine_ref = d.register_server(
            ep, env, self.server, self.ssh_private_key).engine_ref
        self.service.engine_ref = d.create_app(
            ep, env, self.service, self.source, None).engine_ref
        try:
            self.deployment.engine_ref = d.deploy_app(
                ep, env, self.service, self.deployment).engine_ref
            d.cancel_deployment(ep, env, self.service, self.deployment)
            result = self.wait_status(
                d.get_deployment_status, ep, env, self.service,
                self.deployment)
            self.assertIn(result.status, (states.CANCELLED, states.ACTIVE))
        finally:
            d.delete_app(ep, env, self.service)
            d.unregister_server(ep, env, self.server)
            d.delete_stack_project(ep, env)
