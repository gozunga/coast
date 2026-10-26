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

"""Microversion 1.1 role selections before quota and RPC acceptance."""

import copy
import json
from unittest import mock

import fixtures
from openstack.image.v2 import image as sdk_image

from coast.api import microversions
from coast.api.v1 import stack_controllers
from coast.conductor import controller
from coast.db import models
from coast import objects
from coast.tests.unit import test_controllers


class RoleSelectionTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.config.config(volume_gb=1000, group='quotas')
        self.plans = [dict(name=role, kind='vm', roles=[role],
                           flavor='internal-' + role)
                      for role in ('control', 'worker', 'build')]
        self.catalog()
        self.config.config(image_owner_id='owner',
                           image_tag_controller='published',
                           image_tag_worker='published',
                           group='controller_worker')
        self.images = [sdk_image.Image(
            id=role + '-id', name=role + '-image', owner='owner',
            status='active', tags=['published'],
            properties={'coast_role': role})
            for role in ('controller', 'worker')]
        self.proxy = mock.Mock()
        self.proxy.find_flavor.return_value = None
        self.proxy.images.side_effect = lambda **kw: iter(self.images)
        self.useFixture(fixtures.MockPatchObject(
            controller.ControllerCloud, 'proxy', return_value=self.proxy))
        self.handler = self.useFixture(fixtures.MockPatchObject(
            stack_controllers.handlers.Handler, 'create_stack')).mock
        self.body = dict(name='selected', plan='control',
                         workers=[dict(kind='vm', plan='worker')])
        self.v11 = dict(self.headers, **{
            'OpenStack-API-Version': 'app-hosting 1.1'})

    def catalog(self):
        self.config.config(definitions=json.dumps(self.plans), group='plans')

    def create(self, body=None, status=202, headers=None):
        response = self.client.post_json(
            '/v1/stacks', {'stack': self.body if body is None else body},
            headers=self.v11 if headers is None else headers, status=status)
        if status >= 400:
            self.assert_fault(response, status)
        version = (headers or self.v11).get(
            'OpenStack-API-Version', 'app-hosting 1.0').split()[1]
        self.assert_version(response, version)
        return response

    def test_invalid_plans_at_every_microversion_have_no_side_effects(self):
        with self.db.engine.connect() as connection:
            usage_before = connection.execute(
                models.QuotaUsage.__table__.select()).all()
        for minor in range(int(microversions.MAX_VERSION.split('.')[1]) + 1):
            version = '1.%s' % minor
            headers = dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting ' + version})
            for role in ('control', 'worker', 'build'):
                body = copy.deepcopy(self.body)
                if role == 'control':
                    body['plan'] = 'nope'
                elif role == 'worker':
                    body['workers'].append(dict(kind='vm', plan='nope'))
                elif microversions.at_least(version, '1.1'):
                    body.update(build_server=True, build_server_plan='nope')
                else:
                    continue  # Version 1.0 has no explicit build plan.
                with mock.patch.object(stack_controllers, 'quota_hook') as q:
                    response = self.client.post_json(
                        '/v1/stacks', {'stack': body}, headers=headers,
                        status=400)
                self.assertEqual(
                    "Plan 'nope' does not exist. Run 'openstack coast plan "
                    "list' to see the available plans.",
                    response.json['faultstring'])
                q.assert_not_called()
                self.handler.assert_not_called()
                self.assertEqual([self.env.id], [row.id for row in
                                 objects.StackList.get_all(self.ctx)])
                with self.db.engine.connect() as connection:
                    self.assertEqual(usage_before, connection.execute(
                        models.QuotaUsage.__table__.select()).all())

    def test_valid_plans_at_oldest_and_newest_microversion(self):
        for version in ('1.0', microversions.MAX_VERSION):
            headers = dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting ' + version})
            body = dict(self.body, name='selected-' + version)
            result = self.client.post_json(
                '/v1/stacks', {'stack': body}, headers=headers, status=202)
            self.assertEqual('control', result.json['stack']['plan'])
            self.assertEqual(body, self.handler.call_args.args[2])

    def test_legacy_worker_kind_and_build_default_validation(self):
        headers = dict(self.headers, **{
            'OpenStack-API-Version':
                'app-hosting ' + microversions.MAX_VERSION})
        self.config.config(enable_baremetal=True, group='controller_worker')
        body = dict(self.body, workers=[dict(kind='baremetal', plan='worker')])
        response = self.client.post_json(
            '/v1/stacks', {'stack': body}, headers=headers, status=400)
        self.assertIn("Plan 'worker'", response.json['faultstring'])
        self.assertIn("kind 'baremetal'", response.json['faultstring'])
        self.assertIn("openstack coast plan list",
                      response.json['faultstring'])
        self.plans = self.plans[:2]
        self.catalog()
        response = self.client.post_json('/v1/stacks', {
            'stack': dict(self.body, build_server=True)}, headers=headers,
            status=400)
        self.assertEqual(
            "Choose a build plan. Run 'openstack coast plan list' to find "
            "a unique build-compatible VM plan.",
            response.json['faultstring'])
        self.handler.assert_not_called()

    def test_each_role_selects_plan_and_image_without_response_fields(self):
        self.config.config(build_servers=2, group='quotas')
        self.body.update(image='controller-image', build_server=True,
                         build_server_plan='build',
                         build_server_image='worker-image')
        self.body['workers'][0]['image'] = 'worker-id'
        for version in ('1.1', '1.28'):
            self.body['name'] = 'selected-' + version
            headers = dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting ' + version})
            result = self.create(headers=headers).json['stack']
            options = self.handler.call_args.args[2]
            self.assertEqual('controller-id', options['image'])
            self.assertEqual('worker-id', options['workers'][0]['image'])
            self.assertEqual('worker-id', options['build_server_image'])
            self.assertEqual('build', options['build_server_plan'])
            self.assertFalse(set(('image', 'build_server_plan',
                                  'build_server_image')) & set(result))
            row = objects.Stack.get_by_id(self.ctx, result['id'])
            self.assertEqual('project', row.project_id)
            self.assertEqual('control', row.plan)

    def test_latest_create_without_images_preserves_worker_rules(self):
        headers = dict(self.headers, **{
            'OpenStack-API-Version':
                'app-hosting ' + microversions.MAX_VERSION})
        for index, workers in enumerate(
                (None, [], [dict(kind='vm', plan='worker')])):
            body = dict(self.body, name='legacy-%s' % index)
            if workers is None:
                body.pop('workers')
            else:
                body['workers'] = workers
            self.create(body, headers=headers)
        self.plans.append(dict(name='metal', kind='baremetal',
                               roles=['worker'], flavor='internal-metal'))
        self.catalog()
        self.config.config(baremetal_servers=1, group='quotas')
        body = dict(self.body, workers=[dict(kind='baremetal', plan='metal')])
        self.create(body, headers=headers, status=400)
        self.config.config(enable_baremetal=True, group='controller_worker')
        self.create(body, headers=headers)

    def test_true_build_server_uses_unique_default_plan(self):
        self.body['build_server'] = True
        self.create()
        self.assertEqual('build', self.handler.call_args.args[2][
            'build_server_plan'])
        self.assertNotIn('image', self.handler.call_args.args[2])
        self.assertNotIn('build_server_image', self.handler.call_args.args[2])

    def test_multiple_build_plans_require_explicit_selection(self):
        self.plans.append(dict(name='other-build', kind='vm', roles=['build'],
                               flavor='other-internal'))
        self.catalog()
        self.body['build_server'] = True
        self.create(status=400)
        self.body['build_server_plan'] = 'other-build'
        self.create()

    def test_vm_workers_required_even_with_baremetal_enabled(self):
        self.config.config(enable_baremetal=True, group='controller_worker')
        for workers in (None, [], [dict(kind='baremetal', plan='worker')],
                        self.body['workers'] + [
                            dict(kind='baremetal', plan='worker')]):
            body = copy.deepcopy(self.body)
            if workers is None:
                body.pop('workers')
            else:
                body['workers'] = workers
            self.create(body, status=400)
        self.assertFalse(self.handler.called)

    def test_many_workers_with_build_server(self):
        # Q151: any worker count with a build server and per-worker images.
        self.body.update(build_server=True, build_server_plan='build')
        self.body['workers'] = [dict(kind='vm', plan='worker',
                                     image='worker-image')] * 3
        self.create()
        options = self.handler.call_args.args[2]
        self.assertEqual(3, len(options['workers']))
        self.assertEqual({'worker-id'},
                         {w['image'] for w in options['workers']})
        self.assertEqual('build', options['build_server_plan'])

    def test_each_worker_plan_is_validated(self):
        self.body['workers'] = [dict(kind='vm', plan='worker'),
                                dict(kind='vm', plan='control')]
        self.create(status=400)
        self.assertFalse(self.handler.called)

    def test_unknown_wrong_kind_and_wrong_role_plans(self):
        for target, key in ((self.body, 'plan'),
                            (self.body['workers'][0], 'plan'),
                            (self.body, 'build_server_plan')):
            self.body['build_server'] = True
            original = target.get(key)
            for name in ('missing', 'wrong-kind', 'wrong-role'):
                self.plans.extend([
                    dict(name='wrong-kind', kind='baremetal',
                         roles=['control', 'worker', 'build'],
                         flavor='hidden'),
                    dict(name='wrong-role', kind='vm', roles=[],
                         flavor='hidden')])
                self.catalog()
                target[key] = name
                self.create(status=400)
                self.plans = self.plans[:3]
            if original is None:
                target.pop(key)
            else:
                target[key] = original
        self.assertFalse(self.handler.called)

    def test_build_selections_require_boolean_true(self):
        for selection in (dict(build_server_plan='build'),
                          dict(build_server_image='worker-id'),
                          dict(build_server={'plan': 'build'})):
            self.create(dict(self.body, **selection), status=400)

    def test_disallowed_missing_and_ambiguous_images_are_bad_requests(self):
        for target, key, wrong in (
                (self.body, 'image', 'worker-id'),
                (self.body['workers'][0], 'image', 'controller-id'),
                (self.body, 'build_server_image', 'controller-id')):
            self.body['build_server'] = True
            for selection in (wrong, 'missing'):
                target[key] = selection
                self.create(status=400)
            target.pop(key)
        self.images.append(sdk_image.Image(
            id='duplicate', name='controller-image', owner='owner',
            status='active', tags=['published'],
            properties={'coast_role': 'controller'}))
        self.create(dict(self.body, image='controller-image'), status=400)
        self.assertFalse(self.handler.called)

    def test_invalid_catalog_does_not_disclose_flavors(self):
        self.plans.append(self.plans[0])
        self.catalog()
        response = self.create(status=500)
        self.assertNotIn('internal-control', response.text)

    def test_auth_and_quota_checks_remain_in_force(self):
        reader = dict(self.v11, **{'X-Roles': 'reader'})
        for headers, status in (({}, 401), (reader, 403)):
            self.client.post_json('/v1/stacks', {'stack': self.body},
                                  headers=headers, status=status)
        self.config.config(servers=1, group='quotas')
        self.create(status=403)
        self.assertFalse(self.handler.called)

    def test_version_1_0_rejects_new_fields_and_keeps_worker_rules(self):
        for selection in (dict(image='controller-id'),
                          dict(build_server_plan='build'),
                          dict(build_server_image='worker-id'),
                          dict(workers=[dict(kind='vm', plan='worker',
                                             image='worker-id')])):
            self.create(dict(self.body, **selection), status=400,
                        headers=self.headers)
        self.create(dict(self.body, workers=[]), headers=self.headers)
        self.create(dict(self.body, name='two-workers',
                         workers=self.body['workers'] * 2),
                    headers=self.headers)

    def test_invalid_selection_does_not_write_resources_or_hold_quota(self):
        self.body['image'] = 'missing'
        self.create(status=400)
        self.assertEqual([self.env.id], [row.id for row in
                         objects.StackList.get_all(self.ctx)])
        self.body['image'] = 'controller-id'
        self.config.config(environments=2, servers=2, volume_gb=110,
                           group='quotas')
        self.create()

    def test_new_fields_reject_null_empty_and_non_string_values(self):
        for target, key in ((self.body, 'image'),
                            (self.body['workers'][0], 'image'),
                            (self.body, 'build_server_image'),
                            (self.body, 'build_server_plan')):
            self.body['build_server'] = True
            for value in (None, '', 42, {}, 'a' * 256):
                target[key] = value
                self.create(status=400)
            target.pop(key)


