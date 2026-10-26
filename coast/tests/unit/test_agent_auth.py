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

import asyncio
import base64
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

from keystoneauth1 import loading as ks_loading

from coast.common import agent_auth
from coast.common import clients
from coast.common import exception
from coast.conductor import controller
from coast.conductor import service as conductor_service
from coast.conductor import worker
from coast.console import proxy as console_proxy
from coast.health_manager import service as health_service
from coast.tests import base
from coast.tests.unit import test_tls


AGENT_SECRET = 'agent-password-secret'
SERVICE_SECRET = 'service-password-secret'
AUTH_URL = 'https://identity.example.com'


class AgentAuthTest(base.TestCase):

    def _register(self, group):
        self.conf.register_opts(
            ks_loading.get_auth_plugin_conf_options('password'),
            group=group)

    def _password(self, group, username, password, auth_type='password'):
        self._register(group)
        self.config.config(
            group=group, auth_type=auth_type, auth_url=AUTH_URL,
            username=username, password=password, project_name='service',
            project_domain_name='Default', user_domain_name='Default')

    def _both(self, agent_user='coast-agent', service_user='coast'):
        self._password('agent_auth', agent_user, AGENT_SECRET)
        self._password('service_auth', service_user, SERVICE_SECRET)

    def test_unset_auth_type_skips_startup_check(self):
        self.assertIsNone(self.conf.agent_auth.auth_type)
        self.assertIsNone(agent_auth.prepare(self.conf))
        with self.assertLogs('coast.common.agent_auth', level='ERROR'):
            self.assertRaises(exception.ServiceConfigError,
                              agent_auth.get_session, self.conf)

    def test_shared_username_is_rejected_without_logging_the_password(self):
        self._both(agent_user='Coast', service_user='coast')
        with self.assertLogs('coast.common.agent_auth', level='ERROR') as logs:
            self.assertRaises(exception.ServiceConfigError,
                              agent_auth.prepare, self.conf)
        text = '\n'.join(logs.output)
        self.assertNotIn(AGENT_SECRET, text)
        self.assertNotIn(SERVICE_SECRET, text)
        self.assertIn('matches the service user', text)

    def test_missing_password_logs_the_option_name_only(self):
        self._register('agent_auth')
        self._register('service_auth')
        self.config.config(
            group='service_auth', username='coast')
        self.config.config(
            group='agent_auth', auth_type='password', auth_url=AUTH_URL,
            username='coast-agent', project_name='service',
            user_domain_name='Default')
        with self.assertLogs('coast.common.agent_auth', level='ERROR') as logs:
            self.assertRaises(exception.ServiceConfigError,
                              agent_auth.prepare, self.conf)
        text = '\n'.join(logs.output)
        self.assertIn('password', text)
        self.assertNotIn(AUTH_URL, text)
        self.assertNotIn(AGENT_SECRET, text)

    def test_session_identity_stays_off_the_cloud_client(self):
        self._both()
        with mock.patch('keystoneauth1.session.Session.get_token',
                        side_effect=AssertionError('token')):
            session = agent_auth.prepare(self.conf)
            cloud = clients.get_session(self.conf)
        agent = session.auth.get_cache_id_elements()
        service = cloud.auth.get_cache_id_elements()
        self.assertEqual('coast-agent', agent['username'])
        self.assertEqual('service', agent['project_name'])
        self.assertEqual(AGENT_SECRET, agent['password'])
        self.assertEqual('coast', service['username'])
        self.assertEqual(SERVICE_SECRET, service['password'])
        self.assertIsNot(session, cloud)

    def test_unsupported_auth_type_fails_closed(self):
        self._password('agent_auth', 'coast-agent', AGENT_SECRET,
                       auth_type='not-a-plugin')
        self._password('service_auth', 'coast', SERVICE_SECRET)
        with self.assertLogs('coast.common.agent_auth', level='ERROR') as logs:
            self.assertRaises(exception.ServiceConfigError,
                              agent_auth.prepare, self.conf)
        self.assertNotIn(AGENT_SECRET, '\n'.join(logs.output))

    def test_processes_fail_before_they_serve_a_shared_user(self):
        self._both(agent_user='coast', service_user='coast')
        endpoint = mock.Mock()
        conductor = conductor_service.Service(self.conf, endpoint=endpoint)
        with mock.patch('oslo_service.service.Service.start') as started:
            self.assertRaises(exception.ServiceConfigError, conductor.start)
        started.assert_not_called()
        endpoint.recover_stacks.assert_not_called()

        health = health_service.Service(self.conf)
        with mock.patch('coast.health_manager.service.management.'
                        'socket_for_agent') as factory:
            self.assertRaises(exception.ServiceConfigError, health.start)
            self.assertRaises(exception.ServiceConfigError, health.prepare)
        factory.assert_not_called()

        with mock.patch.object(console_proxy.asyncio, 'start_server') as bind:
            self.assertRaises(exception.ServiceConfigError, asyncio.run,
                              console_proxy.serve(self.conf))
        bind.assert_not_called()

    def test_config_drive_omits_the_agent_credential(self):
        self._both()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        test_tls.TLSTest._write_ca(self)
        ca_path = str(Path(self.directory.name) / 'ca.pem')
        self.config.config(
            agent_client_ca=ca_path, admin_ssh_key_name='',
            env_cidr_pool='192.0.2.0/24', env_subnet_prefix=24,
            group='controller_worker')
        stack = SimpleNamespace(
            id='stack-1', project_id='project-a',
            ssh_public_key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest')
        server = SimpleNamespace(
            hostname="coast-shop-5bad06-controller",
            id='11111111-1111-1111-1111-111111111111',
            mgmt_ip='192.0.2.10', role='control')
        cloud = controller.ControllerCloud(self.conf, stack)
        encoded, _serial, _expires = cloud.config_drive(
            server, [], 'hb-value')
        worker_server = SimpleNamespace(
            hostname="coast-shop-5bad06-controller",
            id=server.id, mgmt_ip=server.mgmt_ip, role='worker')
        worker_encoded, _serial, _expires = worker.WorkerCloud(
            self.conf, stack).config_drive(worker_server, [], 'hb-value')
        for payload in (encoded, worker_encoded):
            text = base64.b64decode(payload).decode()
            self.assertIn('heartbeat_key=hb-value', text)
            self.assertIn('[agent]', text)
            self.assertNotIn(AGENT_SECRET, text)
            self.assertNotIn(SERVICE_SECRET, text)
            self.assertNotIn('[agent_auth]', text)
            self.assertNotIn('coast-agent', text)
            self.assertNotIn('auth_url', text)
