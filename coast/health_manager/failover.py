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

"""Durable health episodes and bounded worker failover handoff."""

import datetime
import uuid

from oslo_db import exception as db_exception
from oslo_log import log as logging
from oslo_utils import timeutils

from coast.common import notifications
from coast.common import safe_log
from coast.common import states
from coast.conductor import rpcapi
from coast.db import api as db_api
from coast import objects

LOG = logging.getLogger(__name__)

TEMPLATE_FAULT = 'Operator action needed: template services, Q114.'
BAREMETAL_FAULT = 'Operator action needed: bare-metal controller.'
ROLES = ('worker', 'build', 'control')


def episode(ctx, server):
    identifier = str(uuid.uuid5(uuid.UUID(server.id), 'health'))
    row = db_api.events._query(ctx).filter_by(id=identifier).first()
    return objects.Event.obj_from_db(ctx, row) if row else None


def record(ctx, server, target, now, changed=False):
    previous = episode(ctx, server)
    outcome = 'failure' if target == states.UNHEALTHY else 'success'
    if (previous and previous.outcome == outcome and not changed) or (
            not previous and target == states.ACTIVE):
        return
    if previous:
        detail = dict(previous.detail or {})
        detail.pop('failover_warnings', None)
        previous.detail = detail
        previous.outcome = outcome
        previous.message = now.isoformat()
        previous.save()
    else:
        objects.Event(context=ctx, stack_id=server.stack_id,
                      id=str(uuid.uuid5(uuid.UUID(server.id), 'health')),
                      resource_type='server', resource_id=server.id,
                      action='health', outcome=outcome,
                      message=now.isoformat()).create()


def past_grace(ctx, server, conf, now):
    """True when this UNHEALTHY episode is past failover grace."""
    if (not conf.health_manager.enable_failover or
            server.status != states.UNHEALTHY or
            server.task_state != states.NONE):
        return False
    now = timeutils.normalize_time(now)
    previous = episode(ctx, server)
    return bool(previous and previous.outcome == 'failure' and
                now > datetime.datetime.fromisoformat(
                    previous.message) + datetime.timedelta(
                    seconds=conf.health_manager.failover_grace) and
                timeutils.normalize_time(
                    server.last_heartbeat_at or server.created_at) <
                now - datetime.timedelta(
                    seconds=conf.health_manager.heartbeat_timeout))


def due(ctx, server, conf, now):
    if server.kind != 'vm' or server.role not in ROLES:
        return False
    return past_grace(ctx, server, conf, now)


def note_baremetal(ctx, server):
    """Record one bare-metal controller event for this unhealthy episode."""
    previous = episode(ctx, server)
    if not previous:
        return False
    identifier = str(uuid.uuid5(
        uuid.UUID(str(previous.id)), 'baremetal:' + (previous.message or '')))
    if db_api.events._query(ctx).filter_by(id=identifier).first():
        return False
    objects.Event(context=ctx, id=identifier, stack_id=server.stack_id,
                  resource_type='server', resource_id=server.id,
                  action='failover', outcome='failure',
                  message=BAREMETAL_FAULT).create()
    return True


def templates(ctx, server):
    model = db_api.services.model
    compose_sources = db_api.sources._query(ctx).filter_by(
        type='compose').with_entities(db_api.sources.model.id)
    return db_api.services._query(ctx).filter(
        model.server_id == server.id,
        model.template_id.isnot(None) |
        model.source_id.in_(compose_sources)).first()


def candidate(ctx, server, stack, conf, now):
    if stack.driver == 'noop' or not due(ctx, server, conf, now):
        return None, False
    if server.role == 'control':
        return objects.Server.obj_from_db(ctx, server), False
    if server.role == 'worker' and templates(ctx, server):
        previous = episode(ctx, server)
        identifier = str(uuid.uuid5(
            uuid.UUID(previous.id), 'template:' + previous.message))
        if not db_api.events._query(ctx).filter_by(id=identifier).first():
            objects.Event(context=ctx, id=identifier, stack_id=stack.id,
                          resource_type='server', resource_id=server.id,
                          action='failover', outcome='failure',
                          message=TEMPLATE_FAULT).create()
            return objects.Server.obj_from_db(ctx, server), True
        return None, False
    return objects.Server.obj_from_db(ctx, server), False


def dispatch(ctx, server, blocked, conf):
    # Called only after the health transaction commits. No claim is held
    # across a cast; the conductor owns global admission and final checks.
    if blocked:
        reason = 'bare_metal' if server.kind != 'vm' else 'template_worker'
        warn_once(ctx, server, LOG, reason)
        notifications.emit(ctx, 'coast-health-manager', 'server', 'failover',
                           'error', notifications.payload_for(server),
                           conf=conf)
    else:
        rpcapi.ConductorAPI().cast(ctx, 'failover_server', server)
        safe_log.log_failover(LOG, 'info', server, 'dispatch', 'grace_expired')


def warn_once(ctx, server, logger, reason):
    """Remember refusal reasons on the durable health episode."""
    # Dispatch also calls this after the health transaction has committed.
    # Serialize the read/modify/write with admission and health checks.
    try:
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            previous = episode(ctx, server)
            if previous:
                detail = dict(previous.detail or {})
                reasons = list(detail.get('failover_warnings', []))
                if reason in reasons:
                    return
                reasons.append(reason)
                detail['failover_warnings'] = reasons
                previous.detail = detail
                previous.save()
            safe_log.log_failover(logger, 'warning', server, 'refused', reason)
    except db_exception.CantStartEngineError:
        # Still expose the decision when no database is configured, but
        # propagate the failure rather than proceeding without persistence.
        safe_log.log_failover(logger, 'warning', server, 'refused', reason,
                              deduplication='unavailable')
        raise
