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

"""Durable quiesce leases for volume owners; no engine DB or docker exec."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import uuid

from oslo_log import log as logging

from coast.agent.engines import coolify


LOG = logging.getLogger(__name__)
CONTAINER_ID = re.compile(r'[0-9a-f]{64}')
OWNER_ROLES = ('controller', 'worker', 'build')


def docker(*args, timeout=30):
    try:
        result = subprocess.run(
            ['docker', *args], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        if result.returncode:
            raise coolify.PreconditionFailed()
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise coolify.PreconditionFailed() from None


def identity(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('A canonical UUID is required')
    return value


class Quiesce:
    def __init__(self, conf, command=docker, clock=time.time,
                 directory='export-leases'):
        self.conf = conf
        self.command = command
        self.clock = clock
        self.root = Path(conf.agent.state_path) / directory
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (self.root.resolve() != self.root or
                not stat.S_ISDIR(info.st_mode) or
                info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('Export state must be owned and private')
        coolify._sync_directory(self.root.parent)

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR |
                     os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self, export_id):
        path = self.root / export_id
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as stream:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.geteuid() or
                    stat.S_IMODE(info.st_mode) != 0o600):
                raise coolify.PreconditionFailed()
            return json.load(stream)

    def write(self, export_id, record):
        fd, name = tempfile.mkstemp(dir=self.root, prefix='.write-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / export_id)
            coolify._sync_directory(self.root)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def validate(self, body, quiesce=False):
        if self.conf.agent.role not in OWNER_ROLES:
            raise coolify.PreconditionFailed()
        keys = {'export_id', 'volume_ids'} if quiesce else {'export_id'}
        if not isinstance(body, dict) or set(body) != keys:
            raise ValueError('Invalid export request')
        export_id = identity(body['export_id'])
        if quiesce:
            volumes = body['volume_ids']
            if (not isinstance(volumes, list) or not volumes or
                    len(volumes) > 1000):
                raise ValueError('Invalid volume selection')
            volumes = [identity(value) for value in volumes]
            if len(set(volumes)) != len(volumes):
                raise ValueError('Duplicate volume')
        return export_id

    def containers(self, deadline):
        # Stop all local persistent containers rather than guess DB images.
        # Inspect only documented Docker state/mount metadata, not env vars.
        output = self.command('ps', '--no-trunc', '--quiet',
                              timeout=max(0.01, deadline - self.clock()))
        ids = output.decode('ascii').split()
        if any(not CONTAINER_ID.fullmatch(value) for value in ids):
            raise coolify.PreconditionFailed()
        selected = []
        for value in ids:
            if self.clock() >= deadline:
                raise coolify.PreconditionFailed()
            mounts = json.loads(self.command(
                'inspect', '--format', '{{json .Mounts}}', value,
                timeout=min(30, max(0.01, deadline - self.clock()))))
            if any(mount.get('Type') in ('volume', 'bind')
                   for mount in mounts):
                selected.append(value)
        return selected

    def quiesce(self, body):
        export_id = self.validate(body, quiesce=True)
        volumes = sorted(body['volume_ids'])
        with self.locked():
            record = self.read(export_id)
            if record:
                if record['volume_ids'] != volumes:
                    raise coolify.Conflict()
                if record['state'] != 'quiesced':
                    raise coolify.Conflict()
                if self.clock() >= record['deadline']:
                    self._resume(export_id, record)
                    raise coolify.Conflict()
                return {'status': 'quiesced', 'deadline': record['deadline']}
            # One lease at a time: another export must not resume our stop set.
            for path in self.root.iterdir():
                if path.name.startswith('.'):
                    continue
                previous = self.read(identity(path.name))
                if previous['state'] != 'resumed':
                    raise coolify.Conflict()
            record = {'volume_ids': volumes, 'containers': [],
                      'state': 'stopping',
                      'deadline': self.clock() +
                      self.conf.agent.export_auto_resume_timeout}
            self.write(export_id, record)
            try:
                for value in self.containers(record['deadline']):
                    remaining = record['deadline'] - self.clock()
                    if remaining <= 0:
                        raise coolify.PreconditionFailed()
                    # Write intent BEFORE stop: a crash with an uncertain stop
                    # must still resume this originally running container.
                    record['containers'].append(value)
                    self.write(export_id, record)
                    self.command('stop', '-t', '10', value,
                                 timeout=min(30, remaining))
                if self.clock() >= record['deadline']:
                    raise coolify.PreconditionFailed()
                record['state'] = 'quiesced'
                self.write(export_id, record)
                return {'status': 'quiesced', 'deadline': record['deadline']}
            except Exception:
                try:
                    self._resume(export_id, record)
                except Exception:
                    # The durable record stays pending for the watchdog.
                    LOG.warning('Export resume requires another attempt.')
                raise coolify.PreconditionFailed() from None

    def _resume(self, export_id, record):
        if record['state'] == 'resumed':
            return
        failed = False
        for value in list(record['containers']):
            if not CONTAINER_ID.fullmatch(value):
                raise coolify.PreconditionFailed()
            try:
                # docker start is idempotent for an already running container.
                self.command('start', value)
            except Exception:
                failed = True
                continue
            record['containers'].remove(value)
            self.write(export_id, record)
        if not failed:
            record['state'] = 'resumed'
            self.write(export_id, record)
        else:
            raise coolify.PreconditionFailed()

    def resume(self, body):
        export_id = self.validate(body)
        with self.locked():
            record = self.read(export_id)
            if record is None:
                # A resume that overtakes quiesce must prevent a late stop.
                record = {'volume_ids': [], 'containers': [],
                          'state': 'resumed', 'deadline': self.clock()}
                self.write(export_id, record)
            self._resume(export_id, record)
        return {'status': 'resumed'}

    def recover(self, force=False):
        if self.conf.agent.role not in OWNER_ROLES:
            return
        with self.locked():
            for path in self.root.iterdir():
                if path.name.startswith('.'):
                    continue
                try:
                    export_id = identity(path.name)
                    record = self.read(export_id)
                    if (record['state'] != 'resumed' and
                            (force or record['state'] == 'stopping' or
                             self.clock() >= record['deadline'])):
                        self._resume(export_id, record)
                except Exception:
                    # Keep other leases recoverable; no command output logged.
                    LOG.warning('Export resume requires another attempt.')
