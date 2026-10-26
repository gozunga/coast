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

from collections import OrderedDict
from unittest import mock

import fixtures

from coast.conductor import secret_retry
from coast.tests import base


class SecretRetryTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.useFixture(fixtures.MonkeyPatch(
            'coast.conductor.secret_retry._RETRIES', OrderedDict()))

    def test_exponential_delay_caps_and_success_clears(self):
        now = 100
        with mock.patch.object(secret_retry.time, 'monotonic') as clock:
            for delay in (10, 20, 40, 80, 160, 300, 300):
                clock.return_value = now
                self.assertTrue(secret_retry.ready('server'))
                secret_retry.defer('server', 'stack')
                self.assertFalse(secret_retry.ready('server'))
                clock.return_value = now + delay - 0.1
                self.assertFalse(secret_retry.ready('server'))
                now += delay
            secret_retry.clear('server')
            self.assertTrue(secret_retry.ready('server'))
