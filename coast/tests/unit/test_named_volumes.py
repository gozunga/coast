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

import json
from pathlib import Path

from coast.common import named_volumes
from coast.tests import base


class NamedVolumeTest(base.TestCase):

    def test_names_and_container_paths(self):
        self.assertTrue(named_volumes.volume_name_ok('uploads'))
        self.assertFalse(named_volumes.volume_name_ok('/data'))
        self.assertFalse(named_volumes.volume_name_ok('.hidden'))
        self.assertFalse(named_volumes.volume_name_ok('bad/name'))
        self.assertTrue(named_volumes.container_ok('/var/data'))
        self.assertFalse(named_volumes.container_ok('/'))
        self.assertFalse(named_volumes.container_ok('/var/..'))
        self.assertFalse(named_volumes.container_ok('/var/.'))
        self.assertFalse(named_volumes.container_ok('relative'))

    def test_compose_accounts_named_volumes_and_ignores_binds(self):
        named = named_volumes.parse_compose(
            'services:\n  app:\n    volumes:\n      - data:/var/lib/data\n'
            'volumes:\n  data: {}\n')
        self.assertEqual({'volumes': [{
            'name': 'data', 'container_path': '/var/lib/data'}]}, named)
        bind = named_volumes.parse_compose(
            'services:\n  app:\n    volumes:\n'
            '      - ./minecraft-data:/data\n')
        self.assertIsNone(bind)
        self.assertIsNone(named_volumes.parse_compose(
            'services:\n  app:\n    image: app\n'))

    def test_template_accounts_allow_all_worker_features(self):
        raw = json.dumps({'services': {'web': {
            'image': 'nginx', 'privileged': True, 'network_mode': 'host',
            'pid': 'host', 'ipc': 'host', 'uts': 'host', 'userns_mode': 'host',
            'devices': ['/dev/sda'], 'cap_add': ['SYS_ADMIN'],
            'volumes': ['data:/data', '/host:/host',
                        '/var/run/docker.sock:/var/run/docker.sock']}}})
        self.assertEqual({'docker_socket': True, 'volumes': [{
            'name': 'data', 'container_path': '/data'}]},
            named_volumes.parse_compose(raw))

    def test_pinned_index_classifies_known_templates(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / 'drivers/coolify/templates/volumes-4.4.3.json'
                ).read_text(encoding='utf-8')
        self.assertNotIn('fs_path', text)
        self.assertNotIn('./', text)
        wordpress = named_volumes.for_template(
            '4.4.3', 'wordpress-with-mariadb')
        self.assertEqual([{
            'name': 'mariadb-data', 'container_path': '/var/lib/mysql'}, {
            'name': 'wordpress-files', 'container_path': '/var/www/html'}],
            wordpress['volumes'])
        kuma = named_volumes.for_template('4.4.3', 'uptime-kuma')
        self.assertEqual([{
            'name': 'uptime-kuma-data', 'container_path': '/app/data'}],
            kuma['volumes'])
        self.assertEqual({'volumes': []}, named_volumes.for_template(
            '4.4.3', 'minecraft'))
        self.assertEqual({'volumes': []},
                         named_volumes.for_template('4.4.3', 'postgresql'))
        self.assertIsNone(named_volumes.for_template('test', 'uptime-kuma'))
        self.assertEqual(json.loads(text)['uptime-kuma'], kuma)

    def test_implicit_template_volumes_ignore_host_paths(self):
        self.assertEqual({'volumes': [
            {'name': 'data', 'container_path': '/data'}]},
            named_volumes.parse_compose(
                'services: {web: {image: nginx, volumes: ["data:/data"]}}'))
        self.assertIsNone(named_volumes.parse_compose(
            'services: {web: {image: nginx, volumes: ["/host:/data"]}}'))
