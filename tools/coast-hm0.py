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

"""Prepare shared Neutron resources; attach a pre-existing host OVS bridge.

Run prepare with service credentials, then plug as root on each controller.
A fixed address, host and reachable OVS bridge are explicit operator inputs.
"""

import argparse
import ipaddress
import json
import re
import subprocess
import sys

from oslo_config import cfg

from coast.common import clients
from coast.common import config
from coast.common import naming
from coast.network import tags as neutron_tags


# Neutron details only. Reject URLs, paths and name=value config text.
_NEUTRON_MESSAGE = re.compile(r"[A-Za-z0-9 '.,:()_-]{1,240}\Z")


def failure_cause(exc):
    """Return the exception class, HTTP status and a safe Neutron message."""
    parts = [type(exc).__name__]
    status = getattr(exc, 'status_code', None)
    if isinstance(status, int):
        parts.append('HTTP %d' % status)
    details = getattr(exc, 'details', None)
    if isinstance(details, str):
        text = ' '.join(details.split())
        if _NEUTRON_MESSAGE.fullmatch(text):
            parts.append(text)
    return ' '.join(parts)


def ensure(network, kind, name, tags, **attrs):
    found = list(getattr(network, kind + 's')(name=name))
    if len(found) > 1:
        raise ValueError('Duplicate management resource name')
    if found:
        resource = found[0]
        if not set(tags).issubset(resource.tags or []):
            raise ValueError('Refuse a foreign management resource')
    else:
        resource = neutron_tags.create_tagged(
            network, kind, tags, name=name, **attrs)
    if any(getattr(resource, key) != value for key, value in attrs.items()):
        raise ValueError('Management resource configuration mismatch')
    return resource


