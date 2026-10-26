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

"""Read-only Q78 contract for an isolated, installed local controller."""

import json
import os
from pathlib import Path
import subprocess
from urllib import request

from coast.agent.engines import bootstrap
from coast.tests import base
from coast.tests.contract.coolify.version import EXPECTED_VERSION


class LocalFirstBootContract(base.TestCase):
    def test_supported_environment_and_loopback_api(self):
        if os.environ.get('COAST_CONTRACT_COOLIFY_FIRSTBOOT') != '1':
            self.skipTest('Local first-boot contract is not enabled')
        self.assertEqual(0, os.geteuid(), 'Requires a disposable root guest')
        token = bootstrap.read_token(Path(
            os.environ['COAST_CONTRACT_COOLIFY_TOKEN_FILE']))
        contents = Path('/data/coolify/source/.env').read_text()
        values = dict(line.split('=', 1) for line in contents.splitlines()
                      if line and not line.startswith('#'))
        # Do not put secret values in assertion messages or engine logs.
        self.assertEqual('false', values.get('AUTOUPDATE'))
        for name in ('ROOT_USERNAME', 'ROOT_USER_EMAIL', 'ROOT_USER_PASSWORD'):
            self.assertTrue(bool(values.get(name)),
                            'Missing root env variable')
        ports = json.loads(subprocess.run(
            ['docker', 'inspect', '--format',
             '{{json .NetworkSettings.Ports}}', 'coolify'], check=True,
            capture_output=True, timeout=60).stdout)
        published = [binding for bindings in ports.values() if bindings
                     for binding in bindings]
        self.assertEqual(2, len(published))
        self.assertEqual({'127.0.0.1'}, {p['HostIp'] for p in published})
        self.assertFalse(any(item.get('HostPort') == '6002'
                             for item in published))
        opener = request.build_opener(request.ProxyHandler({}),
                                      bootstrap.NoRedirect())
        query = request.Request(
            'http://127.0.0.1:8000/api/v1/version',
            headers={'Authorization': 'Bearer ' + token})
        with opener.open(query, timeout=30) as response:
            version = response.read(64).decode().strip('"\n ')
        self.assertEqual(EXPECTED_VERSION, version)
