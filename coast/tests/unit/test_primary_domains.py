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

"""Primary address preferences, guards and version boundaries."""

from unittest import mock

from coast.common import exception
from coast.common import states
from coast.db import api as db_api
from coast import objects
from coast.tests import app_api


class PrimaryDomainTest(app_api.AppAPITest):
    def test_task_holder_metadata_and_version_boundary(self):
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        for version in ('1.0', '1.23'):
            view = self.request('get', self.path,
                                version=version).json['service']
            self.assertNotIn('task_owner', view)
            self.assertNotIn('task_started_at', view)
        with mock.patch.object(db_api.LOG, 'debug') as logged:
            service.acquire_task(states.RECONCILING, owner='domain_check')
            started = service.task_started_at
            self.assertIsNotNone(started)
            self.assertEqual('domain_check', service.task_owner)
            db_api.cas_task_state(self.ctx, service.repository.model,
                                  service.id, states.RECONCILING,
                                  expected=states.RECONCILING,
                                  owner='settings_sync')
            current = objects.Service.get_by_id(self.ctx, service.id)
            self.assertEqual(started, current.task_started_at)
            self.assertEqual('domain_check', current.task_owner)
            self.assertRaises(exception.Conflict, current.acquire_task,
                              states.RECONCILING)
            view = self.request('get', self.path,
                                version='1.24').json['service']
            self.assertEqual('domain_check', view['task_owner'])
            self.assertIsNotNone(view['task_started_at'])
            selected = self.request(
                'get', self.path + '?fields=task_owner,task_started_at',
                version='1.24').json['service']
            self.assertEqual({'task_owner', 'task_started_at', 'links'},
                             set(selected))
            self.request('get', self.path + '?fields=task_owner',
                         version='1.23', status=400)
            response = self.request('delete', self.domain_path, status=409)
            self.assertIn('Domain check in progress since ' +
                          started.strftime('%H:%M') + ' UTC', response.text)
            service.release_task(states.RECONCILING)
            self.assertGreaterEqual(logged.call_count, 2)
            release = logged.call_args.args
            self.assertIn(service.id, release)
            self.assertIn('domain_check', release)
        view = self.request('get', self.path, version='1.24').json['service']
        self.assertIsNone(view['task_owner'])
        self.assertIsNone(view['task_started_at'])
        self.request('patch', self.path, {'service': {
            'task_owner': 'domain_check'}}, version='1.24', status=400)

    def test_task_owner_labels_and_legacy_busy_messages(self):
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        for owner, label in (('domain_check', 'Domain check'),
                             ('domain_change', 'Domain change'),
                             ('settings_sync', 'Settings sync'),
                             ('restart', 'Restart')):
            service.acquire_task(states.RECONCILING, owner=owner)
            self.assertIn(label + ' in progress since', db_api._busy_message(
                service.repository.model, service.task_state, service))
            service.release_task(states.RECONCILING)
        for task, phrase in ((states.DEPLOYING, 'A deployment'),
                             (states.ROLLING_BACK, 'A rollback'),
                             (states.DELETING, 'This app is being deleted')):
            self.assertIn(phrase, db_api._busy_message(
                service.repository.model, task, service))
        self.assertIn('Another operation', db_api._busy_message(
            service.repository.model, states.RECONCILING, service))

    def test_stack_task_metadata_default_and_object_compatibility(self):
        stack = objects.Stack.get_by_id(self.ctx, self.service.stack_id)
        stack.acquire_task(states.RECONCILING)
        self.assertEqual(states.RECONCILING, stack.task_owner)
        self.assertIsNotNone(stack.task_started_at)
        for resource, version in ((stack, '1.5'), (self.service, '1.14')):
            old = resource.obj_to_primitive(target_version=version)
            self.assertNotIn('task_owner', old['coast_object.data'])
            self.assertNotIn('task_started_at', old['coast_object.data'])
        stack.release_task(states.RECONCILING)
        self.assertIsNone(stack.task_owner)
        self.assertIsNone(stack.task_started_at)

    def patch_domain(self, path=None, **values):
        return self.request('patch', path or self.domain_path,
                            {'domain': values}, version='1.23',
                            status=202).json['domain']

    def patch_service(self, enabled, status=200):
        return self.request('patch', self.path, {'service': {
            'default_domain_enabled': enabled}}, version='1.23',
            status=status)

    def view(self):
        return self.request('get', self.path, version='1.23').json['service']

    def test_defaults_and_primary_url_scheme_and_fallback(self):
        view = self.view()
        self.assertTrue(view['default_domain_enabled'])
        self.assertEqual(view['default_url'], view['primary_url'])
        result = self.patch_domain(primary=True, tls='none')
        self.assertTrue(result['primary'])
        self.assertEqual('http://www.example.com', self.view()['primary_url'])
        self.unlock()
        self.patch_domain(tls='letsencrypt')
        self.assertEqual('https://www.example.com', self.view()['primary_url'])
        self.unlock()
        self.patch_domain(primary=False)
        self.assertEqual(view['default_url'], self.view()['primary_url'])

    def test_disable_requires_primary_and_removes_default_dns_view(self):
        result = self.patch_service(False, status=409)
        self.assertIn('Pick a main domain before turning off the default '
                      'address.', result.text)
        self.patch_domain(primary=True)
        self.unlock()
        view = self.patch_service(False).json['service']
        self.assertIsNone(view['default_url'])
        self.assertEqual('https://www.example.com', view['primary_url'])
        self.assertFalse(any(r['type'] == 'CNAME' for r in
                             view['dns_records']))
        domain = self.request('get', self.domain_path,
                              version='1.23').json['domain']
        self.assertFalse(any(r['purpose'] == 'routing-alternative'
                             for r in domain['dns_records']))
        self.assertIsNotNone(self.patch_service(True).json[
            'service']['default_url'])

    def test_delete_and_unprimary_guard_even_for_old_clients(self):
        self.patch_domain(primary=True)
        self.unlock()
        self.patch_service(False)
        for method, body, version in (
                ('delete', None, '1.0'),
                ('patch', {'domain': {'primary': False}}, '1.23')):
            response = self.request(method, self.domain_path, body,
                                    version=version, status=409)
            self.assertIn('Turn the default address back on or pick another '
                          'main domain first.', response.text)
        self.assertTrue(objects.Domain.get_by_id(
            self.ctx, self.domain.id).primary)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.patch_service(True)
        self.patch_domain(primary=False)
        self.unlock()
        self.request('delete', self.domain_path, status=202, version='1.23')

    def test_disable_cannot_interleave_with_pending_primary_delete(self):
        self.patch_domain(primary=True)
        self.unlock()
        self.request('delete', self.domain_path, status=202, version='1.23')
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.patch_service(False, status=409)
        self.assertTrue(self.view()['default_domain_enabled'])
        # Finish the queued deletion, then retry the settings change.
        objects.Domain.get_by_id(self.ctx, self.domain.id).destroy()
        self.unlock()
        self.patch_service(False, status=409)
        self.assertIsNotNone(self.view()['default_url'])

    def test_primary_switch_is_atomic_and_scoped_to_service(self):
        self.patch_domain(primary=True)
        self.unlock()
        self.patch_service(False)
        other = self.request('post', self.path + '/domains', {'domain': {
            'fqdn': 'shop.example.com', 'primary': True}},
            version='1.23', status=201).json['domain']
        self.assertTrue(other['primary'])
        self.assertFalse(objects.Domain.get_by_id(
            self.ctx, self.domain.id).primary)
        self.assertEqual('https://shop.example.com',
                         self.view()['primary_url'])
        self.unlock()
        self.request('delete', self.domain_path, status=202)

    def test_primary_change_rolls_back_on_save_failure(self):
        self.patch_domain(primary=True)
        self.unlock()
        other = self.request('post', self.path + '/domains', {'domain': {
            'fqdn': 'shop.example.com'}}, version='1.23',
            status=201).json['domain']
        self.unlock()
        original = objects.Domain.save

        def fail_target(row, *args, **kwargs):
            if row.id == other['id']:
                raise RuntimeError('save failed')
            return original(row, *args, **kwargs)

        with mock.patch.object(objects.Domain, 'save', fail_target):
            self.request('patch', self.path + '/domains/' + other['id'],
                         {'domain': {'primary': True}},
                         version='1.23', status=500)
        self.assertTrue(objects.Domain.get_by_id(
            self.ctx, self.domain.id).primary)
        self.assertFalse(objects.Domain.get_by_id(
            self.ctx, other['id']).primary)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)

    def test_other_service_domain_and_other_project_are_hidden(self):
        created = self.request('post', '/v1/services', self.create_body(),
                               status=202).json['service']
        wrong = '/v1/services/' + created['id'] + '/domains/' + self.domain.id
        self.request('patch', wrong, {'domain': {'primary': True}},
                     version='1.23', status=404)
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        self.request('patch', self.domain_path, {'domain': {'primary': True}},
                     headers=foreign, version='1.23', status=404)
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        self.request('patch', self.domain_path, {'domain': {'primary': True}},
                     headers=reader, version='1.23', status=403)
        self.patch_domain(primary=True)
        self.unlock()
        self.request('patch', '/v1/services/' + created['id'], {'service': {
            'default_domain_enabled': False}}, version='1.23', status=409)

    def test_version_gating_and_boolean_validation(self):
        for version in ('1.0', '1.2', '1.21', '1.22'):
            self.request('patch', self.domain_path,
                         {'domain': {'primary': True}},
                         version=version, status=406)
            self.request('post', self.path + '/domains', {'domain': {
                'fqdn': 'new.example.com', 'primary': True}},
                version=version, status=406)
            self.request('patch', self.path, {'service': {
                'default_domain_enabled': False}}, version=version, status=406)
            view = self.request('get', self.path,
                                version=version).json['service']
            self.assertNotIn('primary_url', view)
            self.assertNotIn('default_domain_enabled', view)
            self.assertNotIn('primary', self.request(
                'get', self.domain_path, version=version).json['domain'])
        for key, path, envelope in (
                ('primary', self.domain_path, 'domain'),
                ('default_domain_enabled', self.path, 'service')):
            for value in (None, 'true', 1):
                self.request('patch', path, {envelope: {key: value}},
                             version='1.23', status=400)
        self.assertIn('primary_url', self.request(
            'get', self.path + '?fields=primary_url',
            version='1.23').json['service'])
