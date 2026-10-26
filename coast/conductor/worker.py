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

"""Worker cloud resources, using controller ownership and boot helpers."""

import base64
import copy
import json
import time

from openstack import exceptions

from coast.common import naming
from coast.conductor import controller


class WorkerCloud(controller.ControllerCloud):
    def image_flavor(self, server, plan_name, selection=None):
        image = self.select_image('worker', selection)
        plans = [plan for plan in json.loads(self.conf.plans.definitions)
                 if plan['name'] == plan_name and plan['kind'] == server.kind
                 and server.role in plan['roles']]
        if len(plans) != 1:
            raise ValueError('One owned worker image and VM plan are required')
        flavor = self.proxy('nova').find_flavor(
            plans[0]['flavor'], ignore_missing=False)
        return image.id, flavor.id

    def port(self, server, role, network_id, group, subnet_id=None):
        # Neutron allocates worker addresses, never controller_ip.
        return super().port(server, role, network_id, group)

    def config_drive(self, server, volumes, heartbeat_key):
        data, serial, expires = super().config_drive(
            server, volumes, heartbeat_key)
        payload = json.loads(base64.b64decode(data).decode().split('\n', 1)[1])
        for entry in payload['write_files']:
            if entry['path'] == '/etc/coast/coast.conf':
                entry['content'] = entry['content'].replace(
                    'role=controller\n', 'role=' + server.role + '\n')
        user = self.conf.coolify.ssh_user
        # cc_ssh also copies metadata keys to root. Import the admin public
        # key explicitly for the default user instead, never through cc_ssh.
        payload['allow_public_ssh_keys'] = False
        payload['disable_root'] = True
        admin = dict(name='default')
        key_name = self.conf.controller_worker.admin_ssh_key_name
        if key_name:
            key = self.proxy('nova').get_keypair(key_name).public_key
            if not isinstance(key, str) or not key.strip():
                raise ValueError('Admin SSH public key is required')
            admin['ssh_authorized_keys'] = [key]
        payload['users'] = [admin, dict(
            name=user, ssh_authorized_keys=[self.stack.ssh_public_key])]
        # Replace image-provided root keys before users-groups runs.
        payload['write_files'].append(dict(
            path='/root/.ssh/authorized_keys', owner='root:root',
            permissions='0600', content=(
                self.stack.ssh_public_key + '\n' if user == 'root' else '')))
        data = '#cloud-config\n' + json.dumps(payload)
        return base64.b64encode(data.encode()).decode(), serial, expires

    def owned_guest(self, identifier):
        try:
            guest = self.proxy('nova').get_server(identifier)
        except exceptions.ResourceNotFound:
            return None
        if not set(self.tags).issubset(guest.tags or []):
            raise ValueError('Guest ownership differs')
        return guest

    def retained_ports(self, server):
        ports = []
        for role, address, net in (
                ('mgmt', server.mgmt_ip,
                 self.conf.controller_worker.mgmt_net_id),
                ('env', server.private_ip, self.stack.network_id)):
            name = naming.resource_name(server.id, role, conf=self.conf)
            found = [port for port in self.proxy('neutron').ports(name=name)
                     if port.name == name and
                     set(self.tags).issubset(port.tags or [])]
            if (len(found) != 1 or found[0].network_id != net or
                    address not in [ip['ip_address']
                                    for ip in found[0].fixed_ips] or
                    found[0].device_id not in ('', None, server.compute_id)):
                raise ValueError('Retained port ownership differs')
            ports.append(found[0])
        return ports

    def stopped(self, identifier, receipt=None):
        nova = self.proxy('nova')
        guest = self.owned_guest(identifier)
        if guest is not None:
            if receipt is not None:
                # Use a private adapter so concurrent cloud calls retain their
                # configured microversion. Older Nova falls back to plain lock.
                locking = copy.copy(nova)
                locking.default_microversion = '2.73'
                try:
                    locking.lock_server(
                        identifier, locked_reason='Coast failover ' + receipt)
                except exceptions.HttpException as exc:
                    if exc.status_code != 406:
                        raise
                    nova.lock_server(identifier)
            if guest.status != 'SHUTOFF':
                nova.stop_server(identifier)
            nova.wait_for_server(
                guest, status='SHUTOFF', failures=['ERROR'],
                interval=self.conf.controller_worker.build_poll_interval,
                wait=self.conf.controller_worker.build_timeout)
        return guest

    def detach(self, identifier, ports, volumes):
        nova = self.proxy('nova')
        if self.owned_guest(identifier) is not None:
            attached = {item.volume_id for item in
                        nova.volume_attachments(identifier)}
            for volume in volumes:
                if volume.cinder_volume_id in attached:
                    nova.delete_volume_attachment(
                        identifier, volume.cinder_volume_id)
            attached = {item.port_id for item in
                        nova.server_interfaces(identifier)}
            for port in ports:
                if port.id in attached:
                    nova.delete_server_interface(port.id, identifier)
        for volume in volumes:
            self.wait_volume(volume.cinder_volume_id)
        deadline = time.monotonic() + self.conf.controller_worker.build_timeout
        while any(self.proxy('neutron').get_port(port.id).device_id
                  for port in ports):
            if time.monotonic() >= deadline:
                raise ValueError('Retained port detach timed out')
            time.sleep(self.conf.controller_worker.build_poll_interval)

    def replacement(self, attempt):
        tags = set(self.tags + ['attempt=' + attempt])
        found = [guest for guest in self.proxy('nova').servers(
            tags=','.join(sorted(tags)))
                 if tags.issubset(guest.tags or [])]
        if len(found) > 1:
            raise ValueError('Duplicate replacement guests')
        return found[0] if found else None

    def delete_guest(self, identifier):
        guest = self.owned_guest(identifier)
        if guest is not None:
            nova = self.proxy('nova')
            nova.unlock_server(identifier)
            nova.delete_server(guest, ignore_missing=True)
            nova.wait_for_delete(
                guest,
                interval=self.conf.controller_worker.build_poll_interval,
                wait=self.conf.controller_worker.build_timeout)

    def restore(self, identifier, ports, volumes):
        nova = self.proxy('nova')
        if self.owned_guest(identifier) is None:
            raise ValueError('Old guest is unavailable')
        attached = {item.volume_id for item in
                    nova.volume_attachments(identifier)}
        for volume in volumes:
            if volume.cinder_volume_id not in attached:
                self.wait_volume(volume.cinder_volume_id)
                nova.create_volume_attachment(identifier,
                                              volume.cinder_volume_id)
        attached = {item.port_id for item in
                    nova.server_interfaces(identifier)}
        for port in ports:
            if port.id not in attached:
                nova.create_server_interface(identifier, port_id=port.id)
        nova.unlock_server(identifier)
        nova.start_server(identifier)
        self.wait_server(identifier)
