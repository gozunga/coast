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

"""Admin inventory scope, live counts and bounded aggregate queries."""

from unittest import mock

from oslo_policy import policy as policy_module
import sqlalchemy as sa

from coast.api.v1 import stack_controllers
from coast.common import context
from coast.common import exception
from coast.common import policy
from coast.common import states
from coast.db import api as db_api
from coast import objects
from coast.tests import app_api


class AdminInventoryTest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.base = '/v1/stacks/' + self.stack.id
        self.admin = dict(self.headers, **{
            'X-Roles': 'admin', 'X-Project-Id': 'operator'})
        session = mock.Mock()
        session.get_project_id.return_value = 'service-project'
        self.session = session
        patch = mock.patch.object(stack_controllers.clients, 'get_session',
                                  return_value=session)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ('network_id', 'subnet_id', 'router_id',
                     'sg_ingress_id', 'sg_mgmt_id'):
            setattr(self.stack, name, name + '-uuid')
        self.stack.save()
        self.server.compute_id = 'nova-uuid'
        self.server.save()
        self.volume = objects.Volume(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='data', size_gb=1,
            purpose='data', mount_path='/data', status=states.IN_USE,
            cinder_volume_id='cinder-uuid')
        self.volume.create()
        for index in range(2):
            objects.Event(
                context=self.ctx, stack_id=self.stack.id,
                resource_type='server', resource_id=self.server.id,
                action='create', outcome='success',
                detail={'compute_id': 'nova-uuid'}).create()

    def get(self, path, headers=None, version='1.18', status=200):
        headers = self.admin if headers is None else headers
        return self.request('get', path, headers=headers,
                            version=version, status=status)

    def test_stack_driver_is_admin_only_at_every_version(self):
        self.stack.driver = 'coolify_v4'
        self.stack.save()
        for version in ('1.0', '1.17', '1.18', '1.25'):
            for role in ('reader', 'reader,member', 'reader,manager'):
                headers = dict(self.headers, **{'X-Roles': role})
                for path, key in ((self.base, 'stack'),
                                  ('/v1/stacks', 'stacks')):
                    response = self.get(path, headers, version)
                    rows = response.json[key]
                    if key == 'stack':
                        rows = [rows]
                    self.assertTrue(rows)
                    for row in rows:
                        self.assertNotIn('driver', row)
                    self.assertNotRegex(response.text.lower(),
                                        'coolify|traefik')
                self.get(self.base + '?fields=driver', headers,
                         version, status=400)
            admin = dict(self.headers, **{'X-Roles': 'admin'})
            row = self.get(self.base, admin, version).json['stack']
            self.assertEqual('coolify_v4', row['driver'])
        policy.get_enforcer().set_rules(policy_module.Rules.from_dict({
            'coast:stack:get_detail': '!'}), overwrite=False)
        row = self.get(self.base, admin, '1.0').json['stack']
        self.assertNotIn('driver', row)

    def test_inventory_fields_and_policy(self):
        row = self.get(self.base + '?all_projects=true').json['stack']
        self.assertEqual('project', row['project_id'])
        self.assertEqual('service-project', row['service_project_id'])
        self.assertEqual((1, 1), (row['server_count'], row['workload_count']))
        for name in ('network_id', 'subnet_id', 'router_id',
                     'sg_ingress_id', 'sg_mgmt_id'):
            self.assertEqual(name + '-uuid', row[name])
        for headers, version in ((self.headers, '1.18'),
                                 (dict(self.headers, **{'X-Roles': 'admin'}),
                                  '1.17')):
            row = self.get(self.base, headers, version).json['stack']
            for name in stack_controllers.ADMIN_STACK_FIELDS:
                self.assertNotIn(name, row)
        self.session.get_project_id.return_value = 'project'
        row = self.get(self.base + '?all_projects=true').json['stack']
        self.assertNotIn('service_project_id', row)
        policy.get_enforcer().set_rules(policy_module.Rules.from_dict({
            'coast:stack:get_detail': '!'}), overwrite=False)
        row = self.get(self.base + '?all_projects=true').json['stack']
        for name in stack_controllers.ADMIN_STACK_FIELDS + ('network_id',):
            self.assertNotIn(name, row)

    def test_live_counts_and_field_selection(self):
        for role in ('control', 'build'):
            objects.Server(
                context=self.ctx, name=role, stack_id=self.stack.id,
                kind='vm', role=role, status=states.ACTIVE,
                task_state=states.NONE).create()
        deleted = objects.Server(
            context=self.ctx, name='removed', stack_id=self.stack.id,
            kind='vm', role='build', status=states.ACTIVE,
            task_state=states.NONE)
        deleted.create()
        deleted.destroy()
        for name, remove in (('removed', True), ('other', False)):
            service = objects.Service(
                context=self.ctx, stack_id=self.stack.id, name=name,
                source_id=self.source.id, server_id=self.server.id,
                kind='service', replicas=1, auto_deploy=False,
                status=states.ACTIVE, task_state=states.NONE)
            service.create()
            if remove:
                service.destroy()
        path = ('/v1/stacks?all_projects=true&fields='
                'id,server_count,workload_count')
        row = self.get(path).json['stacks'][0]
        self.assertEqual({'id', 'server_count', 'workload_count', 'links'},
                         set(row))
        self.assertEqual((3, 2), (row['server_count'], row['workload_count']))
        self.get('/v1/stacks?fields=server_count', self.headers, status=400)
        self.get(self.base + '?fields=subnet_id', self.headers, status=400)
        self.get('/v1/stacks?fields=server_count',
                 dict(self.headers, **{'X-Roles': 'admin'}),
                 version='1.17', status=400)

    def test_cross_project_lists_and_item_reads(self):
        paths = ((self.base + '/servers', 'servers', self.server.id),
                 (self.base + '/volumes', 'volumes', self.volume.id),
                 ('/v1/services?stack_id=' + self.stack.id,
                  'services', self.service.id),
                 (self.base + '/events', 'events', None),
                 ('/v1/events?stack_id=' + self.stack.id, 'events', None))
        for path, key, identifier in paths:
            with self.subTest(path=path):
                suffix = '&' if '?' in path else '?'
                response = self.get(path + suffix + 'all_projects=true')
                self.assertTrue(response.json[key])
                if identifier:
                    self.assertEqual(identifier, response.json[key][0]['id'])
                self.get(path + suffix + 'all_projects=true',
                         self.headers, status=403)
                self.get(path + suffix + 'all_projects=invalid', status=400)
                self.get(path + suffix + 'all_projects=true', {}, status=401)
        for path, key in ((self.base, 'stack'),
                          (self.base + '/servers/' + self.server.id, 'server'),
                          (self.base + '/volumes/' + self.volume.id,
                           'volume')):
            self.get(path, status=404)
            self.get(path + '?all_projects=true')
        servers = self.get(self.base + '/servers?all_projects=true').json
        self.assertEqual('nova-uuid', servers['servers'][0]['compute_id'])
        volumes = self.get(self.base + '/volumes?all_projects=true').json
        self.assertEqual('cinder-uuid',
                         volumes['volumes'][0]['cinder_volume_id'])
        self.get(self.base + '/servers?all_projects=true',
                 version='1.17', status=404)
        self.get(self.base + '/events?all_projects=true',
                 version='1.17', status=404)
        self.get('/v1/stacks/missing/servers?all_projects=true', status=404)
        self.get(self.base + '/servers/missing?all_projects=true', status=404)
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        self.get(self.base, foreign, status=404)
        self.get(self.base + '/events', foreign, status=404)

    def test_cross_project_flag_does_not_enable_writes(self):
        for path in (self.base, self.base + '/servers/' + self.server.id,
                     self.base + '/volumes/' + self.volume.id):
            self.request('delete', path + '?all_projects=true',
                         headers=self.admin, version='1.18', status=404)
        self.assertEqual(states.ACTIVE, objects.Stack.get_by_id(
            self.ctx, self.stack.id).status)
        self.assertEqual(states.ACTIVE, objects.Server.get_by_id(
            self.ctx, self.server.id).status)

    def test_timeline_paging_preserves_admin_scope_and_checks_marker(self):
        path = self.base + '/events?all_projects=true&limit=1'
        first = self.get(path).json
        next_page = self.get(first['links'][0]['href']).json
        self.assertNotEqual(first['events'][0]['id'],
                            next_page['events'][0]['id'])
        self.assertEqual([], next_page['links'])
        self.get(path + '&marker=missing', status=404)
        self.get(path + '&resource_type=volume', status=400)
        self.get(self.base + '/servers?all_projects=true&marker=' +
                 self.server.id)
        policy.get_enforcer().set_rules(policy_module.Rules.from_dict({
            'ADMIN': '!'}), overwrite=False)
        self.get(path, status=403)

    def test_count_queries_are_scoped_and_constant_per_page(self):
        empty = objects.Stack(
            context=self.ctx, name='empty', plan='small', driver='noop',
            engine_version='test', status=states.ACTIVE,
            task_state=states.NONE)
        empty.create()
        statements = []

        def capture(conn, cursor, statement, parameters, ctx, many):
            if 'GROUP BY' in statement:
                statements.append(statement)

        sa.event.listen(self.db.engine, 'before_cursor_execute', capture)
        self.addCleanup(sa.event.remove, self.db.engine,
                        'before_cursor_execute', capture)
        for limit in (1, 2):
            statements.clear()
            rows = self.get('/v1/stacks?all_projects=true&limit=' +
                            str(limit)).json['stacks']
            self.assertEqual(limit, len(rows))
            self.assertEqual(2, len(statements))
        self.assertEqual(
            (0, 0), (rows[1]['server_count'], rows[1]['workload_count']))
        foreign = context.RequestContext(project_id='other')
        self.assertEqual(
            {self.stack.id: {'server_count': 0, 'workload_count': 0}},
            db_api.stack_resource_counts(foreign, [self.stack.id]))
        self.assertRaises(exception.NotAuthorized,
                          db_api.stack_resource_counts, foreign,
                          [self.stack.id], all_projects=True)
        self.session.get_project_id.return_value = None
        self.get(self.base + '?all_projects=true', status=500)


for _name in dir(app_api.AppAPITest):
    if _name.startswith('test_'):
        setattr(AdminInventoryTest, _name, None)
