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

"""Offline permission probes and Kolla role rendering."""

import io
from pathlib import Path
from unittest import mock

from jinja2 import nativetypes
import yaml

from coast.common import service_permissions
from coast.tests import base
from tools import check_service_permissions as checker


class ServicePermissionsTest(base.TestCase):
    def test_read_only_probes(self):
        cloud = mock.Mock()
        for attribute, method in service_permissions.OWN_READS:
            getattr(getattr(cloud, attribute), method).return_value = iter([])
        output = io.StringIO()
        self.assertEqual(0, service_permissions.check(
            lambda service: cloud, 'service-project', output=output))
        self.assertEqual(6, output.getvalue().count('PASS'))
        for attribute, method in service_permissions.OWN_READS:
            getattr(getattr(cloud, attribute), method).assert_called_once()
        self.assertEqual(6, len(cloud.mock_calls))
        cloud.network.networks.assert_called_once_with(
            project_id='service-project')
        cloud.image.images.assert_called_once_with(owner='service-project')
        cloud.compute.servers.assert_called_once_with(all_projects=False)
        cloud.block_storage.volumes.assert_called_once_with(all_projects=False)

    def test_lazy_failure_does_not_print_exception_or_payload(self):
        cloud = mock.Mock()

        def denied():
            raise RuntimeError('token=secret payload=private')
            yield

        for attribute, method in service_permissions.OWN_READS:
            getattr(getattr(cloud, attribute), method).return_value = denied()
        output = io.StringIO()
        self.assertEqual(1, service_permissions.check(
            lambda service: cloud, 'service-project', output=output))
        self.assertEqual(6, output.getvalue().count('FAIL'))
        self.assertNotIn('token=', output.getvalue())
        self.assertNotIn('private', output.getvalue())

    def test_cross_project_probes(self):
        cloud = mock.Mock()
        for attribute, method in service_permissions.OWN_READS:
            getattr(getattr(cloud, attribute), method).return_value = []
        swift = mock.Mock()
        dns = mock.Mock()
        dns.zones.return_value = []
        output = io.StringIO()
        self.assertEqual(0, service_permissions.check(
            lambda service: cloud, 'service-project', swift=lambda: swift,
            dns=lambda: dns,
            output=output))
        swift.get_account_metadata.assert_called_once_with()
        dns.zones.assert_called_once_with()
        self.assertEqual(8, output.getvalue().count('PASS'))
        self.assertEqual(1, len(swift.mock_calls))
        self.assertEqual(1, len(dns.mock_calls))

    def test_missing_project_never_lists_other_projects(self):
        factory = mock.Mock()
        self.assertEqual(1, service_permissions.check(
            factory, None, output=io.StringIO()))
        factory.assert_not_called()

    def test_cross_factory_failure_does_not_skip_other_probes(self):
        cloud = mock.Mock()
        for attribute, method in service_permissions.OWN_READS:
            getattr(getattr(cloud, attribute), method).return_value = []
        denied = mock.Mock(side_effect=RuntimeError('private'))
        output = io.StringIO()
        self.assertEqual(1, service_permissions.check(
            lambda service: cloud, 'service-project', swift=denied,
            dns=denied, output=output))
        self.assertEqual(6, output.getvalue().count('PASS'))
        self.assertEqual(2, output.getvalue().count('FAIL'))
        self.assertNotIn('private', output.getvalue())

    def test_connection_failure_is_sanitized_and_continues(self):
        output = io.StringIO()
        factory = mock.Mock(side_effect=RuntimeError('password=private'))
        self.assertEqual(1, service_permissions.check(
            factory, 'service-project', output=output))
        self.assertEqual(6, factory.call_count)
        self.assertNotIn('private', output.getvalue())

    def test_cli_passes_config_and_service_project(self):
        with mock.patch.object(checker.config, 'parse_args') as parse, \
                mock.patch.object(checker.clients, 'get_session') as session, \
                mock.patch.object(service_permissions, 'check') as check, \
                mock.patch.object(checker.logging, 'disable') as disable:
            session.return_value.get_project_id.return_value = 'service-id'
            check.return_value = 0
            self.assertEqual(0, checker.main(
                ['--config-file', '/example/coast.conf']))
            parse.assert_called_once_with(
                ['--config-file', '/example/coast.conf'])
            self.assertEqual(
                'service-id', check.call_args.kwargs['project_id'])
            disable.assert_called_once_with(checker.logging.CRITICAL)

    def test_cli_sanitizes_authentication_failure(self):
        output = io.StringIO()
        with mock.patch.object(checker.config, 'parse_args',
                               side_effect=RuntimeError('payload=private')), \
                mock.patch.object(checker.logging, 'disable'), \
                mock.patch('sys.stdout', output):
            self.assertEqual(1, checker.main([]))
        self.assertEqual('FAIL configuration/authentication\n',
                         output.getvalue())

    def test_kolla_role_list_rendering(self):
        path = (Path(__file__).resolve().parents[3] /
                'tools/kolla/ansible/roles/coast/defaults/main.yml')
        defaults = yaml.safe_load(path.read_text())
        self.assertEqual(['admin'], defaults['coast_keystone_user_roles'])
        variables = dict(coast_service_auth_project='service',
                         coast_keystone_user='coast',
                         coast_keystone_password='dummy-service',
                         coast_agent_keystone_user='coast-agent',
                         coast_agent_keystone_password='dummy-agent',
                         coast_agent_keystone_role='coast_agent')
        template = nativetypes.NativeEnvironment().from_string(
            defaults['coast_ks_users'])
        for roles in (['admin'], ['member', 'reader'], ['member']):
            users = template.render(coast_keystone_user_roles=roles,
                                    **variables)
            self.assertEqual(roles, [u['role'] for u in users[:-1]])
            self.assertTrue(all(u['user'] == 'coast' for u in users[:-1]))
            self.assertTrue(all(u['project'] == 'service' for u in users))
            self.assertEqual('coast_agent', users[-1]['role'])
            self.assertEqual('coast-agent', users[-1]['user'])
