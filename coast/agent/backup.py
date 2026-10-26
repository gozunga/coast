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

"""Memory-only segmented database backup transport (format COASTDB v1)."""

import base64
import binascii
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import struct
import subprocess
import threading
import time
import urllib.parse
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from coast.agent import containers
from coast.agent import database


MAGIC = b'COASTDB\x01'
CHUNK = 1024 * 1024
MAX_SEGMENT = 4 * 1024 ** 3
ERROR = database.BackupError


def validate(body, restore):
    required = {'service_id', 'container', 'engine', 'key',
                'download_urls' if restore else 'upload_urls'}
    required |= {'target'} if restore else {'segment_bytes'}
    if (not isinstance(body, dict) or set(body) - {'resource_type'} != required
            or body.get('resource_type', 'service') not in (
                'service', 'database')):
        raise ValueError('Invalid database backup command fields')
    if (not isinstance(body['engine'], str) or
            body['engine'] not in database.SCRIPTS or
            not isinstance(body['service_id'], str) or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', body['service_id']) or
            not isinstance(body['container'], str) or
            not containers.NAME.fullmatch(
                body['container'].removeprefix('c-'))):
        raise ValueError('Invalid database engine or container')
    if restore and body['target'] != body['container']:
        raise ValueError('Restore target must equal the selected container')
    if not restore and (type(body['segment_bytes']) is not int or
                        not 1 <= body['segment_bytes'] <= MAX_SEGMENT):
        raise ValueError('Segment size must be between 1 byte and 4 GiB')
    urls = body['download_urls' if restore else 'upload_urls']
    if not isinstance(urls, list) or not 1 <= len(urls) <= 10000:
        raise ValueError('At least one transfer URL is required')
    for url in urls:
        if not isinstance(url, str) or len(url) > 16384:
            raise ValueError('Invalid transfer URL')
        parsed = urllib.parse.urlsplit(url)
        if any(ord(char) < 33 for char in url):
            raise ValueError('Invalid transfer URL')
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError('Invalid transfer URL port')
        if (parsed.scheme != 'https' or not parsed.hostname or
                parsed.username or parsed.password or parsed.fragment):
            raise ValueError('Transfer requires an HTTPS temporary URL')
    try:
        key = base64.b64decode(body['key'], validate=True)
        if len(key) != 32:
            raise ValueError()
    except (ValueError, TypeError, binascii.Error):
        raise ValueError('A base64 32-byte AES key is required') from None
    return key, urls


def encrypt(source, key, engine, version):
    metadata = json.dumps({'engine': engine, 'engine_version': version},
                          sort_keys=True, separators=(',', ':')).encode()
    prefix = os.urandom(8)
    header = MAGIC + struct.pack('>II', CHUNK,
                                 len(metadata)) + prefix + metadata
    yield header
    cipher = AESGCM(key)
    compressor = zlib.compressobj(wbits=31)
    pending = bytearray()
    counter = 0

    def frame(data, index):
        if index >= 2 ** 32:
            raise ERROR('Database backup exceeds encryption frame limit')
        number = struct.pack('>I', index)
        encrypted = cipher.encrypt(prefix + number, data, header + number)
        return struct.pack('>I', len(encrypted)) + encrypted

    while True:
        data = source.read(CHUNK)
        pending.extend(compressor.compress(data) if data else
                       compressor.flush())
        while len(pending) >= CHUNK:
            yield frame(bytes(pending[:CHUNK]), counter)
            counter += 1
            del pending[:CHUNK]
        if not data:
            break
    if pending:
        yield frame(bytes(pending), counter)
        counter += 1
    yield frame(b'', counter)  # Authenticated end marker detects truncation.


class Input:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pending = bytearray()

    def read(self, size, eof=False):
        while len(self.pending) < size:
            try:
                self.pending.extend(next(self.chunks))
            except StopIteration:
                if eof and not self.pending:
                    return b''
                raise ERROR('Database backup is truncated') from None
        data = bytes(self.pending[:size])
        del self.pending[:size]
        return data


def decrypt(source, key):
    header = source.read(24)
    if header[:8] != MAGIC:
        raise ERROR('Unsupported database backup format')
    chunk, size = struct.unpack('>II', header[8:16])
    if chunk != CHUNK or not 1 <= size <= 4096:
        raise ERROR('Invalid database backup header')
    raw = source.read(size)
    header += raw
    metadata = json.loads(raw)
    if (not isinstance(metadata, dict) or set(metadata) != {
            'engine', 'engine_version'} or
            not isinstance(metadata['engine'], str) or
            not isinstance(metadata['engine_version'], str)):
        raise ERROR('Invalid database backup metadata')
    cipher = AESGCM(key)
    counter = 0
    while True:
        size = struct.unpack('>I', source.read(4))[0]
        if not 16 <= size <= CHUNK + 16 or counter >= 2 ** 32:
            raise ERROR('Invalid database backup frame')
        number = struct.pack('>I', counter)
        try:
            plain = cipher.decrypt(header[16:24] + number,
                                   source.read(size), header + number)
        except InvalidTag:
            raise ERROR('Database backup authentication failed') from None
        yield metadata, plain
        counter += 1
        if not plain:
            if source.read(1, eof=True):
                raise ERROR('Unexpected data after database backup')
            return


