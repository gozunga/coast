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

"""Bounded certificate lifetime checks; claims belong to the conductor."""

import time

from oslo_log import log as logging
import sqlalchemy as sa

from coast.common import context
from coast.common import states
from coast.conductor import agent_client
from coast.conductor import certificates
from coast.conductor import rpcapi
from coast.db import api as db_api
from coast.db import models
from coast import objects

LOG = logging.getLogger(__name__)
LIMIT = 100
BUDGET = 30


class Rotation:
    def __init__(self, conf):
        self.conf = conf
        self.cursor = ''
        self._expires = None

    def _after(self, expires):
        # Keyset for nullable expiry, then id. Both ascend. NULL is first.
        # The same shape is in coast.db.api.Repository.list.
        if self._expires is None:
            return sa.or_(
                expires.is_not(None),
                sa.and_(expires.is_(None), models.Server.id > self.cursor))
        return sa.or_(
            expires > self._expires,
            sa.and_(expires == self._expires,
                    models.Server.id > self.cursor))

    def _candidates(self, session):
        expires = models.Server.agent_cert_expires_at
        # IS NOT NULL ascending puts unknown expiry first on SQLite and
        # MySQL. See the same NULL ordering in coast.db.api.
        query = session.query(models.Server).join(models.Stack).filter(
            models.Server.project_id == models.Stack.project_id,
            models.Server.deleted_at.is_(None),
            models.Server.status == states.ACTIVE,
            models.Server.task_state == states.NONE,
            models.Server.agent_cert_serial.isnot(None),
            models.Server.mgmt_ip.isnot(None),
            models.Stack.deleted_at.is_(None),
            models.Stack.status.in_((states.ACTIVE, states.DEGRADED)),
            models.Stack.task_state != states.DELETING,
            models.Stack.driver != 'noop')
        if self.cursor:
            query = query.filter(self._after(expires))
        return query.order_by(
            expires.is_not(None).asc(), expires.asc(),
            models.Server.id.asc()).limit(LIMIT).all()

    def run(self):
        admin = context.RequestContext(is_admin=True)
        started = time.monotonic()
        with db_api.reader.using(admin) as session:
            rows = self._candidates(session)
            batch = [(objects.Server.obj_from_db(admin, row),
                      row.agent_cert_expires_at) for row in rows]
        for server, expires_at in batch:
            if time.monotonic() - started >= BUDGET:
                return
            ctx = context.RequestContext(project_id=server.project_id)
            self.cursor = server.id
            self._expires = expires_at
            try:
                with db_api.reader.using(ctx):
                    if certificates.receipt(ctx, server):
                        continue
                client = agent_client.AgentClient(self.conf, server.mgmt_ip)
                peer = client.certificate_connection(server.id)
                if (str(peer.serial_number) != server.agent_cert_serial or
                        certificates.due(peer)):
                    rpcapi.ConductorAPI().cast(ctx, 'rotate_server', server)
            except Exception:
                LOG.warning('Certificate lifetime check failed; will retry.')
        if len(batch) < LIMIT:
            self.cursor = ''
            self._expires = None
