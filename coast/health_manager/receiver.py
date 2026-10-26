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

"""Q79 validation and bounded batched last-seen updates; no failover."""

import datetime
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from oslo_config import cfg
from oslo_log import log as logging
import sqlalchemy as sa

from coast.common import context
from coast.common import exception
from coast.db import api as db_api
from coast.db import metrics
from coast.db import models

LOG = logging.getLogger(__name__)
FIELDS = {'server_id', 'env_id', 'seq', 'ts', 'docker_ok', 'disk_pct',
          'engine_ok'}


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Duplicate heartbeat fields')
    return result


def _number(value):
    return (type(value) in (int, float) and math.isfinite(value))


class Receiver:
    """Use one owner thread. key_lookup(env_id) returns 32 raw key bytes.

    Lookup is configured by the operator and resolves the stack Barbican ref.
    Never accept a packet or write a checkpoint when lookup fails.
    """

    def __init__(self, key_lookup, conf=cfg.CONF, clock=time.time):
        self.options = conf.health_manager
        self.key_lookup = key_lookup
        self.clock = clock
        self.pending = {}
        self.window = 0
        self.count = 0
        self.state = Path(self.options.state_path)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (self.state.is_symlink() or self.state.stat().st_mode & 0o077 or
                self.state.stat().st_uid != os.geteuid()):
            raise ValueError('Heartbeat state directory must be private')
        fd = os.open(self.state / '.lock',
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, 'w')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError('Heartbeat state directory is already in use')
        self.admin = context.RequestContext(is_admin=True)

    def close(self):
        self.lock.close()

    def _checkpoint(self, server_id, sequence):
        path = self.state / server_id
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                previous = int(stream.read(64))
            if not 0 < previous < 2 ** 64:
                raise ValueError('Invalid replay checkpoint')
            if sequence <= previous:
                return False
        except FileNotFoundError:
            pass
        # Write before accepting: a crash cannot reuse an accepted sequence.
        with tempfile.NamedTemporaryFile(dir=self.state, mode='w',
                                         delete=False) as stream:
            temporary = stream.name
            try:
                stream.write(str(sequence))
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, path)
                fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return True

    def accept(self, packet, source_ip):
        now = self.clock()
        window = int(time.monotonic())
        if window != self.window:
            self.window, self.count = window, 0
        self.count += 1
        if (self.count > self.options.max_packets_per_second or
                not 32 < len(packet) <= self.options.max_packet_size):
            return False
        try:
            message, digest = packet[:-32], packet[-32:]
            payload = json.loads(message.decode('utf-8'),
                                 object_pairs_hook=_object)
            if (not isinstance(payload, dict) or
                    set(payload) not in (FIELDS, FIELDS | {'metric'}) or
                    json.dumps(payload, sort_keys=True, separators=(',', ':'))
                    .encode('utf-8') != message):
                return False
            for name in ('server_id', 'env_id'):
                if str(uuid.UUID(payload[name])) != payload[name]:
                    return False
            if (type(payload['seq']) is not int or
                    not 0 < payload['seq'] < 2 ** 64 or
                    not _number(payload['ts']) or
                    abs(payload['ts'] - now) >= 60 or
                    not _number(payload['disk_pct']) or
                    not 0 <= payload['disk_pct'] <= 100 or
                    type(payload['docker_ok']) is not bool or
                    type(payload['engine_ok']) is not bool):
                return False
            key = self.key_lookup(payload['env_id'])
            if not isinstance(key, bytes) or len(key) != 32:
                return False
            if not hmac.compare_digest(
                    digest, hmac.new(key, message, hashlib.sha256).digest()):
                return False
            server = db_api.servers.get(self.admin, payload['server_id'],
                                        all_projects=True)
            owner = context.RequestContext(project_id=server.project_id)
            db_api.stacks.get(owner, payload['env_id'])
            if (server.stack_id != payload['env_id'] or
                    server.mgmt_ip != source_ip):
                return False
            if 'metric' in payload:
                metrics.validate(payload['metric'])
            if (server.id not in self.pending and
                    len(self.pending) >= self.options.batch_size):
                self.flush()
            if not self._checkpoint(server.id, payload['seq']):
                return False
            if 'metric' in payload:
                timestamp = datetime.datetime.fromtimestamp(
                    payload['ts'], datetime.timezone.utc).replace(tzinfo=None)
                metrics.ingest(owner, server, payload['metric'], timestamp)
            # Never trust guest time for the DB's last-seen time.
            self.pending[server.id] = (
                server.project_id, server.stack_id, source_ip,
                datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
                .replace(tzinfo=None))
            return True
        except (ValueError, TypeError, OverflowError, RecursionError,
                exception.NotFound):
            return False
        except Exception:
            # Hooks, filesystem and DB failures may contain secrets. Do not
            # log the exception, source packet, stack or key.
            LOG.warning('Heartbeat processing failed; packet rejected.')
            return False

    def flush(self):
        if not self.pending:
            return
        # One transaction per bounded batch. Retain the batch on DB failure.
        with db_api.writer.using(self.admin) as session:
            for server_id, (project_id, env_id, source, seen) in (
                    self.pending.items()):
                live_env = session.query(models.Stack.id).filter_by(
                    id=env_id, project_id=project_id, deleted_at=None).exists()
                session.query(models.Server).filter(
                    models.Server.id == server_id,
                    models.Server.project_id == project_id,
                    models.Server.stack_id == env_id,
                    models.Server.mgmt_ip == source,
                    models.Server.deleted_at.is_(None), live_env,
                    sa.or_(models.Server.last_heartbeat_at.is_(None),
                           models.Server.last_heartbeat_at < seen)).update(
                               # Keep updated_at. SQLAlchemy applies
                               # Column.onupdate when the column is absent
                               # from SET, and conductor uses that revision.
                               {'last_heartbeat_at': seen,
                                'updated_at': models.Server.updated_at},
                               synchronize_session=False)
        self.pending.clear()
