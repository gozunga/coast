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

from oslo_config import cfg
from oslo_config import fixture as config_fixture

from coast.common import naming
from coast.common import opts
from coast.tests import base


class ResourceNameTest(base.TestCase):
    def test_default_and_parts(self):
        self.assertEqual('coast-env-123-worker', naming.resource_name(
            'env', '123', 'worker', conf=self.conf))
        self.assertEqual('coast-env', naming.resource_name(
            'env', conf=self.conf))

    def test_global_configuration(self):
        fixture = self.useFixture(config_fixture.Config(cfg.CONF))
        opts.register_opts(cfg.CONF)
        fixture.config(resource_name_prefix='coast-test')
        self.assertEqual('coast-test-env-123',
                         naming.resource_name('env', '123'))

    def test_sanitization_and_empty_parts(self):
        self.config.config(resource_name_prefix=' /Coast test_é/ ')
        self.assertEqual('Coast-test-env-a-b', naming.resource_name(
            'env', '', 'a/b', '☃', conf=self.conf))

    def test_limit_and_distinct_long_names(self):
        self.config.config(resource_name_prefix='x' * 300)
        first = naming.resource_name('env', '123', 'worker', conf=self.conf)
        second = naming.resource_name('env', '123', 'ctrl', conf=self.conf)
        self.assertLessEqual(len(first), 255)
        self.assertRegex(first, r'^[A-Za-z0-9-]+$')
        self.assertNotEqual(first, second)
        self.assertEqual(first, naming.resource_name(
            'env', '123', 'worker', conf=self.conf))

    def test_exact_limit(self):
        self.assertEqual('coast-env-' + 'x' * 245, naming.resource_name(
            'env', 'x' * 245, conf=self.conf))


class ServerHostnameTest(base.TestCase):
    stack_id = '5bad0696-ff60-5aae-9f25-ce75f189e220'
    server_id = 'ab120696-ff60-5aae-9f25-ce75f189e220'

    def hostname(self, stack='Shop', server='worker-2', taken=()):
        return naming.server_hostname(stack, self.stack_id, server,
                                      self.server_id, taken, conf=self.conf)

    def test_readable_example_and_collision_growth(self):
        first = self.hostname()
        self.assertEqual('coast-shop-5bad06-worker-2', first)
        second = self.hostname(taken=[first])
        self.assertEqual('coast-shop-5bad0696ff-worker-2', second)
        self.assertEqual(second + '-ab12',
                         self.hostname(taken=[first, second]))

    def test_slugs_and_dns_limits(self):
        for stack, server, expected in [
                ('!!!', '☃', 'coast-stack-5bad06-server'),
                ('Ünicode Shop', 'Wörker__2',
                 'coast-nicode-shop-5bad06-w-rker-2'),
                ('--Shop---API--', '--WORKER---2--',
                 'coast-shop-api-5bad06-worker-2'),
                ('a' * 50, 'b' * 50,
                 'coast-' + 'a' * 16 + '-5bad06-' + 'b' * 10)]:
            first = self.hostname(stack, server)
            self.assertEqual(expected, first)
            second = self.hostname(stack, server, [first])
            third = self.hostname(stack, server, [first, second])
            for label, limit in [(first, 40), (second, 49), (third, 49)]:
                self.assertLessEqual(len(label), limit)
                self.assertRegex(label, r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

    def test_custom_prefix_remains_a_bounded_dns_label(self):
        self.config.config(resource_name_prefix='!!!CUSTOM---' + 'x' * 80)
        value = self.hostname('a' * 50)
        self.assertLessEqual(len(value), 40)
        self.assertRegex(value, r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
