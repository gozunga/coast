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

import json
from pathlib import Path
import tempfile

from oslo_config import cfg
from oslo_policy import policy as policy_module

from coast.common import config
from coast.common import policy
from coast.tests.unit import test_api


class PlanControllerTest(test_api.APITest):
    def setUp(self):
        super().setUp()
        self.headers = {'X-User-Id': 'user', 'X-Project-Id': 'project',
                        'X-Roles': 'reader'}
        self.definitions = [
            {'name': 'z-worker', 'kind': 'baremetal', 'roles': ['worker'],
             'description': 'Dedicated worker', 'flavor': 'internal-z'},
            {'name': 'a-general', 'kind': 'vm',
             'roles': ['control', 'worker', 'build'],
             'description': None, 'flavor': 'internal-a'}]

    def test_empty_and_configured_projection_sorted_for_all_projects(self):
        result = self.client.get('/v1/plans', headers=self.headers)
        self.assertEqual({'plans': []}, result.json)
        self.assert_version(result)
        self.config.config(definitions=json.dumps(self.definitions),
                           group='plans')
        for role in ('reader', 'member', 'manager', 'admin', 'unrelated', ''):
            headers = dict(
                self.headers, **{'X-Roles': role, 'X-Project-Id': 'other'})
            result = self.client.get('/v1/plans', headers=headers)
            self.assertEqual({'plans': [
                {key: plan[key] for key in ('name', 'kind', 'roles',
                                            'description')}
                for plan in reversed(self.definitions)]}, result.json)
            self.assertNotIn('internal-', result.text)
            self.assert_version(result)

    def test_customer_plan_fields_hide_engine_names(self):
        plan = dict(self.definitions[0],
                    description='CoOlIfY VM with TRAEFIK ingress')
        self.config.config(definitions=json.dumps([plan]), group='plans')
        for role in ('reader', 'member', 'manager'):
            for version in ('1.0', '1.1', '1.25'):
                headers = dict(self.headers, **{
                    'X-Roles': role,
                    'OpenStack-API-Version': 'app-hosting ' + version})
                response = self.client.get('/v1/plans', headers=headers)
                self.assertNotRegex(response.text.lower(), 'coolify|traefik')
                self.assertIn('App engine server', response.text)
        response = self.client.get('/v1/plans', headers=dict(
            self.headers, **{'X-Roles': 'admin'}))
        self.assertEqual(plan['description'],
                         response.json['plans'][0]['description'])

    def test_microversion_1_1_shows_flavor(self):
        # Q150 interim: 1.1 adds the plan flavor; 1.0 still hides it.
        self.config.config(definitions=json.dumps(self.definitions),
                           group='plans')
        headers = dict(self.headers,
                       **{'OpenStack-API-Version': 'app-hosting 1.1'})
        result = self.client.get('/v1/plans', headers=headers)
        self.assertEqual(['internal-a', 'internal-z'],
                         [plan['flavor'] for plan in result.json['plans']])
        result = self.client.get('/v1/plans', headers=self.headers)
        self.assertNotIn('flavor', result.json['plans'][0])

    def test_auth_policy_and_microversion(self):
        result = self.client.get('/v1/plans', status=401)
        self.assert_fault(result, 401)
        enforcer = policy.get_enforcer()
        enforcer.load_rules()
        enforcer.set_rules(policy_module.Rules.from_dict({
            'coast:plan:get_all': 'role:manager'}), overwrite=False)
        result = self.client.get('/v1/plans', headers=self.headers, status=403)
        self.assert_fault(result, 403)
        headers = dict(self.headers, **{'X-Roles': 'manager'})
        self.client.get('/v1/plans', headers=headers)
        headers['OpenStack-API-Version'] = 'app-hosting 1.99'
        self.client.get('/v1/plans', headers=headers, status=406)

    def test_no_item_writes_or_query_parameters(self):
        for method in ('post', 'put', 'patch', 'delete'):
            result = getattr(self.client, method)(
                '/v1/plans', headers=self.headers, status=405)
            self.assert_fault(result, 405)
        self.client.get('/v1/plans/a-general', headers=self.headers,
                        status=404)
        for query in ('limit=1', 'marker=x', 'fields=flavor', 'fields=name',
                      'sort_key=name', 'project_id=other',
                      'all_projects=true'):
            result = self.client.get('/v1/plans?' + query,
                                     headers=self.headers, status=400)
            self.assert_fault(result, 400)

    def test_bad_operator_config_is_sanitized(self):
        invalid = ['{invalid', 'null', '{}', '[true]',
                   json.dumps(self.definitions * 2)]
        for patch in ({'kind': 'byo'}, {'roles': ['unknown']},
                      {'flavor': None}, {'name': ''},
                      {'roles': 'worker'}, {'unknown': 'internal-flavor'}):
            plan = dict(self.definitions[0], **patch)
            invalid.append(json.dumps([plan]))
        invalid.append(json.dumps([{'name': 'missing-fields'}]))
        for value in invalid:
            self.config.config(definitions=value, group='plans')
            result = self.client.get('/v1/plans', headers=self.headers,
                                     status=500)
            self.assert_fault(result, 500)
            self.assertNotIn('internal-', result.text)

    def test_description_can_be_omitted(self):
        plan = dict(self.definitions[0])
        del plan['description']
        self.config.config(definitions=json.dumps([plan]), group='plans')
        result = self.client.get('/v1/plans', headers=self.headers)
        self.assertIsNone(result.json['plans'][0]['description'])

    def test_definitions_load_from_operator_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coast.conf'
            definitions = json.dumps(self.definitions)
            path.write_text('[plans]\ndefinitions = ' + definitions + '\n')
            conf = cfg.ConfigOpts()
            self.addCleanup(conf.reset)
            config.parse_args(['--config-file', str(path)], conf=conf,
                              default_config_files=[])
            self.assertEqual(self.definitions,
                             json.loads(conf.plans.definitions))
            self.config.config(definitions=conf.plans.definitions,
                               group='plans')
            result = self.client.get('/v1/plans', headers=self.headers)
            self.assertEqual(['a-general', 'z-worker'],
                             [plan['name'] for plan in result.json['plans']])
