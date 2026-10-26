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

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from coast.agent import firstboot
from coast.common import engine_versions
from coast.tests import base


STACK_ID = '11111111-1111-4111-8111-111111111111'
ROOT = Path(__file__).resolve().parents[3]
SUPPORT_DATABASE_IMAGE = (
    engine_versions.RELEASES[engine_versions.DEFAULT][2][0])


class FirstBootTest(base.TestCase):
    def test_unique_secrets_and_disabled_updates(self):
        first = firstboot.environment('DB_USERNAME=coolify\nAPP_KEY=\n',
                                      STACK_ID)
        second = firstboot.environment('DB_USERNAME=coolify\nAPP_KEY=\n',
                                       STACK_ID)
        self.assertNotEqual(first, second)
        self.assertIn('AUTOUPDATE=false\n', first)
        self.assertIn('LATEST_IMAGE=4.4.7\n', first)
        self.assertIn('DB_USERNAME=coolify\n', first)
        self.assertIn('APP_KEY=base64:', first)
        values = dict(line.split('=', 1) for line in first.splitlines())
        repeated = dict(line.split('=', 1) for line in second.splitlines())
        password = values['ROOT_USER_PASSWORD']
        self.assertTrue(any(char.isupper() for char in password))
        self.assertTrue(any(char.islower() for char in password))
        self.assertTrue(any(char.isdigit() for char in password))
        self.assertIn('!', password)
        self.assertGreaterEqual(len(password), 64)
        self.assertNotEqual(password, repeated['ROOT_USER_PASSWORD'])
        self.assertEqual('Coast', values['ROOT_USERNAME'])
        self.assertEqual(STACK_ID + '@coast.local', values['ROOT_USER_EMAIL'])

    def test_replacement_pins_new_engine_and_preserves_identity(self):
        retained = ('# retained controller identity\n'
                    'APP_KEY=base64:retained-key==\n'
                    'DB_PASSWORD=retained-db-password\n'
                    'ROOT_USER_EMAIL=owner@example.com\n'
                    'ROOT_USER_PASSWORD=retained-root-password\n'
                    'AUTOUPDATE=true\nLATEST_IMAGE=4.4.3\n')
        upgraded = firstboot.pin_environment(retained, '4.4.7')
        self.assertEqual(retained.splitlines()[:5], upgraded.splitlines()[:5])
        values = dict(line.split('=', 1) for line in upgraded.splitlines()
                      if not line.startswith('#'))
        self.assertEqual('4.4.7', values['LATEST_IMAGE'])
        self.assertEqual('false', values['AUTOUPDATE'])
        self.assertEqual('base64:retained-key==', values['APP_KEY'])
        self.assertEqual(upgraded,
                         firstboot.pin_environment(upgraded, '4.4.7'))

    def test_replacement_removes_duplicate_pins_and_supplies_missing_pin(self):
        retained = ('LATEST_IMAGE=4.4.3\nexport LATEST_IMAGE=latest\n'
                    'APP_KEY=retained\n')
        upgraded = firstboot.pin_environment(retained, '4.4.7')
        self.assertEqual(['LATEST_IMAGE=4.4.7', 'APP_KEY=retained',
                          'AUTOUPDATE=false'], upgraded.splitlines())

    def test_email_requires_guest_stack_uuid(self):
        for stack_id in ('', None, 'not-a-uuid', 'injected\nvalue'):
            self.assertRaises(firstboot.FirstBootError,
                              firstboot.environment, '', stack_id)
        other = '22222222-2222-4222-8222-222222222222'
        self.assertIn('ROOT_USER_EMAIL=' + other + '@coast.local\n',
                      firstboot.environment('', other))

    def test_version_gate_precedes_every_command(self):
        for baked in engine_versions.SUPPORTED_ENGINE_VERSIONS:
            other = next(version for version in
                         engine_versions.SUPPORTED_ENGINE_VERSIONS
                         if version != baked)
            for requested in (other, '4.4.9'):
                with self.subTest(baked=baked, requested=requested), \
                        mock.patch.object(firstboot, 'VERSION', baked), \
                        mock.patch.object(firstboot, 'require_mount',
                                          return_value=None) as mount, \
                        mock.patch.object(firstboot,
                                          'retained_engine_without_env',
                                          return_value=False) as retained, \
                        mock.patch.object(firstboot, 'verify_stage',
                                          return_value=None) as stage, \
                        mock.patch.object(firstboot, 'run', side_effect=(
                            AssertionError('Setup command reached'))) as run:
                    error = self.assertRaises(firstboot.FirstBootError,
                                              firstboot.install_engine,
                                              requested)
                    self.assertEqual('Unsupported engine version', str(error))
                    mount.assert_not_called()
                    retained.assert_not_called()
                    stage.assert_not_called()
                    run.assert_not_called()

    def test_compose_replaces_all_public_bindings(self):
        config = {'services': {
            'coolify': {'image': 'docker.io/coollabsio/coolify:4.4.7',
                        'ports': [{'host_ip': '0.0.0.0'}]},
            'postgres': {'image': SUPPORT_DATABASE_IMAGE,
                         'ports': ['5432:5432', '127.0.0.1:6002:6002']}}}
        result = firstboot.loopback_compose(config)
        ports = result['services']['coolify']['ports']
        self.assertEqual(
            [{'target': 8080, 'published': '8000', 'host_ip': '127.0.0.1',
              'protocol': 'tcp'},
             {'target': 6001, 'published': '6001', 'host_ip': '127.0.0.1',
              'protocol': 'tcp'}], ports)
        self.assertEqual(['5432:5432'],
                         result['services']['postgres']['ports'])
        self.assertEqual({'never'}, {s['pull_policy'] for s in
                                     result['services'].values()})
        # A replacement VM must not start Coolify before first boot.
        self.assertEqual({'no'}, {s['restart'] for s in
                                  result['services'].values()})
        config['services']['postgres']['image'] = (
            SUPPORT_DATABASE_IMAGE.rsplit(':', 1)[0] + ':latest')
        self.assertRaises(firstboot.FirstBootError,
                          firstboot.loopback_compose, config)

    def test_network_uses_explicit_address_and_mac(self):
        data = {'links': [{'id': 'nic-a',
                           'ethernet_mac_address': 'FA:16:3E:00:00:01'}],
                'networks': [{'type': 'ipv4', 'link': 'nic-a',
                              'ip_address': '192.0.2.2',
                              'netmask': '255.255.255.0'}]}
        self.assertEqual(('fa:16:3e:00:00:01', '192.0.2.2/24'),
                         firstboot.management_network(data, '192.0.2.2'))
        self.assertRaises(firstboot.FirstBootError,
                          firstboot.management_network, data, '192.0.2.3')
        data['networks'].append(data['networks'][0])
        self.assertRaises(firstboot.FirstBootError,
                          firstboot.management_network, data, '192.0.2.2')

    def network_data(self):
        return {'links': [
            {'id': 'nic-a', 'type': 'bridge', 'mtu': 1450,
             'ethernet_mac_address': 'fa:16:3e:00:00:01'},
            {'id': 'nic-b', 'type': 'ovs', 'mtu': 1450,
             'ethernet_mac_address': 'fa:16:3e:00:00:02'}],
            'networks': [{'type': 'ipv4', 'link': 'nic-a',
                          'ip_address': '192.0.2.2',
                          'netmask': '255.255.255.0',
                          'routes': [{'network': '0.0.0.0',
                                      'netmask': '0.0.0.0',
                                      'gateway': '192.0.2.1'}]}]}

    def test_guest_routes_are_hosts_and_env_is_dhcp(self):
        data = self.network_data()
        destinations = ['192.0.2.10', '198.51.100.10', '198.51.100.10']
        result = firstboot.guest_network(
            data, '192.0.2.2', destinations)['network']['ethernets']
        mgmt = result['coast-nic-0']
        self.assertEqual(1450, mgmt['mtu'])
        self.assertEqual(1450, result['coast-nic-1']['mtu'])
        self.assertFalse(mgmt['accept-ra'])
        self.assertFalse(mgmt['dhcp4'])
        self.assertNotIn('optional', mgmt)
        self.assertEqual(['192.0.2.2/24'], mgmt['addresses'])
        self.assertEqual([{'to': '198.51.100.10/32', 'via': '192.0.2.1'}],
                         mgmt['routes'])
        self.assertNotIn('gateway4', mgmt)
        self.assertTrue(result['coast-nic-1']['dhcp4'])
        self.assertTrue(result['coast-nic-1']['optional'])
        self.assertNotIn('routes', result['coast-nic-1'])
        data['links'].reverse()
        reversed_result = firstboot.guest_network(
            data, '192.0.2.2', [])['network']['ethernets']
        self.assertEqual(['192.0.2.2/24'],
                         reversed_result['coast-nic-1']['addresses'])
        self.assertNotIn('optional', reversed_result['coast-nic-1'])
        self.assertTrue(reversed_result['coast-nic-0']['optional'])

    def test_offlink_destination_requires_onlink_gateway(self):
        data = self.network_data()
        data['networks'][0]['routes'] = []
        result = firstboot.guest_network(data, '192.0.2.2', ['192.0.2.10'])
        self.assertEqual([], result['network']['ethernets'][
            'coast-nic-0']['routes'])
        self.assertRaises(firstboot.FirstBootError, firstboot.guest_network,
                          data, '192.0.2.2', ['198.51.100.10'])
        data['networks'][0]['gateway'] = '203.0.113.1'
        self.assertRaises(firstboot.FirstBootError, firstboot.guest_network,
                          data, '192.0.2.2', ['198.51.100.10'])

    def test_conflicting_gateways_and_invalid_destinations_fail(self):
        data = self.network_data()
        data['networks'][0]['gateway'] = '192.0.2.254'
        self.assertRaises(firstboot.FirstBootError, firstboot.guest_network,
                          data, '192.0.2.2', ['198.51.100.10'])
        self.assertRaises(ValueError, firstboot.guest_network,
                          data, '192.0.2.2', ['not-an-address'])

    def test_ambiguous_guest_links_fail_closed(self):
        data = self.network_data()
        data['links'].append(data['links'][0])
        self.assertRaises(firstboot.FirstBootError, firstboot.guest_network,
                          data, '192.0.2.2', [])

    def test_route_setup_persists_and_removes_stale_default(self):
        data = self.network_data()
        config = firstboot.configparser.ConfigParser()
        config.read_string('[agent]\nbind_ip=192.0.2.2\n'
                           'health_manager_ips=198.51.100.10\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            network_file = root / 'openstack/latest/network_data.json'
            network_file.parent.mkdir(parents=True)
            network_file.write_text(json.dumps(data))
            netplan = root / 'netplan.yaml'
            cloud = root / 'cloud.cfg'
            original = Path

            def path(value):
                return {'/etc/netplan/50-cloud-init.yaml': netplan,
                        '/etc/cloud/cloud.cfg.d/99-coast-network.cfg': cloud
                        }.get(str(value), original(value))

            def command(*args):
                if args == ('ip', '-j', 'link'):
                    return json.dumps([{'ifname': 'ens9',
                                        'address': 'fa:16:3e:00:00:01'}])
                return b''

            with mock.patch.object(firstboot, 'Path', side_effect=path), \
                    mock.patch.object(firstboot.configparser, 'ConfigParser',
                                      return_value=config), \
                    mock.patch.object(config, 'read'), \
                    mock.patch.object(firstboot.tempfile, 'TemporaryDirectory',
                                      return_value=mock.MagicMock(
                                          __enter__=mock.Mock(
                                              return_value=directory))), \
                    mock.patch.object(firstboot, 'run',
                                      side_effect=command) as run:
                firstboot.setup_routes()
                saved = netplan.read_text()
                firstboot.setup_routes()
                self.assertEqual(saved, netplan.read_text())
            self.assertEqual(firstboot.guest_network(
                data, '192.0.2.2', ['198.51.100.10']), json.loads(saved))
            self.assertEqual(0o600, netplan.stat().st_mode & 0o777)
            self.assertIn('config: disabled', cloud.read_text())
            self.assertIn(mock.call('ip', '-4', 'route', 'flush',
                                    'default', 'dev', 'ens9'),
                          run.call_args_list)
            self.assertIn(mock.call('netplan', 'apply'), run.call_args_list)

    def test_child_secrets_are_not_in_error(self):
        result = subprocess.CompletedProcess([], 1, b'password', b'password')
        with mock.patch.object(subprocess, 'run', return_value=result):
            error = self.assertRaises(firstboot.FirstBootError,
                                      firstboot.run, 'docker')
        self.assertNotIn('password', str(error))

    def test_artifact_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            entries = ''.join(tag + ' sha256:' + 'a' * 64 + '\n'
                              for tag in firstboot.IMAGES)
            (stage / 'images.lock').write_text(entries)
            digest = hashlib.sha256(entries.encode()).hexdigest()
            manifest = digest + '  images.lock\n'
            for name in ('docker-compose.yml', 'docker-compose.prod.yml',
                         '.env.production', 'upgrade.sh',
                         'upgrade-postgres.sh', 'versions.json', 'images.tar'):
                (stage / name).write_text('artifact')
                digest = hashlib.sha256(b'artifact').hexdigest()
                manifest += digest + '  ' + name + '\n'
            (stage / 'SHA256SUMS').write_text(manifest)
            firstboot.verify_stage(stage)
            (stage / 'images.lock').write_text('corrupt')
            self.assertRaises(firstboot.FirstBootError,
                              firstboot.verify_stage, stage)

    def test_private_atomic_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            with mock.patch.object(os, 'chown'):
                firstboot.private_write(path, 'secret')
            self.assertEqual('secret', path.read_text())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def _separate_devices(self, path):
        return 1 if path == '/' else 2

    def test_install_requires_both_cinder_mounts(self):
        with mock.patch.object(
                os.path, 'ismount', side_effect=[True, False]), \
                mock.patch.object(firstboot, 'mount_device',
                                  side_effect=self._separate_devices):
            self.assertRaises(firstboot.FirstBootError,
                              firstboot.install_engine, '4.4.7')

    def test_install_rejects_a_root_disk_mount(self):
        with mock.patch.object(os.path, 'ismount', return_value=True), \
                mock.patch.object(firstboot, 'mount_device', return_value=1):
            self.assertRaises(firstboot.FirstBootError,
                              firstboot.install_engine, '4.4.7')

    def test_install_refuses_new_env_over_existing_engine_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'data'
            (data / 'ssh').mkdir(parents=True)
            source = data / 'source'
            with mock.patch.multiple(
                    firstboot, DATA=data, SOURCE=source), \
                    mock.patch.object(
                        os.path, 'ismount', return_value=True), \
                    mock.patch.object(
                        firstboot, 'mount_device',
                        side_effect=self._separate_devices), \
                    mock.patch.object(firstboot, 'run') as run, \
                    mock.patch.object(firstboot, 'environment') as mint:
                self.assertRaises(firstboot.FirstBootError,
                                  firstboot.install_engine, '4.4.7')
            run.assert_not_called()
            mint.assert_not_called()
            self.assertFalse((source / '.env').exists())

    def test_install_retry_preserves_secrets_and_private_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'data'
            source = data / 'source'
            stage = root / 'stage'
            stage.mkdir()
            for name in ('docker-compose.yml', 'docker-compose.prod.yml',
                         '.env.production', 'upgrade.sh',
                         'upgrade-postgres.sh'):
                (stage / name).write_text('DB_USERNAME=coolify\n')
            key = data / 'ssh/keys/id.root@host.docker.internal'
            key.parent.mkdir(parents=True)
            key.write_text('private')
            key.with_suffix(key.suffix + '.pub').write_text('public\n')
            registry_dir = root / 'registry'
            registry_dir.mkdir()
            for name in ('config.yml', 'htpasswd', 'tls.crt', 'tls.key'):
                (registry_dir / name).write_text('material')
            digest = 'a' * 64
            image = 'registry:3@sha256:' + digest
            (stage / 'images.lock').write_text(
                'registry:3 sha256:' + digest + '\n')
            initial = firstboot.environment('DB_USERNAME=coolify\n', STACK_ID)
            initial = initial.replace('LATEST_IMAGE=4.4.7',
                                      'LATEST_IMAGE=4.4.2')
            source.mkdir(parents=True)
            (source / '.env').write_text(initial)
            original_values = dict(line.split('=', 1)
                                   for line in initial.splitlines())
            calls = []
            seen = {'registry': 0}

            def command(*args, **kwargs):
                calls.append(args)
                if args[:2] == ('docker', 'exec'):
                    self.fail('First boot must not execute container commands')
                if (args[:2] == ('docker', 'inspect') and
                        args[-1] == 'coast-registry'):
                    seen['registry'] += 1
                    if seen['registry'] == 1:
                        raise firstboot.FirstBootError('absent')
                    bindings = json.dumps({'5000/tcp': [{
                        'HostIp': '192.0.2.2', 'HostPort': '5000'}]})
                    return (image + '|always|' + bindings).encode()
                if 'config' in args:
                    selected = dict(line.split('=', 1) for line in
                                    (source / '.env').read_text().splitlines())
                    self.assertEqual('4.4.7', selected['LATEST_IMAGE'])
                    self.assertEqual(original_values['APP_KEY'],
                                     selected['APP_KEY'])
                    return json.dumps({'services': {
                        'coolify': {'image': 'coollabsio/coolify:4.4.7'},
                        'postgres': {'image': SUPPORT_DATABASE_IMAGE}}}
                    ).encode()
                if args[:2] == ('docker', 'inspect'):
                    return b'coollabsio/coolify:4.4.7'
                return b'coolify'

            config = firstboot.configparser.ConfigParser()
            config.read_string(
                '[agent]\nstack_id=' + STACK_ID + '\nrole=controller\n'
                '[registry]\naddress=192.0.2.2\nport=5000\nimage=' +
                image + '\n')
            with mock.patch.object(firstboot.configparser, 'ConfigParser',
                                   return_value=config), \
                    mock.patch.object(config, 'read'), \
                    mock.patch.multiple(
                        firstboot, DATA=data, SOURCE=source, STAGE=stage,
                        SSH_HOME=root / 'ssh', REGISTRY_DIR=registry_dir), \
                    mock.patch.object(firstboot, 'verify_stage'), \
                    mock.patch.object(firstboot, 'run', side_effect=command), \
                    mock.patch.object(os.path, 'ismount', return_value=True), \
                    mock.patch.object(
                        firstboot, 'mount_device',
                        side_effect=self._separate_devices), \
                    mock.patch.object(os, 'chown') as chown:
                firstboot.install_engine('4.4.7')
                secret = (source / '.env').read_bytes()
                firstboot.install_engine('4.4.7')
                runs = [args for args in calls
                        if args[:3] == ('docker', 'run', '-d')]
                self.assertEqual(1, len(runs))
                self.assertIn('--pull', runs[0])
                self.assertEqual('never', runs[0][runs[0].index('--pull') + 1])
                self.assertIn('192.0.2.2:5000:5000', runs[0])
                # Classic overlay2 store has no repo digest after load.
                self.assertIn('registry:3', runs[0])
                self.assertNotIn(image, runs[0])
                published = ' '.join(runs[0])
                self.assertNotIn('0.0.0.0', published)
                self.assertNotIn('127.0.0.1', published)
                self.assertEqual(
                    [('docker', 'start', 'coast-registry')],
                    [args for args in calls
                     if args[:2] == ('docker', 'start')])
                self.assertFalse(any(args[:2] == ('docker', 'exec')
                                     for args in calls))
                self.assertEqual(2, chown.call_args_list.count(
                    mock.call(data / 'ssh', 9999, 0)))
                for path in (data / 'ssh', key.parent, data / 'ssh/mux'):
                    self.assertEqual(0o700, path.stat().st_mode & 0o777)
            self.assertEqual(secret, (source / '.env').read_bytes())
            values = dict(line.split('=', 1) for line in
                          secret.decode().splitlines())
            self.assertEqual('false', values['AUTOUPDATE'])
            self.assertEqual('4.4.7', values['LATEST_IMAGE'])
            for key, value in original_values.items():
                if key not in ('LATEST_IMAGE', 'AUTOUPDATE'):
                    self.assertEqual(value, values[key])
            self.assertEqual(STACK_ID + '@coast.local',
                             values['ROOT_USER_EMAIL'])
            self.assertTrue(bool(values['ROOT_USER_PASSWORD']))
            self.assertEqual(0o600, (source / '.env').stat().st_mode & 0o777)
            compose = source / 'coast-compose.json'
            self.assertEqual(0o600, compose.stat().st_mode & 0o777)
            config = json.loads(compose.read_text())
            self.assertEqual({'127.0.0.1'}, {port['host_ip'] for port in
                             config['services']['coolify']['ports']})
            self.assertEqual(1, (root / 'ssh/authorized_keys').read_text().
                             splitlines().count('public'))

    def test_element_offline_dry_run_uses_baked_release(self):
        for version in engine_versions.SUPPORTED_ENGINE_VERSIONS:
            with self.subTest(version=version), mock.patch.multiple(
                    firstboot, VERSION=version,
                    IMAGES=engine_versions.images(version)):
                self.test_element_offline_dry_run()

    def test_element_offline_dry_run(self):
        script = ROOT / ('tools/build-images/elements/coast-controller/'
                         'root.d/21-stage-offline')
        version = firstboot.VERSION
        manifest = engine_versions.manifest(version)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            lines = []
            # Substitute the manifest through a copied element, not the tree.
            element = root / 'tools/build-images/elements/coast-controller'
            (element / 'root.d').mkdir(parents=True)
            (element / 'files').mkdir()
            staged_script = element / 'root.d/21-stage-offline'
            staged_script.write_text(script.read_text())
            for line in manifest.splitlines():
                name = line.split()[1]
                (cache / name).write_text('support file')
                (cache / name).chmod(0o600)
                digest = hashlib.sha256(b'support file').hexdigest()
                lines.append(digest + '  ' + name)
            (element / 'files/SHA256SUMS').write_text('\n'.join(lines) + '\n')
            package = root / 'coast/common'
            package.mkdir(parents=True)
            (package.parent / '__init__.py').touch()
            (package / '__init__.py').touch()
            registry = (ROOT / 'coast/common/engine_versions.py').read_text()
            registry = registry.replace(
                engine_versions.RELEASES[version][4], digest)
            for checksum in engine_versions.ARTIFACTS.values():
                registry = registry.replace(checksum, digest)
            (package / 'engine_versions.py').write_text(registry)
            default_lock = script.parents[1] / 'files/coolify-images.lock'
            (element / 'files/coolify-images.lock').write_bytes(
                default_lock.read_bytes())
            lock = root / 'images.lock'
            lock.write_text(''.join(tag + ' sha256:' + 'a' * 64 + '\n'
                                    for tag in sorted(firstboot.IMAGES)))
            lock.chmod(0o600)
            binary = root / 'bin'
            binary.mkdir()
            docker = binary / 'docker'
            docker.write_text('#!/bin/bash\nset -eu\n'
                              '[ "$1" != pull ] || exit 99\n'
                              'if [ "$1" = save ]; then '
                              'printf archive; fi\n')
            docker.chmod(0o755)
            sudo = binary / 'sudo'
            sudo.write_text('#!/bin/bash\nexec "$@"\n')
            sudo.chmod(0o755)
            env = dict(os.environ, TARGET_ROOT=str(root / 'target'),
                       COAST_COOLIFY_VERSION=version, COAST_OFFLINE='1',
                       COAST_COOLIFY_CACHE=str(cache),
                       PATH=str(binary) + ':' + os.environ['PATH'])
            env.pop('COAST_COOLIFY_IMAGES_LOCK', None)
            staged = root / 'target/usr/lib/coast/coolify'
            for selected in (lock,):
                if selected is not None:
                    env['COAST_COOLIFY_IMAGES_LOCK'] = str(selected)
                result = subprocess.run(['bash', str(staged_script)], env=env,
                                        capture_output=True, check=False,
                                        cwd=root)
                self.assertEqual(0, result.returncode, result.stderr)
                firstboot.verify_stage(staged)
                # Root-owned staged files must also be readable by the
                # unprivileged builder, regardless of source permissions.
                for artifact in staged.iterdir():
                    self.assertEqual(0o644, artifact.stat().st_mode & 0o777,
                                     str(artifact))
                expected = selected if selected is not None else default_lock
                self.assertEqual(expected.read_bytes(),
                                 (staged / 'images.lock').read_bytes())
            valid_lock = lock.read_text()
            lock.write_text('example.com/unreviewed:tag sha256:' +
                            'a' * 64 + '\n')
            result = subprocess.run(['bash', str(staged_script)], env=env,
                                    capture_output=True, check=False, cwd=root)
            self.assertNotEqual(0, result.returncode)
            self.assertIn(b'Unexpected image in lock', result.stderr)
            lock.write_text(valid_lock)
            (cache / 'docker-compose.yml').write_text('corrupt')
            result = subprocess.run(['bash', str(staged_script)], env=env,
                                    capture_output=True, check=False, cwd=root)
            self.assertNotEqual(0, result.returncode)
