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

"""Customer flavor representation and catalog contracts."""

import json
from types import SimpleNamespace
from unittest import mock

from coast.api.v1 import flavor_controllers
from coast.common import states
from coast import objects
from coast.tests.unit import test_controllers


class FlavorTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.headers.update({'OpenStack-API-Version': 'app-hosting 1.27',
                             'X-Roles': 'reader'})
        self.server.flavor_id = 'size-id'
        self.server.save()
        self.config.config(definitions=json.dumps([
            dict(name='small', flavor='size-id', kind='vm',
                 roles=['worker', 'build']),
            dict(name='metal', flavor='metal-id', kind='baremetal',
                 roles=['worker'])]), group='plans')
        patcher = mock.patch.object(flavor_controllers.controller,
                                    'ControllerCloud')
        self.cloud = patcher.start()
        self.addCleanup(patcher.stop)
        self.find = self.cloud.return_value.proxy.return_value.find_flavor
        self.find.return_value = SimpleNamespace(
            id='size-id', name='Small', vcpus=1, ram=1024, disk=10)
        self.expected = dict(id='size-id', name='Small', vcpus=1,
                             ram=1024, disk=10)

    def test_project_list_show_and_fields(self):
        for path, envelope in ((self.server_path, 'server'),
                               (self.path + '/servers', 'servers')):
            response = self.client.get(path, headers=self.headers).json
            row = response[envelope]
            row = row[0] if isinstance(row, list) else row
            self.assertEqual(self.expected, row['flavor'])
            self.assertNotIn('flavor_id', row)
        row = self.client.get(self.server_path + '?fields=flavor',
                              headers=self.headers).json['server']
        self.assertEqual({'flavor', 'links'}, set(row))
        rows = self.client.get(self.path + '/servers?fields=flavor',
                               headers=self.headers).json['servers']
        self.assertEqual({'flavor', 'links'}, set(rows[0]))
        self.client.get(self.path + '/servers?sort_key=flavor',
                        headers=self.headers, status=400)
        self.client.get(self.path + '/servers?flavor=size-id',
                        headers=self.headers, status=400)

    def test_old_versions_unchanged_and_admin_detail_preserved(self):
        for version in ('1.0', '1.26', '1.27'):
            for role in ('reader', 'admin'):
                headers = dict(self.headers, **{
                    'OpenStack-API-Version': 'app-hosting ' + version,
                    'X-Roles': role})
                for path, key in ((self.server_path, 'server'),
                                  (self.path + '/servers', 'servers')):
                    row = self.client.get(path, headers=headers).json[key]
                    row = row[0] if isinstance(row, list) else row
                    self.assertEqual(version == '1.27', 'flavor' in row)
                    self.assertEqual(role == 'admin', 'flavor_id' in row)

    def test_list_caches_distinct_flavors(self):
        objects.Server(context=self.ctx, name='another', stack_id=self.env.id,
                       kind='vm', role='build', flavor_id='size-id',
                       status=states.ACTIVE, task_state=states.NONE).create()
        self.client.get(self.path + '/servers', headers=self.headers)
        self.find.assert_called_once_with('size-id')

    def test_missing_flavor_and_baremetal(self):
        self.find.return_value = None
        row = self.client.get(self.server_path,
                              headers=self.headers).json['server']
        self.assertEqual({'id': 'size-id', 'name': None}, row['flavor'])
        self.server.kind = 'baremetal'
        self.server.save()
        self.assertIsNone(self.client.get(self.server_path,
                          headers=self.headers).json['server']['flavor'])

    def test_no_saved_flavor_avoids_nova(self):
        self.server.flavor_id = None
        self.server.save()
        row = self.client.get(self.server_path,
                              headers=self.headers).json['server']
        self.assertIsNone(row['flavor'])
        self.find.assert_not_called()

    def test_empty_and_invalid_catalog(self):
        self.config.config(definitions='[]', group='plans')
        self.assertEqual({'flavors': []}, self.client.get(
            '/v1/flavors', headers=self.headers).json)
        self.find.assert_not_called()
        self.config.config(definitions='invalid', group='plans')
        self.client.get('/v1/flavors', headers=self.headers, status=500)

    def test_catalog_contents_and_mapping(self):
        response = self.client.get('/v1/flavors', headers=self.headers)
        self.assertEqual({'flavors': [dict(self.expected, plan='small',
                          roles=['worker', 'build'])]}, response.json)
        self.find.assert_called_once_with('size-id')

    def test_catalog_ambiguous_roles_and_missing_flavors(self):
        self.config.config(definitions=json.dumps([
            dict(name='one', flavor='size-id', kind='vm',
                 roles=['worker', 'build']),
            dict(name='two', flavor='size-id', kind='vm',
                 roles=['worker'])]), group='plans')
        rows = self.client.get('/v1/flavors', headers=self.headers).json
        self.assertEqual([dict(self.expected, plan='one', roles=['build'])],
                         rows['flavors'])
        self.find.return_value = None
        self.assertEqual({'flavors': []}, self.client.get(
            '/v1/flavors', headers=self.headers).json)

    def test_policy_version_and_errors(self):
        for role in ('reader', 'reader,member',
                     'reader,member,manager', 'admin'):
            self.client.get('/v1/flavors', headers=dict(
                self.headers, **{'X-Roles': role}), status=200)
        self.client.get('/v1/flavors', headers={}, status=401)
        self.client.get('/v1/flavors', headers=dict(
            self.headers, **{'X-Roles': 'unrelated'}), status=403)
        self.client.get('/v1/flavors', headers=dict(self.headers, **{
            'OpenStack-API-Version': 'app-hosting 1.26'}), status=404)
        self.client.get('/v1/flavors?extra=true', headers=self.headers,
                        status=400)
        self.client.post_json('/v1/flavors', {}, headers=self.headers,
                              status=405)
        self.client.get(self.server_path, headers=dict(
            self.headers, **{'X-Project-Id': 'other'}), status=404)
        self.find.side_effect = RuntimeError('private cloud failure')
        for path in ('/v1/flavors', self.server_path):
            response = self.client.get(path, headers=self.headers, status=503)
            self.assertNotIn('private cloud failure', response.text)
