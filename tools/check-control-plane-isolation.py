#!/usr/bin/env python3
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

"""Run as root inside a guest; require timeout, not connection refusal."""

import argparse
import ipaddress
import socket
import ssl


def connect(interface, address, port, timeout):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                        interface.encode() + b'\0')
        sock.settimeout(timeout)
        sock.connect((address, port))
        return sock
    except BaseException:
        sock.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', required=True)
    parser.add_argument('--control-plane-host', required=True,
                        type=ipaddress.ip_address)
    parser.add_argument('--public-host', default='example.com')
    parser.add_argument('--timeout', type=float, default=5)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('timeout must be positive')
    for port in (9600, 5672):
        try:
            with connect(args.interface, str(args.control_plane_host),
                         port, args.timeout):
                raise RuntimeError('Control-plane TCP port is reachable')
        except TimeoutError:
            print('PASS: control-plane TCP/%s timed out' % port)
    address = socket.getaddrinfo(args.public_host, 443, socket.AF_INET,
                                 socket.SOCK_STREAM)[0][4][0]
    with connect(args.interface, address, 443, args.timeout) as sock:
        with ssl.create_default_context().wrap_socket(
                sock, server_hostname=args.public_host) as tls:
            tls.sendall(('HEAD / HTTP/1.1\r\nHost: %s\r\n'
                         'Connection: close\r\n\r\n' %
                         args.public_host).encode('ascii'))
            if not tls.recv(16).startswith(b'HTTP/'):
                raise RuntimeError('Public host did not return HTTP')
    print('PASS: public HTTPS works on the environment NIC')


if __name__ == '__main__':
    main()
