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

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

from coast.tests import base


SCRIPT = (Path(__file__).resolve().parents[3]
          / 'tools/coolify/bootstrap_token.py')
spec = importlib.util.spec_from_file_location('bootstrap_token', SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
TOKEN = '1|' + 'a' * 48


class PatchMixin:
    def patch_mock(self, target, name, value=None):
        if value is None:
            value = mock.MagicMock()
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value


class BootstrapTest(PatchMixin, base.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / 'token'
        self.calls = []
        self.image = b'coollabsio/coolify:4.4.7\n'
        self.copy = TOKEN
        self.run = self.patch_mock(bootstrap.subprocess, 'run')
        self.run.side_effect = self.docker_run
        self.api = self.patch_mock(bootstrap, 'enable_api')

    def docker_run(self, command, **kwargs):
        self.calls.append(command)
        if command[1] == 'inspect':
            return SimpleNamespace(returncode=0, stdout=self.image)
        if command[1] == 'cp':
            Path(command[-1]).write_text(self.copy + '\n')
        return SimpleNamespace(returncode=0)

    def write_token(self, mode=0o600):
        self.output.write_text(TOKEN + '\n')
        self.output.chmod(mode)

    def test_create_then_repeat_without_second_creation(self):
        bootstrap.bootstrap(self.output)
        self.assertEqual(TOKEN, bootstrap.read_token(self.output))
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)
        bootstrap.bootstrap(self.output)
        creates = [cmd for cmd in self.calls if 'php' in cmd]
        self.assertEqual(1, len(creates))
        self.assertEqual(2, self.api.call_count)

    def test_version_rejected_before_internal_access(self):
        self.image = b'coollabsio/coolify:latest\n'
        self.assertRaises(bootstrap.BootstrapError,
                          bootstrap.bootstrap, self.output)
        self.assertEqual(1, len(self.calls))
        self.assertFalse(self.output.exists())

    def test_existing_file_permissions_rejected(self):
        self.write_token(0o644)
        self.assertRaises(bootstrap.BootstrapError,
                          bootstrap.bootstrap, self.output)
        self.api.assert_not_called()

    def test_symlink_rejected_without_touching_target(self):
        target = self.output.parent / 'target'
        target.write_text('keep')
        self.output.symlink_to(target)
        self.assertRaises(OSError, bootstrap.bootstrap, self.output)
        self.assertEqual('keep', target.read_text())
        self.assertFalse(any('php' in cmd for cmd in self.calls))

    def test_unsafe_directory_rejected(self):
        self.output.parent.chmod(0o755)
        self.assertRaises(bootstrap.BootstrapError,
                          bootstrap.bootstrap, self.output)
        self.assertEqual([], self.calls)

    def test_invalid_copy_not_published(self):
        self.copy = 'invalid'
        self.assertRaises(bootstrap.BootstrapError,
                          bootstrap.bootstrap, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(any('rm' in cmd for cmd in self.calls))

    def test_api_failure_retains_token_for_retry(self):
        self.api.side_effect = OSError('secret-bearing failure')
        self.assertRaises(OSError, bootstrap.bootstrap, self.output)
        self.assertEqual(TOKEN, bootstrap.read_token(self.output))
        self.assertFalse(any('rm' in cmd for cmd in self.calls))
        self.api.side_effect = None
        bootstrap.bootstrap(self.output)
        creates = [cmd for cmd in self.calls if 'php' in cmd]
        self.assertEqual(1, len(creates))

    def test_main_sanitizes_failures(self):
        self.patch_mock(bootstrap.os, 'geteuid').return_value = 0
        self.patch_mock(bootstrap.sys, 'argv',
                        ['bootstrap', '--output', str(self.output)])
        self.run.side_effect = OSError(TOKEN)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr):
            self.assertEqual(1, bootstrap.main())
        self.assertEqual('', stdout.getvalue())
        self.assertNotIn(TOKEN, stderr.getvalue())

    def test_docker_failure_discards_output(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=1)
        self.assertRaises(bootstrap.BootstrapError, bootstrap.docker, 'cp')
        kwargs = self.run.call_args.kwargs
        self.assertEqual(bootstrap.subprocess.DEVNULL, kwargs['stdout'])
        self.assertEqual(bootstrap.subprocess.DEVNULL, kwargs['stderr'])


class LocalAPITest(PatchMixin, base.TestCase):
    def test_enable_and_version_are_local(self):
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = b'4.4.7'
        builder = self.patch_mock(bootstrap.request, 'build_opener')
        builder.return_value = opener
        bootstrap.enable_api(TOKEN, 8000)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(['POST', 'GET'], [req.method for req in requests])
        self.assertEqual(['http://127.0.0.1:8000/api/v1/enable',
                          'http://127.0.0.1:8000/api/v1/version'],
                         [req.full_url for req in requests])
        self.assertEqual({}, builder.call_args.args[0].proxies)

    def test_wrong_api_version_rejected(self):
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = (
            b'4.4.4')
        builder = self.patch_mock(bootstrap.request, 'build_opener')
        builder.return_value = opener
        self.assertRaises(bootstrap.BootstrapError,
                          bootstrap.enable_api, TOKEN, 8000)

    def test_redirect_rejected(self):
        self.assertRaises(bootstrap.BootstrapError,
                          bootstrap.NoRedirect().redirect_request,
                          None, None, 302, '', {}, 'http://example.com')
