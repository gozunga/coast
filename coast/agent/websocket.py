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

"""RFC 6455 frames for one agent socket. No extensions and no new library.

The accept value is SHA-1 of the client key plus the RFC GUID:
https://datatracker.ietf.org/doc/html/rfc6455#section-1.3
Server frames are not masked. Client frames must be masked.
"""

import base64
import hashlib
import struct


GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
TEXT = 1
BINARY = 2
CLOSE = 8
PING = 9
PONG = 10
_MAX = 1048576


class ProtocolError(Exception):
    """Handshake or frame rejected. The message is not for the client."""


def accept_value(key):
    try:
        raw = base64.b64decode(key, validate=True)
    except (ValueError, TypeError):
        raise ProtocolError() from None
    if len(raw) != 16:
        raise ProtocolError()
    digest = hashlib.sha1((key + GUID).encode('ascii'),
                          usedforsecurity=False).digest()
    return base64.b64encode(digest).decode('ascii')


def _single(headers, name):
    values = headers.get_all(name) or []
    if len(values) != 1 or not isinstance(values[0], str):
        raise ProtocolError()
    return values[0]


def handshake(headers):
    """Return the accept value or raise when the upgrade is not valid."""
    connection = _single(headers, 'Connection')
    upgrade = _single(headers, 'Upgrade')
    version = _single(headers, 'Sec-WebSocket-Version')
    key = _single(headers, 'Sec-WebSocket-Key').strip()
    tokens = [part.strip().lower() for part in connection.split(',')]
    if 'upgrade' not in tokens or upgrade.strip().lower() != 'websocket':
        raise ProtocolError()
    if version.strip() != '13':
        raise ProtocolError()
    return accept_value(key)


def _exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise ProtocolError()
        data.extend(chunk)
    return bytes(data)


def _header(stream):
    first, second = _exact(stream, 2)
    fin = bool(first & 0x80)
    if first & 0x70:
        raise ProtocolError()
    opcode = first & 0x0F
    if not second & 0x80:
        raise ProtocolError()
    length = second & 0x7F
    if length == 126:
        length = struct.unpack('!H', _exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack('!Q', _exact(stream, 8))[0]
        if length > _MAX:
            raise ProtocolError()
    if length > _MAX:
        raise ProtocolError()
    mask = _exact(stream, 4)
    payload = _exact(stream, length)
    decoded = bytes(byte ^ mask[index % 4] for index, byte in
                    enumerate(payload))
    return fin, opcode, decoded


def read_frame(stream):
    """Return ``(opcode, payload)`` for one client data or control frame."""
    fin, opcode, payload = _header(stream)
    if opcode in (CLOSE, PING, PONG):
        if not fin or len(payload) > 125:
            raise ProtocolError()
        return opcode, payload
    if opcode not in (TEXT, BINARY):
        raise ProtocolError()
    parts = [payload]
    while not fin:
        fin, nxt, more = _header(stream)
        if nxt != 0:
            raise ProtocolError()
        parts.append(more)
        if sum(len(part) for part in parts) > _MAX:
            raise ProtocolError()
    return opcode, b''.join(parts)


def write_frame(stream, opcode, payload):
    if len(payload) > _MAX:
        raise ProtocolError()
    header = bytearray((0x80 | opcode,))
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header.extend(struct.pack('!H', size))
    else:
        header.append(127)
        header.extend(struct.pack('!Q', size))
    stream.write(bytes(header) + payload)


def write_close(stream, code, reason):
    if len(reason) > 123:
        reason = b''
    write_frame(stream, CLOSE, struct.pack('!H', code) + reason)
