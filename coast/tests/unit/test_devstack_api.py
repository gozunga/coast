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

"""DevStack API listen address and uWSGI entry point."""

import os
import subprocess
import textwrap

import fixtures

from coast.tests import base


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
LIB = os.path.join(ROOT, 'devstack', 'lib', 'coast')


class DevstackApiTest(base.TestCase):
    def _bash(self, script, env=None):
        command = ['bash', '-c', script]
        result = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True,
            check=False)
        return result

    def test_shell_scripts_have_valid_syntax(self):
        for name in ('devstack/lib/coast', 'devstack/plugin.sh',
                     'devstack/settings'):
            result = subprocess.run(
                ['bash', '-n', os.path.join(ROOT, name)],
                capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)

    def test_listen_address_refuses_a_wildcard(self):
        for address in ('0.0.0.0', '::', '[::]', '*', ''):
            result = self._bash(
                'source devstack/lib/coast; '
                'coast_api_listen_address "$ADDRESS"',
                env={'ADDRESS': address, 'PATH': os.environ['PATH']})
            self.assertEqual(1, result.returncode, address)
            self.assertIn('one host address', result.stderr)

    def test_default_listen_address_is_one_host(self):
        result = self._bash(
            'source devstack/lib/coast; coast_api_listen_address',
            env={'PATH': os.environ['PATH']})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('127.0.0.1\n', result.stdout)

    def test_endpoint_uses_the_listen_host(self):
        result = self._bash(
            'source devstack/lib/coast; '
            'coast_api_endpoint_url 192.0.2.10 9600',
            env={'PATH': os.environ['PATH']})
        self.assertEqual('http://192.0.2.10:9600\n', result.stdout)
        result = self._bash(
            'source devstack/lib/coast; '
            'coast_api_http_socket 2001:db8::10 9600',
            env={'PATH': os.environ['PATH']})
        self.assertEqual('[2001:db8::10]:9600\n', result.stdout)

    def test_uwsgi_ini_uses_coast_wsgi_api_on_one_address(self):
        path = self.useFixture(fixtures.TempDir()).path
        conf = os.path.join(path, 'coast-api-uwsgi.ini')
        result = self._bash(
            'source devstack/lib/coast; '
            'coast_write_uwsgi_config "$CONF" '
            '"/opt/stack/bin/coast-wsgi-api" 192.0.2.10 9600; '
            'cat "$CONF"',
            env={'CONF': conf, 'PATH': os.environ['PATH']})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            'wsgi-file = /opt/stack/bin/coast-wsgi-api\n', result.stdout)
        self.assertIn('http-socket = 192.0.2.10:9600\n', result.stdout)
        self.assertNotIn('0.0.0.0', result.stdout)
        refused = self._bash(
            'source devstack/lib/coast; '
            'coast_write_uwsgi_config "$CONF" /tmp/coast-api 192.0.2.10 9600',
            env={'CONF': conf, 'PATH': os.environ['PATH']})
        self.assertEqual(1, refused.returncode)
        wildcard = self._bash(
            'source devstack/lib/coast; '
            'coast_write_uwsgi_config "$CONF" '
            '/tmp/coast-wsgi-api 0.0.0.0 9600',
            env={'CONF': conf, 'PATH': os.environ['PATH']})
        self.assertEqual(1, wildcard.returncode)

    def test_plugin_starts_uwsgi_and_settings_avoid_the_wildcard(self):
        with open(os.path.join(ROOT, 'devstack', 'plugin.sh')) as handle:
            plugin = handle.read()
        self.assertIn('coast-wsgi-api', plugin)
        self.assertIn('uwsgi --procname-prefix coast-api --ini', plugin)
        self.assertNotIn('$COAST_BIN_DIR/coast-api', plugin)
        self.assertNotIn('coast-api --config', plugin)
        start = plugin.split('function start_coast', 1)[1].split(
            '\nfunction ', 1)[0]
        self.assertIn('coast_set_bin_dir', start)
        self.assertLess(start.index('coast_set_bin_dir'),
                        start.index('uwsgi'))
        script = textwrap.dedent("""\
            enable_service() { true; }
            DEST=/opt/stack
            HOST_IP=192.0.2.10
            source devstack/settings
            printf '%s\\n' "$COAST_SERVICE_LISTEN_ADDRESS"
            printf '%s\\n' "$COAST_SERVICE_HOST"
            printf '%s\\n' "$COAST_SERVICE_PORT"
            """)
        result = self._bash(script, env={'PATH': os.environ['PATH']})
        self.assertEqual(
            '192.0.2.10\n192.0.2.10\n9600\n', result.stdout, result.stderr)
        self.assertNotIn('0.0.0.0', result.stdout)
