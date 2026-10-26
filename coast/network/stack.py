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

"""Resumable stack networking, separate from conductor state changes."""

import ipaddress
import time

from openstack import exceptions
from oslo_config import cfg

from coast.common import clients
from coast.common import context as context_module
from coast.common import database_public
from coast.common import naming
from coast.db import api as db_api
from coast.network import tags as neutron_tags


# Fixed addresses at offsets 2 through 9; offset 1 is the gateway.
RESERVED_FIXED_IP_COUNT = 8


def stack_subnet(options):
    """Return the first IPv4 subnet with room for fixed and pooled IPs."""
    pool = ipaddress.IPv4Network(options.env_cidr_pool)
    prefix = options.env_subnet_prefix
    if prefix < pool.prefixlen:
        raise ValueError('Stack prefix is shorter than CIDR pool')
    subnet = next(pool.subnets(new_prefix=prefix))
    if subnet.num_addresses < RESERVED_FIXED_IP_COUNT + 4:
        raise ValueError('Stack subnet has no room for the allocation pool')
    return subnet


def port_ranges(blocked):
    ports = sorted(set(int(port) for port in blocked))
    if any(port < 1 or port > 65535 for port in ports):
        raise ValueError('Blocked ports must be between 1 and 65535')
    start = 1
    result = []
    for port in ports + [65536]:
        if start < port:
            result.append((start, port - 1))
        start = port + 1
    return result


def rule_description(prefix, tags):
    """Return a run-prefixed SG rule description of at most 255 characters.

    Neutron 2026.1 has no working tag API for security group rules.
    The description starts with the configured name prefix, then the
    ownership tags when they fit. Neutron's description limit is 255.
    """
    text = str(prefix or '')
    if len(text) > 255:
        return text[:255]
    for tag in tags:
        candidate = (text + ' ' + tag).strip()
        if len(candidate) > 255:
            break
        text = candidate
    return text


def subtract_cidrs(destinations, excluded):
    """Subtract same-family networks, including overlapping exclusions."""
    remaining = [ipaddress.ip_network(value, strict=False)
                 for value in destinations]
    for value in excluded:
        blocked = ipaddress.ip_network(value, strict=False)
        updated = []
        for network in remaining:
            if network.version != blocked.version or not network.overlaps(
                    blocked):
                updated.append(network)
            elif not network.subnet_of(blocked):
                updated.extend(network.address_exclude(blocked))
        remaining = updated
    return [str(network) for network in remaining]


METADATA_CIDR = '169.254.169.254/32'


def egress_excluded(options, management_cidrs=()):
    """Return destinations guests must not reach over the env NIC."""
    return (list(options.control_plane_cidrs) + list(management_cidrs) +
            [METADATA_CIDR])


def allowed_destinations(options, management_cidrs=()):
    """Return the collapsed IPv4 allow list for guest internet egress."""
    remaining = subtract_cidrs(['0.0.0.0/0'], [
        value for value in egress_excluded(options, management_cidrs)
        if ipaddress.ip_network(value, strict=False).version == 4])
    return [str(network) for network in ipaddress.collapse_addresses(
        ipaddress.ip_network(value) for value in remaining)]


def controller_prefix(controller_ip, cidr):
    """Return the controller host prefix inside the environment subnet."""
    try:
        address = ipaddress.IPv4Address(controller_ip)
        network = ipaddress.ip_network(cidr)
    except ValueError:
        raise ValueError(
            'Controller address must be in the env subnet') from None
    if address not in network:
        raise ValueError('Controller address must be in the env subnet')
    return str(address) + '/32'


INTERNET = object()


