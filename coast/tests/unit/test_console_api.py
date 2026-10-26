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

"""Shell session grants. The response carries a URL, never a shell."""

import datetime
import json
import urllib.parse

from oslo_utils import timeutils

from coast.common import states
from coast.console import tokens
from coast import objects
from coast.tests import app_api


class ConsoleAPITest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.config.config(public_endpoint='console.example.com:9610',
                           group='console')
        self.server.mgmt_ip = '198.51.100.10'
        self.server.save()
        self.service.engine_ref = 'engine-app-1'
        self.service.save()
        self.app_path = '/v1/apps/' + self.service.id + '/console'
        self.service_path = self.path + '/console'

    def open(self, path=None, body=None, status=201, version='1.9',
             headers=None):
        return self.request('post', path or self.app_path, body,
                            headers=headers, status=status, version=version)

    def stored(self):
        return list(objects.ConsoleTokenList.get_all(self.ctx))

    def test_member_receives_one_hashed_single_use_url(self):
        response = self.open(body={'command': ['sh', '-c', 'true']})
        body = response.json
        self.assertEqual({'url', 'expires_at'}, set(body))
        self.assertTrue(body['url'].startswith(
            'wss://console.example.com:9610/?token='))
        parsed = urllib.parse.urlsplit(body['url'])
        secret = urllib.parse.parse_qs(parsed.query)['token'][0]
        rows = self.stored()
        self.assertEqual(1, len(rows))
        self.assertEqual(tokens.digest(secret), rows[0].token_hash)
        self.assertNotIn(secret, rows[0].token_hash)
        self.assertEqual(['sh', '-c', 'true'], rows[0].command)
        self.assertEqual('app', rows[0].resource_type)
        self.assertEqual(self.service.id, rows[0].resource_id)
        self.assertEqual('user', rows[0].user_id)
        self.assertIsNone(rows[0].used_at)
        expires = datetime.datetime.fromisoformat(body['expires_at'])
        remaining = (expires - timeutils.utcnow()).total_seconds()
        self.assertGreater(remaining, 50)
        self.assertLess(remaining, 65)
        encoded = json.dumps(body).lower()
        for forbidden in ('coolify', 'traefik', 'docker', 'proxy'):
            self.assertNotIn(forbidden, encoded)

    def test_omitted_command_is_an_interactive_shell(self):
        self.open()
        self.assertIsNone(self.stored()[0].command)
        self.open(body={'command': None})
        self.assertIsNone(self.stored()[-1].command)

    def test_plain_url_when_tls_is_off(self):
        self.config.config(tls=False, group='console')
        body = self.open().json
        self.assertTrue(body['url'].startswith(
            'ws://console.example.com:9610/?token='))

    def test_reader_and_manager_cannot_open(self):
        for role in ('reader', 'manager'):
            headers = dict(self.headers, **{'X-Roles': role})
            self.open(headers=headers, status=403)
        self.assertEqual([], self.stored())

    def test_admin_can_open(self):
        headers = dict(self.headers, **{'X-Roles': 'admin'})
        self.open(headers=headers)
        self.assertEqual(1, len(self.stored()))

    def test_other_project_and_wrong_kind_are_hidden(self):
        headers = dict(self.headers, **{'X-Project-Id': 'other'})
        self.open(headers=headers, status=404)
        self.open(self.service_path, status=404)
        self.service.template_id = 'wordpress'
        self.service.kind = 'service'
        self.service.save()
        self.open(status=404)
        self.open(self.service_path)
        self.assertEqual('service', self.stored()[0].resource_type)
        self.open('/v1/apps/missing/console', status=404)
        self.client.get('/v1/apps', headers=self.headers, status=404)

    def test_old_microversion_and_method(self):
        self.open(version='1.8', status=404)
        self.open(version=None, status=404)
        self.client.get(self.app_path, headers=dict(self.headers, **{
            'OpenStack-API-Version': 'app-hosting 1.9'}), status=405)
        self.assertEqual([], self.stored())

    def test_bad_command_is_rejected(self):
        for body in ({'command': []}, {'command': ['']},
                     {'command': ['ok', 1]}, {'command': 'sh'},
                     {'user': 'root'}, {'command': ['sh'], 'user': 'root'}):
            self.open(body=body, status=400)
        self.assertEqual([], self.stored())

    def test_unavailable_app_does_not_grant_a_token(self):
        self.service.status = states.ERROR
        self.service.save()
        response = self.open(status=409)
        self.assertEqual('The app is not available.',
                         response.json['faultstring'])
        self.service.status = states.ACTIVE
        self.service.save()
        self.server.mgmt_ip = None
        self.server.save()
        self.open(status=409)
        self.server.mgmt_ip = '198.51.100.10'
        self.server.role = 'control'
        self.server.save()
        self.open(status=409)
        self.server.role = 'worker'
        self.server.save()
        self.service.server_id = None
        self.service.save()
        self.open(status=409)
        self.assertEqual([], self.stored())

    def test_missing_public_endpoint_is_unavailable(self):
        for value in ('', 'https://console.example.com',
                      'console.example.com/shell'):
            self.config.config(public_endpoint=value, group='console')
            response = self.open(status=503)
            self.assertEqual('Service Unavailable',
                             response.json['faultstring'])
        self.assertEqual([], self.stored())
