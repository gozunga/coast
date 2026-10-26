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

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from coast.common import engine_versions
from coast.common import public_text
from coast.tests import base


SCRIPT = Path(__file__).resolve().parents[3] / 'tools/gen-template-catalog.py'


class TemplateCatalogTest(base.TestCase):

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'service-templates.json'
        self.output = self.root / 'output'
        compose = ('services:\n  sample:\n    environment:\n'
                   '      - TOKEN=${TOKEN:-private-default}\n'
                   '      - SERVICE_PASSWORD_DB=generated-secret\n'
                   '      - DB=${SERVICE_PASSWORD_DB}\n'
                   '      - HOST=$HOST\n      - ESCAPED=$$IGNORE\n')
        self.fixture = {
            'zeta': {'slogan': 'Sample database', 'category': 'database',
                     'logo': 'svgs/sample.svg', 'tags': ['sql', 'db', 'sql'],
                     'port': '8443, 80, 80',
                     'compose': base64.b64encode(compose.encode()).decode(),
                     'envs': {'TOKEN': 'private-token',
                              'SERVICE_PASSWORD_DB': 'generated-secret'}},
            'alpha': {'name': 'Alpha', 'slogan': 'Sample application',
                      'envs': ['USER=private-user'], 'port': '3000'},
            'middle': {'name': None, 'slogan': None, 'tags': None},
        }

    def _run(self, checksum=None, version='4.4.3', check=False):
        self.source.write_text(json.dumps(self.fixture), encoding='utf-8')
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source),
             '--version', version, '--sha256', checksum or digest,
             '--output-dir', str(self.output)] +
            (['--check'] if check else []),
            capture_output=True, text=True, check=False)

    def test_normalized_deterministic_catalog_has_no_values(self):
        result = self._run()
        self.assertEqual(0, result.returncode, result.stderr)
        target = self.output / 'catalog-4.4.3.json'
        first = target.read_bytes()
        catalog = json.loads(first)
        self.assertEqual(['alpha', 'middle', 'zeta'],
                         [item['id'] for item in catalog])
        self.assertEqual({
            'id': 'zeta', 'name': 'zeta', 'description': 'Sample database',
            'logo': 'svgs/sample.svg', 'category': 'database',
            'tags': ['db', 'sql'],
            'required_env': ['HOST', 'SERVICE_PASSWORD_DB', 'TOKEN'],
            'ports': [80, 8443], 'http_port': None,
        }, catalog[2])
        self.assertEqual(['USER'], catalog[0]['required_env'])
        self.assertEqual([], catalog[1]['ports'])
        for secret in ('private-default', 'generated-secret',
                       'private-token', 'private-user', 'compose', 'IGNORE'):
            self.assertNotIn(secret, first.decode())
        self.assertEqual('{}\n', (self.output /
                                  'volumes-4.4.3.json').read_text())
        self.fixture = dict(reversed(list(self.fixture.items())))
        self.assertEqual(0, self._run().returncode)
        self.assertEqual(first, target.read_bytes())

    def test_checksum_mismatch_writes_nothing(self):
        result = self._run(checksum='0' * 64)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.output.exists())

    def test_invalid_source_does_not_echo_secret(self):
        self.fixture['zeta']['compose'] = 'private-invalid-base64'
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn('private-invalid-base64', result.stderr)
        self.assertFalse(self.output.exists())

    def test_invalid_port_writes_nothing(self):
        self.fixture['zeta']['port'] = 'not-a-port'
        self.assertNotEqual(0, self._run().returncode)
        self.assertFalse(self.output.exists())

    def test_version_cannot_escape_output_directory(self):
        self.assertNotEqual(0, self._run(version='../escape').returncode)
        self.assertFalse(self.output.exists())

    def test_check_detects_drift_without_writing(self):
        self.assertEqual(0, self._run().returncode)
        self.assertEqual(0, self._run(check=True).returncode)
        target = self.output / 'catalog-4.4.3.json'
        volumes = self.output / 'volumes-4.4.3.json'
        self.assertEqual('{}\n', volumes.read_text())
        target.write_text('[]\n')
        self.assertNotEqual(0, self._run(check=True).returncode)
        self.assertEqual('[]\n', target.read_text())
        self.assertEqual('{}\n', volumes.read_text())

    def test_check_detects_volume_drift_without_writing(self):
        self.assertEqual(0, self._run().returncode)
        volumes = self.output / 'volumes-4.4.3.json'
        volumes.write_text('{"drift": true}\n')
        self.assertNotEqual(0, self._run(check=True).returncode)
        self.assertEqual('{"drift": true}\n', volumes.read_text())

    def test_compose_asset_matches_source_and_check_detects_drift(self):
        self.assertEqual(0, self._run().returncode)
        target = self.output / 'compose-4.4.3.json'
        self.assertEqual({'zeta': self.fixture['zeta']['compose']},
                         json.loads(target.read_text()))
        target.write_text('{}\n')
        self.assertNotEqual(0, self._run(check=True).returncode)
        self.assertEqual('{}\n', target.read_text())

    def test_volume_index_accounts_named_volumes_and_ignores_binds(self):
        named = ('services:\n  app:\n    volumes:\n'
                 '      - data:/var/lib/data\nvolumes:\n  data: {}\n')
        bind = ('services:\n  app:\n    volumes:\n'
                '      - ./host-data:/var/lib/data\nvolumes:\n  data: {}\n')
        self.fixture = {
            'named': {'compose': base64.b64encode(named.encode()).decode()},
            'bind': {'compose': base64.b64encode(bind.encode()).decode()},
        }
        result = self._run()
        self.assertEqual(0, result.returncode, result.stderr)
        text = (self.output / 'volumes-4.4.3.json').read_text()
        volumes = json.loads(text)
        self.assertEqual({'volumes': [{
            'name': 'data', 'container_path': '/var/lib/data'}]},
            volumes['named'])
        self.assertNotIn('bind', volumes)
        self.assertNotIn('host-data', text)
        catalog = (self.output / 'catalog-4.4.3.json').read_text()
        self.assertNotIn('host-data', catalog)
        self.assertNotIn('/var/lib/data', catalog)

    def test_socket_and_host_network_templates_are_accepted(self):
        from coast.common import named_volumes
        socket = '/var/run/docker.sock:/var/run/docker.sock:ro'
        raw = 'services: {web: {image: nginx, volumes: ["%s"]}}' % socket
        self.assertEqual({'docker_socket': True, 'volumes': []},
                         named_volumes.parse_compose(raw))
        self.assertEqual({'docker_socket': True, 'volumes': []},
                         named_volumes.parse_compose(raw.replace(
                             'image: nginx,', 'network_mode: host,')))
        self.assertTrue(named_volumes.for_template(
            '4.4.3', 'dozzle')['docker_socket'])
        for name in ('beszel', 'beszel-agent'):
            self.assertTrue(named_volumes.for_template(
                '4.4.3', name)['docker_socket'])

    def test_out_of_range_ports_are_not_advertised(self):
        self.fixture['zeta']['port'] = '0,80000,443'
        self.assertEqual(0, self._run().returncode)
        result = json.loads((self.output / 'catalog-4.4.3.json').read_text())
        self.assertEqual([443], result[2]['ports'])

    def test_http_port_magic_variables_in_compose_order(self):
        cases = (
            ('SERVICE_URL_APP_3000 SERVICE_FQDN_OTHER_8080', '80', 3000),
            ('SERVICE_FQDN_APP_5678 SERVICE_URL_OTHER_80', '80', 5678),
            ('SERVICE_URL_APP SERVICE_FQDN_OTHER_3001', '80', 3001),
            ('SERVICE_URL_APP SERVICE_FQDN_OTHER', '8080', 8080),
            ('SERVICE_FQDN_APP', '', None),
            ('SERVICE_PASSWORD_APP', '25565', None),
            ('', '7777', None),
        )
        for compose, port, expected in cases:
            with self.subTest(compose=compose, port=port):
                self.fixture = {'sample': {
                    'compose': base64.b64encode(compose.encode()).decode(),
                    'port': port}}
                result = self._run()
                self.assertEqual(0, result.returncode, result.stderr)
                catalog = json.loads((self.output /
                                      'catalog-4.4.3.json').read_text())
                self.assertEqual(expected, catalog[0]['http_port'])

    def test_pinned_catalog_http_ports(self):
        target = SCRIPT.parent.parent / (
            'coast/drivers/coolify/templates/catalog-4.4.3.json')
        catalog = {item['id']: item for item in json.loads(target.read_text())}
        for name, expected in (('uptime-kuma', 3001), ('n8n', 5678),
                               ('wikijs', 3000), ('minecraft', None)):
            self.assertEqual(expected, catalog[name]['http_port'])

    def test_display_fields_never_name_engine(self):
        compose = 'services:\n  app:\n    volumes:\n' \
                  '      - ${COOLIFY_VOLUME_APP}:/data\n'
        self.fixture = {'traefik-ui': {
            'name': 'Traefik dashboard',
            'slogan': 'Runs on Coolify \u2014 fast',
            'logo': 'coolify-logo.svg', 'category': 'coolify',
            'tags': ['traefik', 'proxy'],
            'compose': base64.b64encode(compose.encode()).decode()},
            'plain': {'logo': 'svgs/app\u2014logo.svg'}}
        version = engine_versions.TEMPLATE_VERSION
        result = self._run(version=version)
        self.assertEqual(0, result.returncode, result.stderr)
        catalog = {entry['id']: entry for entry in json.loads(
            (self.output / ('catalog-%s.json' % version)).read_text())}
        self.assertEqual('', catalog['plain']['logo'])
        item = catalog['traefik-ui']
        self.assertEqual('', item['logo'])
        self.assertEqual('application routing dashboard', item['name'])
        self.assertEqual('Runs on application engine - fast',
                         item['description'])
        self.assertEqual('application engine', item['category'])
        self.assertEqual(['application routing', 'proxy'], item['tags'])
        self.assertFalse(public_text.contains_engine_name(str(
            [item[field] for field in
             ('name', 'description', 'logo', 'category', 'tags')])))
        # Identifiers stay as upstream has them.
        self.assertEqual('traefik-ui', item['id'])
        self.assertEqual(['COOLIFY_VOLUME_APP'], item['required_env'])
