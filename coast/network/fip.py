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

"""Worker ingress floating IP steps; call under the stack lock."""

from openstack import exceptions
from oslo_config import cfg

from coast.common import clients
from coast.common import naming
from coast.network import stack
from coast.network import tags as neutron_tags


class FloatingIP:
    def __init__(self, conf=cfg.CONF, connection=None):
        self.conf = conf
        self.options = conf.controller_worker
        self.network = (connection or clients.get_connection(
            'neutron', conf=conf)).network

    @staticmethod
    def _tags(project_id, stack_id, server_id):
        return stack.EnvironmentNetwork._tags(
            project_id, stack_id) + ['server_id=' + server_id]

    def _find(self, tags):
        found = [ip for ip in self.network.ips(tags=','.join(tags))
                 if set(tags).issubset(ip.tags or [])]
        if len(found) > 1:
            raise ValueError('Multiple Coast floating IPs match')
        return found[0] if found else None

    def ensure(self, project_id, stack_id, server_id, port_id):
        """Allocate or adopt the worker FIP, then associate its env port."""
        if not self.options.external_network_id:
            raise ValueError('External network ID is required')
        tags = self._tags(project_id, stack_id, server_id)
        owner = set(tags[:3])
        port = self.network.get_port(port_id)
        net = self.network.get_network(port.network_id)
        if (net.name != naming.resource_name(
                'env', stack_id, conf=self.conf) or
                not owner.issubset(net.tags or []) or
                not port.is_port_security_enabled):
            raise ValueError('Ingress port must be on the owned env network')
        groups = [self.network.get_security_group(id)
                  for id in port.security_group_ids]
        if not any(group.name == naming.resource_name(
                'env', stack_id, 'worker', conf=self.conf)
                   and owner.issubset(group.tags or []) for group in groups):
            raise ValueError('Ingress port must have the owned worker group')
        ip = self._find(tags)
        if ip is None:
            # Tags are set after create so a later call can adopt the FIP.
            # A crash between the two calls leaves an untagged FIP. Remove
            # it before retry. Tagging failure deletes the new FIP.
            ip = neutron_tags.create_tagged(
                self.network, 'ip', tags,
                floating_network_id=self.options.external_network_id,
                description='Coast worker ingress')
        if ip.floating_network_id != self.options.external_network_id:
            raise ValueError('Owned floating IP uses another external network')
        if ip.port_id not in (None, '', port_id):
            raise ValueError('Owned floating IP is associated with '
                             'another port')
        if ip.port_id != port_id:
            ip = self.network.update_ip(ip, port_id=port_id)
        return ip

    def delete(self, project_id, stack_id, server_id):
        """Explicit revert; release only this worker's tagged floating IP."""
        ip = self._find(self._tags(project_id, stack_id, server_id))
        if ip is not None:
            try:
                self.network.delete_ip(ip, ignore_missing=True)
            except exceptions.ResourceNotFound:
                pass
