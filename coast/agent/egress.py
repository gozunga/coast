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

"""Bounded first-start environment egress check; no target/error logging."""

import http.client
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time
from urllib import parse

import dns.exception
import dns.resolver


DNS_HOST = 'docker.io'
PROBE_TIMEOUT = 5


def target(url):
    if not url:
        return None
    value = parse.urlsplit(url)
    if (value.scheme != 'https' or not value.hostname or
            value.username is not None or value.password is not None or
            value.query or value.fragment or
            any(ord(char) <= 32 or ord(char) == 127 for char in url)):
        raise ValueError('Invalid egress probe URL')
    # Access the port here so invalid ports fail before config-drive writes.
    if value.port == 0:
        raise ValueError('Invalid egress probe URL')
    if ':' in value.hostname:
        raise ValueError('IPv4 egress probe is required')
    return value


def ip(*args, ipv4=True):
    family = ['-4'] if ipv4 else []
    result = subprocess.run(['ip', '-j', *family, *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=2)
    return json.loads(result.stdout)


def environment_route(bind_ip):
    # This file is owned by the first-boot route step, not an operator YAML.
    configuration = json.loads(
        Path('/etc/netplan/50-cloud-init.yaml').read_text())
    macs = [value['match']['macaddress'] for value in
            configuration['network']['ethernets'].values()
            if value.get('dhcp4')]
    # The IPv4 family filter omits link-layer MAC addresses in iproute2.
    links = [link for link in ip('address', ipv4=False)
             if link.get('address') in macs]
    routes = ip('route', 'show', 'default')
    if len(macs) != 1 or len(links) != 1 or len(routes) != 1:
        raise ValueError('Environment default route is required')
    link, route = links[0], routes[0]
    if route.get('dev') != link['ifname'] or not route.get('gateway'):
        raise ValueError('Environment default route is required')
    gateway = ipaddress.IPv4Address(route['gateway'])
    for address in link.get('addr_info', []):
        if address.get('family') == 'inet':
            interface = ipaddress.IPv4Interface(
                '%s/%s' % (address['local'], address['prefixlen']))
            if (gateway in interface.network and
                    str(interface.ip) != bind_ip and
                    not interface.ip.is_link_local):
                return link['ifname'], str(interface.ip)
    raise ValueError('Environment IPv4 lease is required')


def https(value, addresses, interface, source):
    deadline = time.monotonic() + 3 * PROBE_TIMEOUT
    for address in addresses:
        connection = http.client.HTTPSConnection(
            value.hostname, value.port or 443, timeout=PROBE_TIMEOUT)
        timer = None
        try:
            routes = ip('route', 'get', address, 'from', source)
            if len(routes) != 1 or routes[0].get('dev') != interface:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            def expire(connection=connection):
                if connection.sock:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            # Avoid a second, unbounded hostname lookup in HTTPSConnection.
            # The explicit address is from the bounded system DNS query.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(min(PROBE_TIMEOUT, remaining))
                sock.bind((source, 0))
                sock.connect((address, value.port or 443))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                sock.settimeout(min(PROBE_TIMEOUT, remaining))
                connection.sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=value.hostname)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                connection.sock.settimeout(min(PROBE_TIMEOUT, remaining))
                # Arm after TLS wraps the socket so shutdown cannot race
                # with the transfer of the raw socket's file descriptor.
                timer = threading.Timer(remaining, expire)
                timer.start()
                connection.request('GET', value.path or '/')
                with connection.getresponse():
                    # Any response is success; never read its body.
                    return True
        except (OSError, http.client.HTTPException):
            continue
        finally:
            if timer:
                timer.cancel()
            connection.close()
    return False


class Check:
    def __init__(self, conf):
        self.conf = conf
        self.result = {'status': 'pending'}

    def run(self):
        reason = 'config'
        try:
            value = target(self.conf.agent.egress_check_url)
            reason = 'route'
            deadline = time.monotonic() + self.conf.agent.request_timeout
            while True:
                try:
                    interface, source = environment_route(
                        self.conf.agent.bind_ip)
                    break
                except (OSError, ValueError, KeyError, TypeError,
                        subprocess.SubprocessError):
                    if time.monotonic() >= deadline:
                        raise ValueError('Environment route deadline exceeded')
                    time.sleep(1)
            reason = 'dns'
            hostname = value.hostname if value else DNS_HOST
            try:
                literal = str(ipaddress.IPv4Address(hostname))
            except ValueError:
                literal = None
            answers = dns.resolver.resolve(
                DNS_HOST if literal else hostname, 'A',
                lifetime=PROBE_TIMEOUT, search=False)
            addresses = ([literal] if literal else
                         [str(item) for item in answers])
            if not addresses:
                raise ValueError('DNS answer is required')
            if value:
                reason = 'https'
                if not https(value, addresses, interface, source):
                    raise ValueError('HTTPS egress failed')
            self.result = {'status': 'ok'}
        except (OSError, ValueError, KeyError, TypeError,
                subprocess.SubprocessError,
                dns.exception.DNSException):
            # Only fixed codes reach health. Never include exception text.
            self.result = {'status': 'error', 'reason': reason}
