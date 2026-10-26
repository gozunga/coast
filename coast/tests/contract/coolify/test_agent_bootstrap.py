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

"""Destructive local Q4 contract; opt in only on a fresh disposable guest."""

import hmac
import os
import tempfile
from urllib import error
from urllib import request
import uuid

from coast.agent.engines import bootstrap
from coast.agent.engines import coolify
from coast.tests import base
from coast.tests.contract.coolify.version import EXPECTED_VERSION


class LocalBootstrapContract(base.TestCase):
    def test_bootstrap_and_rotate_revokes_old_token(self):
        if os.environ.get('COAST_CONTRACT_COOLIFY_AGENT_BOOTSTRAP') != '1':
            self.skipTest('Local destructive agent contract is not enabled')
        self.assertEqual(0, os.geteuid(), 'Requires a disposable root guest')
        with tempfile.TemporaryDirectory(prefix='coast-agent-') as root:
            self.config.config(role='controller', state_path=root,
                               runtime_path=root, group='agent')
            engine = coolify.Engine(self.conf)
            body = {'engine_version': EXPECTED_VERSION}
            first = engine.bootstrap(body)
            self.assertEqual(EXPECTED_VERSION, first['engine_version'])
            self.assertNotIn('env_file_backup', first)
            self.assertRaises(coolify.Conflict, engine.bootstrap, body)
            rotation = dict(body, rotation_id=str(uuid.uuid4()),
                            previous_name='coast-agent-bootstrap-v1',
                            phase='create')
            second = engine.bootstrap(rotation, rotate=True)
            self.assertEqual(second, engine.bootstrap(rotation, rotate=True))
            self.assertFalse(hmac.compare_digest(first['token'],
                                                 second['token']))
            opener = request.build_opener(request.ProxyHandler({}),
                                          bootstrap.NoRedirect())
            for phase in ('create', 'revoke'):
                if phase == 'revoke':
                    rotation['phase'] = 'revoke'
                    engine.bootstrap(rotation, rotate=True)
                    engine.bootstrap(rotation, rotate=True)
                for token, expected in ((first['token'],
                                         200 if phase == 'create' else 401),
                                        (second['token'], 200)):
                    query = request.Request(
                        'http://127.0.0.1:8000/api/v1/version',
                        headers={'Authorization': 'Bearer ' + token})
                    try:
                        with opener.open(query, timeout=30) as response:
                            status = response.status
                    except error.HTTPError as failure:
                        status = failure.code
                    self.assertEqual(expected, status)
