#!/usr/bin/env python3
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

"""Check generated guest units and enablement without systemd or a chroot."""

import configparser
import json
import os
from pathlib import Path
import subprocess
import tempfile


ELEMENTS = Path(__file__).resolve().parents[1] / 'elements'


def run(script, root):
    # These fragments only write files, set modes and enable units.
    for prefix in ('/etc/', '/usr/local/', '/usr/lib/', '/lib/'):
        script = script.replace(prefix, str(root) + prefix)
    subprocess.run(['bash', '-eu', '-c', script], check=True)


def install_targets(path, targets):
    # systemd list options append; an empty assignment resets the list.
    section = ''
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith('['):
            section = line
        if section == '[Install]' and line.startswith('WantedBy='):
            values = set(line.split('=', 1)[1].split())
            targets = targets | values if values else set()
    return targets


# Ubuntu basic.target is After=sockets.target, multi-user.target is
# After=basic.target, and cloud-final.service is After=multi-user.target.
# A socket with default dependencies is Before=sockets.target
# (systemd.socket(5)). That edge cycles with a late After=.
DISTRO_BEFORE = (
    ('sockets.target', 'basic.target'),
    ('basic.target', 'multi-user.target'),
    ('multi-user.target', 'cloud-final.service'),
)


def parser_for(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path)
    return parser


def list_values(parser, option):
    if not parser.has_section('Unit') or not parser.has_option('Unit', option):
        return None
    return set(parser.get('Unit', option).split())


def merge_list(store, unit, values):
    # An empty assignment resets the list. Later values append.
    if values is None:
        return
    if not values:
        store[unit] = set()
        return
    store.setdefault(unit, set()).update(values)


def absorb(path, unit, after, before, requires, defaults):
    parser = parser_for(path)
    merge_list(after, unit, list_values(parser, 'After'))
    merge_list(before, unit, list_values(parser, 'Before'))
    merge_list(requires, unit, list_values(parser, 'Requires'))
    if (parser.has_section('Unit') and
            parser.has_option('Unit', 'DefaultDependencies')):
        raw = parser.get('Unit', 'DefaultDependencies').strip().lower()
        defaults[unit] = raw not in ('no', 'false', '0', 'off')


def find_cycle(graph):
    visiting = set()
    visited = set()
    stack = []

    def walk(node):
        if node in visiting:
            return stack[stack.index(node):] + [node]
        if node in visited:
            return None
        visiting.add(node)
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            found = walk(nxt)
            if found:
                return found
        stack.pop()
        visiting.discard(node)
        visited.add(node)
        return None

    for node in sorted(graph):
        found = walk(node)
        if found:
            return found
    return None


def ordering_graph(after, before, defaults):
    graph = {}

    def edge(earlier, later):
        if earlier and later:
            graph.setdefault(earlier, set()).add(later)
            graph.setdefault(later, set())

    for unit, deps in after.items():
        for dep in deps:
            edge(dep, unit)
    for unit, deps in before.items():
        for dep in deps:
            edge(unit, dep)
    for earlier, later in DISTRO_BEFORE:
        edge(earlier, later)
    known = set(after) | set(before) | set(defaults)
    for unit in known:
        # Unset DefaultDependencies means yes for a socket unit.
        if unit.endswith('.socket') and defaults.get(unit, True):
            edge(unit, 'sockets.target')
    return graph


