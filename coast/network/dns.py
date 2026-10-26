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

"""Optional default-service Designate records, separate from API state."""

import ipaddress
import re

from openstack import exceptions
from oslo_config import cfg

from coast.network import dns_credentials


class DNS:
    def __init__(self, conf=cfg.CONF, connection=None, provider=None):
        self.options = conf.dns
        self.conf = conf
        self.connection = connection
        self.provider = provider or dns_credentials.ServiceProjectProvider(
            conf)

    def _proxy(self):
        if not self.options.designate_zone_id:
            raise ValueError('Designate zone ID is required when enabled')
        if self.connection is not None:
            return self.connection.dns
        return self.provider.proxy(None, self.options.designate_zone_id)

    @staticmethod
    def _owner(project_id, stack_id, service_id):
        # Recordsets support descriptions, not Neutron-style tags.
        return ('coast project_id=' + project_id +
                ' stack_id=' + stack_id + ' service_id=' + service_id)

    def _name(self, hostname):
        name = hostname.rstrip('.').lower()
        domain = self.conf.domains.default_suffix.rstrip('.').lower()
        if (not domain or not name.endswith('.' + domain) or len(name) > 253 or
                any(not re.fullmatch(
                    r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                    for label in name.split('.'))):
            raise ValueError(
                'An explicit default-service hostname is required')
        return name + '.'

    def ensure(self, project_id, stack_id, service_id, hostname, address):
        """Return exact A records in both modes; publish only when enabled."""
        name = self._name(hostname)
        address = str(ipaddress.IPv4Address(address))
        required = [dict(name=name, type='A', records=[address])]
        if not self.options.use_designate:
            return required
        proxy = self._proxy()
        zone = self.options.designate_zone_id
        zone_name = proxy.get_zone(zone).name.rstrip('.').lower()
        if not name.rstrip('.').endswith('.' + zone_name):
            raise ValueError(
                'Default-service hostname is outside the DNS zone')
        owner = self._owner(project_id, stack_id, service_id)
        matches = [item for item in proxy.recordsets(zone, name=name, type='A')
                   if item.name == name and item.type == 'A']
        if len(matches) > 1:
            raise ValueError('Multiple DNS recordsets match')
        if matches:
            record = matches[0]
            if record.description != owner:
                raise ValueError('DNS recordset is not owned by this service')
            if (record.records != [address] or
                    record.ttl != self.options.record_ttl):
                proxy.update_recordset(
                    record, zone_id=zone, records=[address],
                    ttl=self.options.record_ttl)
        else:
            proxy.create_recordset(
                zone, name=name, type='A', records=[address],
                description=owner, ttl=self.options.record_ttl)
        return required

    def delete(self, project_id, stack_id, service_id, names=None):
        """Delete this service's owned records, including old hostnames."""
        if not self.options.use_designate:
            return
        proxy = self._proxy()
        zone = self.options.designate_zone_id
        owner = self._owner(project_id, stack_id, service_id)
        for record in proxy.recordsets(zone):
            if (record.description == owner and
                    (names is None or record.name.rstrip('.') in names)):
                try:
                    proxy.delete_recordset(
                        record, zone=zone, ignore_missing=True)
                except exceptions.ResourceNotFound:
                    pass
