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

"""Idempotent DNS steps. Retain database links until cleanup succeeds."""

import contextlib

from oslo_db import exception as db_exception

from coast.common import context
from coast.common import exception
from coast.common import naming
from coast.common import states
from coast.network import customer_dns
from coast.network import dns as operator_dns
from coast import objects


def client(conf, project_id, trust_id=None):
    return customer_dns.CustomerDNS(project_id, conf, trust_id=trust_id)


def names(fqdn):
    return [fqdn, '_coast-challenge.' + fqdn]


def delete_link(conf, service, link):
    if link.get('auto_dns') and link.get('zone_id'):
        client(conf, service.project_id, link.get('dns_trust_id')).delete(
            service.stack_id, service.id, link['zone_id'], names(link['fqdn']))


def delete_domain(conf, service, domain):
    if domain.dns_previous:
        delete_link(conf, service, domain.dns_previous)
    delete_link(conf, service, dict(
        fqdn=domain.fqdn, zone_id=domain.zone_id, auto_dns=domain.auto_dns,
        dns_trust_id=domain.dns_trust_id))


def _live_service(ctx, service):
    try:
        current = objects.Service.get_by_id(ctx, service.id)
        stack = objects.Stack.get_by_id(ctx, current.stack_id)
    except exception.NotFound:
        return None
    if (current.status == states.DELETING or
            stack.status in (states.DELETING, states.DELETED)):
        return None
    return current


@contextlib.contextmanager
def domain_records(manager, ctx, service, domain, guarded):
    """Compensate late/partial writes, including rejected observation saves.

    Only deleted or superseded DNS links need cleanup. An unrelated task
    or rejected observation save does not invalidate current DNS records.
    """
    link = dict(fqdn=domain.fqdn, zone_id=domain.zone_id,
                auto_dns=domain.auto_dns, dns_trust_id=domain.dns_trust_id)
    try:
        yield
    finally:
        if guarded:
            current = _live_service(ctx, service)
            try:
                fresh = objects.Domain.get_by_id(ctx, domain.id)
            except exception.NotFound:
                fresh = None
            if (current is None or fresh is None or
                    any(getattr(fresh, key) != value
                        for key, value in link.items())):
                delete_link(manager.conf, service, link)
                raise exception.Conflict()


def _default_current(manager, ctx, service, hostname):
    current = _live_service(ctx, service)
    return (current is not None and
            manager._advertised_host(ctx, current) == hostname and
            naming.optional_attr(current, 'default_dns_zone_id') ==
            naming.optional_attr(service, 'default_dns_zone_id') and
            naming.optional_attr(current, 'default_dns_trust_id') ==
            naming.optional_attr(service, 'default_dns_trust_id'))


def ensure_domain(manager, ctx, service, stack, domain, save=None):
    """DNS errors are safe status reasons; TLS still waits for public DNS."""
    guarded = save is not None
    save = save or manager._save
    try:
        if domain.dns_previous:
            delete_link(manager.conf, service, domain.dns_previous)
            save(domain, dns_previous=None, managed_records=[])
        if not domain.auto_dns or not domain.zone_id:
            return True  # Old microversions retain their existing behavior.
        host = manager._advertised_host(ctx, service)
        address = manager._worker_address(ctx, service)
        kind = domain.record_type or 'A'
        if kind == 'CNAME' and not host:
            kind = 'AAAA' if address and ':' in address else 'A'
        route = customer_dns.routing(domain.fqdn, address, kind, host)
        records = [dict(name='_coast-challenge.' + domain.fqdn, type='TXT',
                        records=['"' + domain.verification_token + '"']),
                   route]
        dns_client = client(manager.conf, service.project_id,
                            domain.dns_trust_id)
        with domain_records(manager, ctx, service, domain, guarded):
            managed = dns_client.ensure(
                domain.zone_id, stack.id, service.id, records,
                replace_types=True)
            save(domain, managed_records=managed, status_reason=None)
        return True
    except (db_exception.DBDeadlock, exception.Conflict, exception.NotFound):
        raise
    except Exception as error:
        save(domain, status_reason=customer_dns.failure(error))
        return False


def ensure_default(manager, ctx, service, stack, hostname):
    if naming.optional_attr(service, 'default_domain_enabled') is False:
        zone = naming.optional_attr(service, 'default_dns_zone_id')
        host = naming.optional_attr(service, 'default_domain')
        if zone and host:
            client(manager.conf, service.project_id, naming.optional_attr(
                service, 'default_dns_trust_id')).delete(
                    stack.id, service.id, zone, [host])
        if host:
            operator_dns.DNS(manager.conf).delete(
                service.project_id, stack.id, service.id, names=[host])
        return
    address = manager._worker_address(ctx, service)
    if not hostname or not address:
        return
    zone = naming.optional_attr(service, 'default_dns_zone_id')
    try:
        if zone:
            dns_client = client(manager.conf, service.project_id,
                                naming.optional_attr(
                                    service, 'default_dns_trust_id'))
            dns_client.ensure(zone, stack.id, service.id,
                              [customer_dns.routing(hostname, address)])
        elif manager.conf.dns.use_designate:
            operator_dns.DNS(manager.conf).ensure(
                service.project_id, stack.id, service.id, hostname, address)
    finally:
        if not _default_current(manager, ctx, service, hostname):
            if zone:
                client(manager.conf, service.project_id, naming.optional_attr(
                    service, 'default_dns_trust_id')).delete(
                        stack.id, service.id, zone, [hostname])
            elif manager.conf.dns.use_designate:
                operator_dns.DNS(manager.conf).delete(
                    service.project_id, stack.id, service.id, names=[hostname])


def delete_service(conf, ctx, service):
    # Scan descriptions as well as observations: a process can stop after
    # Designate accepts a create but before its ID is saved in Coast.
    zone = naming.optional_attr(service, 'default_dns_zone_id')
    if zone:
        client(conf, service.project_id, naming.optional_attr(
            service, 'default_dns_trust_id')).delete(
                service.stack_id, service.id, zone)
    marker = None
    while True:
        domains = objects.DomainList.get_all(
            ctx, filters={'service_id': service.id},
            marker=marker, sort_key='id')
        if not domains:
            break
        for domain in domains:
            delete_domain(conf, service, domain)
        marker = domains[-1].id
    operator_dns.DNS(conf).delete(
        service.project_id, service.stack_id, service.id)


class Cleanup:
    """Stack-teardown adapter, including the optional operator zone."""

    def __init__(self, conf):
        self.conf = conf

    def delete(self, project_id, stack_id, service_id):
        ctx = context.RequestContext(project_id=project_id)
        service = objects.Service.get_by_id(ctx, service_id)
        if service.stack_id != stack_id:
            return
        delete_service(self.conf, ctx, service)
