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

"""The committed sample files must match their generators.

Lanes run py3 before every push, so a stale sample fails here, next to
the option or policy change that made it stale.
"""

from pathlib import Path
import tempfile

from oslo_config import cfg
from oslo_config import generator as config_generator
from oslo_policy import generator as policy_generator

from coast.common import engine_versions
from coast.tests import base

ROOT = Path(__file__).resolve().parents[3]
ETC = ROOT / 'etc' / 'coast'


class SampleFilesTest(base.TestCase):

    def _generated(self, name, generate):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / name
            generate(str(output))
            return output.read_text()

    def _assert_current(self, name, generated, tox_env):
        committed = (ETC / name).read_text()
        self.assertTrue(
            committed == generated,
            'etc/coast/%s is stale; run "tox -e %s" and commit the result.'
            % (name, tox_env))

    def _config_sample(self):
        return self._generated('coast.conf.sample', lambda out: (
            config_generator.main([
                '--config-file',
                str(ETC / 'coast-config-generator.conf'),
                '--output-file', out])))

    def test_config_sample_matches_genconfig(self):
        self._assert_current(
            'coast.conf.sample', self._config_sample(), 'genconfig')

    def test_policy_sample_matches_genpolicy(self):
        def generate(out):
            conf = cfg.ConfigOpts()
            self.addCleanup(conf.reset)
            policy_generator.generate_sample(
                ['--config-file', str(ETC / 'coast-policy-generator.conf'),
                 '--output-file', out], conf=conf)

        self._assert_current(
            'policy.yaml.sample',
            self._generated('policy.yaml.sample', generate), 'genpolicy')

    def test_config_sample_has_no_engine_version_literal(self):
        # Releases and the default live only in engine_versions.py.
        sample = self._config_sample()
        for version in engine_versions.SUPPORTED_ENGINE_VERSIONS:
            self.assertNotIn(version, sample)
        self.assertIn(
            '#pinned_version = <newest release in engine_versions.py>\n',
            sample)
