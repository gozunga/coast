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

"""Customer DNS records with strict project and service ownership."""

import functools
import ipaddress
import re

from keystoneauth1 import exceptions as ks_exceptions
from openstack import exceptions
from oslo_config import cfg

from coast.common import exception
from coast.network import dns
from coast.network import dns_credentials


def name(value):
    value = str(value or '').rstrip('.').lower()
    if (len(value) > 253 or not value or
            any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',
                                 label) for label in value.split('.'))):
        raise exception.Invalid('Invalid DNS name.')
    return value


def default_hostname(app_name, service_id, domain):
    """Build one valid host label from the app name and stable ID suffix."""
    domain = name(domain)
    available = min(58, 247 - len(domain))
    if available < 1:
        raise exception.Invalid(
            'The default domain leaves no room for a host.')
    label = re.sub(r'[^a-z0-9-]+', '-', app_name.lower()).strip('-')
    label = (label or 'app')[:available].rstrip('-') or 'a'
    return name('%s-%s.%s' % (label, service_id[:4], domain))


def contains(zone, fqdn):
    return fqdn == zone or fqdn.endswith('.' + zone)


class DNSFailure(exception.CoastException):
    msg_fmt = 'DNS operation failed.'
    code = 409


class DNSUnavailable(DNSFailure):
    msg_fmt = 'DNS zones are not available on this cloud.'
    code = 404


def failure(error):
    if isinstance(error, (ks_exceptions.EndpointNotFound,
                          exceptions.EndpointNotFound)):
        return DNSUnavailable.msg_fmt
    if isinstance(error, (DNSFailure, exception.Invalid)):
        return str(error)
    code = getattr(error, 'status_code', None)
    if code == 404:
        return 'DNS zone not found.'
    if code == 409:
        return 'DNS record conflict.'
    if code == 413 or code == 429:
        return 'DNS quota or rate limit exceeded.'
    if code in (401, 403):
        return 'DNS credentials do not permit this operation.'
    return 'DNS service unavailable.'


def safe_call(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (DNSFailure, exception.Invalid):
            raise
        except (ks_exceptions.EndpointNotFound,
                exceptions.EndpointNotFound):
            raise DNSUnavailable() from None
        except Exception as error:
            safe_error = DNSFailure(failure(error))
            safe_error.exception_class = type(error).__name__
            raise safe_error from None
    return call


class CustomerDNS:
    def __init__(self, project_id, conf=cfg.CONF, provider=None,
                 trust_id=None):
        self.project_id = project_id
        self.conf = conf
        self.provider = provider or dns_credentials.get_provider(
            conf, trust_id)
        self.trust_id = trust_id

    def proxy(self, zone_id=None):
        return self.provider.proxy(self.project_id, zone_id, self.trust_id)

    @safe_call
    def zones(self):
        # Sudo is not a substitute for checking the returned ownership.
        return [zone for zone in self.proxy().zones()
                if zone.project_id == self.project_id]

    @safe_call
    def zone(self, fqdn, zone_id=None):
        fqdn = name(fqdn)
        matches = [z for z in self.zones()
                   if contains(name(z.name), fqdn) and
                   (zone_id is None or z.id == zone_id)]
        if not matches:
            raise DNSFailure('DNS zone not found in this project.')
        return max(matches, key=lambda z: len(name(z.name)))

    @safe_call
    def ensure(self, zone_id, stack_id, service_id, records,
               replace_types=False):
        owner = dns.DNS._owner(self.project_id, stack_id, service_id)
        result = []
        # Validate every name, zone and collision before the first write.
        proxy = self.proxy(zone_id)
        plan = []
        for wanted in records:
            fqdn = wanted['name'].rstrip('.').lower()
            check = fqdn.removeprefix('_coast-challenge.')
            self.zone(check, zone_id)
            matches = [r for r in proxy.recordsets(zone_id, name=fqdn + '.')
                       if r.name.rstrip('.').lower() == fqdn]
            same = [r for r in matches if r.type == wanted['type']]
            if (len(same) > 1 or any(
                    r.description != owner and
                    (r.type == wanted['type'] or r.type == 'CNAME' or
                     wanted['type'] == 'CNAME') for r in matches)):
                raise DNSFailure('DNS record conflict: record is not owned '
                                 'by this service.')
            incompatible = [r for r in matches if r.type != wanted['type']
                            and (r.type == 'CNAME' or
                                 wanted['type'] == 'CNAME')]
            if incompatible and not replace_types:
                raise DNSFailure('DNS record conflict: incompatible type.')
            plan.append((wanted, same[0] if same else None, incompatible))
        for wanted, existing, incompatible in plan:
            # Use actual owned records, never saved observations: the last
            # write may have succeeded before Coast could persist its IDs.
            for record in incompatible:
                try:
                    proxy.delete_recordset(record, zone=zone_id,
                                           ignore_missing=True)
                except exceptions.ResourceNotFound:
                    pass
            attrs = dict(records=wanted['records'],
                         ttl=self.conf.dns.record_ttl)
            if existing:
                if (existing.records != wanted['records'] or
                        existing.ttl != self.conf.dns.record_ttl):
                    proxy.update_recordset(existing, zone_id=zone_id, **attrs)
                record_id = existing.id
            else:
                record_id = proxy.create_recordset(
                    zone_id, name=wanted['name'].rstrip('.') + '.',
                    type=wanted['type'], description=owner, **attrs).id
            result.append(dict(wanted, zone_id=zone_id, id=record_id))
        return result

    @safe_call
    def delete(self, stack_id, service_id, zone_id=None, names=None):
        owner = dns.DNS._owner(self.project_id, stack_id, service_id)
        zones = self.zones()
        for zone in zones:
            if zone_id and zone.id != zone_id:
                continue
            proxy = self.proxy(zone.id)
            for record in proxy.recordsets(zone.id):
                if (record.description != owner or
                        (names is not None and
                         record.name.rstrip('.') not in names)):
                    continue
                try:
                    proxy.delete_recordset(record, zone=zone.id,
                                           ignore_missing=True)
                except exceptions.ResourceNotFound:
                    pass


def routing(fqdn, address, record_type='A', target=None):
    if record_type == 'CNAME':
        if not target:
            raise DNSFailure('The default hostname is unavailable.')
        return dict(name=fqdn, type='CNAME',
                    records=[target.rstrip('.') + '.'])
    if not address:
        raise DNSFailure('The worker has no public IP address.')
    parsed = ipaddress.ip_address(address)
    if record_type == 'AAAA' and parsed.version != 6:
        raise DNSFailure('The worker has no public IPv6 address.')
    return dict(name=fqdn, type='AAAA' if parsed.version == 6 else 'A',
                records=[str(parsed)])
