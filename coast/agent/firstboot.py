#!/usr/bin/python3
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

"""Offline guest setup for the pinned Coolify image (Q78 and Q81)."""

import base64
import configparser
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import uuid

from coast.common import engine_versions


VERSION = engine_versions.build_version()
STAGE = Path('/usr/lib/coast/coolify')
DATA = Path('/data/coolify')
SOURCE = DATA / 'source'
SSH_HOME = Path('/root/.ssh')
IMAGES = engine_versions.images(VERSION)
REGISTRY_NAME = 'coast-registry'
REGISTRY_MOUNT = '/var/lib/registry'
REGISTRY_DIR = Path('/etc/coast/registry')


class FirstBootError(Exception):
    """Safe failure with no child process output or secrets."""


def run(*args):
    result = subprocess.run(args, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=600,
                            check=False)
    if result.returncode:
        raise FirstBootError('Guest setup command failed')
    return result.stdout


def require_version(version):
    if version != VERSION:
        raise FirstBootError('Unsupported engine version')


def verify_stage(stage):
    names = set()
    for line in (stage / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split()
        if Path(name).name != name or name in names:
            raise FirstBootError('Invalid artifact name')
        names.add(name)
        checksum = hashlib.sha256()
        with (stage / name).open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(block)
        if checksum.hexdigest() != digest:
            raise FirstBootError('Image artifact checksum mismatch')
    if names != {'docker-compose.yml', 'docker-compose.prod.yml',
                 '.env.production', 'upgrade.sh', 'upgrade-postgres.sh',
                 'versions.json', 'images.lock', 'images.tar'}:
        raise FirstBootError('Image artifact manifest is incomplete')
    entries = [line.split() for line in
               (stage / 'images.lock').read_text().splitlines()]
    if (len(entries) != len(IMAGES) or
            {entry[0] for entry in entries} != IMAGES or
            any(len(entry) != 2 or not entry[1].startswith('sha256:') or
                len(entry[1]) != 71 for entry in entries)):
        raise FirstBootError('Invalid image lock')


def environment(template, stack_id):
    try:
        stack_id = str(uuid.UUID(stack_id))
    except (ValueError, TypeError, AttributeError):
        raise FirstBootError('Guest stack UUID is required')
    values = dict(line.split('=', 1) for line in template.splitlines()
                  if line and not line.startswith('#'))
    for key in ('APP_ID', 'DB_PASSWORD', 'REDIS_PASSWORD', 'PUSHER_APP_ID',
                'PUSHER_APP_KEY', 'PUSHER_APP_SECRET'):
        values[key] = secrets.token_hex(32)
    # RootUserSeeder requires mixed case, digits and a symbol. The random
    # suffix supplies entropy; the prefix meets the stock password rules.
    values['ROOT_USER_PASSWORD'] = 'Aa1!' + secrets.token_hex(32)
    values.update(APP_KEY='base64:' + base64.b64encode(
        secrets.token_bytes(32)).decode(), ROOT_USERNAME='Coast',
        ROOT_USER_EMAIL=stack_id + '@coast.local',
        AUTOUPDATE='false', LATEST_IMAGE=VERSION)
    return ''.join('%s=%s\n' % pair for pair in values.items())


def pin_environment(contents, version):
    """Select this image's engine without regenerating retained identity."""
    # The stock production Compose file selects its tag with LATEST_IMAGE:
    # The reviewed docker-compose.prod.yml uses this variable.
    pins = {'AUTOUPDATE': 'false', 'LATEST_IMAGE': version}
    seen = set()
    lines = []
    for line in contents.splitlines():
        key = line.partition('=')[0].strip().removeprefix('export ').strip()
        if key in pins:
            if key in seen:
                continue
            line = key + '=' + pins[key]
            seen.add(key)
        lines.append(line)
    lines.extend(key + '=' + value for key, value in pins.items()
                 if key not in seen)
    return '\n'.join(lines) + '\n'


def private_write(path, value):
    # Atomic replacement prevents a retry from using a truncated secret file.
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, 9999, 0)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _terminal_port(item):
    """Return True when a compose port publishes or targets 6002."""
    if isinstance(item, str):
        return '6002' in item.split('/', 1)[0].split(':')
    if isinstance(item, dict):
        published = str(item.get('published', ''))
        target = item.get('target')
        return published == '6002' or target in (6002, '6002')
    return False


def _drop_terminal_ports(service):
    ports = service.get('ports')
    if not ports:
        return
    service['ports'] = [item for item in ports if not _terminal_port(item)]


