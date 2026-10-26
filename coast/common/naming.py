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

"""Names for Coast-created Nova and Neutron resources."""

import hashlib
import ipaddress
import re
import uuid

from oslo_config import cfg


def optional_attr(resource, name):
    """Return a field, or None when a versioned object has not set it."""
    checker = getattr(resource, 'obj_attr_is_set', None)
    if checker is not None and not checker(name):
        return None
    return getattr(resource, name, None)


def dashed_ip(address):
    """Return one DNS label for a wildcard-IP hostname.

    IPv4 replaces dots with dashes. IPv6 uses the compressed text and
    replaces colons with dashes. IPv4-mapped IPv6 uses the IPv4 form.
    """
    parsed = ipaddress.ip_address(str(address).strip())
    if parsed.version == 6 and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    if parsed.version == 4:
        return str(parsed).replace('.', '-')
    text = parsed.compressed
    return text.replace(':', '-')


def default_hostname(name, service_id, zone, address=None):
    """Return the default service host, or None when an input is empty.

    A usable address is embedded: ``<name>-<id4>.<dashed-ip>.<zone>``.
    No address, or an address that is not an IP, uses
    ``<name>-<id4>.<zone>``.
    """
    if not zone or not name or not service_id:
        return None
    zone = zone.rstrip('.').lower()
    if not zone:
        return None
    label = '%s-%s' % (name, str(service_id)[:4])
    if address:
        try:
            dashed = dashed_ip(address)
        except ValueError:
            dashed = None
        if dashed:
            return '%s.%s.%s' % (label, dashed, zone)
    return '%s.%s' % (label, zone)


def _host_text(value):
    """Return the host text of a bare name or an http(s) URL."""
    text = str(value or '').strip()
    if '://' in text:
        text = text.split('://', 1)[1]
    return text.split('/')[0].split(':')[0].rstrip('.')


def hostname_of(value):
    """Return the lower-case host of a bare name or an http(s) URL."""
    return _host_text(value).lower()


def domain_url(fqdn, tls='letsencrypt'):
    """Return ``http://`` for tls ``none``, otherwise ``https://``.

    The host keeps the case of ``fqdn``. Comparison uses ``hostname_of``.
    """
    host = _host_text(fqdn)
    if not host:
        return None
    scheme = 'http' if tls == 'none' else 'https'
    return '%s://%s' % (scheme, host)


def domain_set(items):
    """Return comparable lower-case domain strings."""
    return {str(item).strip().lower() for item in items if str(item).strip()}


def _default_slot(host, name, service_id, suffix, old_host):
    if old_host and host == hostname_of(old_host):
        return True
    if not name or not service_id or not suffix:
        return False
    prefix = '%s-%s.' % (str(name).lower(), str(service_id)[:4])
    tail = '.' + suffix.rstrip('.').lower()
    if not (host.startswith(prefix) and host.endswith(tail)):
        return False
    if len(host) < len(prefix) + len(tail) - 1:
        return False
    middle = host[len(prefix):len(host) - len(tail)]
    return middle == '' or ('.' not in middle and bool(middle))


def replace_default_domain(domains, old_host, new_host, name, service_id,
                           suffix):
    """Replace the generated default host and keep every other name.

    A kept name keeps its scheme. The new default is always ``https``.
    """
    kept = []
    new_text = _host_text(new_host) if new_host else ''
    new_key = new_text.lower()
    for item in domains:
        host = hostname_of(item)
        if not host:
            continue
        if _default_slot(host, name, service_id, suffix, old_host):
            continue
        if new_key and host == new_key:
            continue
        kept.append(item)
    if new_text:
        kept.insert(0, 'https://' + new_text)
    return kept


def resource_name(kind, *parts, conf=cfg.CONF):
    """Return a safe name of at most 255 ASCII characters.

    Replace unsafe runs with a hyphen and omit empty components. Long
    names keep a digest suffix so different resource roles stay distinct.
    The optional conf supports callers with an isolated configuration.
    """
    components = [re.sub(r'[^A-Za-z0-9-]+', '-', str(value)).strip('-')
                  for value in (conf.resource_name_prefix, kind, *parts)]
    name = '-'.join(value for value in components if value)
    if len(name) > 255:
        digest = hashlib.sha256(name.encode('ascii')).hexdigest()[:12]
        name = name[:242].rstrip('-') + '-' + digest
    return name


def server_hostname(stack_name, stack_id, server_name, server_id, taken=(),
                    conf=cfg.CONF):
    """Choose a DNS label once; callers persist it with the server row."""
    def slug(value, limit, default):
        value = re.sub(r'[^a-z0-9-]+', '-', str(value).lower())
        value = re.sub('-+', '-', value).strip('-')
        return value[:limit].rstrip('-') or default

    # Reserve room for every component even with a custom resource prefix.
    prefix = slug(conf.resource_name_prefix, 16, 'coast')
    stack = slug(stack_name, min(16, 21 - len(prefix)), 'stack')
    server = slug(server_name, 10, 'server')
    identifier = uuid.UUID(str(stack_id)).hex
    base = '%s-%s-%s-%s' % (prefix, stack, identifier[:6], server)
    if base not in taken:
        return base
    grown = '%s-%s-%s-%s' % (prefix, stack, identifier[:10], server)
    if grown not in taken:
        return grown
    return grown + '-' + uuid.UUID(str(server_id)).hex[:4]


def assign_server_hostname(ctx, stack, server, taken=(), conf=cfg.CONF):
    """Include live project peers and pending siblings in collision checks."""
    from coast.db import api
    if not server.obj_attr_is_set('id'):
        server.id = str(uuid.uuid4())
    with api.reader.using(ctx):
        peers = api.servers._query(ctx).filter_by(project_id=stack.project_id)
        names = {row.hostname for row in peers}
    server.hostname = server_hostname(
        stack.name, stack.id, server.name, server.id,
        names.union(taken), conf=conf)