def check(root):
    system = root / 'etc/systemd/system'
    after = {}
    before = {}
    requires = {}
    defaults = {}
    wanted = {}
    units = []
    for directory in (root / 'lib/systemd/system', system):
        units.extend(directory.glob('*.service'))
        units.extend(directory.glob('*.socket'))
    for unit in units:
        absorb(unit, unit.name, after, before, requires, defaults)
        wanted[unit.name] = install_targets(unit, set())
    # Include drop-ins: agent -> firstboot, Docker -> mounts, socket order.
    for pattern in ('*.service.d/*.conf', '*.socket.d/*.conf'):
        for dropin in system.glob(pattern):
            unit = dropin.parent.name[:-2]
            absorb(dropin, unit, after, before, requires, defaults)
            wanted[unit] = install_targets(dropin, wanted.get(unit, set()))
    delayed = {'cloud-final.service'}
    while True:
        more = {unit for unit, deps in after.items() if deps & delayed}
        if more <= delayed:
            break
        delayed.update(more)
    assert {'docker.service', 'coast-guest-routes.service',
            'coast-agent.service', 'coast-volume-mounts.service'} <= delayed
    assert 'coast-volume-mounts.service' in after['docker.service']
    # Vendor docker.service Requires=docker.socket. A mask fails the start
    # before ExecStart, so the controller agent never listens.
    assert 'docker.socket' in requires['docker.service']
    socket = system / 'docker.socket'
    masked = socket.is_symlink() and os.readlink(socket) == '/dev/null'
    assert not (masked and 'docker.socket' in requires['docker.service'])
    assert 'coast-volume-mounts.service' in requires['docker.socket']
    assert 'coast-volume-mounts.service' in after['docker.socket']
    assert defaults.get('docker.socket') is False
    cycle = find_cycle(ordering_graph(after, before, defaults))
    if cycle:
        raise AssertionError('ordering cycle: ' + ' -> '.join(cycle))
    socket_wants = system / 'sockets.target.wants' / 'docker.socket'
    assert not socket_wants.exists() and not socket_wants.is_symlink()
    for unit in delayed - {'cloud-final.service'}:
        if not unit.endswith('.service'):
            continue
        assert wanted[unit] == {'cloud-init.target'}, unit
        assert (system / 'cloud-init.target.wants' / unit).is_symlink(), unit
        old = system / 'multi-user.target.wants' / unit
        assert not old.exists() and not old.is_symlink(), unit
    assert 'network-online.target' in after['coast-guest-routes.service']
    assert 'coast-guest-routes.service' in after['coast-agent.service']
    assert not (system / 'coast-mgmt-netns.service').exists()
    assert not (system / 'multi-user.target.wants/'
                'coast-mgmt-netns.service').is_symlink()
    assert (system / 'multi-user.target.wants/chrony.service').is_symlink()
    assert (system / 'multi-user.target.wants/containerd.service').is_symlink()


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        system = root / 'etc/systemd/system'
        old = system / 'multi-user.target.wants'
        old.mkdir(parents=True)
        vendor = root / 'lib/systemd/system'
        vendor.mkdir(parents=True)
        for unit in ('docker', 'containerd', 'chrony'):
            path = vendor / (unit + '.service')
            if unit == 'docker':
                path.write_text(
                    '[Unit]\n'
                    'After=docker.socket network-online.target\n'
                    'Requires=docker.socket\n'
                    '[Service]\n'
                    'ExecStart=/usr/bin/dockerd -H fd://\n'
                    '[Install]\n'
                    'WantedBy=multi-user.target\n')
            else:
                path.write_text(
                    '[Unit]\n[Install]\nWantedBy=multi-user.target\n')
            (old / path.name).symlink_to(path)
        socket = vendor / 'docker.socket'
        socket.write_text(
            '[Unit]\nDescription=Docker Socket for the API\n'
            '[Socket]\nListenStream=/run/docker.sock\n'
            '[Install]\nWantedBy=sockets.target\n')
        socket_wants = system / 'sockets.target.wants'
        socket_wants.mkdir(parents=True)
        (socket_wants / 'docker.socket').symlink_to(socket)
        for unit in ('coast-mgmt-netns', 'coast-guest-routes', 'coast-agent',
                     'coast-coolify-firstboot'):
            (old / (unit + '.service')).symlink_to('/missing')
        base = (ELEMENTS / 'coast-base/install.d/75-coast-base').read_text()
        (system / 'docker.service.d').mkdir()
        start = base.index('cat > /etc/systemd/system/docker.service.d/')
        dropin = base[start:]
        run(dropin, root)
        agent = (ELEMENTS / 'coast-agent/install.d/76-coast-agent').read_text()
        run(agent[agent.index('mkdir -p /usr/local/sbin'):], root)
        check(root)  # Worker/build image.
        daemon = json.loads((root / 'etc/docker/daemon.json').read_text())
        assert daemon['features']['containerd-snapshotter'] is False
        assert daemon['storage-driver'] == 'overlay2'
        needle = ("printf '%s\\n' /data /var/lib/docker "
                  "> /etc/coast/volume-mounts")
        assert needle in base
        controller = (ELEMENTS /
                      'coast-controller/install.d/77-coast-controller')
        script = controller.read_text()
        script = script[script.index('cat > /etc/systemd/system/'):]
        run(script[:script.index("printf '%s\\n' controller")], root)
        check(root)  # Controller image, including the agent drop-in.
        needle = ("printf '%s\\n' /var/lib/registry "
                  ">> /etc/coast/volume-mounts")
        assert needle in script
        firstboot = (system / 'coast-coolify-firstboot.service').read_text()
        assert 'coast-volume-mounts.service' in firstboot
    print('Guest unit ordering checks passed (worker and controller)')


if __name__ == '__main__':
    main()