class Transfer:
    """HTTPS without URL logging, ambient credentials or redirects."""

    @contextlib.contextmanager
    def request(self, method, url, data=None, headers=None, **kwargs):
        parsed = urllib.parse.urlsplit(url)
        connection = http.client.HTTPSConnection(parsed.hostname,
                                                 parsed.port or 443,
                                                 timeout=10)
        try:
            connection.connect()
            connection.sock.settimeout(60)
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request(method, path, body=data, headers=headers or {})
            response = connection.getresponse()
            yield Response(response)
        finally:
            connection.close()

    def put(self, url, **kwargs):
        return self.request('PUT', url, **kwargs)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)


class Response:
    def __init__(self, response):
        self.response = response
        self.status_code = response.status
        self.headers = response.headers

    def iter_content(self, size):
        while data := self.response.read(size):
            yield data


class Runner:
    def __init__(self, docker=None, session=None, sleep=time.sleep,
                 timeout=3600):
        self.lock = threading.Lock()
        self.docker = docker or database.Docker(timeout=timeout)
        self.session = session or Transfer()
        self.sleep = sleep

    def upload(self, chunks, urls, segment_bytes):
        size = 0
        digest = hashlib.sha256()
        segments = []
        buffer = io.BytesIO()

        def send():
            nonlocal size
            index = len(segments)
            if index >= len(urls):
                raise ERROR('Not enough database backup upload URLs')
            length = buffer.tell()
            for attempt in range(3):
                buffer.seek(0)
                try:
                    with self.session.put(
                            urls[index], data=buffer, timeout=(10, 60),
                            allow_redirects=False,
                            headers={'Content-Length': str(length),
                                     'Content-Type':
                                     'application/octet-stream'}) as response:
                        if 200 <= response.status_code < 300:
                            break
                        if response.status_code < 500 and (
                                response.status_code not in (408, 429)):
                            raise ERROR('Database backup upload was rejected')
                except (OSError, http.client.HTTPException):
                    pass
                if attempt == 2:
                    raise ERROR('Database backup segment upload failed')
                self.sleep(2 ** attempt)
            view = buffer.getbuffer()
            segments.append({'size_bytes': length,
                             'sha256': hashlib.sha256(view).hexdigest()})
            digest.update(view)
            view.release()
            size += length
            buffer.seek(0)
            buffer.truncate()

        for chunk in chunks:
            offset = 0
            while offset < len(chunk):
                count = min(segment_bytes - buffer.tell(), len(chunk) - offset)
                buffer.write(chunk[offset:offset + count])
                offset += count
                if buffer.tell() == segment_bytes:
                    send()
        if buffer.tell():
            send()
        return {'size_bytes': size, 'sha256': digest.hexdigest(),
                'segments': segments}

    def download(self, urls):
        # Do not retry a partially consumed GET: duplication corrupts frames.
        for url in urls:
            try:
                with self.session.get(url, stream=True, timeout=(10, 60),
                                      allow_redirects=False,
                                      headers={
                                          'Accept-Encoding': 'identity'}) as r:
                    if r.status_code != 200 or r.headers.get(
                            'Content-Encoding', 'identity') != 'identity':
                        raise ERROR('Database backup download was rejected')
                    yield from r.iter_content(CHUNK)
            except (OSError, http.client.HTTPException):
                raise ERROR('Database backup download failed') from None

    def run(self, body, restore=False):
        validate(body, restore)
        if not self.lock.acquire(blocking=False):
            raise ERROR('A database backup or restore is already running')
        try:
            return self._run(body, restore)
        finally:
            self.lock.release()

    def _run(self, body, restore):
        key, urls = validate(body, restore)
        start = time.monotonic()
        try:
            if body.get('resource_type') == 'database':
                cid = self.docker.resolve(
                    body['service_id'], body['container'], kind='database')
            else:
                cid = self.docker.resolve(
                    body['service_id'], body['container'])
            standalone = body.get('resource_type') == 'database'
            options = {'standalone': True} if standalone else {}
            engine = body['engine']
            version = self.docker.version(cid, engine)
            if restore:
                self.restore(cid, engine, version, key, urls, **options)
                result = {}
            else:
                with self.docker.process(cid, engine, **options) as source:
                    result = self.upload(encrypt(source, key, engine, version),
                                         urls, body['segment_bytes'])
                result['engine_version'] = version
            result['duration_seconds'] = time.monotonic() - start
            return result
        except ERROR:
            raise
        except (OSError, ValueError, KeyError, TypeError, zlib.error,
                subprocess.SubprocessError):
            raise ERROR('Database backup or restore failed') from None

    def restore(self, cid, engine, version, key, urls, **options):
        frames = iter(decrypt(Input(self.download(urls)), key))
        metadata, first = next(frames)
        if (metadata.get('engine') != engine or
                database.major(engine, metadata['engine_version']) >
                database.major(engine, version)):
            raise ERROR('Restore requires the same engine and a server '
                        'version at least as new as the backup major version')
        decompressor = zlib.decompressobj(wbits=31)
        process = self.docker.process(cid, engine, restore=True, **options)
        with process as target:
            def write(data):
                # A valid encrypted frame can still be a compression bomb.
                while data:
                    plain = decompressor.decompress(data, CHUNK)
                    target.write(plain)
                    data = decompressor.unconsumed_tail
                if decompressor.unused_data:
                    raise ERROR(
                        'Unexpected data after compressed database dump')

            write(first)
            for _, data in frames:
                write(data)
            if not decompressor.eof:
                raise ERROR('Compressed database dump is truncated')
