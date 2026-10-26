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

"""Regression coverage for actionable collection query errors over WSGI."""

from coast.tests import app_api


class RequestValidationTest(app_api.AppAPITest):
    def assert_query_error(self, path, query, *details, version='1.27'):
        response = self.request('get', path + '?' + query,
                                status=400, version=version)
        message = response.json['faultstring']
        for detail in details:
            self.assertIn(detail, message)

    def test_service_numeric_and_boolean_filters(self):
        for path, key, requirement in (
                ('/v1/services', 'replicas', 'an integer'),
                ('/v1/services', 'memory_limit_mb', 'an integer'),
                ('/v1/services', 'cpu_limit', 'a finite number'),
                (self.path + '/deployments', 'number', 'an integer'),
                ('/v1/services', 'auto_deploy', 'false, true'),
                (self.path + '/domains', 'auto_dns', 'false, true')):
            with self.subTest(path=path, key=key):
                self.assert_query_error(path, key + '=bogus',
                                        key, "'bogus'", requirement)
        for value in ('nan', 'inf', '-inf'):
            self.assert_query_error('/v1/services', 'cpu_limit=' + value,
                                    'cpu_limit', "'%s'" % value,
                                    'a finite number')
        for path, keys in ((self.path + '/deployments',
                            ('started_at', 'finished_at')),
                           (self.path + '/domains', ('verified_at',))):
            for key in keys:
                self.assert_query_error(path, key + '=yesterday',
                                        key, "'yesterday'",
                                        'an ISO 8601 timestamp')
        self.assert_query_error('/v1/services', 'kind=bogus',
                                "'bogus'", 'app, service')

    def test_endpoints_without_query_parameters(self):
        for path in ('/v1/plans', '/v1/flavors', '/v1/quotas/project',
                     self.path + '/backup_policy', self.path + '/backups',
                     self.path + '/file_mounts'):
            with self.subTest(path=path):
                self.assert_query_error(
                    path, 'unknown=bogus', "'unknown'", "'bogus'",
                    'This endpoint accepts no query parameters.')

    def test_dns_and_template_parameters(self):
        self.assert_query_error('/v1/dns/zones', 'unknown=bogus',
                                "'unknown'", "'bogus'", 'Use one of: trust_id')
        for path in ('/v1/templates', '/v1/templates/wordpress-with-mariadb'):
            self.assert_query_error(path, 'unknown=bogus',
                                    "'unknown'", "'bogus'",
                                    'Use one of: all_projects, project_id, '
                                    'stack_id')
            self.assert_query_error(path, 'stack_id=',
                                    "stack_id ''", 'Specify a stack ID')
            response = self.request('get', path, status=400)
            self.assertIn("stack_id ''", response.json['faultstring'])
            self.assertIn('Specify a stack ID', response.json['faultstring'])

    def test_collection_parameter_formats_and_choices(self):
        for path in ('/v1/stacks', '/v1/services',
                     '/v1/stacks/' + self.stack.id + '/appgroups',
                     '/v1/appgroups/' + self.application.id + '/environments',
                     '/v1/stacks/' + self.stack.id + '/storages'):
            for query, value, requirement in (
                    ('unknown=bogus', 'bogus', 'Use one of:'),
                    ('all_projects=maybe', 'maybe', 'false, true'),
                    ('limit=bogus', 'bogus',
                     'an integer greater than or equal to 0'),
                    ('limit=-1', '-1',
                     'an integer greater than or equal to 0'),
                    ('fields=bogus', 'bogus', 'name'),
                    ('sort_key=bogus', 'bogus', 'name'),
                    ('sort_dir=sideways', 'sideways', 'asc, desc')):
                with self.subTest(path=path, query=query):
                    self.assert_query_error(path, query,
                                            "'%s'" % value, requirement)
            if not path.endswith('/storages'):
                for key in ('created_at', 'updated_at'):
                    self.assert_query_error(path, key + '=yesterday',
                                            key, "'yesterday'",
                                            'an ISO 8601 timestamp')

    def test_item_fields_and_inaccessible_detail_fields(self):
        for path in ('/v1/stacks/' + self.stack.id,
                     '/v1/appgroups/' + self.application.id,
                     '/v1/environments/' + self.environment.id,
                     self.path):
            self.assert_query_error(path, 'fields=bogus',
                                    "'bogus'", 'name')
        self.assert_query_error('/v1/stacks/' + self.stack.id,
                                'all_projects=maybe',
                                "'maybe'", 'false, true')
        self.assert_query_error('/v1/stacks', 'fields=engine_ref',
                                "'engine_ref'", 'Choose accessible fields:',
                                'name')

    def test_stack_event_query_errors(self):
        path = '/v1/stacks/' + self.stack.id + '/events'
        for query, value, requirement in (
                ('unknown=bogus', 'bogus', 'Use one of:'),
                ('limit=bogus', 'bogus',
                 'an integer greater than or equal to 1'),
                ('limit=0', '0', 'an integer greater than or equal to 1'),
                ('resource_type=bogus', 'bogus', 'stack'),
                ('outcome=bogus', 'bogus', 'failure, start, success')):
            self.assert_query_error(path, query, "'%s'" % value, requirement)
