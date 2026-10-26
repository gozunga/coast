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

"""Controller-local registry repository delete and blob garbage collection.

The agent stops the registry, deletes named repository directories on
the registry volume, runs garbage-collect, and starts the registry.
It does not read the engine database and it does not use the registry
password.
"""

import configparser
from pathlib import Path
import re
import shutil
import subprocess


NAME = 'coast-registry'
CONF = Path('/etc/coast/coast.conf')
ROOT = Path('/var/lib/registry/docker/registry/v2/repositories')
CONFIG = Path('/etc/coast/registry/config.yml')
HTPASSWD = Path('/etc/coast/registry/htpasswd')
CERT = Path('/etc/coast/registry/tls.crt')
KEY = Path('/etc/coast/registry/tls.key')
DATA = Path('/var/lib/registry')
_REPO = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/'
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')


class RegistryError(OSError):
    """Safe registry failure. The message has no command output."""


def _run(args, runner):
    if runner is None:
        result = subprocess.run(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=600,
                                check=False)
        if result.returncode:
            raise RegistryError('Registry command failed')
        return b''
    result = runner(args)
    if result is None:
        raise RegistryError('Registry command failed')
    return result


def _repositories(body):
    if not isinstance(body, dict) or set(body) != {'repositories'}:
        raise ValueError('Registry request is invalid')
    names = body['repositories']
    if not isinstance(names, list) or any(
            not isinstance(name, str) or not _REPO.fullmatch(name)
            for name in names):
        raise ValueError('Registry request is invalid')
    return names


def _image():
    config = configparser.ConfigParser()
    config.read(CONF)
    image = config.get('registry', 'image', fallback='')
    name, separator, digest = image.partition('@')
    if (name != 'registry:3' or separator != '@' or
            not digest.startswith('sha256:') or len(digest) != 71):
        raise RegistryError('Registry image is not pinned')
    return image


def _delete(root, name):
    base = root.resolve()
    current = base
    for part in name.split('/'):
        if part in ('', '.', '..'):
            raise ValueError('Registry repository is invalid')
        current = current / part
        if current.is_symlink():
            raise ValueError('Registry repository is invalid')
    target = current.resolve()
    if base not in target.parents:
        raise ValueError('Registry repository is invalid')
    if target.exists():
        shutil.rmtree(target)


def _mounts():
    pairs = (
        (DATA, '/var/lib/registry', ''),
        (CONFIG, '/etc/distribution/config.yml', ':ro'),
        (HTPASSWD, '/auth/htpasswd', ':ro'),
        (CERT, '/certs/tls.crt', ':ro'),
        (KEY, '/certs/tls.key', ':ro'),
    )
    args = []
    for source, target, mode in pairs:
        if not source.is_file() and source != DATA:
            raise RegistryError('Registry files are required')
        if source == DATA and not source.is_dir():
            raise RegistryError('Registry volume is required')
        args.extend(('--volume', '%s:%s%s' % (source, target, mode)))
    return args


def garbage_collect(body, runner=None, root=None):
    """Delete named repositories, then collect unreferenced blobs.

    ``repositories`` may be empty. An empty list still runs garbage
    collection. A missing registry container fails closed.
    """
    names = _repositories(body)
    image = _image()
    root = ROOT if root is None else root
    present = True
    try:
        _run(['docker', 'inspect', NAME], runner)
    except RegistryError:
        present = False
    if not present:
        raise RegistryError('Registry container is absent')
    if not root.is_dir() or not (root.parent / 'blobs').is_dir():
        # Nothing was ever pushed. distribution garbage-collect exits 1 on
        # a missing repositories or blobs path, and no named repository
        # can exist without them. Leave the registry running.
        return {'collected': 0}
    _run(['docker', 'stop', NAME], runner)
    error = None
    try:
        for name in names:
            _delete(root, name)
        _run(['docker', 'run', '--rm', '--pull', 'never', '--name',
              NAME + '-gc', *_mounts(), image.partition('@')[0],
              'garbage-collect',
              '/etc/distribution/config.yml'], runner)
    except Exception as exc:
        error = exc
    try:
        _run(['docker', 'start', NAME], runner)
    except RegistryError:
        if error is None:
            raise
    if error is not None:
        raise error
    return {'collected': len(names)}
