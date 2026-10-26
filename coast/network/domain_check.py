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

"""DNS and TLS readiness checks, independent of domain state."""

import ipaddress
import socket
import ssl

import dns.exception
import dns.resolver


def make_resolver(nameservers):
    """Use explicit nameservers, or load the system resolver configuration."""
    resolver = dns.resolver.Resolver(configure=not bool(nameservers))
    if nameservers:
        resolver.nameservers = list(nameservers)
    return resolver


def txt_matches(fqdn, token, resolver):
    """Match a complete TXT record, joining its character-string chunks."""
    try:
        answers = resolver.resolve('_coast-challenge.' + fqdn, 'TXT')
        return any(b''.join(record.strings) == token.encode('utf-8')
                   for record in answers)
    except dns.exception.DNSException:
        return False


def routes_to(fqdn, default_host, fip, resolver):
    """Accept the default-host CNAME or an address equal to the worker FIP."""
    try:
        answers = resolver.resolve(fqdn, 'CNAME')
        if any(str(record.target).rstrip('.').lower() ==
               default_host.rstrip('.').lower() for record in answers):
            return True
    except dns.exception.DNSException:
        pass
    address = ipaddress.ip_address(fip)
    for kind in ('A', 'AAAA'):
        try:
            answers = resolver.resolve(fqdn, kind)
            if any(ipaddress.ip_address(record.address) == address
                   for record in answers):
                return True
        except dns.exception.DNSException:
            pass
    return False


def tls_ready(fqdn, timeout=10):
    """Check the TLS handshake with system trust and FQDN validation."""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((fqdn, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=fqdn):
                return True
    except (ssl.SSLError, OSError):
        return False


def http_ready(host, address, timeout=5):
    """Return True when https://host/ answers below 500 at address."""
    return 200 <= http_status(host, address, timeout) < 500


def http_status(host, address, timeout=5, path='/'):
    """Return the HTTP status of https://host/ at address, or 0.

    The request goes to the worker address with SNI and Host set, so it
    tests the engine proxy route even before public DNS converges. The
    certificate is not verified: this is a readiness probe, not trust.
    """
    if not path.startswith('/') or any(
            ord(char) < 33 or ord(char) > 126 for char in path):
        return 0
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((address, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as sock:
                sock.settimeout(timeout)
                sock.sendall(('GET %s HTTP/1.1\r\nHost: %s\r\n'
                              'Connection: close\r\n\r\n' %
                              (path, host)).encode())
                line = sock.recv(64).split(b'\r\n', 1)[0].split()
        return int(line[1]) if len(line) > 1 else 0
    except (ssl.SSLError, OSError, ValueError):
        return 0
