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

"""Serial-guarded rotation with the server task lock and durable receipts."""

from datetime import datetime
from datetime import timezone
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography import x509
from oslo_log import log as logging

from coast.common import notifications
from coast.common import safe_log
from coast.common import states
from coast.common import tls
from coast.conductor import agent_client
from coast.db import api as db_api
from coast import objects

LOG = logging.getLogger(__name__)
ACTION = 'rotate_certificate'
FAULT = 'Certificate rotation failed; operator recovery is required.'


def receipt_id(server):
    return str(uuid.uuid5(uuid.UUID(server.id),
                          ACTION + ':' + server.agent_cert_serial))


def receipt(ctx, server):
    return db_api.events._query(ctx).filter_by(id=receipt_id(server)).first()


def due(cert, now=None):
    now = now or datetime.now(timezone.utc)
    return now >= cert.not_valid_before_utc + (
        cert.not_valid_after_utc - cert.not_valid_before_utc) * 0.8


def event(manager, ctx, server, phase, identifier, message=None):
    outcome = {'start': 'start', 'end': 'success', 'error': 'failure'}[phase]
    objects.Event(context=ctx, id=identifier, stack_id=server.stack_id,
                  resource_type='server', resource_id=server.id,
                  action=ACTION, outcome=outcome, message=message,
                  request_id=ctx.request_id).create()
    notifications.emit(ctx, notifications.CONDUCTOR, 'server', ACTION, phase,
                       notifications.payload_for(server, ctx), manager.conf,
                       notifier=manager.notifier)


def _failure_exc(exc):
    """Return the linked error so the log names the original class.

    Certificate connections replace the original error ``from None``.
    That keeps exception text out of the stored receipt. ``__context__``
    still holds the original error. One link is enough for that wrapper.
    """
    linked = exc.__cause__
    if not isinstance(linked, Exception):
        linked = exc.__context__
    if isinstance(linked, Exception):
        return linked
    return exc


def rotate(manager, ctx, listed):
    conf = manager.conf
    with db_api.writer.using(ctx):
        db_api._lock_project(ctx.session, ctx.project_id)
        stack = objects.Stack.obj_from_db(ctx, db_api.stacks._get(
            ctx, listed.stack_id, lock=True))
        server = objects.Server.obj_from_db(ctx, db_api.servers._get(
            ctx, listed.id, lock=True))
        # A deployment holds stack RECONCILING for the whole attempt.
        # Rotation must still run. Claim only this server, from NONE.
        # Stack delete sets DELETING before each server task changes.
        # Failover of this server sets the server task. Another server
        # on a stack that is FAILING_OVER stays eligible.
        if (server.stack_id != stack.id or stack.driver == 'noop' or
                stack.status not in (states.ACTIVE, states.DEGRADED) or
                server.status != states.ACTIVE or
                server.role not in ('control', 'worker', 'build') or
                server.task_state != states.NONE or
                stack.task_state == states.DELETING or
                not server.agent_cert_serial or not server.mgmt_ip or
                listed.agent_cert_serial != server.agent_cert_serial or
                receipt(ctx, server)):
            return
        server.acquire_task(states.RECONCILING)
    started = False
    identifier = receipt_id(server)
    final_id = str(uuid.uuid5(uuid.UUID(identifier), 'complete'))
    try:
        client = agent_client.AgentClient(conf, server.mgmt_ip)
        current = client.certificate_connection(server.id)
        if str(current.serial_number) != server.agent_cert_serial:
            raise ValueError('Stored certificate serial differs')
        if not due(current):
            return
        with db_api.writer.using(ctx):
            event(manager, ctx, server, 'start', identifier)
        started = True
        replacement = tls.issue_server_certificate(
            server.id, server.mgmt_ip, conf=conf)
        cert = x509.load_pem_x509_certificate(replacement.certificate_pem)
        result = client.certificate_connection(server.id, {
            'expected_serial': server.agent_cert_serial,
            'certificate': replacement.certificate_pem.decode('ascii'),
            'private_key': replacement.private_key_pem.decode('ascii')})
        if result != {'status': 'rotated', 'serial': str(cert.serial_number)}:
            raise ValueError('Certificate update was not confirmed')
        peer = client.certificate_connection(server.id)
        if (peer.serial_number != cert.serial_number or
                peer.public_bytes(serialization.Encoding.DER) !=
                cert.public_bytes(serialization.Encoding.DER)):
            raise ValueError('New certificate was not presented')
        with db_api.writer.using(ctx):
            server.agent_cert_serial = str(peer.serial_number)
            server.agent_cert_expires_at = peer.not_valid_after_utc
            server.save()
            event(manager, ctx, server, 'end', final_id)
    except Exception as exc:
        # Failed or interrupted attempts are not automatically replayed.
        # No replacement key or remote exception is stored in a receipt.
        # Log before the receipt write. A database error must not hide
        # the class that caused the failure.
        safe_log.log_failure(
            LOG,
            'Certificate rotation failed for server %(server)s stack '
            '%(stack)s %(phase)s the start receipt: %(cause)s at '
            '%(where)s',
            _failure_exc(exc),
            server=server.id,
            stack=server.stack_id,
            phase='after' if started else 'before')
        with db_api.writer.using(ctx):
            fresh = objects.Server.get_by_id(ctx, server.id)
            if not started:
                event(manager, ctx, fresh, 'start', identifier)
            event(manager, ctx, fresh, 'error', final_id, FAULT)
    finally:
        with db_api.writer.using(ctx):
            # Leave the stack task for the operation that claimed it.
            server.release_task(states.RECONCILING)