class FlavorToPlanTest(RoleSelectionTest):
    """Q150 interim: Nova flavor names or IDs stand in for plan names."""

    def flavor(self, name):
        found = {'internal-control': 'ctl-id', 'internal-worker': 'wrk-id',
                 'internal-build': 'bld-id'}
        by_id = {value: key for key, value in found.items()}
        if name not in found and name not in by_id:
            return None
        flavor = mock.Mock(id=found.get(name, name))
        flavor.name = by_id.get(name, name)
        return flavor

    def setUp(self):
        super().setUp()
        self.proxy.find_flavor.side_effect = self.flavor

    def test_flavor_names_map_to_plans(self):
        self.body.update(plan='internal-control', build_server=True,
                         build_server_plan='internal-build')
        self.body['workers'][0]['plan'] = 'internal-worker'
        self.create()
        options = self.handler.call_args.args[2]
        self.assertEqual('control', options['plan'])
        self.assertEqual('worker', options['workers'][0]['plan'])
        self.assertEqual('build', options['build_server_plan'])

    def test_flavor_ids_map_to_plans_on_1_0(self):
        self.body.update(plan='ctl-id')
        self.body['workers'][0]['plan'] = 'wrk-id'
        self.create(headers=self.headers)
        options = self.handler.call_args.args[2]
        self.assertEqual('control', options['plan'])
        self.assertEqual('worker', options['workers'][0]['plan'])

    def test_flavor_with_wrong_role_is_rejected(self):
        self.body['plan'] = 'internal-worker'
        response = self.create(status=400)
        self.assertEqual(
            'Flavor internal-worker is not available for App engine server.',
            response.json['faultstring'])
        self.body.update(plan='control', build_server=True,
                         build_server_plan='internal-control')
        response = self.create(status=400)
        self.assertEqual(
            'Flavor internal-control is not available for build servers.',
            response.json['faultstring'])

    def test_flavor_with_wrong_role_is_rejected_on_1_0(self):
        self.body['plan'] = 'internal-worker'
        self.create(status=400, headers=self.headers)

    def test_unknown_flavor_is_rejected(self):
        self.body['plan'] = 'no-such-flavor'
        self.create(status=400)
