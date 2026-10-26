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

"""Control-side namespace selection independent of guest routing."""

import socket
from unittest import mock

import requests_mock

from coast.agent import namespace
from coast.common import management
from coast.conductor import agent_client
from coast.drivers import base as driver_base
from coast.drivers.coolify import client
from coast.tests import base


class ManagementNamespaceTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(agent_client_cert='client.pem',
                           agent_client_key='client.key',
                           group='controller_worker')
        self.config.config(ca_certificate='ca.pem', group='certificates')
        self.config.config(bind_ip='192.0.2.2', group='health_manager')
        self.agent = agent_client.AgentClient(self.conf, '192.0.2.10')
        self.endpoint = driver_base.EngineEndpoint(
            'https://controller.example.com/engine', 'cert', 'key', 'ca',
            'token')

    def call_agent(self):
        return self.agent.request('GET', '/health')

    def call_coolify(self):
        with client.Client(self.endpoint, self.conf) as transport:
            return transport.request('GET', '/health')

    def call_listener(self):
        # Use the real socket factory, but no host interface is required.
        with mock.patch.object(management.socket, 'socket') as factory:
            sock = management.socket_for_agent(
                self.conf, socket.SOCK_DGRAM, 5565)
            self.assertIs(factory.return_value, sock)
            factory.return_value.bind.assert_called_once_with(
                ('192.0.2.2', 5565))
        return {'status': 'ok'}

    def exercise_callers(self, name, expected_entries):
        self.config.config(mgmt_netns=name, group='controller_worker')
        for call in (self.call_agent, self.call_coolify, self.call_listener):
            with self.subTest(name=name, caller=call.__name__):
                libc = mock.Mock()
                libc.setns.return_value = 0
                with mock.patch.object(namespace.ctypes, 'CDLL',
                                       return_value=libc) as load, \
                        mock.patch('builtins.open',
                                   mock.mock_open()) as files, \
                        requests_mock.Mocker() as http:
                    http.get('https://192.0.2.10:9443/health',
                             json={'status': 'ok'})
                    http.get(self.endpoint.base_url + '/health',
                             json={'status': 'ok'})
                    self.assertEqual({'status': 'ok'}, call())
                self.assertEqual(expected_entries, libc.setns.call_count)
                if expected_entries:
                    self.assertEqual(
                        [mock.call('/proc/thread-self/ns/net', 'rb'),
                         mock.call('/var/run/netns/' + name, 'rb')],
                        files.call_args_list)
                else:
                    load.assert_not_called()
                    files.assert_not_called()

    def test_empty_control_namespace_does_not_setns(self):
        self.exercise_callers('', 0)

    def test_named_control_namespace_enters_and_restores(self):
        self.exercise_callers('coast-mgmt', 2)

    def test_none_control_namespace_does_not_setns(self):
        with mock.patch.object(namespace.ctypes, 'CDLL') as load:
            with namespace.control_management_namespace(None):
                pass
        load.assert_not_called()

    def test_invalid_names_rejected_by_each_caller(self):
        for name in ('../netns', ' ', 'coast/mgmt', 'coast.mgmt'):
            self.config.config(mgmt_netns=name, group='controller_worker')
            for call in (self.call_agent, self.call_coolify,
                         self.call_listener):
                with self.subTest(name=name, caller=call.__name__):
                    error = (driver_base.EngineUnreachable
                             if call == self.call_agent else ValueError)
                    with mock.patch.object(namespace.ctypes, 'CDLL') as load:
                        self.assertRaises(error, call)
                    load.assert_not_called()

    def test_named_scope_rejects_empty_namespace(self):
        with self.assertRaises(ValueError):
            with namespace.management_namespace(''):
                self.fail('Named scope must not use the host')
