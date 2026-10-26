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

from types import SimpleNamespace
from unittest import mock

from keystoneauth1.exceptions import auth_plugins
from openstack.config import cloud_region

from coast.common import clients
from coast.common import exception
from coast.conductor import controller
from coast.network import fip
from coast.network import stack
from coast.tests import base

SECRET = 'coast-service-secret-value'


class ServiceClientTest(base.TestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(clients.reset)

    def _service(self, service, region, interfaces=True, **kwargs):
        values = {'region_name': region}
        if interfaces:
            values['valid_interfaces'] = ['internal']
        values.update(kwargs)
        self.config.config(group=service, **values)

    def _patches(self):
        auth = mock.patch.object(
            clients.ks_loading, 'load_auth_from_conf_options',
            return_value=mock.Mock(name='auth'))
        session = mock.patch.object(
            clients.ks_loading, 'load_session_from_conf_options',
            side_effect=lambda *args, **kwargs: mock.Mock(name='session'))
        conn = mock.patch.object(
            clients.connection, 'Connection',
            side_effect=lambda **kwargs: mock.Mock(name='connection'))
        return auth, session, conn

    def test_session_and_connection_are_cached(self):
        self._service('nova', 'RegionA')
        self._service('neutron', 'RegionB')
        auth, session, conn = self._patches()
        with auth as load_auth, session as load_session, conn as connection:
            first = clients.get_connection('nova', self.conf)
            second = clients.get_connection('nova', self.conf)
            neutron = clients.get_connection('neutron', self.conf)
            shared = clients.get_session(self.conf)
        self.assertIs(first, second)
        self.assertIsNot(first, neutron)
        self.assertEqual(1, load_auth.call_count)
        self.assertEqual(1, load_session.call_count)
        self.assertEqual(2, connection.call_count)
        sessions = [call.kwargs['session']
                    for call in connection.call_args_list]
        self.assertIs(sessions[0], sessions[1])
        self.assertIs(shared, sessions[0])
        self.assertIs(self.conf, connection.call_args_list[0].kwargs[
            'oslo_conf'])
        self.assertEqual(['compute'], connection.call_args_list[0].kwargs[
            'service_types'])
        self.assertEqual(['network'], connection.call_args_list[1].kwargs[
            'service_types'])
        self.assertEqual('RegionA', self.conf.nova.region_name)
        self.assertEqual('RegionB', self.conf.neutron.region_name)
        self.assertEqual('service_auth', load_session.call_args.args[1])
        self.assertNotIn(SECRET, '\n'.join(
            str(item) for item in load_auth.call_args.kwargs.items()))

    def test_designate_uses_service_auth_and_dns_service_type(self):
        self._service('designate', 'RegionD')
        self._service('neutron', 'RegionN')
        auth, session, conn = self._patches()
        with auth, session as load_session, conn as connection:
            dns = clients.get_connection('designate', self.conf)
            self.assertIs(dns, clients.get_connection('designate', self.conf))
            clients.get_connection('neutron', self.conf)
        self.assertEqual(1, load_session.call_count)
        self.assertEqual(['dns'], connection.call_args_list[0].kwargs[
            'service_types'])
        self.assertIs(connection.call_args_list[0].kwargs['session'],
                      connection.call_args_list[1].kwargs['session'])

    def test_per_service_interface_and_tls(self):
        self._service('glance', 'RegionG', interfaces=False,
                      endpoint_type='internalURL')
        self._service('cinder', 'RegionC',
                      ca_certificates_file='/etc/coast/service-ca.pem')
        self._service('barbican', 'RegionK', insecure=True)
        auth, session, conn = self._patches()
        with auth, session as load_session, conn as connection:
            clients.get_connection('glance', self.conf)
            clients.get_connection('cinder', self.conf)
            clients.get_connection('barbican', self.conf)
            shared = clients.get_session(self.conf)
        self.assertEqual(['internal'], list(
            self.conf.glance.valid_interfaces))
        self.assertEqual(['block-storage'],
                         connection.call_args_list[1].kwargs['service_types'])
        self.assertEqual(['key-manager'],
                         connection.call_args_list[2].kwargs['service_types'])
        verify_values = [call.kwargs.get('verify', 'service-auth')
                         for call in load_session.call_args_list]
        self.assertEqual(
            ['service-auth', '/etc/coast/service-ca.pem', False],
            verify_values)
        self.assertIs(False, connection.call_args_list[2].kwargs[
            'session'].verify)
        self.assertEqual(3, load_session.call_count)
        self.assertIs(connection.call_args_list[0].kwargs['session'], shared)

    def test_missing_region_fails_closed(self):
        self.config.config(valid_interfaces=['internal'], group='nova')
        auth, session, conn = self._patches()
        with auth as load_auth, session as load_session, conn as connection:
            with self.assertLogs('coast.common.clients',
                                 level='ERROR') as logs:
                self.assertRaises(
                    exception.ServiceConfigError,
                    clients.get_connection, 'nova', self.conf)
        self.assertNotIn(SECRET, '\n'.join(logs.output))
        load_auth.assert_not_called()
        load_session.assert_not_called()
        connection.assert_not_called()

    def test_missing_interface_fails_closed(self):
        self.config.config(region_name='RegionA', group='neutron')
        auth, session, conn = self._patches()
        with auth as load_auth, session, conn as connection:
            self.assertRaises(
                exception.ServiceConfigError,
                clients.get_connection, 'neutron', self.conf)
        load_auth.assert_not_called()
        connection.assert_not_called()

    def test_missing_auth_fails_closed(self):
        self._service('nova', 'RegionA')
        missing = auth_plugins.MissingRequiredOptions(
            [mock.Mock(dest='password')])
        auth, session, conn = self._patches()
        with auth as load_auth, session as load_session, conn as connection:
            load_auth.side_effect = missing
            with self.assertLogs('coast.common.clients',
                                 level='ERROR') as logs:
                error = self.assertRaises(
                    exception.ServiceConfigError,
                    clients.get_connection, 'nova', self.conf)
        output = '\n'.join(logs.output)
        self.assertNotIn(SECRET, output)
        self.assertNotIn(SECRET, str(error))
        self.assertIn('password', output)
        load_session.assert_not_called()
        connection.assert_not_called()

    def test_unset_auth_type_fails_closed(self):
        self._service('nova', 'RegionA')
        auth, session, conn = self._patches()
        with auth as load_auth, session as load_session, conn as connection:
            load_auth.return_value = None
            with self.assertLogs('coast.common.clients',
                                 level='ERROR') as logs:
                error = self.assertRaises(
                    exception.ServiceConfigError,
                    clients.get_connection, 'nova', self.conf)
        output = '\n'.join(logs.output)
        self.assertNotIn(SECRET, output)
        self.assertNotIn(SECRET, str(error))
        self.assertIn('auth_type', output)
        load_session.assert_not_called()
        connection.assert_not_called()

    def test_conflicting_options_fail_closed(self):
        self._service('cinder', 'RegionC', endpoint_type='publicURL',
                      valid_interfaces=['internal'])
        self._service('barbican', 'RegionK', insecure=True,
                      ca_certificates_file='/etc/coast/service-ca.pem')
        auth, session, conn = self._patches()
        with auth as load_auth, session, conn as connection:
            self.assertRaises(
                exception.ServiceConfigError,
                clients.get_connection, 'cinder', self.conf)
            self.assertRaises(
                exception.ServiceConfigError,
                clients.get_connection, 'barbican', self.conf)
            self.assertRaises(
                exception.ServiceConfigError,
                clients.get_connection, 'ironic', self.conf)
        load_auth.assert_not_called()
        connection.assert_not_called()

    def test_oslo_conf_keeps_region_and_interface(self):
        self._service('nova', 'RegionA', interfaces=False,
                      endpoint_type='internalURL')
        self._service('neutron', 'RegionN',
                      valid_interfaces=['admin', 'internal'])
        clients._prepare_service(self.conf, 'nova')
        clients._prepare_service(self.conf, 'neutron')
        cloud = cloud_region.from_conf(
            self.conf, session=mock.Mock(name='session'),
            service_types=['compute', 'network'])
        self.assertEqual('RegionA', cloud.get_region_name('compute'))
        self.assertEqual('RegionN', cloud.get_region_name('network'))
        self.assertEqual(['internal'], list(cloud.get_interface('compute')))
        self.assertEqual(['admin', 'internal'], list(
            cloud.get_interface('network')))

    def test_reset_drops_the_cached_connection(self):
        self._service('nova', 'RegionA')
        auth, session, conn = self._patches()
        with auth, session, conn:
            first = clients.get_connection('nova', self.conf)
            clients.reset(self.conf)
            second = clients.get_connection('nova', self.conf)
        self.assertIsNot(first, second)

    def test_infra_clients_use_configured_service_identity(self):
        # Use the real auth/session loaders, with no cloud HTTP requests.
        self.conf.register_opts(
            clients.ks_loading.get_auth_plugin_conf_options('password'),
            group='service_auth')
        self.config.config(
            group='service_auth', auth_type='password',
            auth_url='https://identity.example.com/v3',
            username='coast', password=SECRET,
            project_name='service', user_domain_name='Default',
            project_domain_name='Default')
        self.conf.register_opts(
            clients.ks_loading.get_auth_plugin_conf_options('password'),
            group='keystone_authtoken')
        self.config.config(
            group='keystone_authtoken', username='api-validator',
            password='different-api-password', project_name='api-validation')
        tenant_stack = mock.Mock(project_id='tenant-project', id='stack-id')
        cloud = controller.ControllerCloud(self.conf, tenant_stack)
        for service in ('nova', 'neutron', 'cinder', 'glance', 'barbican'):
            self._service(service, 'example-region')

        def offline_connection(session, **kwargs):
            # SDK proxy access discovers endpoints; keep this test offline.
            return SimpleNamespace(**{
                name: SimpleNamespace(session=session)
                for name in ('compute', 'network', 'block_storage', 'image',
                             'key_manager')})

        with mock.patch.object(clients.connection, 'Connection',
                               side_effect=offline_connection):
            proxies = [cloud.proxy(service) for service in
                       ('nova', 'neutron', 'cinder', 'glance')]
            proxies.append(clients.get_sensitive_proxy('barbican', self.conf))
            proxies.append(stack.EnvironmentNetwork(self.conf).network)
            proxies.append(fip.FloatingIP(self.conf).network)
        session = clients.get_session(self.conf)
        for proxy in proxies:
            self.assertIs(session, proxy.session)
            identity = proxy.session.auth.get_cache_id_elements()
            self.assertEqual('coast', identity['username'])
            self.assertEqual('service', identity['project_name'])
            self.assertEqual(SECRET, identity['password'])
        self.assertIn('project_id=tenant-project', cloud.tags)

    def test_infra_does_not_fall_back_to_api_credentials(self):
        self._service('nova', 'example-region')
        self.conf.register_opts(
            clients.ks_loading.get_auth_plugin_conf_options('password'),
            group='keystone_authtoken')
        self.config.config(
            group='keystone_authtoken', auth_type='password',
            auth_url='https://identity.example.com/v3', username='api-user',
            password=SECRET, project_name='tenant-project')
        cloud = controller.ControllerCloud(
            self.conf, mock.Mock(project_id='tenant-project', id='stack-id'))
        self.assertRaises(exception.ServiceConfigError, cloud.proxy, 'nova')
