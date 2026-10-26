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

"""Bounded heartbeat status checks and post-commit worker failover casts."""

import datetime
import time

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import timeutils

from coast.common import context
from coast.common import notifications
from coast.common import safe_log
from coast.common import states
from coast.db import api as db_api
from coast.db import models
from coast.health_manager import failover
from coast.health_manager import keys
from coast import objects

LOG = logging.getLogger(__name__)

HEALTH_FAULT = 'Server heartbeat timeout.'
BATCH_SIZE = 100


def apply_health_rollup(session, project_id, stack):
    """Return the next stack status from live server rows.

    The caller holds the project lock and the stack lock. A non-ACTIVE
    live server makes an ACTIVE stack DEGRADED. A DEGRADED stack with the
    heartbeat fault returns to ACTIVE when every live server is ACTIVE.
    Other faults stay in place. The caller stores the returned status.
    """
    peers = session.query(models.Server.id).filter_by(
        stack_id=stack.id, project_id=project_id, deleted_at=None)
    unhealthy = session.query(peers.filter(
        models.Server.status != states.ACTIVE).exists()).scalar()
    if unhealthy and stack.status == states.ACTIVE:
        if stack.fault is None:
            stack.fault = HEALTH_FAULT
        return states.DEGRADED
    if (not unhealthy and stack.status == states.DEGRADED and
            stack.fault == HEALTH_FAULT):
        stack.fault = None
        return states.ACTIVE
    return None


class Detector:
    def __init__(self, conf=cfg.CONF, clock=timeutils.utcnow,
                 monotonic=time.monotonic, key_lookup=None):
        self.key_lookup = key_lookup
        self.conf = conf
        self.clock = clock
        self.monotonic = monotonic
        self.started = monotonic()
        self.cursor = ''
        self.admin = context.RequestContext(is_admin=True)

    def run(self):
        # Allow receivers to repopulate last-seen times after a restart.
        if self.monotonic() - self.started <= (
                self.conf.health_manager.heartbeat_timeout):
            return
        with db_api.reader.using(self.admin) as session:
            rows = session.query(
                models.Server.id, models.Server.project_id).join(
                    models.Stack,
                    models.Stack.id == models.Server.stack_id).filter(
                        models.Server.id > self.cursor,
                        models.Server.project_id == models.Stack.project_id,
                        models.Server.deleted_at.is_(None),
                        models.Server.status.in_(
                            [states.ACTIVE, states.UNHEALTHY]),
                        models.Server.task_state == states.NONE,
                        models.Stack.deleted_at.is_(None),
                        (((models.Stack.task_state == states.NONE) &
                          models.Stack.status.in_(
                              [states.ACTIVE, states.DEGRADED])) |
                         (models.Server.status == states.UNHEALTHY))).order_by(
                            models.Server.id).limit(BATCH_SIZE).all()
        for server_id, project_id in rows:
            try:
                self.check(context.RequestContext(project_id=project_id),
                           server_id)
            except Exception:
                LOG.warning('Heartbeat status check failed; will retry.')
            self.cursor = server_id
        if len(rows) < BATCH_SIZE:
            self.cursor = ''

    def check(self, owner, server_id):
        if self.key_lookup is not None:
            # A network lookup must never run under the health writer locks.
            server = db_api.servers.get(owner, server_id)
            try:
                self.key_lookup(server.stack_id)
            except keys.KeyUnavailable:
                return
            except ValueError:
                # Invalid or absent keys are not a secret-store outage.
                pass
        changes = []
        candidate = None
        blocked = False
        unhealthy = None
        now = self.clock()
        cutoff = now - datetime.timedelta(
            seconds=self.conf.health_manager.heartbeat_timeout)
        with db_api.writer.using(owner) as session:
            # Match API lock order; never hold a lock across network calls.
            db_api._lock_project(session, owner.project_id)
            server = db_api.servers._get(owner, server_id)
            stack = db_api.stacks._get(owner, server.stack_id, lock=True)
            if (stack.task_state != states.NONE or
                    stack.status not in (states.ACTIVE, states.DEGRADED)):
                if (stack.driver != 'noop' and failover.past_grace(
                        owner, server, self.conf, now)):
                    failover.warn_once(owner, server, LOG, 'stack_busy')
                return
            server = db_api.servers._get(owner, server_id, lock=True)
            if (server.task_state != states.NONE or
                    server.status not in (states.ACTIVE, states.UNHEALTHY)):
                return
            seen = server.last_heartbeat_at or server.created_at
            target = states.UNHEALTHY if seen < cutoff else states.ACTIVE
            changed = server.status != target
            self._change(server, target, 'server', changes)
            failover.record(owner, server, target, now, changed=changed)
            if changed and target == states.UNHEALTHY:
                unhealthy = objects.Server.obj_from_db(owner, server)
            session.flush()
            # SQL EXISTS bounds memory even for stacks with many servers.
            # Do not overwrite an unrelated fault or recover it later.
            target = apply_health_rollup(
                session, owner.project_id, stack)
            if target is not None:
                self._change(stack, target, 'environment', changes)
            baremetal = (stack.driver != 'noop' and server.role == 'control'
                         and server.kind != 'vm' and failover.past_grace(
                             owner, server, self.conf, now))
            candidate, blocked = None, False
            if baremetal:
                # No rebuild. UNHEALTHY -> ERROR is the bare-metal page.
                server.fault = failover.BAREMETAL_FAULT
                self._change(server, states.ERROR, 'server', changes)
                if failover.note_baremetal(owner, server):
                    blocked = True
                    candidate = objects.Server.obj_from_db(owner, server)
            elif (stack.driver != 'noop' and server.kind != 'vm' and
                  failover.past_grace(owner, server, self.conf, now)):
                failover.warn_once(owner, server, LOG, 'bare_metal')
            else:
                candidate, blocked = failover.candidate(
                    owner, server, stack, self.conf, now)
        if unhealthy is not None:
            safe_log.log_failover(
                LOG, 'info', unhealthy, 'unhealthy', 'heartbeat_timeout')
        for resource, payload in changes:
            for phase in ('start', 'end'):
                notifications.emit(owner, 'coast-health-manager', resource,
                                   'update', phase, payload, conf=self.conf)
        if candidate is not None or blocked:
            failover.dispatch(owner, candidate, blocked,
                              self.conf)

    @staticmethod
    def _change(row, target, resource, changes):
        if row.status == target:
            return
        states.check_transition(resource, row.status, target)
        row.status = target
        changes.append((resource, notifications.payload_for(row)))
