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

"""Default hostname shape and default-domain replacement."""


from coast.common import naming
from coast.tests import base


class HostnameTest(base.TestCase):

    def test_dashed_ipv4_and_ipv6(self):
        self.assertEqual('203-0-113-10', naming.dashed_ip('203.0.113.10'))
        self.assertEqual('2001-db8--1', naming.dashed_ip('2001:db8::1'))
        self.assertEqual('192-0-2-1', naming.dashed_ip('::ffff:192.0.2.1'))
        self.assertNotIn('.', naming.dashed_ip('::ffff:192.0.2.1'))

    def test_default_hostname_embeds_a_usable_address(self):
        self.assertEqual(
            'web-abcd.203-0-113-10.sslip.io',
            naming.default_hostname(
                'web', 'abcd-rest', 'sslip.io', '203.0.113.10'))
        self.assertEqual(
            'web-abcd.203-0-113-10.example.com',
            naming.default_hostname(
                'web', 'abcd-rest', 'Example.COM.', '203.0.113.10'))
        self.assertEqual(
            'web-abcd.sslip.io',
            naming.default_hostname('web', 'abcd-rest', 'sslip.io'))
        self.assertEqual(
            'web-abcd.sslip.io',
            naming.default_hostname('web', 'abcd-rest', 'sslip.io', 'nope'))
        self.assertIsNone(naming.default_hostname('web', 'abcd', ''))
        self.assertIsNone(naming.default_hostname('', 'abcd', 'sslip.io'))
        self.assertIsNone(naming.default_hostname('web', '', 'sslip.io'))

    def test_domain_url_keeps_case_and_scheme(self):
        self.assertEqual('https://Web.Example.com',
                         naming.domain_url('Web.Example.com'))
        self.assertEqual('http://Web.Example.com',
                         naming.domain_url('https://Web.Example.com/x',
                                           'none'))
        self.assertEqual('web.example.com',
                         naming.hostname_of('HTTP://Web.Example.com:80/a'))

    def test_replace_default_domain(self):
        suffix = 'sslip.io'
        old = 'web-abcd.203-0-113-10.sslip.io'
        new = 'web-abcd.198-51-100-20.sslip.io'
        domains = ['https://' + old, 'http://Custom.Example.com',
                   'https://web-abcd.10-0-0-1.sslip.io',
                   'https://web-abcd.10-0-0-1.extra.sslip.io']
        replaced = naming.replace_default_domain(
            domains, old, new, 'web', 'abcd-rest', suffix)
        self.assertEqual('https://' + new, replaced[0])
        self.assertIn('http://Custom.Example.com', replaced)
        self.assertIn('https://web-abcd.10-0-0-1.extra.sslip.io', replaced)
        self.assertNotIn('https://' + old, replaced)
        self.assertNotIn('https://web-abcd.10-0-0-1.sslip.io', replaced)
        again = naming.replace_default_domain(
            replaced, new, new, 'web', 'abcd-rest', suffix)
        self.assertEqual(naming.domain_set(replaced),
                         naming.domain_set(again))
        cleared = naming.replace_default_domain(
            replaced, new, None, 'web', 'abcd-rest', suffix)
        self.assertNotIn('https://' + new, cleared)
        self.assertIn('http://Custom.Example.com', cleared)
