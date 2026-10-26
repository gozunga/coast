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

"""Stack timeline scope, paging and safe lifecycle detail."""

import datetime
import json
from unittest import mock

from oslo_policy import policy as policy_module

from coast.common import policy
from coast.common import states
from coast.conductor import manager
from coast.db import api as db_api
from coast.db import models
from coast import objects
from coast.tests.unit import test_controllers


class StackEventsTest(test_controllers.ControllerFixture):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.8'
        self.events_path = self.path + '/events'

    def event(self, kind='stack', stack=None):
        event = objects.Event(
            context=self.ctx, stack_id=stack or self.env.id,
            resource_type=kind, resource_id=self.env.id, action='create',
            outcome='success', detail={'worker_count': 1})
        event.create()
        return event

    def get(self, suffix='', headers=None, status=200):
        return self.client.get(self.events_path + suffix,
                               headers=headers or self.headers, status=status)

    def test_outcome_validation_on_project_and_stack_routes(self):
        for outcome in ('start', 'success', 'failure'):
            event = self.event()
            event.outcome = outcome
            event.save()
        for path in ('/v1/events', self.events_path):
            response = self.client.get(
                path + '?outcome=bogus', headers=self.headers, status=400)
            message = response.json['faultstring']
            self.assertIn("'bogus'", message)
            for outcome in ('start', 'success', 'failure'):
                self.assertIn(outcome, message)
                response = self.client.get(
                    path + '?outcome=' + outcome, headers=self.headers).json
                self.assertEqual([outcome],
                                 [e['outcome'] for e in response['events']])

    def test_collection_filter_errors_identify_value_and_choices(self):
        for path, allowed in (('/v1/stacks', 'name'),
                              ('/v1/events', 'outcome')):
            for query, bad, expected in (
                    ('sort_key=bogus', 'bogus', allowed),
                    ('sort_dir=sideways', 'sideways', 'asc, desc'),
                    ('all_projects=maybe', 'maybe', 'false, true'),
                    ('limit=abc', 'abc', 'integer'),
                    ('limit=-1', '-1', 'integer'),
                    ('created_at=yesterday', 'yesterday', 'ISO 8601'),
                    ('unknown=bogus', 'bogus', 'query parameter'),
                    ('fields=bogus', 'bogus', 'fields')):
                response = self.client.get(path + '?' + query,
                                           headers=self.headers, status=400)
                self.assertIn(bad, response.json['faultstring'])
                self.assertIn(expected, response.json['faultstring'])

    def test_order_paging_filter_and_legacy_null(self):
        rows = [self.event(kind) for kind in
                ('stack', 'server', 'stack', 'volume', 'environment')]
        with db_api.writer.using(self.ctx) as session:
            session.query(models.Event).update({
                'created_at': datetime.datetime(2026, 1, 1)})
        expected = sorted([row.id for row in rows[:3]], reverse=True)
        first = self.get('?limit=2').json
        self.assertEqual(expected[:2], [r['id'] for r in first['events']])
        next_page = self.client.get(first['links'][0]['href'],
                                    headers=self.headers).json
        self.assertEqual(expected[2:], [r['id'] for r in next_page['events']])
        self.assertEqual([], next_page['links'])
        filtered = self.get('?resource_type=server').json
        self.assertEqual([rows[1].id], [r['id'] for r in filtered['events']])
        self.assertEqual({'worker_count': 1}, filtered['events'][0]['detail'])
        legacy = self.event()
        legacy.detail = None
        legacy.save()
        found = self.get().json['events']
        self.assertIsNone(next(r for r in found if r['id'] == legacy.id)[
            'detail'])

    def test_gate_auth_and_policy(self):
        self.get(headers=dict(self.headers,
                              **{'OpenStack-API-Version': 'app-hosting 1.7'}),
                 status=404)
        self.get(headers=dict(self.headers, **{'X-Project-Id': 'other'}),
                 status=404)
        self.get(headers=dict(self.headers, **{'X-Roles': 'unprivileged'}),
                 status=403)
        self.get(headers=dict(self.headers, **{'X-User-Id': ''}), status=401)
        self.get(headers=dict(self.headers, **{'X-Roles': 'reader'}))
        policy.get_enforcer().set_rules(policy_module.Rules.from_dict({
            'coast:stack:events:get': '!'}), overwrite=False)
        self.get(status=403)

    def test_console_events_hidden_until_1_9(self):
        hidden = self.event('app')
        service = self.event('service')
        self.assertNotIn(hidden.id, [row['id'] for row in self.get().json[
            'events']])
        self.get('?marker=' + hidden.id, status=404)
        headers = dict(self.headers, **{
            'OpenStack-API-Version': 'app-hosting 1.9'})
        shown = [row['id'] for row in self.get('', headers=headers).json[
            'events']]
        self.assertIn(hidden.id, shown)
        self.assertIn(service.id, shown)
        self.get('?marker=' + hidden.id, headers=headers)
        self.get('?resource_type=app', headers=headers)
        volume = self.event('volume')
        self.get('?marker=' + volume.id, headers=headers, status=404)
        self.get('?resource_type=volume', headers=headers, status=400)

    def test_bad_filters_marker_and_methods(self):
        other = objects.Stack(context=self.ctx, name='other', plan='small',
                              driver='noop', engine_version='test',
                              status=states.ACTIVE, task_state=states.NONE)
        other.create()
        foreign = self.event(stack=other.id)
        volume = self.event('volume')
        for marker in (foreign.id, volume.id, 'missing'):
            self.get('?marker=' + marker, status=404)
        for query in ('limit=-1', 'limit=0', 'limit=no',
                      'resource_type=volume', 'sort_dir=asc'):
            self.get('?' + query, status=400)
        self.client.post(self.events_path, headers=self.headers, status=405)
        self.client.get('/v1/stacks/missing/events', headers=self.headers,
                        status=404)

    def test_detail_limit_and_default(self):
        with mock.patch.object(
                objects.EventList, 'get_all', return_value=[]) as get:
            self.get()
            self.assertEqual(50, get.call_args.kwargs['limit'])
            self.get('?limit=999')
            self.assertEqual(200, get.call_args.kwargs['limit'])

    def test_event_detail_excludes_secrets_and_preserves_failover_ids(self):
        owner = manager.Manager(self.conf)
        self.addCleanup(owner.close)
        self.server.flavor_id = 'flavor'
        self.server.image_id = 'image'
        self.server.compute_id = 'old'
        self.server.fault = 'password=hidden-secret'
        self.server.save()
        owner._event(self.ctx, self.server, self.env, 'failover', 'start',
                     'receipt')
        self.server.compute_id = 'new'
        self.server.save()
        owner._event(self.ctx, self.server, self.env, 'failover', 'success',
                     'receipt')
        detail = objects.Event.get_by_id(self.ctx, 'receipt').detail
        self.assertEqual('old', detail['old_compute_id'])
        self.assertEqual('new', detail['new_compute_id'])
        self.assertEqual('worker', detail['server_name'])
        encoded = json.dumps(detail)
        for forbidden in ('password', 'hidden-secret', 'token', 'cert',
                          'env', 'ssh', 'private_key'):
            self.assertNotIn(forbidden, encoded)

    def test_failed_failover_records_replacement_if_booted(self):
        owner = manager.Manager(self.conf)
        self.addCleanup(owner.close)
        for new_id in ('old', 'replacement'):
            self.server.compute_id = 'old'
            self.server.save()
            receipt = 'failure-' + new_id
            owner._event(self.ctx, self.server, self.env, 'failover', 'start',
                         receipt)
            self.server.compute_id = new_id
            self.server.save()
            owner._event(self.ctx, self.server, self.env, 'failover',
                         'failure', receipt)
            detail = objects.Event.get_by_id(self.ctx, receipt).detail
            self.assertEqual('old', detail['old_compute_id'])
            self.assertEqual(None if new_id == 'old' else new_id,
                             detail['new_compute_id'])

    def test_create_and_delete_history_records_nova_id(self):
        owner = manager.Manager(self.conf)
        self.addCleanup(owner.close)
        self.server.compute_id = 'nova-id'
        self.server.save()
        for action in ('create', 'delete'):
            owner._event(self.ctx, self.server, self.env, action, 'success')
        rows = self.get().json['events']
        self.assertEqual(2, len(rows))
        for row in rows:
            self.assertEqual('nova-id', row['detail']['compute_id'])

    def test_event_object_backport(self):
        event = self.event()
        primitive = event.obj_to_primitive(target_version='1.0')
        self.assertNotIn('detail', primitive['coast_object.data'])


for _name in dir(test_controllers.ControllerFixture):
    if _name.startswith('test_'):
        setattr(StackEventsTest, _name, None)
