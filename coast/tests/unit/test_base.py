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

"""Regression tests for shared test fixtures."""

import time
from unittest import mock

from oslotest import base as test_base
import sqlalchemy as sa

from coast.tests import base


class DatabaseFixtureTest(test_base.BaseTestCase):
    def test_clock_jump_preserves_in_memory_database(self):
        db = self.useFixture(base.DatabaseFixture())
        with db.engine.begin() as connection:
            connection.execute(sa.text('CREATE TABLE probe (id INTEGER)'))
            connection.execute(sa.text('INSERT INTO probe VALUES (1)'))
        future = time.time() + 7200
        with mock.patch('sqlalchemy.pool.base.time.time', return_value=future):
            with db.engine.connect() as connection:
                self.assertEqual(
                    1, connection.execute(sa.text('SELECT id FROM probe'))
                    .scalar_one())
