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

"""Bounded snapshot archive creation and credential-free HTTPS PUT."""

import contextlib
import gzip
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import tarfile
import tempfile
import time
from urllib import parse

from coast.agent.engines import coolify
from coast.agent import export


CHUNK = 1024 * 1024


def destination(url):
    if not isinstance(url, str) or any(ord(c) <= 32 for c in url):
        raise ValueError('Invalid upload destination')
    try:
        parts = parse.urlsplit(url)
        parts.port  # Validate the port without connecting.
    except ValueError:
        raise ValueError('Invalid upload destination') from None
    if (parts.scheme != 'https' or not parts.hostname or
            parts.username or parts.password or parts.fragment or
            not parts.path or not parts.query):
        raise ValueError('A signed HTTPS PUT destination is required')
    return parts


def command(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise coolify.PreconditionFailed()
    subprocess.run(args, check=True, timeout=remaining,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextlib.contextmanager
def mounted(device, filesystem, directory, deadline):
    path = Path(device)
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not resolved.is_relative_to('/dev') or not stat.S_ISBLK(info.st_mode):
        raise coolify.PreconditionFailed()
    # Reject an already mounted device, including its aliases, before mount.
    number = '%s:%s' % (os.major(info.st_rdev), os.minor(info.st_rdev))
    with open('/proc/self/mountinfo') as stream:
        if any(line.split()[2] == number for line in stream):
            raise coolify.PreconditionFailed()
    options = {'ext4': 'ro,noload', 'xfs': 'ro,nouuid,norecovery'}
    target = directory / 'mount'
    target.mkdir(mode=0o700)
    try:
        command(['mount', '-t', filesystem, '-o', options[filesystem],
                 str(resolved), str(target)], deadline)
        yield target
    finally:
        # Also attempt cleanup for an uncertain mount subprocess outcome.
        command(['umount', str(target)], time.monotonic() + 30)


class LimitedWriter:
    def __init__(self, stream, limit, deadline):
        self.stream = stream
        self.limit = limit
        self.deadline = deadline
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, data):
        if (self.size + len(data) > self.limit or
                time.monotonic() >= self.deadline):
            raise coolify.PreconditionFailed()
        count = self.stream.write(data)
        self.size += count
        self.digest.update(data[:count])
        return count

    def flush(self):
        self.stream.flush()


def build_archive(root, purpose, manifest, stream, raw_limit,
                  object_limit, deadline, manifest_limit=1048576):
    selected = root if purpose == 'data' else root / 'volumes'
    if not selected.is_dir() or selected.is_symlink():
        raise coolify.PreconditionFailed()
    writer = LimitedWriter(stream, object_limit, deadline)
    total = 0
    checksums = {}
    metadata_size = len(json.dumps(manifest).encode())

    def walk_error(error):
        raise coolify.PreconditionFailed()

    with gzip.GzipFile(fileobj=writer, mode='wb', mtime=0, filename='') as gz:
        with tarfile.open(fileobj=gz, mode='w|') as archive:
            for current, directories, files in os.walk(selected,
                                                       followlinks=False,
                                                       onerror=walk_error):
                directories.sort()
                for name in sorted(directories + files):
                    path = Path(current) / name
                    info = path.lstat()
                    relative = 'data/' + path.relative_to(root).as_posix()
                    metadata_size += len(relative.encode()) + 80
                    if metadata_size > manifest_limit:
                        raise coolify.PreconditionFailed()
                    if stat.S_ISDIR(info.st_mode):
                        member = tarfile.TarInfo(relative)
                        member.type = tarfile.DIRTYPE
                        member.mode = stat.S_IMODE(info.st_mode)
                        archive.addfile(member)
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        # Do not follow links or export device/socket nodes.
                        raise coolify.PreconditionFailed()
                    total += info.st_size
                    if total > raw_limit or time.monotonic() >= deadline:
                        raise coolify.PreconditionFailed()
                    digest = hashlib.sha256()
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, 'rb') as source:
                        opened = os.fstat(fd)
                        if (opened.st_ino != info.st_ino or
                                opened.st_dev != info.st_dev or
                                opened.st_size != info.st_size):
                            raise coolify.PreconditionFailed()
                        while chunk := source.read(CHUNK):
                            if time.monotonic() >= deadline:
                                raise coolify.PreconditionFailed()
                            digest.update(chunk)
                        source.seek(0)
                        member = tarfile.TarInfo(relative)
                        member.size = info.st_size
                        member.mode = stat.S_IMODE(info.st_mode)
                        archive.addfile(member, source)
                    checksums[relative] = digest.hexdigest()
            document = dict(manifest, checksums=checksums,
                            exported_paths=['/' if purpose == 'data'
                                            else 'volumes/'])
            payload = json.dumps(document, sort_keys=True,
                                 separators=(',', ':')).encode('utf-8')
            if len(payload) > manifest_limit:
                raise coolify.PreconditionFailed()
            member = tarfile.TarInfo('manifest.json')
            member.size = len(payload)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(payload))
    return {'status': 'uploaded', 'size': writer.size,
            'sha256': writer.digest.hexdigest()}


