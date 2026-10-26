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

from unittest import mock

from oslo_policy import policy as policy_module

from coast.common import policy
from coast.common import public_text
from coast.common import states
from coast import objects
from coast.tests import app_api


class TemplateControllerTest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.stack.driver = 'coolify_v4'
        self.stack.engine_version = '4.4.3'
        self.stack.save()
        objects.Volume(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='docker', size_gb=100,
            mount_path='/var/lib/docker', purpose='docker',
            status=states.AVAILABLE).create()
        self.list_path = '/v1/templates?stack_id=' + self.stack.id
        self.item_path = ('/v1/templates/wordpress-with-mariadb?stack_id=' +
                          self.stack.id)

    def test_socket_and_host_network_templates_are_visible(self):
        result = self.client.get(self.list_path, headers=self.headers)
        ids = {item['id'] for item in result.json['templates']}
        self.assertIn('dozzle', ids)
        for name in ('beszel', 'beszel-agent'):
            self.assertIn(name, ids)
            self.client.get('/v1/templates/' + name + '?stack_id=' +
                            self.stack.id, headers=self.headers)

    def test_host_bind_templates_are_visible(self):
        result = self.client.get(self.list_path, headers=self.headers)
        self.assertIn('minecraft',
                      [item['id'] for item in result.json['templates']])
        self.client.get('/v1/templates/minecraft?stack_id=' + self.stack.id,
                        headers=self.headers)

    def test_reads_for_each_project_role(self):
        for role in ('reader', 'member', 'manager', 'admin'):
            headers = dict(self.headers, **{'X-Roles': role})
            result = self.client.get(self.list_path, headers=headers)
            self.assert_version(result)
            self.assertIn('wordpress-with-mariadb',
                          [x['id'] for x in result.json['templates']])
            item = self.client.get(self.item_path, headers=headers)
            self.assertEqual('wordpress-with-mariadb',
                             item.json['template']['id'])
            self.assertEqual({'id', 'name', 'description', 'logo', 'category',
                              'tags', 'required_env', 'ports'},
                             set(item.json['template']))

    def test_display_fields_never_name_engine(self):
        # No engine names and no em dashes in customer text. Upstream
        # ships next-image-transformation with an engine-named logo file;
        # the catalog drops it at generation.
        display = ('name', 'description', 'logo', 'category', 'tags')
        item_path = ('/v1/templates/next-image-transformation?stack_id=' +
                     self.stack.id)
        for role in ('reader', 'member', 'admin'):
            headers = dict(self.headers, **{'X-Roles': role})
            listed = self.client.get(self.list_path, headers=headers)
            for item in listed.json['templates']:
                shown = str([item[field] for field in display])
                self.assertFalse(public_text.contains_engine_name(shown),
                                 item)
                self.assertNotIn('\u2014', shown, item)
            item = self.client.get(item_path, headers=headers)
            self.assertEqual('', item.json['template']['logo'])
            self.assertFalse(public_text.contains_engine_name(item.body))

    def test_auth_roles_projects_and_microversion(self):
        for path in (self.list_path, self.item_path):
            self.client.get(path, status=401)
            self.client.get(path, headers=dict(
                self.headers, **{'X-Roles': 'unrelated'}), status=403)
            for role in ('reader', 'admin'):
                self.client.get(path, headers=dict(self.headers, **{
                    'X-Project-Id': 'other', 'X-Roles': role}), status=404)
            self.client.get(path, headers=dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting 1.99'}), status=406)

    def test_template_list_includes_catalog_ports(self):
        result = self.client.get(self.list_path, headers=self.headers)
        templates = result.json['templates']
        for item in templates:
            self.assertIn('ports', item)
        kuma = next(item for item in templates if item['id'] == 'uptime-kuma')
        self.assertEqual([3001], kuma['ports'])

    def test_missing_stack_bad_query_unknown_id_and_methods(self):
        for path in ('/v1/templates', '/v1/templates/wordpress',
                     '/v1/templates?stack_id=', self.list_path + '&limit=1'):
            self.client.get(path, headers=self.headers, status=400)
        self.client.get('/v1/templates?stack_id=absent',
                        headers=self.headers, status=404)
        self.client.get('/v1/templates/absent?stack_id=' + self.stack.id,
                        headers=self.headers, status=404)
        for path in (self.list_path, self.item_path):
            for method in ('post', 'put', 'patch', 'delete'):
                getattr(self.client, method)(path, headers=self.headers,
                                             status=405)

    def test_get_policy_hides_items_and_prevents_create(self):
        enforcer = policy.get_enforcer()
        enforcer.load_rules()
        enforcer.set_rules(policy_module.Rules.from_dict({
            'coast:template:get': '!'}), overwrite=False)
        self.assertEqual([], self.client.get(
            self.list_path, headers=self.headers).json['templates'])
        self.client.get(self.item_path, headers=self.headers, status=403)
        self.client.post_json('/v1/services', self.template_body(),
                              headers=self.headers, status=403)

    def test_conditional_list(self):
        first = self.client.get(self.list_path, headers=self.headers)
        tag = first.headers['ETag']
        self.assertEqual('private, no-cache', first.headers['Cache-Control'])
        for value in (tag, tag[2:], '"other", ' + tag, '*'):
            response = self.client.get(self.list_path, headers=dict(
                self.headers, **{'If-None-Match': value}), status=304)
            self.assertEqual(b'', response.body)
            self.assertEqual(tag, response.headers['ETag'])
        response = self.client.get(self.list_path, headers=dict(
            self.headers, **{'If-None-Match': '"unmatched"'}))
        self.assertEqual(first.json, response.json)
        self.client.get(self.list_path, headers=dict(self.headers, **{
            'If-None-Match': tag, 'X-Project-Id': 'other'}), status=404)
        self.client.get(self.list_path, headers=dict(self.headers, **{
            'If-None-Match': tag, 'X-Roles': 'unrelated'}), status=403)

    def template_body(self, source=None):
        body = self.create_body()
        body['service']['source'] = source or {
            'type': 'template', 'template_id': 'wordpress-with-mariadb'}
        return body

    def test_template_catalog_port_precedes_health_validation(self):
        for index, ports in enumerate((None, '', '  ', '8080')):
            body = self.template_body('template:uptime-kuma')
            body['service'].update(name='kuma-%s' % index, ports=ports,
                                   health_check='http')
            result = self.request('post', '/v1/services', body,
                                  version='1.4', status=202).json['service']
            expected = ports or '3001'
            if not expected.strip():
                expected = '3001'
            self.assertEqual(expected, result['ports'])
            self.assertEqual(int(expected), result['health_check_port'])
            self.assertIsNotNone(result['default_url'])

    def test_template_health_port_defaults_when_omitted_or_null(self):
        for index, options in enumerate((
                {}, {'ports': '8080,9090'}, {'health_check_port': None},
                {'ports': '8080,9090', 'health_check_port': None})):
            body = self.template_body('template:uptime-kuma')
            body['service'].update(name='health-kuma-%s' % index,
                                   health_check='http', **options)
            created = self.request('post', '/v1/services', body,
                                   version='1.4', status=202).json['service']
            expected_ports = options.get('ports', '3001')
            expected_port = int(expected_ports.split(',')[0])
            shown = self.request('get', '/v1/services/' + created['id'],
                                 version='1.4').json['service']
            for service in (created, shown):
                self.assertEqual(expected_ports, service['ports'])
                self.assertEqual(expected_port, service['health_check_port'])
            stored = objects.Service.get_by_id(self.ctx, created['id'])
            self.assertIsNone(stored.health_check_port)

    def test_template_without_catalog_ports_rejects_http(self):
        body = self.template_body('template:uptime-kuma')
        with mock.patch(
                'coast.api.v1.template_controllers.get_template',
                return_value={'ports': []}):
            created = self.request('post', '/v1/services', body,
                                   status=202).json['service']
            self.assertEqual('', created['ports'])
            body['service'].update(name='http-kuma', health_check='http')
            self.request('post', '/v1/services', body,
                         version='1.4', status=400)

    def test_template_create_persists_template_id(self):
        sources = (
            'template:wordpress-with-mariadb',
            {'type': 'template',
             'template_id': 'wordpress-with-mariadb'})
        for index, source in enumerate(sources):
            body = self.template_body(source)
            body['service']['name'] = 'tpl%s' % index
            response = self.request('post', '/v1/services', body, status=202)
            row = objects.Service.get_by_id(
                self.ctx, response.json['service']['id'])
            self.assertEqual('wordpress-with-mariadb', row.template_id)
            self.assertEqual(self.environment.id, row.environment_id)
            for item in (response.json['service'], self.request(
                    'get', '/v1/services/' + row.id).json['service']):
                self.assertEqual(self.application.id, item['appgroup_id'])
                self.assertEqual(self.environment.id, item['environment_id'])
                self.assertEqual(row.template_id, item['template_id'])
            stored = objects.Source.get_by_id(self.ctx, row.source_id)
            self.assertEqual('template', stored.type)
            self.assertIsNone(stored.repo_url)

    def test_template_create_env_build_time(self):
        for version in ('1.0', '1.6'):
            body = self.template_body()
            body['service']['name'] = 'env-' + version.replace('.', '-')
            body['service']['env'] = {'KEY': {'value': 'runtime'}}
            result = self.request('post', '/v1/services', body,
                                  status=202, version=version)
            rows = objects.SecretList.get_all(self.ctx, filters={
                'service_id': result.json['service']['id']})
            self.assertFalse(rows[0].build_time)
            body['service']['name'] += '-rejected'
            body['service']['env']['KEY']['build_time'] = True
            before = len(objects.ServiceList.get_all(self.ctx))
            self.request('post', '/v1/services', body,
                         status=400, version=version)
            self.assertEqual(
                before, len(objects.ServiceList.get_all(self.ctx)))

    def test_invalid_template_sources(self):
        for source in ('template:', 'template:../wordpress',
                       {'type': 'template'},
                       {'type': 'template',
                        'template_id': 'wordpress-with-mariadb',
                        'repo_url': 'https://example.com/secret'}):
            self.client.post_json('/v1/services', self.template_body(source),
                                  headers=self.headers, status=400)
        self.client.post_json('/v1/services', self.template_body({
            'type': 'template', 'template_id': 'absent'}),
            headers=self.headers, status=404)
        self.client.post_json('/v1/services', self.template_body(),
                              headers=dict(self.headers, **{
                                  'X-Roles': 'reader'}), status=403)

    def test_version_matching_and_unsupported_driver(self):
        self.stack.engine_version = '4.4.4'
        self.stack.save()
        for path in (self.list_path, self.item_path):
            self.client.get(path, headers=self.headers, status=409)
        self.stack.driver = 'noop'
        self.stack.engine_version = 'test'
        self.stack.save()
        self.assertEqual([], self.client.get(
            self.list_path, headers=self.headers).json['templates'])
        self.client.get(self.item_path, headers=self.headers, status=404)

    def test_http_port_list_show_and_older_versions(self):
        for version in ('1.0', '1.6', '1.7'):
            result = self.request('get', self.list_path, version=version)
            for item in result.json['templates']:
                self.assertEqual(version == '1.7', 'http_port' in item)
            for name, expected in (('uptime-kuma', 3001), ('n8n', 5678),
                                   ('wikijs', 3000),
                                   ('terraria-server', None)):
                path = '/v1/templates/' + name + '?stack_id=' + self.stack.id
                item = self.request('get', path, version=version)
                if version == '1.7':
                    self.assertEqual(expected,
                                     item.json['template']['http_port'])
                else:
                    self.assertNotIn('http_port', item.json['template'])

    def test_template_http_health_defaults(self):
        cases = (
            ('uptime-kuma', {}, 'http', 3001, '/', '3001'),
            ('wikijs', {}, 'http', 3000, '/', '3000'),
            ('terraria-server', {}, 'none', None, None, '7777'),
            ('uptime-kuma', {'health_check': 'none'},
             'none', None, None, '3001'),
            ('uptime-kuma', {'ports': '8080', 'health_check_port': 9090,
                             'health_check_path': '/ready'},
             'http', 9090, '/ready', '8080'),
            ('uptime-kuma', {'healthcheck_path': '/status'},
             'http', 3001, '/status', '3001'),
        )
        for index, case in enumerate(cases):
            name, options, mode, port, path, ports = case
            body = self.template_body('template:' + name)
            body['service'].pop('ports', None)
            body['service'].update(name='defaults-%s' % index, **options)
            created = self.request('post', '/v1/services', body,
                                   version='1.7', status=202).json['service']
            shown = self.request('get', '/v1/services/' + created['id'],
                                 version='1.7').json['service']
            for item in (created, shown):
                self.assertEqual(mode, item['health_check'])
                self.assertEqual(port or int(ports.split(',')[0]),
                                 item['health_check_port'])
                self.assertEqual(path, item['healthcheck_path'])
                self.assertEqual(ports, item['ports'])
            row = objects.Service.get_by_id(self.ctx, created['id'])
            self.assertEqual(port, row.health_check_port)
            self.assertEqual(path, row.healthcheck_path)

    def test_older_template_create_retains_no_health_default(self):
        body = self.template_body('template:wikijs')
        body['service'].pop('ports', None)
        created = self.request('post', '/v1/services', body,
                               version='1.6', status=202).json['service']
        self.assertEqual('80', created['ports'])
        self.assertEqual('none', created['health_check'])