def loopback_compose(configuration):
    """Bind the engine to loopback; pin images; disable autostart."""
    engine = configuration['services']['coolify']
    # Replace (do not merge) every upstream published port. Keep the
    # realtime log port. Do not publish the built-in terminal port.
    engine['ports'] = [dict(target=target, published=str(port),
                            host_ip='127.0.0.1', protocol='tcp')
                       for port, target in ((8000, 8080), (6001, 6001))]
    if engine['image'] not in ('docker.io/coollabsio/coolify:' + VERSION,
                               'coollabsio/coolify:' + VERSION):
        raise FirstBootError('Unexpected engine image')
    for service in configuration['services'].values():
        image = service.get('image', '').removeprefix('docker.io/')
        if image not in IMAGES:
            raise FirstBootError('Unexpected support image')
        service['image'] = image
        service['pull_policy'] = 'never'
        # dockerd must never autostart Coolify on boot. Only first boot
        # starts it, after the Cinder mounts and retained state checks.
        service['restart'] = 'no'
        _drop_terminal_ports(service)
    return configuration


def mount_device(path):
    """Return the device id for path. Tests replace this helper."""
    return os.stat(path).st_dev


def _same_device(path, root):
    try:
        return mount_device(path) == mount_device(root)
    except OSError:
        return True


def require_mount(path, message):
    # ismount is true for a bind of the root disk. That disk must not
    # hold the Coolify database or .env.
    if not os.path.ismount(path) or _same_device(path, '/'):
        raise FirstBootError(message)


def retained_engine_without_env():
    """True when moved engine directories have no private .env yet."""
    names = ('ssh', 'applications', 'databases', 'services')
    return (not (SOURCE / '.env').exists() and
            any((DATA / name).exists() for name in names))


