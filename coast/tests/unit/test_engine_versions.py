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

"""Release registry validation and guard against scattered runtime pins."""

import importlib.util
from pathlib import Path
import re
import subprocess
from unittest import mock

from coast.common import engine_versions
from coast.tests import base


class EngineVersionsTest(base.TestCase):
    def test_default_and_supported_config(self):
        self.assertEqual(('4.4.3', '4.4.7'),
                         engine_versions.SUPPORTED_ENGINE_VERSIONS)
        self.assertEqual('4.4.7', self.conf.coolify.pinned_version)
        self.assertRaises(ValueError, self.config.config,
                          pinned_version='4.4.9', group='coolify')

    def test_pinned_version_accepts_exact_supported_releases_only(self):
        for version in engine_versions.SUPPORTED_ENGINE_VERSIONS:
            self.config.config(pinned_version=version, group='coolify')
            self.assertEqual(version, self.conf.coolify.pinned_version)
            for bad in (version + '\n', ' ' + version, version + '.1',
                        version.replace('.', 'x')):
                self.assertRaises(ValueError, self.config.config,
                                  pinned_version=bad, group='coolify')

    def test_baked_version_accepts_supported_releases_only(self):
        for version in engine_versions.SUPPORTED_ENGINE_VERSIONS:
            with mock.patch('pathlib.Path') as path:
                path.return_value.read_text.return_value = version + '\n'
                self.assertEqual(version, engine_versions.build_version())
        with mock.patch('pathlib.Path') as path:
            path.return_value.read_text.return_value = "4.4.9'; exit(0);"
            self.assertRaises(ValueError, engine_versions.build_version)

    def test_each_baked_release_gates_php_and_stock_images(self):
        root = Path(__file__).resolve().parents[3]
        source = root / 'coast/agent/engines/bootstrap.py'
        for version in engine_versions.SUPPORTED_ENGINE_VERSIONS:
            spec = importlib.util.spec_from_file_location('baked', source)
            module = importlib.util.module_from_spec(spec)
            with mock.patch.object(engine_versions, 'build_version',
                                   return_value=version):
                spec.loader.exec_module(module)
            self.assertIn("!== '" + version + "'", module.PHP)
            for prefix in ('', 'docker.io/'):
                image = prefix + 'coollabsio/coolify:' + version
                result = subprocess.CompletedProcess(
                    [], 0, stdout=image.encode())
                with mock.patch.object(module.subprocess, 'run',
                                       return_value=result):
                    module.check_image('coolify')
            for image in ('example.com/custom:' + version,
                          'coollabsio/coolify:latest',
                          'coollabsio/coolify:' + next(
                              v for v in engine_versions.RELEASES
                              if v != version)):
                result = subprocess.CompletedProcess(
                    [], 0, stdout=image.encode())
                with mock.patch.object(module.subprocess, 'run',
                                       return_value=result):
                    self.assertRaises(module.BootstrapError,
                                      module.check_image, 'coolify')

    def test_runtime_versions_only_live_in_registry(self):
        root = Path(__file__).resolve().parents[3]
        # Historical provenance fixtures, never consumed as release defaults.
        allowed = {
            'coast/common/engine_versions.py',
            'tools/template-catalog-source.json',
            'coast/drivers/coolify/templates/catalog-4.4.3.json',
            'coast/drivers/coolify/templates/volumes-4.4.3.json',
            'tools/build-images/elements/coast-controller/files/'
            'coolify-images.lock',
        }
        candidates = [root / '.gitlab-ci.yml']
        for directory in ('coast', 'tools', 'devstack'):
            candidates.extend((root / directory).rglob('*'))
        failures = []
        for path in candidates:
            relative = path.relative_to(root)
            if (not path.is_file() or 'tests' in relative.parts or
                    '__pycache__' in relative.parts or
                    str(relative) in allowed or
                    path.suffix in ('.md', '.rst', '.pyc', '.png', '.svg')):
                continue
            try:
                contents = path.read_text()
            except UnicodeError:
                continue
            if re.search(r'(?<![0-9.])4\.\d+\.\d+\b', contents):
                failures.append(str(relative))
        self.assertEqual([], failures)