def rules(options, cidr, controller_ip, has_build_servers, control_group,
          management_cidrs=(), egress_group=None, ingress_rules=()):
    """Return complete allow lists; an empty list means deny all.

    Internet egress uses the stack egress address group when one is given:
    it holds 0.0.0.0/0 minus control-plane, management and metadata ranges,
    so each egress rule stays a single Neutron rule. Without a group the
    legacy 0.0.0.0/0 prefix is used.
    """
    controller = controller_prefix(controller_ip, cidr)
    internet = [INTERNET]
    result = {role: [] for role in ('ctrl', 'worker', 'build', 'agent-mgmt')}

    def allow(role, direction, protocol, destinations, low=None, high=None,
              remote='remote_ip_prefix'):
        for destination in destinations:
            if destination is INTERNET:
                if egress_group:
                    target = {'remote_address_group_id': egress_group}
                else:
                    target = {'remote_ip_prefix': '0.0.0.0/0'}
            else:
                target = {remote: destination}
            rule = dict(direction=direction, ether_type='IPv4',
                        protocol=protocol, **target)
            if low is not None:
                rule.update(port_range_min=low,
                            port_range_max=low if high is None else high)
            result[role].append(rule)

    allow('agent-mgmt', 'ingress', 'tcp', [control_group], 9443,
          remote='remote_group_id')
    allow('agent-mgmt', 'egress', 'udp', [control_group], 5565,
          remote='remote_group_id')
    allow('ctrl', 'egress', 'tcp', [cidr], 22)
    if has_build_servers:
        # Same port as [controller_worker] registry_port. Open it only
        # while the stack has a build server.
        allow('ctrl', 'ingress', 'tcp', [cidr], options.registry_port)
        allow('build', 'egress', 'tcp', [controller], options.registry_port)
    for role in ('worker', 'build'):
        allow(role, 'ingress', 'tcp', [controller], 22)
    for port in (80, 443):
        allow('worker', 'ingress', 'tcp', ['0.0.0.0/0'], port)
        allow('ctrl', 'egress', 'tcp', internet, port)
        allow('build', 'egress', 'tcp', internet, port)
    # DHCP discovery must reach Neutron before the env default route exists.
    for role in ('ctrl', 'build'):
        allow(role, 'egress', 'udp', internet, 67)
    for port in (22, 9418):
        allow('build', 'egress', 'tcp', internet, port)
    for low, high in port_ranges(options.egress_blocked_ports):
        allow('worker', 'egress', 'tcp', internet, low, high)
    allow('worker', 'egress', 'udp', internet, 1, 65535)
    allow('worker', 'egress', 'icmp', internet, 8, 0)
    for role in ('ctrl', 'worker', 'build'):
        dns = [str(ipaddress.IPv4Network(value)) for value in
               options.dns_servers] or internet
        ntp = [str(ipaddress.IPv4Network(value)) for value in
               options.ntp_servers] or internet
        for protocol in ('tcp', 'udp'):
            allow(role, 'egress', protocol, dns, 53)
        allow(role, 'egress', 'udp', ntp, 123)
    # Filter explicit DNS/NTP prefixes too (the env subnet and controller
    # are never control-plane ranges, so they pass through unchanged).
    excluded = egress_excluded(options, management_cidrs)
    for role in ('ctrl', 'worker', 'build'):
        filtered = []
        for rule in result[role]:
            if (rule['direction'] != 'egress' or
                    'remote_ip_prefix' not in rule or not egress_group):
                filtered.append(rule)
                continue
            for destination in subtract_cidrs(
                    [rule['remote_ip_prefix']], excluded):
                filtered.append(dict(rule, remote_ip_prefix=destination))
        result[role] = filtered
    # ICMP fields are type/code, not port ranges. Keep management unchanged.
    for role in ('ctrl', 'worker', 'build'):
        result[role] = merge_egress_ranges(result[role])
    result['worker'].extend(customer_ingress(ingress_rules))
    return result


def customer_ingress(ingress_rules):
    result = []
    for custom in ingress_rules:
        rule = dict(direction='ingress', ether_type='IPv4',
                    protocol=custom['protocol'],
                    remote_ip_prefix=custom['source'])
        if custom.get('port_range_min') is not None:
            rule.update(port_range_min=custom['port_range_min'],
                        port_range_max=custom['port_range_max'])
        result.append(rule)
    return result


def merge_egress_ranges(rules):
    """Return minimal TCP/UDP egress ranges for each protocol and remote."""
    result = []
    grouped = {}
    for rule in rules:
        if rule['direction'] != 'egress' or rule['protocol'] not in (
                'tcp', 'udp'):
            result.append(rule)
            continue
        key = (rule['protocol'], rule.get('remote_ip_prefix'),
               rule.get('remote_address_group_id'))
        grouped.setdefault(key, []).append(rule)
    for group in grouped.values():
        merged = []
        for rule in sorted(group, key=lambda r: r['port_range_min']):
            if (merged and rule['port_range_min'] <=
                    merged[-1]['port_range_max'] + 1):
                merged[-1]['port_range_max'] = max(
                    merged[-1]['port_range_max'], rule['port_range_max'])
            else:
                merged.append(rule.copy())
        result.extend(merged)
    return result


