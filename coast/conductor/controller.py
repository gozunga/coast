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

"""Controller cloud resources and config-drive data; no DB transactions."""

import base64
import json
import re

from cryptography import x509
from openstack import exceptions

from coast.agent import egress
from coast.common import clients
from coast.common import exception
from coast.common import naming
from coast.common import tls
from coast.conductor import registry
from coast.network import stack as network
from coast.network import tags as neutron_tags

# virtio-blk returns 20 serial bytes (Linux VIRTIO_BLK_ID_BYTES).
# QEMU scsi-disk uses that same length for the default VPD 0x83 device
# id (MAX_SERIAL_LEN_FOR_DEVID). Libvirt may set device_id to the full
# 36-character Cinder id. systemd scsi_id then creates
# scsi-0QEMU_QEMU_HARDDISK_<serial>. Page 0x80 allows 36 characters.
_VOLUME_ID = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_DISK_SERIAL_LEN = 20
_SCSI_ID_PREFIX = '0QEMU_QEMU_HARDDISK_'
_LINK_WAIT_SECONDS = 60


def controller_ip(conf):
    options = conf.controller_worker
    subnet = network.stack_subnet(options)
    # Gateway uses the first host. Reserve the second for the controller.
    return str(subnet.network_address + 2)


def disk_serial(volume_id):
    """Return the 20-character guest serial for a Cinder volume id."""
    if not isinstance(volume_id, str) or _VOLUME_ID.match(volume_id) is None:
        raise ValueError('Cinder volume id is not a UUID')
    return volume_id[:_DISK_SERIAL_LEN]


def disk_candidates(volume_id):
    """Return virtio-blk and virtio-scsi by-id paths for one volume.

    virtio-blk publishes the 20-character serial. virtio-scsi publishes
    that serial when QEMU fills device_id, or the full id when libvirt
    sets device_id.
    """
    serial = disk_serial(volume_id)
    names = (
        'virtio-' + serial,
        'scsi-' + _SCSI_ID_PREFIX + serial,
        'scsi-' + _SCSI_ID_PREFIX + volume_id,
    )
    return tuple('/dev/disk/by-id/' + name for name in names)


def disk_alias(volume_id):
    """Return the stable by-id path that cloud-init mounts."""
    disk_serial(volume_id)
    return '/dev/disk/by-id/coast-' + volume_id


def _udev_lines(volume_id):
    serial = disk_serial(volume_id)
    alias = 'coast-' + volume_id
    values = (
        serial,
        _SCSI_ID_PREFIX + serial,
        _SCSI_ID_PREFIX + volume_id,
    )
    return [
        'SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", '
        'ENV{ID_SERIAL}=="%s", SYMLINK+="disk/by-id/%s"' % (value, alias)
        for value in values]


def volume_link_script(volumes):
    """Return a bootcmd that links each volume for virtio-blk or scsi.

    bootcmd runs before cloud-init formats and mounts the disks. The
    udev rule creates the same link on later boots, before fstab runs.
    Do not stop docker.service or mask docker.socket. The vendor unit
    requires the socket, and a stop cancels the start queued for after
    the Cinder mounts.
    """
    identifiers = [volume.cinder_volume_id for volume in volumes]
    serials = [disk_serial(value) for value in identifiers]
    if len(set(serials)) != len(serials):
        raise ValueError('Guest disk serials are not unique')
    rules = '\n'.join(
        line for value in identifiers for line in _udev_lines(value))
    calls = []
    for value in identifiers:
        quoted = ' '.join("'%s'" % path for path in disk_candidates(value))
        calls.append("link_disk '%s' %s" % (disk_alias(value), quoted))
    return '\n'.join([
        'set -eu',
        'mkdir -p /dev/disk/by-id /etc/udev/rules.d',
        "cat > /etc/udev/rules.d/99-coast-volumes.rules <<'ENDCOASTRULES'",
        rules,
        'ENDCOASTRULES',
        'chmod 0644 /etc/udev/rules.d/99-coast-volumes.rules',
        'udevadm control --reload-rules || true',
        'udevadm trigger --subsystem-match=block --action=change || true',
        'link_disk() {',
        '  dest=$1',
        '  shift',
        '  for candidate in "$@"; do',
        '    if [ -b "$candidate" ]; then',
        '      ln -sfn "$candidate" "$dest"',
        '      return 0',
        '    fi',
        '  done',
        '  return 1',
        '}',
        'n=0',
        'while [ "$n" -lt %d ]; do' % _LINK_WAIT_SECONDS,
        '  pending=0',
        *['  ' + call + ' || pending=1' for call in calls],
        '  if [ "$pending" -eq 0 ]; then',
        '    exit 0',
        '  fi',
        '  n=$((n + 1))',
        '  sleep 1',
        'done',
        'echo "coast: missing block device" >&2',
        'exit 1',
    ])


