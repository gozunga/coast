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

"""Durable one-shot wrapper for the verified Q4 bootstrap method."""

import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from coast.agent.engines import bootstrap


class Conflict(Exception):
    pass


class PreconditionFailed(Exception):
    pass


def _claim(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                 os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory(path.parent)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_state(path, value):
    # Rename a synced file so process death cannot leave a partial identity.
    temporary = path.with_name(path.name + '.next')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                 os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_directory(path.parent)


class Engine:
    def __init__(self, conf):
        self.conf = conf
        self.root = Path(conf.agent.state_path)
        self.runtime = Path(conf.agent.runtime_path)
        for directory in (self.root, self.runtime):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.lstat()
            if (directory.resolve() != directory or
                    not stat.S_ISDIR(info.st_mode) or
                    info.st_uid != os.geteuid() or
                    stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('Agent directories must be owned and private')
        # Serialize startup cleanup with any local in-flight operation.
        fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR |
                     os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for abandoned in self.runtime.glob('token-*'):
                if abandoned.is_dir() and not abandoned.is_symlink():
                    shutil.rmtree(abandoned)

    def bootstrap(self, body, rotate=False):
        if self.conf.agent.role != 'controller':
            raise PreconditionFailed()
        if (not isinstance(body, dict) or
                body.get('engine_version') != bootstrap.VERSION):
            raise PreconditionFailed()
        fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR |
                     os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict() from None
            if rotate:
                return self.rotate(body)
            try:
                _claim(self.root / 'bootstrap-started')
            except FileExistsError:
                raise Conflict() from None
            try:
                with tempfile.TemporaryDirectory(
                        dir=self.runtime, prefix='token-') as directory:
                    output = Path(directory) / 'token'
                    bootstrap.bootstrap(output)
                    result = {'token': bootstrap.read_token(output),
                              'engine_version': bootstrap.VERSION}
                    _claim(self.root / 'bootstrap-done')
                return result
            except Exception:
                raise PreconditionFailed() from None
            finally:
                try:
                    bootstrap.check_image('coolify')
                    bootstrap.docker('exec', '--user', '0', 'coolify',
                                     'rm', '-f', bootstrap.CONTAINER_TOKEN)
                except Exception:
                    pass

    def rotate(self, body):
        if not (self.root / 'bootstrap-done').exists():
            raise PreconditionFailed()
        try:
            identifier = str(uuid.UUID(body['rotation_id']))
            if identifier != body['rotation_id']:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise PreconditionFailed() from None
        previous = body.get('previous_name')
        if (not isinstance(previous, str) or not re.fullmatch(
                r'coast-agent-(?:bootstrap-v1|rotation-[0-9a-f-]{36})',
                previous)):
            raise PreconditionFailed()
        name = 'coast-agent-rotation-' + identifier
        if previous == name:
            raise PreconditionFailed()
        directory = self.root / ('rotation-' + identifier)
        directory.mkdir(mode=0o700, exist_ok=True)
        manifest = directory / 'previous-name'
        if manifest.exists():
            if manifest.read_text() != previous:
                raise Conflict()
        else:
            _write_state(manifest, previous)
        output = directory / 'token'
        pending = self.root / 'rotation-pending'
        if pending.exists() and pending.read_text() != identifier:
            raise Conflict()
        if body.get('phase') == 'revoke':
            # The conductor has durably stored and published this replacement.
            if not (directory / 'revoked').exists():
                if not output.exists():
                    raise PreconditionFailed()
                self._revoke(previous)
                _claim(directory / 'revoked')
            output.unlink(missing_ok=True)
            _sync_directory(directory)
            if pending.exists():
                pending.unlink()
                _sync_directory(self.root)
            return {'status': 'ok'}
        if body.get('phase') != 'create':
            raise PreconditionFailed()
        if (directory / 'revoked').exists():
            raise Conflict()
        if not pending.exists():
            _write_state(pending, identifier)
        try:
            self._rotate(output, name)
            return {'token': bootstrap.read_token(output),
                    'engine_version': bootstrap.VERSION}
        except Exception:
            raise PreconditionFailed() from None

    def _rotate(self, output, name):
        # Only an unpublished replacement may be re-created after losing its
        # plaintext during engine-side creation. The current token stays valid.
        container_token = bootstrap.CONTAINER_TOKEN + '-' + name
        php = bootstrap.PHP.replace(
            "if (!file_exists($path)) {",
            "if (file_exists($path) && !preg_match('/^[0-9]+[|]"
            "[A-Za-z0-9]{48}\\s*$/', file_get_contents($path))) "
            "{ unlink($path); }\nif (!file_exists($path)) {").replace(
            "coast-agent-bootstrap-v1", name).replace(
                bootstrap.CONTAINER_TOKEN, container_token).replace(
            "if ($user->tokens()->where('name', $name)->exists()) "
            "{ exit(1); }",
            "$user->tokens()->where('name', $name)->delete();")
        bootstrap.bootstrap(output, token_payload=php,
                            container_token=container_token)

    def _revoke(self, name):
        # Version-gated Q4 exception, scoped to the previous Coast name.
        php = bootstrap.PHP.split('umask(0077);')[0] + (
            "App\\Models\\User::findOrFail(0)->tokens()->where('name', '" +
            name + "')->delete();\n} catch (\\Throwable $e) { exit(1); }\n")
        try:
            bootstrap.check_image('coolify')
            bootstrap.docker('exec', '-i', '--user', '0', 'coolify', 'php',
                             payload=php.encode())
        except Exception:
            raise PreconditionFailed() from None