class EnvironmentNetwork:
    """Call under the existing per-stack conductor lock.

    create is a resumable step. delete is its explicit revert, and also
    offboarding cleanup after guest ports and floating IPs are removed.
    The caller reserves the controller fixed IP before it attaches guests.
    """

    def __init__(self, conf=cfg.CONF, connection=None):
        self.conf = conf
        self.options = conf.controller_worker
        self.network = (connection or clients.get_connection(
            'neutron', conf=conf)).network

    @staticmethod
    def _tags(project_id, stack_id):
        return ['coast', 'project_id=' + project_id,
                'stack_id=' + stack_id]

    def _find(self, kind, name, tags):
        found = list(getattr(self.network, kind + 's')(
            name=name, tags=','.join(tags)))
        # Check returned ownership too; never adopt a name-only match.
        found = [resource for resource in found
                 if resource.name == name and
                 set(tags).issubset(resource.tags or [])]
        if len(found) > 1:
            raise ValueError('Multiple Coast network resources match')
        return found[0] if found else None

    def _ensure(self, kind, name, tags, **attrs):
        resource = self._find(kind, name, tags)
        if resource is None:
            resource = neutron_tags.create_tagged(
                self.network, kind, tags, name=name, **attrs)
        for field, value in attrs.items():
            observed = getattr(resource, field)
            if isinstance(value, dict):
                matches = all(observed.get(key) == item
                              for key, item in value.items())
            else:
                matches = observed == value
            if not matches:
                raise ValueError('Existing network resource differs from '
                                 'the requested configuration')
        return resource

    def _create_rule(self, group_id, rule, description, matches):
        try:
            self.network.create_security_group_rule(
                security_group_id=group_id, description=description, **rule)
        except exceptions.ConflictException:
            # Another apply may have created the rule since our initial read.
            # Confirm it exists; unrelated conflicts must still fail.
            current = list(self.network.security_group_rules(
                security_group_id=group_id))
            if not any(matches(item) for item in current):
                raise

    def _delete_rule(self, rule_id):
        try:
            self.network.delete_security_group_rule(rule_id)
        except exceptions.NotFoundException:
            # Another apply already removed this stale rule.
            pass

    def _sync_rules(self, group, desired, tags, deadline=None):
        fields = ('direction', 'ether_type', 'protocol', 'remote_ip_prefix',
                  'remote_group_id', 'remote_address_group_id',
                  'port_range_min', 'port_range_max')

        def key(rule):
            if isinstance(rule, dict):
                values = [rule.get(field) for field in fields]
            else:
                values = [getattr(rule, field, None) for field in fields]
            # Neutron stores a full TCP/UDP port range as no range.
            if values[2] in ('tcp', 'udp') and values[-2:] == [1, 65535]:
                values[-2:] = [None, None]
            return tuple(values)

        expected = {key(rule) for rule in desired}
        current = list(self.network.security_group_rules(
            security_group_id=group.id))
        existing = {key(rule) for rule in current if key(rule) in expected}
        # Rules are not tagged. Parent SG ownership plus this description
        # identifies them. See networking.md.
        description = rule_description(self.conf.resource_name_prefix, tags)
        # Create missing rules before deleting stale ones so a repair that
        # narrows a broad egress rule never leaves a window with no egress.
        for rule in desired:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError('Security group repair time budget spent')
            if key(rule) not in existing:
                self._create_rule(
                    group.id, rule, description,
                    lambda item: key(item) == key(rule))
                existing.add(key(rule))
        kept = set()
        for rule in current:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError('Security group repair time budget spent')
            if key(rule) not in expected or key(rule) in kept:
                self._delete_rule(rule.id)
            else:
                kept.add(key(rule))

    def ensure_egress_group(self, project_id, stack_id, destinations):
        """Create or update the stack egress address group (idempotent)."""
        name = naming.resource_name('env', stack_id, 'egress', conf=self.conf)
        # Same owner as the stack security groups (the Coast service
        # project); the stack id in the name makes it unique.
        found = [group for group in self.network.address_groups(name=name)
                 if group.name == name]
        if len(found) > 1:
            raise ValueError('Multiple Coast address groups match')
        if not found:
            return self.network.create_address_group(
                name=name,
                description=rule_description(
                    self.conf.resource_name_prefix,
                    self._tags(project_id, stack_id)),
                addresses=list(destinations))
        group = found[0]
        current = set(group.addresses or [])
        wanted = set(destinations)
        # Add first, then remove, so guests never lose internet egress.
        if wanted - current:
            group = self.network.add_addresses_to_address_group(
                group, sorted(wanted - current))
        if current - wanted:
            group = self.network.remove_addresses_from_address_group(
                group, sorted(current - wanted))
        return group

    def management_cidrs(self):
        if not self.options.mgmt_net_id:
            raise ValueError('Management network required for '
                             'egress isolation')
        cidrs = [subnet.cidr for subnet in self.network.subnets(
            network_id=self.options.mgmt_net_id)
            if subnet.network_id == self.options.mgmt_net_id]
        if not cidrs:
            raise ValueError('Management subnets required for '
                             'egress isolation')
        return cidrs

    def customer_rules(self, project_id, stack_id, ctx=None):
        # Conductor callers must see state saved in their current transaction,
        # especially ERROR after a rolled-back create.
        ctx = ctx or context_module.RequestContext(project_id=project_id)
        custom = [{key: getattr(row, key) for key in (
            'protocol', 'source', 'port_range_min', 'port_range_max')}
            for row in db_api.ingress_rules.list_for_stack(ctx, stack_id)]
        return custom + database_public.rules(ctx, stack_id)

    def repair_peers(self, peer, tags, deadline=None):
        # Reconcile only rules targeting this stack's management group.
        # Other stacks and operator rules on the shared SG are untouched.
        group_id = self.options.mgmt_sg_id
        current = list(self.network.security_group_rules(
            security_group_id=group_id))
        desired = [dict(direction=direction, ether_type='IPv4',
                        protocol=protocol, port_range_min=port,
                        port_range_max=port, remote_group_id=peer)
                   for direction, protocol, port in (
                       ('ingress', 'udp', 5565), ('egress', 'tcp', 9443))]
        kept = set()
        stale = []
        for item in current:
            if item.get('remote_group_id') != peer:
                continue
            match = next((i for i, rule in enumerate(desired)
                          if all(item.get(k) == v for k, v in rule.items())),
                         None)
            if match is None or match in kept:
                stale.append(item.id)
            else:
                kept.add(match)
        for i, rule in enumerate(desired):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError('Security group repair time budget spent')
            if i not in kept:
                self._create_rule(
                    group_id, rule,
                    rule_description(self.conf.resource_name_prefix, tags),
                    lambda item: all(item.get(k) == v
                                     for k, v in rule.items()))
        for id in stale:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError('Security group repair time budget spent')
            self._delete_rule(id)

    def reconcile(self, project_id, stack_id, controller_ip,
                  has_build_servers=False, deadline=None, ctx=None):
        """Repair owned env SGs without changing network topology."""
        tags = self._tags(project_id, stack_id)
        name = naming.resource_name('env', stack_id, conf=self.conf)
        subnet = self._find('subnet', name, tags)
        if subnet is None:
            raise ValueError('Stack subnet is missing')
        management = self.management_cidrs()
        egress = self.ensure_egress_group(
            project_id, stack_id,
            allowed_destinations(self.options, management))
        desired = rules(self.options, subnet.cidr, controller_ip,
                        has_build_servers, self.options.mgmt_sg_id,
                        management, egress.id,
                        self.customer_rules(project_id, stack_id, ctx=ctx))
        for role in ('ctrl', 'worker', 'build', 'agent-mgmt'):
            group = self._find('security_group', naming.resource_name(
                'env', stack_id, role, conf=self.conf), tags)
            if group is None:
                raise ValueError('Stack security group is missing')
            self._sync_rules(group, desired[role], tags, deadline)
            if role == 'agent-mgmt':
                self.repair_peers(group.id, tags, deadline)

    def create(self, project_id, stack_id, controller_ip,
               has_build_servers=False):
        options = self.options
        if not options.external_network_id or not options.env_cidr_pool:
            raise ValueError('External network and stack CIDR required')
        if not options.mgmt_sg_id:
            raise ValueError('Control-side management security group required')
        subnet_cidr = stack_subnet(options)
        cidr = str(subnet_cidr)
        # Reject a bad controller address before any Neutron read.
        controller_prefix(controller_ip, cidr)
        allocation_pools = [{
            'start': str(subnet_cidr.network_address +
                         2 + RESERVED_FIXED_IP_COUNT),
            'end': str(subnet_cidr.broadcast_address - 1)}]
        management = self.management_cidrs()
        egress = self.ensure_egress_group(
            project_id, stack_id, allowed_destinations(options, management))
        desired = rules(options, cidr, controller_ip, has_build_servers,
                        options.mgmt_sg_id, management, egress.id,
                        self.customer_rules(project_id, stack_id))
        tags = self._tags(project_id, stack_id)
        name = naming.resource_name('env', stack_id, conf=self.conf)
        network = self._ensure('network', name, tags,
                               is_port_security_enabled=True)
        subnet = self._ensure(
            'subnet', name, tags, network_id=network.id, ip_version=4,
            cidr=cidr, dns_nameservers=options.dns_servers,
            is_dhcp_enabled=True, allocation_pools=allocation_pools)
        router = self._ensure(
            'router', name, tags, external_gateway_info={
                'network_id': options.external_network_id})
        attached = any(
            fixed['subnet_id'] == subnet.id
            for port in self.network.ports(device_id=router.id)
            for fixed in port.fixed_ips)
        if not attached:
            self.network.add_interface_to_router(router, subnet_id=subnet.id)
        groups = {}
        for role, allow_list in desired.items():
            group = self._ensure('security_group', naming.resource_name(
                'env', stack_id, role, conf=self.conf), tags)
            self._sync_rules(group, allow_list, tags)
            groups[role] = group.id
        self.repair_peers(groups['agent-mgmt'], tags)
        return dict(network_id=network.id, subnet_id=subnet.id,
                    router_id=router.id, security_groups=groups)

    def _delete(self, method, *args, **kwargs):
        for attempt in range(self.options.network_delete_attempts):
            try:
                return method(*args, **kwargs)
            except exceptions.ResourceNotFound:
                return
            except exceptions.HttpException as exc:
                if (exc.status_code not in (409, 500, 502, 503, 504) or
                        attempt + 1 == self.options.network_delete_attempts):
                    raise
                time.sleep(self.options.network_delete_retry_interval)

    def delete(self, project_id, stack_id):
        tags = self._tags(project_id, stack_id)
        name = naming.resource_name('env', stack_id, conf=self.conf)
        for role in ('agent-mgmt', 'build', 'worker', 'ctrl'):
            group = self._find('security_group', naming.resource_name(
                'env', stack_id, role, conf=self.conf), tags)
            if group:
                if role == 'agent-mgmt':
                    for rule in self.network.security_group_rules(
                            security_group_id=self.options.mgmt_sg_id,
                            remote_group_id=group.id):
                        if (rule.remote_group_id == group.id and
                                rule.security_group_id ==
                                self.options.mgmt_sg_id):
                            self._delete(
                                self.network.delete_security_group_rule,
                                rule.id)
                self._delete(self.network.delete_security_group, group)
        egress = naming.resource_name('env', stack_id, 'egress',
                                      conf=self.conf)
        for group in self.network.address_groups(name=egress):
            if group.name == egress:
                self._delete(self.network.delete_address_group, group)
        router = self._find('router', name, tags)
        subnet = self._find('subnet', name, tags)
        if router:
            if subnet:
                self._delete(self.network.remove_interface_from_router,
                             router, subnet_id=subnet.id)
            self._delete(self.network.delete_router, router)
        if subnet:
            self._delete(self.network.delete_subnet, subnet)
        network = self._find('network', name, tags)
        if network:
            self._delete(self.network.delete_network, network)
