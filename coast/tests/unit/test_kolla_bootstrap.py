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

import os
from pathlib import Path
import subprocess
import tempfile

from coast.tests import base


ROOT = Path(__file__).resolve().parents[3]
START = ROOT / 'tools/kolla/images/docker/coast/coast-base/extend_start.sh'


class KollaBootstrapTest(base.TestCase):
    """Source the real hook as kolla_start does, without root or Docker."""

    def run_start(self, bootstrap=None, override='', migration_rc=0):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.pop('KOLLA_BOOTSTRAP', None)
            if bootstrap is not None:
                env['KOLLA_BOOTSTRAP'] = bootstrap
            env['COAST_BOOTSTRAP_COMMAND'] = override
            # Redirect container paths; ownership is stubbed without root.
            hook = START.read_text().replace('/var/log/kolla/coast',
                                             directory + '/logs').replace(
                '/var/lib/coast/locks', directory + '/locks')
            script = Path(directory) / 'extend_start.sh'
            script.write_text(hook)
            wrapper = """
set -o errexit
set -o nounset
set -o xtrace
chown() { :; }
coast-db-manage() {
    printf '%s\n' "$*"
    return %s
}
source "$1"
echo conductor-started
""" % ('%s', migration_rc)
            result = subprocess.run(
                ['bash', '-c', wrapper, 'kolla_start', str(script)],
                env=env, capture_output=True, text=True, check=False)
            mode = (Path(directory) / 'locks').stat().st_mode & 0o777
            self.assertEqual(0o700, mode)
            return result

    def test_empty_bootstrap_runs_upgrade_and_exits(self):
        result = self.run_start(bootstrap='')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('--config-file /etc/coast/coast.conf upgrade\n',
                         result.stdout)
        self.assertNotIn('conductor-started', result.stdout)

    def test_nonempty_bootstrap_also_runs_upgrade(self):
        result = self.run_start(bootstrap='true')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('upgrade', result.stdout)
        self.assertNotIn('conductor-started', result.stdout)

    def test_migration_failure_propagates(self):
        result = self.run_start(bootstrap='', migration_rc=17)
        self.assertEqual(17, result.returncode)
        self.assertNotIn('conductor-started', result.stdout)

    def test_normal_service_does_not_migrate(self):
        result = self.run_start(override='exit 29')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('conductor-started\n', result.stdout)

    def test_override_runs_without_secret_in_trace(self):
        result = self.run_start(
            bootstrap='', override='secret=test-password; exit 0')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('', result.stdout)
        self.assertNotIn('test-password', result.stderr)

    def test_override_failure_propagates(self):
        result = self.run_start(bootstrap='', override='exit 23')
        self.assertEqual(23, result.returncode)
        self.assertNotIn('conductor-started', result.stdout)
