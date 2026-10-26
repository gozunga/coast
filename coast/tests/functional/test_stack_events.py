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

"""API to conductor timeline with real persistence and fake RPC."""

from coast.api import microversions
from coast.db import api as db_api
from coast.db import models
from coast import objects
from coast.tests import conductor


class StackTimelineTest(conductor.ConductorTest):
    def test_invalid_plan_never_reaches_persistence_or_rpc(self):
        for version in ('1.0', microversions.MAX_VERSION):
            headers = dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting ' + version})
            response = self.request('post', '/v1/stacks', {
                'stack': {'name': 'invalid', 'plan': 'nope'}},
                headers=headers, status=400)
            self.assertIn("Plan 'nope' does not exist.",
                          response.json['faultstring'])
            self.assertFalse(objects.StackList.get_all(self.ctx))
            with db_api.reader.using(self.ctx):
                self.assertEqual(0, self.ctx.session.query(
                    models.QuotaUsage).count())
            self.assertEqual([], self.manager.notifier.events)
        # A valid request still completes the real noop conductor flow.
        self.stack()

    def test_event_outcomes_and_sort_errors(self):
        path = self.stack()
        headers = dict(self.headers, **{
            'OpenStack-API-Version':
                'app-hosting ' + microversions.MAX_VERSION})
        for route in ('/v1/events', path + '/events'):
            response = self.request('get', route + '?outcome=bogus',
                                    headers=headers, status=400)
            self.assertIn("'bogus'", response.json['faultstring'])
            for outcome in ('start', 'success', 'failure'):
                response = self.request(
                    'get', route + '?outcome=' + outcome, headers=headers)
                self.assertTrue(all(row['outcome'] == outcome
                                    for row in response.json['events']))
        for query in ('sort_key=bogus', 'sort_dir=bogus'):
            response = self.request(
                'get', '/v1/stacks?' + query, headers=headers, status=400)
            self.assertIn("'bogus'", response.json['faultstring'])
            self.assertIn('Use one of:', response.json['faultstring'])

    def test_lifecycle_events_are_project_scoped_and_pageable(self):
        path = self.stack()
        headers = dict(self.headers,
                       **{'OpenStack-API-Version': 'app-hosting 1.8'})
        response = self.request('get', path + '/events?limit=1',
                                headers=headers).json
        self.assertEqual(1, len(response['events']))
        ids = []
        order = []
        while True:
            ids.extend(row['id'] for row in response['events'])
            order.extend((row['created_at'], row['id'])
                         for row in response['events'])
            for row in response['events']:
                self.assertIn(row['resource_type'], ('stack', 'server'))
                self.assertEqual(path.rsplit('/', 1)[-1], row['stack_id'])
            if not response['links']:
                break
            response = self.request('get', response['links'][0]['href'],
                                    headers=headers).json
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(order, reverse=True), order)
        self.assertGreaterEqual(len(ids), 3)
        stack_rows = self.request('get', path + '/events?resource_type=stack',
                                  headers=headers).json['events']
        self.assertTrue(all(row['resource_type'] == 'stack'
                            for row in stack_rows))
        start = next(row for row in stack_rows if row['outcome'] == 'start')
        self.assertEqual(1, start['detail']['worker_count'])
        self.assertEqual('small', start['detail']['plan'])
        self.request('get', path + '/events', headers=dict(
            headers, **{'X-Project-Id': 'other'}), status=404)
        self.request('get', path + '/events', headers=dict(
            headers, **{'X-Roles': 'unrelated'}), status=403)
        self.request('get', path + '/events', status=404)
        # Legacy environment receipts still exist for existing consumers.
        self.assertTrue(objects.EventList.get_all(self.ctx, filters={
            'stack_id': path.rsplit('/', 1)[-1],
            'resource_type': 'environment'}))


for _name in dir(conductor.ConductorTest):
    if _name.startswith('test_'):
        setattr(StackTimelineTest, _name, None)
