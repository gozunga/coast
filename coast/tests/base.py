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

import fixtures
from oslo_config import cfg
from oslo_config import fixture as config_fixture
from oslo_db.sqlalchemy import enginefacade
from oslotest import base

from coast.common import config
from coast.common import notifications


class DatabaseFixture(fixtures.Fixture):
    """Give each test an independent in-memory SQLite enginefacade."""

    def _setUp(self):
        self.facade = enginefacade.transaction_context()
        # Recycling an in-memory connection discards its entire schema.
        # A wall-clock jump must not reset a database during a test.
        self.facade.configure(connection='sqlite://', sqlite_fk=True,
                              connection_recycle_time=-1)
        self.engine = self.facade.writer.get_engine()
        self.addCleanup(self.engine.dispose)


class TestCase(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.conf = cfg.ConfigOpts()
        self.config = self.useFixture(config_fixture.Config(self.conf))
        config.parse_args([], conf=self.conf, default_config_files=[])
        # oslo.messaging metrics options are registered on the global CONF
        # at import time, not by its public list_opts on isolated configs.
        self.conf.register_opt(cfg.BoolOpt('metrics_enabled', default=False),
                               group='oslo_messaging_metrics')
        self.config.config(transport_url='fake://')
        self.config.config(connection='sqlite://', group='database')
        # Unit tests never probe real addresses; probe tests opt in.
        self.config.config(http_check=False, stable_seconds=0,
                           stable_samples=1, group='conductor')
        # Keep unit tests off a broker. Cases that need messagingv2 opt in.
        self.config.config(driver=['test'],
                           group='oslo_messaging_notifications')
        self.addCleanup(notifications.reset)
        self.db = self.useFixture(DatabaseFixture())