def install_engine(version):
    require_version(version)
    for path in ('/data', '/var/lib/docker'):
        require_mount(path, 'Cinder mounts are required')
    # Check before mkdir. A fresh disk has none of these directories.
    if retained_engine_without_env():
        raise FirstBootError('Existing engine state has no environment file')
    verify_stage(STAGE)
    run('docker', 'load', '-i', str(STAGE / 'images.tar'))
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(DATA, 9999, 0)
    for directory in ('source', 'ssh', 'ssh/keys', 'ssh/mux', 'applications',
                      'databases', 'services', 'backups', 'images'):
        path = DATA / directory
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        os.chown(path, 9999, 0)
    env = SOURCE / '.env'
    if not env.exists():
        template = (STAGE / '.env.production').read_text()
        config = configparser.ConfigParser()
        config.read('/etc/coast/coast.conf')
        stack_id = config.get('agent', 'stack_id', fallback='')
        private_write(env, environment(template, stack_id))
    retained = env.read_text()
    pinned = pin_environment(retained, version)
    if pinned != retained:
        private_write(env, pinned)
    # Never regenerate secrets on replacement or retry.
    for name in ('docker-compose.yml', 'docker-compose.prod.yml',
                 '.env.production', 'upgrade.sh', 'upgrade-postgres.sh'):
        private_write(SOURCE / name, (STAGE / name).read_text())
    key = DATA / 'ssh/keys/id.root@host.docker.internal'
    if not key.exists():
        run('ssh-keygen', '-t', 'ed25519', '-f', str(key), '-N', '',
            '-C', 'coolify')
        os.chown(key, 9999, 0)
    authorized = SSH_HOME / 'authorized_keys'
    authorized.parent.mkdir(mode=0o700, exist_ok=True)
    public = key.with_suffix(key.suffix + '.pub').read_text()
    existing = authorized.read_text() if authorized.exists() else ''
    if public.strip() not in existing.splitlines():
        with authorized.open('a') as stream:
            stream.write('\n' + public)
        authorized.chmod(0o600)
    # Do not run upgrade.sh: it fetches CDN files and pulls images.
    if not run('docker', 'network', 'ls', '--filter', 'name=^coolify$',
               '--format', '{{.Name}}').strip():
        run('docker', 'network', 'create', '--attachable', 'coolify')
    config = json.loads(run('docker', 'compose', '--env-file', str(env),
                            '-f', str(SOURCE / 'docker-compose.yml'),
                            '-f', str(SOURCE / 'docker-compose.prod.yml'),
                            'config', '--format', 'json'))
    compose = SOURCE / 'coast-compose.json'
    private_write(compose, json.dumps(loopback_compose(config)))
    run('docker', 'compose', '-p', 'coolify', '-f', str(compose),
        'up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '300')
    image = run('docker', 'inspect', '--format', '{{.Config.Image}}',
                'coolify').decode().strip()
    if image not in ('coollabsio/coolify:' + VERSION,
                     'docker.io/coollabsio/coolify:' + VERSION):
        raise FirstBootError('Unsupported installed engine')
    start_registry()


def pinned_registry_image(stage):
    for line in (stage / 'images.lock').read_text().splitlines():
        if not line.strip():
            continue
        name, digest = line.split()
        if name != 'registry:3':
            continue
        if not digest.startswith('sha256:') or len(digest) != 71:
            raise FirstBootError('Invalid image lock')
        return name + '@' + digest
    raise FirstBootError('Registry image is absent')


def local_image(image):
    """Return the tag loaded from images.tar for a pinned reference."""
    return image.partition('@')[0]


def registry_address(address, port):
    try:
        ip = ipaddress.IPv4Address(address)
        port = int(port)
    except (TypeError, ValueError):
        raise FirstBootError('Registry address is invalid') from None
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise FirstBootError('Registry address is invalid')
    if port < 1 or port > 65535:
        raise FirstBootError('Registry port is invalid')
    return str(ip), port


def _registry_bindings(text, address, port):
    try:
        bindings = json.loads(text)
    except (TypeError, ValueError):
        return False
    ports = bindings.get('5000/tcp') if isinstance(bindings, dict) else None
    if not isinstance(ports, list) or len(ports) != 1:
        return False
    entry = ports[0]
    return (isinstance(entry, dict) and entry.get('HostIp') == address and
            entry.get('HostPort') == str(port))


def start_registry():
    """Start the pinned registry on the environment address only.

    The controller coast.conf [registry] section is required. Publish
    does not use 0.0.0.0 or 127.0.0.1. A retry starts the same container.
    """
    require_mount(REGISTRY_MOUNT, 'Registry volume is required')
    config = configparser.ConfigParser()
    config.read('/etc/coast/coast.conf')
    if config.get('agent', 'role', fallback='') != 'controller':
        raise FirstBootError('Registry configuration is required')
    address, port = registry_address(
        config.get('registry', 'address', fallback=''),
        config.get('registry', 'port', fallback=''))
    image = config.get('registry', 'image', fallback='')
    if image != pinned_registry_image(STAGE):
        raise FirstBootError('Registry image is not pinned')
    files = [REGISTRY_DIR / name for name in (
        'config.yml', 'htpasswd', 'tls.crt', 'tls.key')]
    if any(not path.is_file() for path in files):
        raise FirstBootError('Registry files are required')
    try:
        current = run(
            'docker', 'inspect', '--format',
            '{{.Config.Image}}|{{.HostConfig.RestartPolicy.Name}}|'
            '{{json .HostConfig.PortBindings}}',
            REGISTRY_NAME).decode().strip()
    except FirstBootError:
        current = None
    if current is None:
        # overlay2 (classic store) drops repo digests on docker load, so
        # name@digest does not resolve. images.tar is already checksummed
        # against SHA256SUMS and --pull never stops any network fetch.
        local = local_image(image)
        run('docker', 'run', '-d', '--name', REGISTRY_NAME, '--restart',
            'always', '--pull', 'never', '--publish',
            '%s:%s:5000' % (address, port),
            '--volume', REGISTRY_MOUNT + ':' + REGISTRY_MOUNT,
            '--volume', str(files[0]) + ':/etc/distribution/config.yml:ro',
            '--volume', str(files[1]) + ':/auth/htpasswd:ro',
            '--volume', str(files[2]) + ':/certs/tls.crt:ro',
            '--volume', str(files[3]) + ':/certs/tls.key:ro',
            local)
        return
    image_name, restart, bindings = current.split('|', 2)
    if (image_name not in (image, local_image(image)) or restart != 'always' or
            not _registry_bindings(bindings, address, port)):
        raise FirstBootError('Registry container configuration differs')
    run('docker', 'start', REGISTRY_NAME)


def management_network(data, address):
    ipaddress.IPv4Address(address)
    networks = [item for item in data['networks']
                if item.get('ip_address') == address and
                item.get('type') == 'ipv4']
    if len(networks) != 1:
        raise FirstBootError('Explicit static management network is required')
    network = networks[0]
    links = [item for item in data['links'] if item['id'] == network['link']]
    if len(links) != 1:
        raise FirstBootError('Management NIC is ambiguous')
    prefix = ipaddress.IPv4Network('0.0.0.0/' + network['netmask']).prefixlen
    mac = links[0]['ethernet_mac_address'].lower()
    return mac, address + '/' + str(prefix)


def guest_network(data, address, destinations):
    """Build the two-NIC Netplan contract from Nova link MAC addresses."""
    mac, cidr = management_network(data, address)
    subnet = ipaddress.IPv4Interface(cidr).network
    network = next(item for item in data['networks']
                   if item.get('ip_address') == address and
                   item.get('type') == 'ipv4')
    # These are default-route fields, not wildcard socket bindings.
    gateways = {route['gateway'] for route in network.get('routes', [])
                if route.get('network') == '0.0.0.0' and  # nosec B104
                route.get('netmask') == '0.0.0.0'}  # nosec B104
    if network.get('gateway'):
        gateways.add(network['gateway'])
    routes = []
    for destination in sorted(set(destinations)):
        target = ipaddress.IPv4Address(destination)
        if target in subnet:
            continue
        if len(gateways) != 1:
            raise FirstBootError('Management route gateway is required')
        gateway = ipaddress.IPv4Address(next(iter(gateways)))
        if gateway not in subnet:
            raise FirstBootError('Management gateway must be on-link')
        routes.append({'to': str(target) + '/32', 'via': str(gateway)})
    # Nova link types depend on the Neutron backend (bridge, ovs, vif).
    links = [item for item in data['links']
             if item.get('ethernet_mac_address')]
    macs = {item['ethernet_mac_address'].lower() for item in links}
    if len(links) != 2 or len(macs) != 2:
        raise FirstBootError('Exactly two guest NICs are required')
    ethernets = {}
    for index, link in enumerate(links):
        link_mac = link['ethernet_mac_address'].lower()
        values = {'match': {'macaddress': link_mac}, 'dhcp6': False,
                  'accept-ra': False}
        if link.get('mtu'):
            values['mtu'] = link['mtu']
        if link_mac == mac:
            values.update(dhcp4=False, addresses=[cidr], routes=routes)
        else:
            values['dhcp4'] = True
            values['optional'] = True
        ethernets['coast-nic-' + str(index)] = values
    if mac not in {link['ethernet_mac_address'].lower() for link in links}:
        raise FirstBootError('Management NIC is absent')
    return {'network': {'version': 2, 'renderer': 'networkd',
                        'ethernets': ethernets}}


def setup_routes():
    config = configparser.ConfigParser()
    config.read('/etc/coast/coast.conf')
    address = config['agent']['bind_ip']
    with tempfile.TemporaryDirectory() as mount:
        run('mount', '-o', 'ro', '/dev/disk/by-label/config-2', mount)
        try:
            network_file = Path(mount) / 'openstack/latest/network_data.json'
            data = json.loads(network_file.read_text())
        finally:
            run('umount', mount)
    destinations = [value.strip() for value in config.get(
        'agent', 'health_manager_ips', fallback='').split(',')
        if value.strip()]
    configuration = guest_network(data, address, destinations)
    # JSON is valid YAML. Replace cloud-init's guest-owned network file,
    # rather than merge its management default gateway into an override.
    path = Path('/etc/netplan/50-cloud-init.yaml')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(configuration))
    temporary.chmod(0o600)
    temporary.replace(path)
    Path('/etc/cloud/cloud.cfg.d/99-coast-network.cfg').write_text(
        'network: {config: disabled}\n')
    run('netplan', 'generate')
    run('netplan', 'apply')
    # Remove any stale default installed before the persistent replacement.
    mac, unused = management_network(data, address)
    links = json.loads(run('ip', '-j', 'link'))
    names = [item['ifname'] for item in links if item.get('address') == mac]
    if len(names) != 1:
        raise FirstBootError('Management NIC is absent or ambiguous')
    run('ip', '-4', 'route', 'flush', 'default', 'dev', names[0])


def main():
    os.umask(0o077)
    try:
        if sys.argv[1:] == ['network']:
            setup_routes()
        elif sys.argv[1:] == ['engine']:
            install_engine(VERSION)
        elif len(sys.argv) == 3 and sys.argv[1] == 'engine':
            install_engine(sys.argv[2])
        else:
            raise FirstBootError('Invalid first-boot command')
    except Exception:
        # Engine errors, config-drive values and environment never reach logs.
        sys.exit('Coast first-boot setup failed; operator recovery required')


if __name__ == '__main__':
    main()