class ControllerCloud:
    def __init__(self, conf, stack=None):
        self.conf = conf
        self.stack = stack
        self.tags = network.EnvironmentNetwork._tags(
            stack.project_id, stack.id) if stack is not None else []

    def proxy(self, service):
        if service == 'nova':
            return clients.get_sensitive_proxy(service, self.conf)
        connection = clients.get_connection(service, self.conf)
        attribute = {'neutron': 'network', 'cinder': 'block_storage',
                     'glance': 'image'}[service]
        return getattr(connection, attribute)

    def select_image(self, role, selection=None):
        options = self.conf.controller_worker
        tag = getattr(options, 'image_tag_' + role)
        identifier = (None if selection is not None else
                      getattr(options, role + '_image_id'))
        if not tag or not options.image_owner_id:
            raise ValueError('Image tag and owner are required')
        images = [image for image in self.proxy('glance').images(
            tag=tag, owner=options.image_owner_id, status='active')
            if image.owner == options.image_owner_id and
            tag in (image.tags or []) and image.status == 'active' and
            (image.properties or {}).get('coast_role') == role and
            (not identifier or image.id == identifier) and
            (selection is None or selection in (image.id, image.name))]
        if len(images) != 1:
            raise ValueError('Exactly one owned %s image is required' % role)
        return images[0]

    def image_flavor(self, selection=None):
        image = self.select_image('controller', selection)
        plans = [plan for plan in json.loads(self.conf.plans.definitions)
                 if plan['name'] == self.stack.plan and
                 plan['kind'] == 'vm' and 'control' in plan['roles']]
        if len(plans) != 1:
            raise ValueError('A controller VM plan is required')
        flavor = self.proxy('nova').find_flavor(
            plans[0]['flavor'], ignore_missing=False)
        return image.id, flavor.id

    def port(self, server, role, network_id, group, subnet_id=None):
        proxy = self.proxy('neutron')
        name = naming.resource_name(server.id, role, conf=self.conf)
        ports = [port for port in proxy.ports(name=name,
                 tags=','.join(self.tags)) if port.name == name and
                 set(self.tags).issubset(port.tags or [])]
        if len(ports) > 1:
            raise ValueError('Duplicate owned controller ports')
        fixed = ([{'subnet_id': subnet_id,
                   'ip_address': controller_ip(self.conf)}]
                 if subnet_id else None)
        if ports:
            port = ports[0]
        else:
            values = dict(name=name, network_id=network_id,
                          security_group_ids=[group],
                          is_port_security_enabled=True)
            if fixed:
                values['fixed_ips'] = fixed
            port = neutron_tags.create_tagged(
                proxy, 'port', self.tags, **values)
        if (port.network_id != network_id or
                port.security_group_ids != [group] or
                not port.is_port_security_enabled or
                (fixed and port.fixed_ips != fixed)):
            raise ValueError('Controller port configuration differs')
        return port

    def delete_ports(self, server):
        proxy = self.proxy('neutron')
        for role in ('mgmt', 'env'):
            name = naming.resource_name(server.id, role, conf=self.conf)
            for port in proxy.ports(name=name, tags=','.join(self.tags)):
                if port.name == name and set(self.tags).issubset(port.tags):
                    try:
                        proxy.delete_port(port, ignore_missing=True)
                    except exceptions.ResourceNotFound:
                        continue

    def find_volume(self, volume):
        proxy = self.proxy('cinder')
        metadata = dict(tag.split('=', 1) if '=' in tag else (tag, 'true')
                        for tag in self.tags)
        found = [item for item in proxy.volumes(name=volume.name)
                 if item.name == volume.name and
                 all(item.metadata.get(k) == v for k, v in metadata.items())]
        if len(found) > 1:
            raise ValueError('Duplicate owned controller volumes')
        return found[0] if found else None

    def volume(self, volume):
        proxy = self.proxy('cinder')
        result = self.find_volume(volume)
        if result:
            if (result.size != volume.size_gb or
                    (volume.volume_type and
                     result.volume_type != volume.volume_type)):
                raise ValueError('Controller volume configuration differs')
        else:
            metadata = dict(tag.split('=', 1) if '=' in tag else (tag, 'true')
                            for tag in self.tags)
            # Cinder has metadata, not Neutron-style tags (Q92).
            result = proxy.create_volume(
                name=volume.name, size=volume.size_gb,
                volume_type=volume.volume_type, metadata=metadata)
        return result

    def wait_volume(self, identifier):
        proxy = self.proxy('cinder')
        proxy.wait_for_status(proxy.get_volume(identifier), status='available',
                              failures=['error'],
                              interval=self.conf.controller_worker.
                              build_poll_interval,
                              wait=self.conf.controller_worker.build_timeout)

    def delete_volume(self, identifier):
        proxy = self.proxy('cinder')
        try:
            volume = proxy.get_volume(identifier)
        except exceptions.ResourceNotFound:
            return
        proxy.delete_volume(volume, ignore_missing=True)
        proxy.wait_for_delete(volume, interval=self.conf.controller_worker.
                              build_poll_interval,
                              wait=self.conf.controller_worker.build_timeout)

    def check_router(self):
        proxy = self.proxy('neutron')
        if not self.stack.router_id:
            raise exception.RouterGatewayError()
        try:
            router = proxy.get_router(self.stack.router_id)
        except exceptions.ResourceNotFound:
            raise exception.RouterGatewayError() from None
        if not router or not (router.external_gateway_info or {}).get(
                'network_id'):
            raise exception.RouterGatewayError()
        if not self.stack.subnet_id:
            raise exception.SubnetGatewayError()
        try:
            subnet = proxy.get_subnet(self.stack.subnet_id)
        except exceptions.ResourceNotFound:
            raise exception.SubnetGatewayError() from None
        if not subnet or not subnet.gateway_ip:
            raise exception.SubnetGatewayError()

    def config_drive(self, server, volumes, heartbeat_key):
        cert = tls.issue_server_certificate(server.id, server.mgmt_ip,
                                            conf=self.conf)
        serial = str(x509.load_pem_x509_certificate(
            cert.certificate_pem).serial_number)
        options = self.conf.controller_worker
        with open(options.agent_client_ca) as stream:
            client_ca = stream.read()
        with open(self.conf.certificates.ca_certificate) as stream:
            server_ca = stream.read()
        egress.target(options.egress_check_url)
        project = self.stack.project_id
        if (not isinstance(project, str) or not re.fullmatch(
                r'[A-Za-z0-9_-]{1,64}', project)):
            raise ValueError('Stack project id is required')
        configuration = (
            '[agent]\nbind_ip=%s\nserver_id=%s\nstack_id=%s\n'
            'project_id=%s\n'
            'role=controller\nserver_cert=/etc/coast/agent.crt\n'
            'server_key=/etc/coast/agent.key\n'
            'client_ca=/etc/coast/client-ca.crt\n'
            'server_ca=/etc/coast/server-ca.crt\nheartbeat_key=%s\n'
            'health_manager_ips=%s\negress_check_url=%s\n' % (
                server.mgmt_ip, server.id, self.stack.id, project,
                heartbeat_key, ','.join(options.health_manager_ips),
                options.egress_check_url))
        extra, registry_files = registry.files_for(
            self.conf, self.stack, server, controller_ip(self.conf))
        configuration += extra
        files = {'/etc/coast/coast.conf': configuration,
                 '/etc/coast/agent.crt': cert.certificate_pem.decode('ascii'),
                 '/etc/coast/agent.key': cert.private_key_pem.decode('ascii'),
                 '/etc/coast/client-ca.crt': client_ca,
                 '/etc/coast/server-ca.crt': server_ca}
        files.update(registry_files)
        payload = {'hostname': server.hostname, 'fqdn': server.hostname,
                   'preserve_hostname': False,
                   'write_files': [dict(path=path, content=value,
                                        owner='root:root', permissions='0600')
                                   for path, value in files.items()],
                   'fs_setup': [], 'mounts': []}
        if options.admin_ssh_key_name:
            # The base image disables users; let cloud-init install Nova's
            # public key for the image's default user, without passwords.
            payload['users'] = ['default']
        if volumes:
            # Runs on every boot, before fs_setup and mounts.
            payload['bootcmd'] = [volume_link_script(volumes)]
        for volume in volumes:
            device = disk_alias(volume.cinder_volume_id)
            # partition 'auto' keeps an ext4 with this label. 'none' makes
            # cloud-init format the raw disk on every new instance, so a
            # failover replacement wiped the moved volumes.
            payload['fs_setup'].append(dict(
                label='coast-' + volume.purpose, filesystem='ext4',
                device=device, partition='auto', overwrite=False))
            payload['mounts'].append([
                device, volume.mount_path, 'ext4', 'defaults', '0', '2'])
        data = '#cloud-config\n' + json.dumps(payload)
        return (base64.b64encode(data.encode()).decode('ascii'), serial,
                cert.not_valid_after)

    def boot(self, server, ports, volumes, user_data, attempt=None):
        proxy = self.proxy('nova')
        mapping = [dict(uuid=volume.cinder_volume_id, source_type='volume',
                        destination_type='volume', boot_index=-1,
                        delete_on_termination=False) for volume in volumes]
        if mapping:
            mapping.insert(0, dict(
                uuid=server.image_id, source_type='image',
                destination_type='local', boot_index=0,
                delete_on_termination=True))
        extra = {}
        if self.conf.controller_worker.admin_ssh_key_name:
            extra['key_name'] = self.conf.controller_worker.admin_ssh_key_name
        return proxy.create_server(
            name=server.hostname,
            image_id=server.image_id,
            flavor_id=server.flavor_id,
            tags=self.tags + ['server_id=' + server.id,
                              'attempt=' + (attempt or server.id)],
            config_drive=True,
            networks=[{'port': port.id} for port in ports],
            block_device_mapping=mapping, user_data=user_data, **extra)

    def wait_server(self, identifier):
        proxy = self.proxy('nova')
        proxy.wait_for_server(proxy.get_server(identifier), status='ACTIVE',
                              failures=['ERROR'],
                              interval=self.conf.controller_worker.
                              build_poll_interval,
                              wait=self.conf.controller_worker.build_timeout)

    def delete_server(self, server):
        proxy = self.proxy('nova')
        name = server.hostname
        owned = set(self.tags + ['server_id=' + server.id])
        found = ([item for item in proxy.servers(name=name)
                  if item.name == name and owned.issubset(item.tags or [])]
                 if name is not None else [])
        # Both old and replacement guests may share the saved hostname.
        if server.compute_id and server.role in ('control', 'worker', 'build'):
            try:
                guest = proxy.get_server(server.compute_id)
            except exceptions.ResourceNotFound:
                guest = None
            if guest is not None:
                tags = set(guest.tags or [])
                if not owned.issubset(tags):
                    raise ValueError('Guest ownership differs')
                if not any(item.id == guest.id for item in found):
                    found.append(guest)
        for item in found:
            proxy.delete_server(item, ignore_missing=True)
            proxy.wait_for_delete(item, interval=self.conf.controller_worker.
                                  build_poll_interval,
                                  wait=self.conf.controller_worker.
                                  build_timeout)