def upload(url, stream, size, deadline):
    parts = destination(url)
    connection = http.client.HTTPSConnection(
        parts.hostname, parts.port or 443,
        timeout=max(0.01, deadline - time.monotonic()),
        context=ssl.create_default_context())
    try:
        connection.putrequest('PUT', parts.path + '?' + parts.query)
        connection.putheader('Content-Type', 'application/gzip')
        connection.putheader('Content-Length', str(size))
        connection.endheaders()
        stream.seek(0)
        while chunk := stream.read(CHUNK):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise coolify.PreconditionFailed()
            connection.sock.settimeout(remaining)
            connection.send(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise coolify.PreconditionFailed()
        connection.sock.settimeout(remaining)
        response = connection.getresponse()
        if response.status not in (200, 201, 204):
            # Do not follow redirects or read potentially sensitive errors.
            raise coolify.PreconditionFailed()
    finally:
        connection.close()


class Archive(export.Quiesce):
    """One immutable selection and destination per export and source volume."""

    def __init__(self, conf, mount=mounted, put=upload):
        super().__init__(conf, directory='export-archives')
        self.mount = mount
        self.put = put

    def archive(self, body):
        try:
            return self._archive(body)
        except ValueError:
            raise ValueError('Invalid archive request') from None
        except coolify.Conflict:
            raise
        except Exception:
            # URLs, signatures, paths and subprocess errors never escape.
            raise coolify.PreconditionFailed() from None

    def _archive(self, body):
        keys = {'export_id', 'volume_id', 'device', 'filesystem', 'purpose',
                'put_url', 'manifest'}
        if (self.conf.agent.role not in export.OWNER_ROLES or
                not isinstance(body, dict) or set(body) != keys):
            raise ValueError('Invalid archive request')
        export_id = export.identity(body['export_id'])
        volume_id = export.identity(body['volume_id'])
        if (body['purpose'] not in ('data', 'docker') or
                body['filesystem'] not in ('ext4', 'xfs') or
                not isinstance(body['device'], str) or
                not body['device'].startswith('/dev/') or
                not isinstance(body['manifest'], dict) or
                len(json.dumps(body).encode()) >
                self.conf.agent.max_body_size):
            raise ValueError('Invalid archive selection')
        parts = destination(body['put_url'])
        # Expiring signatures can change; the immutable object cannot.
        selection = {key: body[key] for key in
                     ('volume_id', 'purpose', 'filesystem', 'manifest')}
        selection['destination'] = parts._replace(query='').geturl()
        fingerprint = hashlib.sha256(json.dumps(
            selection, sort_keys=True).encode()).hexdigest()
        key = export_id + '-' + volume_id
        with self.locked():
            previous = self.read(key)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise coolify.Conflict()
                if previous.get('result'):
                    return previous['result']
            else:
                self.write(key, {'fingerprint': fingerprint})
            deadline = time.monotonic() + self.conf.export.archive_timeout
            with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                directory = Path(temporary)
                with self.mount(body['device'], body['filesystem'], directory,
                                deadline) as root:
                    with tempfile.TemporaryFile(dir=directory) as stream:
                        manifest = dict(body['manifest'], export_id=export_id,
                                        volume_id=volume_id,
                                        purpose=body['purpose'])
                        result = build_archive(
                            root, body['purpose'], manifest, stream,
                            self.conf.export.max_size_gb * 1024 ** 3,
                            self.conf.export.max_object_size_gb * 1024 ** 3,
                            deadline, self.conf.agent.max_body_size)
                        self.put(body['put_url'], stream, result['size'],
                                 deadline)
            self.write(key, {'fingerprint': fingerprint, 'result': result})
            return result
