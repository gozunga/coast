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

"""Open the workload agent exec stream with the conductor client material."""

import asyncio
import base64
import ipaddress
import os
import ssl

from cryptography import x509

from coast.agent import certificates
from coast.common import management
from coast.console import frames


def tls_context(conf):
    options = conf.controller_worker
    if not all((options.agent_client_cert, options.agent_client_key,
                conf.certificates.ca_certificate)):
        raise OSError('Agent client material is incomplete')
    context = ssl.create_default_context(
        cafile=conf.certificates.ca_certificate)
    context.load_cert_chain(options.agent_client_cert,
                            options.agent_client_key)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def check_peer(writer, conf, server_id, address):
    ssl_object = writer.get_extra_info('ssl_object')
    der = ssl_object.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    with open(conf.certificates.ca_certificate, 'rb') as stream:
        ca = x509.load_pem_x509_certificate(stream.read())
    certificates.validate(cert, None, ca, server_id, address)


async def client_upgrade(reader, writer, host, port):
    """Finish a client handshake. The streams stay positioned on frames."""
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    request = (
        'GET /v1/exec HTTP/1.1\r\n'
        'Host: %s:%s\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Key: %s\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        '\r\n' % (host, port, key))
    writer.write(request.encode('ascii'))
    await writer.drain()
    raw = await reader.readuntil(b'\r\n\r\n')
    if len(raw) > 8192 or not raw.startswith(b'HTTP/1.1 101'):
        raise frames.ProtocolError('status')
    accept = frames.accept_value(key)
    header = raw.decode('iso-8859-1')
    found = None
    for line in header.split('\r\n')[1:]:
        if line.lower().startswith('sec-websocket-accept:'):
            found = line.split(':', 1)[1].strip()
    if found != accept:
        raise frames.ProtocolError('accept')


async def open_exec(conf, address, server_id):
    """Return streams past the agent websocket handshake."""
    address = str(ipaddress.IPv4Address(address))
    port = conf.controller_worker.agent_port
    context = tls_context(conf)
    raw = management.socket_for_agent(conf)
    raw.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_connect(raw, (address, port))
        reader, writer = await asyncio.open_connection(
            sock=raw, ssl=context, server_hostname=address)
    except Exception:
        raw.close()
        raise
    try:
        check_peer(writer, conf, server_id, address)
        await client_upgrade(reader, writer, address, port)
    except Exception:
        writer.close()
        raise
    return reader, writer
