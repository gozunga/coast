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

"""Project-only Designate discovery and zone-link validation."""

from oslo_log import log as logging
import pecan

from coast.api import microversions
from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers as common
from coast.api.v1 import validation
from coast.common import exception
from coast.network import customer_dns


LOG = logging.getLogger(__name__)


def _warn_failure(ctx, error):
    error_class = getattr(error, 'exception_class', None)
    if error_class is None and not isinstance(
            error, (customer_dns.DNSFailure, exception.Invalid)):
        error_class = type(error).__name__
    if error_class:
        LOG.warning('DNS operation failed: %s request_id=%s',
                    error_class, ctx.request_id)


LINK_FIELDS = {'default_domain': {'type': ['string', 'null'],
                                  'maxLength': 253},
               'dns_zone_id': {'type': ['string', 'null'], 'maxLength': 36},
               'dns_trust_id': {'type': ['string', 'null'], 'maxLength': 255}}
DOMAIN_FIELDS = {'host': {'type': 'string', 'maxLength': 253},
                 'zone_id': schemas.text(36),
                 'dns_trust_id': {'type': ['string', 'null'],
                                  'maxLength': 255},
                 'record_type': {'enum': ['A', 'AAAA', 'CNAME']}}


def enabled():
    return microversions.at_least(pecan.request.version,
                                  microversions.DNS_VERSION)


def schema(base, kind):
    if not enabled():
        return base
    props = dict(base['properties'])
    props.update(LINK_FIELDS if kind == 'stack' else DOMAIN_FIELDS)
    if kind == 'domain':
        props.update(schemas.DOMAIN['properties'])
        result = schemas.document(props)
        result['minProperties'] = 1
        return result
    return schemas.document(props, base.get('required', ()))


def stack_link(ctx, values, row=None):
    if not set(values).intersection(LINK_FIELDS):
        return
    domain = values.get('default_domain', getattr(row, 'default_domain', None))
    zone_id = values.get('dns_zone_id', getattr(row, 'dns_zone_id', None))
    trust_id = values.get('dns_trust_id', getattr(row, 'dns_trust_id', None))
    if 'default_domain' in values and 'dns_zone_id' not in values:
        zone_id = None
    elif 'dns_zone_id' in values and 'default_domain' not in values:
        domain = None
    if not domain and not zone_id:
        values.update(default_domain=None, dns_zone_id=None, dns_trust_id=None)
        return
    client = customer_dns.CustomerDNS(ctx.project_id, trust_id=trust_id)
    try:
        if not domain:
            zones = [z for z in client.zones() if z.id == zone_id]
            if not zones:
                raise customer_dns.DNSFailure('DNS zone not found.')
            domain = zones[0].name
        domain = customer_dns.name(domain)
        customer_dns.default_hostname('app', '0000', domain)
        zone = client.zone(domain, zone_id)
    except Exception as error:
        _warn_failure(ctx, error)
        raise exception.Invalid(customer_dns.failure(error)) from None
    values.update(default_domain=domain, dns_zone_id=zone.id,
                  dns_trust_id=trust_id)


def domain_link(ctx, values, row=None):
    automatic = values.get('auto_dns', getattr(row, 'auto_dns', False))
    if not automatic:
        if 'host' in values or 'zone_id' in values:
            raise exception.Invalid('A host and zone require auto_dns.')
        if not row and 'fqdn' not in values:
            raise exception.Invalid('A domain name is required.')
        if values.get('auto_dns') is False:
            values.update(zone_id=None, dns_trust_id=None)
        return
    trust_id = values.get('dns_trust_id', getattr(row, 'dns_trust_id', None))
    zone_id = values.get('zone_id', getattr(row, 'zone_id', None))
    if 'fqdn' in values and 'zone_id' not in values:
        zone_id = None
    client = customer_dns.CustomerDNS(ctx.project_id, trust_id=trust_id)
    try:
        fqdn = values.get('fqdn', getattr(row, 'fqdn', None))
        if 'host' in values:
            if not zone_id or 'fqdn' in values:
                raise exception.Invalid('Specify host with zone_id, or fqdn.')
            zones = [z for z in client.zones() if z.id == zone_id]
            if not zones:
                raise customer_dns.DNSFailure('DNS zone not found.')
            host = values.pop('host')
            fqdn = (host + '.' if host else '') + zones[0].name.rstrip('.')
        fqdn = customer_dns.name(fqdn)
        zone = client.zone(fqdn, zone_id)
        record_type = values.get('record_type',
                                 getattr(row, 'record_type', None) or 'A')
        if record_type == 'CNAME' and fqdn == customer_dns.name(zone.name):
            raise exception.Invalid('A zone apex cannot use CNAME.')
    except Exception as error:
        _warn_failure(ctx, error)
        raise exception.Invalid(customer_dns.failure(error)) from None
    values.update(fqdn=fqdn, zone_id=zone.id, dns_trust_id=trust_id,
                  record_type=record_type)


class Zones:
    @pecan.expose('json')
    def index(self, **kwargs):
        ctx = common.authorize('dns', 'zones:list')
        if not enabled():
            pecan.abort(404)
        if pecan.request.method != 'GET':
            pecan.abort(405)
        validation.parameters(kwargs, {'trust_id'})
        try:
            zones = customer_dns.CustomerDNS(
                ctx.project_id, trust_id=kwargs.get('trust_id')).zones()
            return {'zones': [dict(id=z.id, name=z.name,
                                   project_id=z.project_id) for z in zones]}
        except customer_dns.DNSFailure as error:
            _warn_failure(ctx, error)
            raise
        except Exception as error:
            _warn_failure(ctx, error)
            reason = customer_dns.failure(error)
            raise customer_dns.DNSFailure(reason) from None


class DNS:
    zones = Zones()