def prepare(connection, cidr, address, host, conf=cfg.CONF):
    project = connection.current_project_id
    if not project:
        raise ValueError('Service project authentication is required')
    subnet_cidr = ipaddress.IPv4Network(cidr)
    ip = ipaddress.IPv4Address(address)
    unusable = (subnet_cidr.network_address, subnet_cidr.broadcast_address)
    if ip not in subnet_cidr or ip in unusable:
        raise ValueError('Host address must be usable in the management CIDR')
    tags = ['coast', 'project_id=' + project, 'stack_id=control-plane']
    network = connection.network
    net = ensure(network, 'network',
                 naming.resource_name('mgmt-net', conf=conf), tags,
                 project_id=project, is_port_security_enabled=True,
                 is_router_external=False, is_shared=False)
    subnet = ensure(network, 'subnet',
                    naming.resource_name('mgmt-subnet', conf=conf), tags,
                    project_id=project, network_id=net.id, ip_version=4,
                    cidr=str(subnet_cidr), gateway_ip=None,
                    is_dhcp_enabled=False)
    sg = ensure(network, 'security_group',
                naming.resource_name('ctl-mgmt', conf=conf), tags,
                project_id=project)
    # Strip only Neutron's broad default egress rules. Do not remove Q72
    # per-environment reciprocal 5565/9443 rules on later prepare calls.
    for rule in network.security_group_rules(security_group_id=sg.id):
        if (rule.direction == 'egress' and rule.protocol is None and
                rule.remote_group_id is None and
                rule.remote_address_group_id is None and
                rule.remote_ip_prefix in (None, '0.0.0.0/0', '::/0')):
            network.delete_security_group_rule(rule.id)
        elif not (rule.ether_type == 'IPv4' and rule.remote_group_id and
                  rule.remote_ip_prefix is None and
                  rule.remote_address_group_id is None and
                  (rule.direction, rule.protocol, rule.port_range_min,
                   rule.port_range_max) in (
                      ('ingress', 'udp', 5565, 5565),
                      ('egress', 'tcp', 9443, 9443))):
            raise ValueError('Unexpected control-side security group rule')
    # Like Octavia:health-mgr, do not use Neutron's trusted network: prefix.
    # Keep the legacy host suffix, including dots in an FQDN.
    name = naming.resource_name('hm0', conf=conf) + '-' + host
    port = ensure(network, 'port', name, tags,
                  project_id=project, network_id=net.id,
                  fixed_ips=[{'subnet_id': subnet.id, 'ip_address': str(ip)}],
                  security_group_ids=[sg.id], is_port_security_enabled=True,
                  binding_host_id=host, device_owner='Coast:health-mgr')
    return {'network_id': net.id, 'security_group_id': sg.id,
            'port_id': port.id, 'mac_address': port.mac_address,
            'address': str(ip) + '/' + str(subnet_cidr.prefixlen),
            'host': host}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def plug(bridge, port_id, mac, address, namespace='coast-mgmt'):
    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,15}', bridge) or
            not re.fullmatch(r'[A-Za-z0-9_-]+', namespace) or
            not re.fullmatch(r'[a-f0-9-]{36}', port_id) or
            not re.fullmatch(r'(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}', mac)):
        raise ValueError('Invalid host plumbing input')
    interface = ipaddress.IPv4Interface(address)
    if (interface.ip.is_unspecified or interface.ip.is_loopback or
            interface.ip.is_multicast or
            interface.ip in (interface.network.network_address,
                             interface.network.broadcast_address)):
        raise ValueError('A usable management IPv4 address is required')
    run('ovs-vsctl', 'br-exists', bridge)
    namespaces = {line.split()[0] for line in
                  run('ip', 'netns', 'list').stdout.splitlines()}
    if namespace not in namespaces:
        run('ip', 'netns', 'add', namespace)
    links = json.loads(run('ip', '-n', namespace, '-j', 'link', 'show').stdout)
    # Do not modify a namespace shared with another host interface.
    if any(link['ifname'] not in ('lo', 'coast-hm0') for link in links):
        raise ValueError('Management namespace contains another interface')
    if run('ip', '-n', namespace, 'route', 'show', 'default').stdout.strip():
        raise ValueError('Management namespace has a default route')
    exists = any(link['ifname'] == 'coast-hm0' for link in links)
    if exists:
        owner = run('ovs-vsctl', 'get', 'Interface', 'coast-bhm0',
                    'external_ids:iface-id').stdout.strip().strip('"')
        actual_bridge = run('ovs-vsctl', 'port-to-br', 'coast-bhm0')
        if owner != port_id or actual_bridge.stdout.strip() != bridge:
            raise ValueError('Existing interface belongs to another port '
                             'or bridge')
    else:
        # Refuse root-namespace name collisions, never steal an interface.
        root_links = json.loads(run('ip', '-j', 'link', 'show').stdout)
        if any(link['ifname'] in ('coast-hm0', 'coast-bhm0')
               for link in root_links):
            raise ValueError('Host interface name collision')
        run('ip', 'link', 'add', 'coast-hm0', 'type', 'veth',
            'peer', 'name', 'coast-bhm0')
        try:
            run('ip', 'link', 'set', 'coast-hm0', 'netns', namespace)
            run('ovs-vsctl', '--may-exist', 'add-port', bridge, 'coast-bhm0',
                '--', 'set', 'Interface', 'coast-bhm0',
                'external_ids:iface-id=' + port_id,
                'external_ids:attached-mac=' + mac,
                'external_ids:iface-status=active')
        except Exception:
            # Removing either veth end removes the pair. Leave Neutron
            # resources intact so a later plug can resume.
            run('ovs-vsctl', '--if-exists', 'del-port', bridge, 'coast-bhm0')
            run('ip', 'link', 'delete', 'coast-bhm0')
            raise
    run('sysctl', '-qw', 'net.ipv6.conf.coast-bhm0.disable_ipv6=1')
    run('ip', 'address', 'flush', 'dev', 'coast-bhm0')
    run('ip', 'netns', 'exec', namespace, 'sysctl', '-qw',
        'net.ipv4.ip_forward=0', 'net.ipv6.conf.all.forwarding=0',
        'net.ipv6.conf.all.disable_ipv6=1')
    run('ip', '-n', namespace, 'link', 'set', 'coast-hm0', 'address', mac)
    run('ip', '-n', namespace, 'address', 'flush', 'dev', 'coast-hm0')
    run('ip', '-n', namespace, 'address', 'add', str(interface),
        'dev', 'coast-hm0')
    run('ip', 'link', 'set', 'coast-bhm0', 'up')
    run('ip', '-n', namespace, 'link', 'set', 'lo', 'up')
    run('ip', '-n', namespace, 'link', 'set', 'coast-hm0', 'up')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    prepare_args = actions.add_parser('prepare')
    for arg in ('cidr', 'address', 'host'):
        prepare_args.add_argument('--' + arg, required=True)
    plug_args = actions.add_parser('plug')
    for arg in ('bridge', 'port-id', 'mac', 'address'):
        plug_args.add_argument('--' + arg, required=True)
    plug_args.add_argument('--namespace', default='coast-mgmt')
    args, config_args = parser.parse_known_args()
    try:
        if args.action == 'prepare':
            config.parse_args(config_args)
            print(json.dumps(prepare(clients.get_connection('neutron'),
                                     args.cidr, args.address, args.host)))
        else:
            if config_args:
                parser.error('Unexpected plug arguments')
            plug(args.bridge, args.port_id, args.mac, args.address,
                 args.namespace)
    except Exception as exc:
        # Do not print str(exc). It can include the request URL and
        # configuration values.
        print('coast-hm0 setup failed; check inputs and prerequisites',
              file=sys.stderr)
        print(failure_cause(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
