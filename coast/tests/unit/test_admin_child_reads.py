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

import datetime
from unittest import mock

from coast.common import engine_versions
from coast.common import states
from coast.db import api as db_api
from coast import objects
from coast.tests import app_api


class AdminChildReadsTest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.stack.driver = 'coolify_v4'
        self.stack.engine_version = engine_versions.DEFAULT
        self.stack.save()
        self.storage = objects.Storage(
            context=self.ctx, stack_id=self.stack.id, name='uploads',
            size_gb=1, origin='api', status=states.AVAILABLE)
        self.storage.create()
        self.export = objects.Export(
            context=self.ctx, stack_id=self.stack.id, backend='swift',
            volume_ids=[], status=states.PENDING)
        self.export.create()
        self.admin = dict(self.headers, **{
            'X-Project-Id': 'other', 'X-Roles': 'admin',
            'OpenStack-API-Version': 'app-hosting 1.19'})
        self.member = dict(self.admin, **{'X-Roles': 'member'})

    def check_reads(self, collection, item, key, identifier, body):
        for query in ('all_projects=true', 'project_id=project'):
            separator = '&' if '?' in collection else '?'
            path = collection + separator + query
            result = self.client.get(path, headers=self.admin).json
            self.assertIn(identifier, [row['id'] for row in result[key]])
            self.client.get(path, headers=self.member, status=403)
            separator = '&' if '?' in item else '?'
            path = item + separator + query
            result = self.client.get(path, headers=self.admin).json
            self.assertEqual(identifier, result[key[:-1]]['id'])
            self.client.get(path, headers=self.member, status=403)
            for headers in (self.admin, self.member):
                self.client.post_json(
                    collection + ('&' if '?' in collection else '?') + query,
                    body, headers=headers, status=404)
                self.client.delete(path, headers=headers, status=404)
        self.client.get(collection, headers=self.admin, status=404)
        self.client.get(item, headers=self.admin, status=404)

    def test_appgroups(self):
        self.check_reads(
            '/v1/stacks/' + self.stack.id + '/appgroups',
            '/v1/appgroups/' + self.application.id, 'appgroups',
            self.application.id, {'appgroup': {'name': 'new'}})

    def test_environments(self):
        self.check_reads(
            '/v1/appgroups/' + self.application.id + '/environments',
            '/v1/environments/' + self.environment.id, 'environments',
            self.environment.id, {'environment': {'name': 'new'}})

    def test_storage(self):
        for version in ('1.10', '1.16', '1.19'):
            self.admin['OpenStack-API-Version'] = 'app-hosting ' + version
            self.member['OpenStack-API-Version'] = 'app-hosting ' + version
            path = '/v1/stacks/' + self.stack.id + '/storages'
            self.check_reads(path, path + '/' + self.storage.id, 'storages',
                             self.storage.id, {'storage': {'name': 'new'}})

    def test_exports(self):
        path = '/v1/stacks/' + self.stack.id + '/exports'
        self.check_reads(path, path + '/' + self.export.id, 'exports',
                         self.export.id, {'export': {}})

    def test_templates(self):
        self.check_reads(
            '/v1/templates?stack_id=' + self.stack.id,
            '/v1/templates/wordpress-with-mariadb?stack_id=' + self.stack.id,
            'templates', 'wordpress-with-mariadb', {})

    def test_ingress_rules(self):
        rule = db_api.ingress_rules.create(self.ctx, dict(
            stack_id=self.stack.id, protocol='tcp', port_range_min=8080,
            port_range_max=8080, source='192.0.2.0/24', description='app'))
        path = '/v1/stacks/' + self.stack.id + '/ingress_rules'
        for headers in (self.admin, self.member):
            headers['OpenStack-API-Version'] = 'app-hosting 1.25'
        for query in ('all_projects=true', 'project_id=project'):
            result = self.client.get(path + '?' + query,
                                     headers=self.admin).json
            self.assertIn(rule.id, [row['id']
                                    for row in result['ingress_rules']])
            self.client.get(path + '?' + query, headers=self.member,
                            status=403)
            self.client.post_json(path + '?' + query, {},
                                  headers=self.admin, status=404)

    def test_active_export_urls_use_owner_project(self):
        for status in (states.SNAPSHOTTING, states.UPLOADING, states.ACTIVE):
            self.export.status = status
            self.export.save()
        self.export.object_expires_at = (
            datetime.datetime.now(datetime.timezone.utc) +
            datetime.timedelta(hours=1))
        self.export.save()
        path = '/v1/stacks/' + self.stack.id + '/exports'
        with mock.patch('coast.conductor.rpcapi.ConductorAPI.'
                        'get_export_urls', return_value=[]) as urls:
            self.client.get(path + '?all_projects=true', headers=self.admin)
            self.assertEqual(self.ctx.project_id,
                             urls.call_args.args[0].project_id)
