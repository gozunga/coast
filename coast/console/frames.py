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

"""A small RFC 6455 subset on asyncio streams.

The browser is a client, so its frames are masked. This service is the
server toward the browser and the client toward the workload agent.
"""

import base64
import collections
import hashlib
import os
import struct


# RFC 6455 section 1.3. SHA-1 is required for the handshake, not for
# new security use.
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_PAYLOAD = 1024 * 1024
CONT = 0
TEXT = 1
BINARY = 2
CLOSE = 8
PING = 9
PONG = 10
CONTROL = frozenset((CLOSE, PING, PONG))
KNOWN = frozenset((CONT, TEXT, BINARY, CLOSE, PING, PONG))
DATA = frozenset((CONT, TEXT, BINARY))
Frame = collections.namedtuple('Frame', 'fin opcode payload')


class ProtocolError(Exception):
    """A frame or handshake violated the subset this service accepts."""


def accept_value(key):
    raw = (key + GUID).encode('ascii')
    digest = hashlib.sha1(raw, usedforsecurity=False).digest()  # nosec B324
    return base64.b64encode(digest).decode('ascii')


def encode(opcode, payload, masked, fin=True):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    length = len(payload)
    if length > MAX_PAYLOAD:
        raise ProtocolError('length')
    first = (0x80 if fin else 0) | opcode
    mask_bit = 0x80 if masked else 0
    header = bytearray((first,))
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack('!H', length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack('!Q', length))
    if masked:
        mask = os.urandom(4)
        header.extend(mask)
        payload = bytes(byte ^ mask[index % 4]
                        for index, byte in enumerate(payload))
    return bytes(header) + payload


def close_code(payload):
    if len(payload) < 2:
        return 1000
    return struct.unpack('!H', payload[:2])[0]


async def read_frame(stream, expect_mask):
    header = await stream.readexactly(2)
    first, second = header[0], header[1]
    if first & 0x70:
        raise ProtocolError('reserved')
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if masked != expect_mask or opcode not in KNOWN:
        raise ProtocolError('frame')
    if length == 126:
        length = struct.unpack('!H', await stream.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack('!Q', await stream.readexactly(8))[0]
    if length > MAX_PAYLOAD or (opcode in CONTROL and
                                (not fin or length > 125)):
        raise ProtocolError('length')
    mask = await stream.readexactly(4) if masked else None
    payload = await stream.readexactly(length) if length else b''
    if mask:
        payload = bytes(byte ^ mask[index % 4]
                        for index, byte in enumerate(payload))
    return Frame(fin, opcode, payload)


async def write_frame(writer, opcode, payload, masked, fin=True):
    writer.write(encode(opcode, payload, masked, fin))
    await writer.drain()
