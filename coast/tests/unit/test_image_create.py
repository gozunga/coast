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
import os
from pathlib import Path
import re
import subprocess
import tempfile

from coast.tests import base


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'tools' / 'build-images' / 'coast-image-create.sh'
SHA = 'a' * 64


class ImageCreateTest(base.TestCase):

    def _run(self, *args):
        return subprocess.run(
            ['bash', str(SCRIPT), *args],
            check=False, capture_output=True, text=True)

    def _plan(self, *args):
        result = self._run('--print-command', *args)
        self.assertEqual(0, result.returncode, result.stderr)
        plan = {}
        for line in result.stdout.splitlines():
            key, value = line.split('=', 1)
            plan[key] = value
        return plan

    def test_help_lists_flags(self):
        result = self._run('--help')
        self.assertEqual(0, result.returncode, result.stderr)
        for flag in (
                '--role', '--distro', '--release', '--format',
                '--coolify-version', '--offline',
                '--coolify-installer-sha256', '--agent-sha256',
                'COAST_COOLIFY_INSTALLER_SHA256', 'COAST_AGENT_SHA256',
                'ubuntu-noble', 'qcow2', 'controller', 'worker'):
            self.assertIn(flag, result.stdout)

    def test_missing_role_is_usage_error(self):
        result = self._run('--print-command')
        self.assertEqual(2, result.returncode)
        self.assertIn('--role', result.stderr)

    def test_rejects_unknown_role_distro_format_and_version(self):
        self.assertEqual(
            2, self._run('--print-command', '--role', 'build').returncode)
        self.assertEqual(2, self._run(
            '--print-command', '--role', 'worker',
            '--distro', 'fedora').returncode)
        self.assertEqual(2, self._run(
            '--print-command', '--role', 'worker',
            '--format', 'raw').returncode)
        self.assertEqual(2, self._run(
            '--print-command', '--role', 'controller',
            '--coolify-version', '4.4').returncode)
        self.assertEqual(2, self._run(
            '--print-command', '--role', 'controller',
            '--coolify-version', '4.4.7;rm').returncode)

    def test_controller_plan_uses_noble_qcow2_and_pin(self):
        plan = self._plan(
            '--role', 'controller', '--coolify-installer-sha256', SHA)
        self.assertEqual('controller', plan['role'])
        self.assertEqual(SHA, plan['coolify_installer_sha256'])
        self.assertEqual('ubuntu', plan['distro'])
        self.assertEqual('noble', plan['release'])
        self.assertEqual('qcow2', plan['format'])
        self.assertEqual('4.4.7', plan['coolify_version'])
        self.assertEqual('0', plan['offline'])
        self.assertEqual(
            'coast-coolify-controller-4.4.7.qcow2', plan['output'])
        self.assertIn('coast-controller', plan['elements'])
        self.assertNotIn('coast-worker', plan['elements'])
        self.assertIn('disk-image-create', plan['command'])
        self.assertIn('qcow2', plan['command'])

    def test_ubuntu_noble_token_and_worker_elements(self):
        plan = self._plan(
            '--role', 'worker', '--distro', 'ubuntu-noble',
            '--coolify-version', '4.4.7')
        self.assertEqual('ubuntu', plan['distro'])
        self.assertEqual('noble', plan['release'])
        self.assertEqual('coast-worker-4.4.7.qcow2', plan['output'])
        self.assertIn('coast-worker', plan['elements'])
        self.assertNotIn('coast-controller', plan['elements'])

    def test_output_suffix_is_not_doubled(self):
        plan = self._plan(
            '--role', 'worker', '--output', '/tmp/coast-worker.qcow2')
        self.assertEqual('/tmp/coast-worker.qcow2', plan['output'])
        self.assertIn('-o /tmp/coast-worker ', plan['command'])

    def test_offline_controller_requires_local_installer(self):
        refused = self._run(
            '--print-command', '--role', 'controller', '--offline')
        self.assertEqual(2, refused.returncode)
        payload = b'installer-bytes'
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(payload)
            handle.flush()
            plan = self._plan(
                '--role', 'controller', '--offline',
                '--coolify-installer', handle.name,
                '--coolify-installer-sha256', digest)
            self.assertEqual('1', plan['offline'])
            self.assertEqual(handle.name, plan['coolify_installer'])
            self.assertEqual(digest, plan['coolify_installer_sha256'])

    def test_worker_rejects_coolify_installer(self):
        with tempfile.NamedTemporaryFile() as handle:
            result = self._run(
                '--print-command', '--role', 'worker',
                '--coolify-installer', handle.name)
        self.assertEqual(2, result.returncode)

    def test_offline_rejects_agent_url(self):
        result = self._run(
            '--print-command', '--role', 'worker', '--offline',
            '--agent-source', 'https://example.com/coast-agent',
            '--agent-sha256', SHA)
        self.assertEqual(2, result.returncode)
        self.assertIn('agent-source', result.stderr)

    def test_missing_checksum_is_usage_error(self):
        missing = self._run('--print-command', '--role', 'controller')
        self.assertEqual(0, missing.returncode)
        self.assertIn('coolify_installer_sha256=', missing.stdout)

        short = self._run(
            '--print-command', '--role', 'controller',
            '--coolify-installer-sha256', 'abcd')
        self.assertEqual(2, short.returncode)
        self.assertIn('64 hex', short.stderr)

        agent = self._run(
            '--print-command', '--role', 'worker',
            '--agent-source', 'https://example.com/coast-agent')
        self.assertEqual(2, agent.returncode)
        self.assertIn('agent-sha256', agent.stderr)

    def test_checksum_mismatch_is_usage_error(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b'not-the-installer')
            handle.flush()
            result = self._run(
                '--print-command', '--role', 'controller',
                '--coolify-installer', handle.name,
                '--coolify-installer-sha256', SHA)
        self.assertEqual(2, result.returncode)
        self.assertIn('does not match', result.stderr)

    def test_checksum_env_is_an_input(self):
        env = os.environ.copy()
        env['COAST_COOLIFY_INSTALLER_SHA256'] = SHA
        result = subprocess.run(
            ['bash', str(SCRIPT), '--print-command', '--role',
             'controller'],
            check=False, capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('coolify_installer_sha256=' + SHA, result.stdout)

    def test_build_scripts_do_not_execute_coolify_installer(self):
        run = re.compile(
            r'(^|[;&|])\s*(bash|sh|source|\.)\s+\S*install\.sh\b')
        root = ROOT / 'tools' / 'build-images'
        for path in root.rglob('*'):
            if not path.is_file() or path.suffix == '.rst':
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                self.assertIsNone(
                    run.search(stripped),
                    f'{path} executes the installer: {line}')

    def test_root_hooks_require_sudo_for_target_writes(self):
        elements = ROOT / 'tools/build-images/elements'
        # These hooks write only to TARGET_ROOT, including dest/stage aliases.
        write = re.compile(r'(^|[;|])\s*(mkdir|cp|install|chmod|tee)\s')
        redirect = re.compile(r'>\s*"?\$(?:\{)?(TARGET_ROOT|dest|stage)')
        for phase in ('root.d', 'extra-data.d'):
            for hook in elements.glob('*/' + phase + '/*'):
                for line in hook.read_text().splitlines():
                    if line.lstrip().startswith('#'):
                        continue
                    self.assertIsNone(write.search(line), f'{hook}: {line}')
                    self.assertIsNone(redirect.search(line), f'{hook}: {line}')
                    self.assertNotRegex(line, r'\b(curl|docker)\b.*\s-o\s')

    def test_build_env_retry_preserves_values_and_mode(self):
        hook = ROOT / ('tools/build-images/elements/coast-base/'
                       'root.d/10-build-env')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'bin'
            binary.mkdir()
            # Unit tests must not need host sudo privileges.
            sudo = binary / 'sudo'
            sudo.write_text('#!/bin/bash\nexec "$@"\n')
            sudo.chmod(0o755)
            env = dict(os.environ, TARGET_ROOT=str(root / 'target'),
                       COAST_ROLE='worker', COAST_COOLIFY_VERSION='4.4.7',
                       COAST_AGENT_SOURCE='https://example.com/a?x=one two',
                       PATH=str(binary) + ':' + os.environ['PATH'])
            output = root / 'target/etc/coast/build.env'
            for role in ('controller', 'worker', 'worker'):
                env['COAST_ROLE'] = role
                subprocess.run(['bash', str(hook)], env=env, check=True,
                               capture_output=True)
                result = subprocess.run(
                    ['bash', '-c', 'source "$1"; printf "%s\\n%s" '
                     '"$COAST_ROLE" "$COAST_AGENT_URL"', 'bash', str(output)],
                    check=True, capture_output=True, text=True)
                self.assertEqual(role + '\nhttps://example.com/a?x=one two',
                                 result.stdout)
                self.assertEqual(0o644, output.stat().st_mode & 0o777)
                self.assertEqual(8, len(output.read_text().splitlines()))
