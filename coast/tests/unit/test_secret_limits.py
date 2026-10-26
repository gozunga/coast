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

import base64
from unittest import mock

from oslo_config import cfg

from coast.common import compose
from coast.common import exception
from coast.common import file_mounts
from coast.common import secret_limits
from coast.tests import base


class SecretLimitsTest(base.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cfg, 'CONF', self.conf)
        patch.start()
        self.addCleanup(patch.stop)

    def test_utf8_boundary_and_safe_error(self):
        self.config.config(max_secret_bytes=8, group='secrets')
        secret_limits.validate('ééé', 'Content')
        with self.assertRaises(exception.Invalid) as caught:
            secret_limits.validate('éééx', 'Content')
        self.assertIn('6 bytes', str(caught.exception))
        self.assertNotIn('éééx', str(caught.exception))
        self.assertRaises(exception.Invalid, secret_limits.validate,
                          '\ud800', 'Content')

    def test_base64_boundary_including_padding(self):
        for cap in range(1, 17):
            self.config.config(max_secret_bytes=cap, group='secrets')
            limit = 3 * (cap // 4)
            value = 'é' * (limit // 2) + 'x' * (limit % 2)
            secret_limits.validate(value, 'Content')
            self.assertLessEqual(len(base64.b64encode(value.encode('utf-8'))),
                                 cap)
            with self.assertRaises(exception.Invalid) as caught:
                secret_limits.validate(value + 'x', 'Content')
            self.assertIn('%d bytes (UTF-8)' % limit, str(caught.exception))

    def test_file_cap_and_configured_secret_cap(self):
        for configured, limit in ((8, 6), (90000, 65536)):
            self.config.config(max_secret_bytes=configured, group='secrets')
            secret_limits.validate('x' * limit, 'File mount content',
                                   file_mounts.MAX_BYTES)
            with self.assertRaises(exception.Invalid) as caught:
                secret_limits.validate('x' * (limit + 1),
                                       'File mount content',
                                       file_mounts.MAX_BYTES)
            self.assertIn('%d bytes' % limit, str(caught.exception))

    def test_compose_uses_lower_cap(self):
        raw = 'services: {web: {image: example/web}}'
        raw += ' ' * (-len(raw.encode('utf-8')) % 3)
        size = len(raw.encode('utf-8'))
        for secret_cap, api_cap in ((size, size + 1), (size + 1, size)):
            self.config.config(
                max_secret_bytes=4 * ((secret_cap + 2) // 3), group='secrets')
            self.config.config(max_compose_bytes=api_cap, group='api')
            compose.parse(raw)
            with self.assertRaises(exception.Invalid) as caught:
                compose.parse(raw + ' ')
            self.assertIn('%d bytes' % size, str(caught.exception))
