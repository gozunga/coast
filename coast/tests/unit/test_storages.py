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

from coast.api import errors
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.common import storage as storage_rules
from coast.db import api as db_api
from coast.drivers import base as driver_base
from coast import objects
from coast.tests import app_api


class StorageAPITest(app_api.AppAPITest):

    def setUp(self):
        super().setUp()
        self.base = '/v1/stacks/' + self.stack.id + '/storages'

    def test_collection_sort_errors_name_value_and_allowed_choices(self):
        common = ('container_path', 'created_at', 'id', 'name', 'origin',
                  'project_id', 'server_id', 'service_id', 'size_gb',
                  'stack_id', 'status', 'updated_at')
        for version in ('1.10', '1.16', '1.19'):
            fields = common
            if version != '1.10':
                fields += ('source',)
            if version == '1.19':
                fields += ('type', 'host_path', 'read_only')
            choices = ', '.join(sorted(fields))
            for query, expected in (
                    ('sort_key=bogus',
                     "Invalid sort key 'bogus'. Use one of: %s." % choices),
                    ('sort_dir=sideways',
                     "Invalid sort direction 'sideways'. "
                     "Use one of: asc, desc.")):
                with self.subTest(version=version, query=query):
                    response = self.request('get', self.base + '?' + query,
                                            status=400, version=version)
                    self.assertEqual(expected, response.json['faultstring'])

    def _disk(self, size_gb=100):
        objects.Volume(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='docker', size_gb=size_gb,
            mount_path='/var/lib/docker', purpose='docker',
            status=states.AVAILABLE).create()

    def _create(self, name='uploads', size_gb=1, status=201):
        return self.request(
            'post', self.base,
            {'storage': {'name': name, 'size_gb': size_gb}},
            status=status, version='1.10')

    def test_create_attach_detach_and_delete(self):
        self._disk()
        created = self._create().json['storage']
        self.assertEqual('uploads', created['name'])
        self.assertEqual(1, created['size_gb'])
        self.assertEqual(states.AVAILABLE, created['status'])
        self.assertEqual('api', created['origin'])
        self.assertIsNone(created['service_id'])
        self.assertIsNone(created['server_id'])
        self.assertIsNone(created['container_path'])
        self.assertEqual(1, quota.usage(self.ctx)['in_use']['storage_gb'])
        listed = self.request('get', self.base, version='1.10')
        self.assertEqual([created['id']],
                         [row['id'] for row in listed.json['storages']])
        shown = self.request('get', self.base + '/' + created['id'],
                             version='1.10').json['storage']
        self.assertEqual(created['id'], shown['id'])
        attached = self.request(
            'post', self.base + '/' + created['id'] + '/attach',
            {'attach': {'service_id': self.service.id,
                        'container_path': '/var/data'}},
            version='1.10').json['storage']
        self.assertEqual(states.IN_USE, attached['status'])
        self.assertEqual(self.service.id, attached['service_id'])
        self.assertEqual(self.server.id, attached['server_id'])
        self.assertEqual('/var/data', attached['container_path'])
        self.request('delete', self.base + '/' + created['id'],
                     status=409, version='1.10')
        detached = self.request(
            'post', self.base + '/' + created['id'] + '/detach', {},
            version='1.10').json['storage']
        self.assertEqual(states.AVAILABLE, detached['status'])
        self.assertEqual(self.service.id, detached['service_id'])
        self.assertEqual(self.server.id, detached['server_id'])
        self.assertIsNone(detached['container_path'])
        self.request('delete', self.base + '/' + created['id'],
                     status=204, version='1.10')
        self.request('get', self.base + '/' + created['id'],
                     status=404, version='1.10')
        self.assertEqual(0, quota.usage(self.ctx)['in_use']['storage_gb'])

    def test_auth_roles_and_project_isolation(self):
        created = self._create().json['storage']
        item = self.base + '/' + created['id']
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        member = dict(self.headers, **{'X-Roles': 'member'})
        self.request('get', self.base, headers={}, status=401, version='1.10')
        self.request('get', self.base, headers=reader, version='1.10')
        self.request('post', self.base,
                     {'storage': {'name': 'other', 'size_gb': 1}},
                     headers=reader, status=403, version='1.10')
        self.request('delete', item, headers=member, status=403,
                     version='1.10')
        foreign = dict(self.headers, **{
            'X-Project-Id': 'other', 'X-Roles': 'admin'})
        self.request('get', self.base, headers=foreign, status=404,
                     version='1.10')
        self.request('get', item, headers=foreign, status=404, version='1.10')

    def test_microversion_gate(self):
        self.request('get', self.base, status=404)
        self.request('get', self.base, status=404, version='1.8')
        self.request('get', self.base, status=404, version='1.9')
        self.request('get', self.base, version='1.10')

    def test_rejects_host_path_and_parent_segment(self):
        self._disk()
        created = self._create().json['storage']
        self.request(
            'post', self.base,
            {'storage': {'name': 'bad', 'size_gb': 1, 'host_path': '/tmp'}},
            status=400, version='1.10')
        self.request(
            'post', self.base + '/' + created['id'] + '/attach',
            {'attach': {'service_id': self.service.id,
                        'container_path': '/var/../etc',
                        'read_only': True}},
            status=400, version='1.10')
        row = objects.Storage.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.AVAILABLE, row.status)
        self.assertIsNone(row.container_path)

    def test_quota_disk_duplicate_and_cross_service(self):
        self.config.config(storage_gb=1, group='quotas')
        denied = self._create(size_gb=2, status=403)
        self.assertIn('storage_gb', denied.json['faultstring'])
        self.config.config(storage_gb=50, group='quotas')
        self._create(name='wide', size_gb=2)
        self.request(
            'post', self.base + '/' + objects.StorageList.get_all(
                self.ctx)[0].id + '/attach',
            {'attach': {'service_id': self.service.id,
                        'container_path': '/var/data'}},
            status=409, version='1.10')
        self._disk(size_gb=1)
        self.request(
            'post', self.base + '/' + objects.StorageList.get_all(
                self.ctx)[0].id + '/attach',
            {'attach': {'service_id': self.service.id,
                        'container_path': '/var/data'}},
            status=409, version='1.10')
        self._create(name='wide', status=409)
        other = objects.Service(
            context=self.ctx, stack_id=self.stack.id,
            environment_id=self.environment.id, name='other',
            source_id=self.source.id, server_id=self.server.id,
            ports='3000', replicas=1, auto_deploy=False,
            status=states.ACTIVE, task_state=states.NONE)
        other.create()
        created = self._create(name='bound', size_gb=1).json['storage']
        self._disk(size_gb=100)
        # The 1 GiB disk is still present. Replace the cap with a large disk
        # by using a server that has exactly one live docker volume.
        for row in objects.VolumeList.get_all(
                self.ctx, filters={'purpose': 'docker'}):
            if row.size_gb == 1:
                row.status = states.DELETING
                row.save()
        self.request(
            'post', self.base + '/' + created['id'] + '/attach',
            {'attach': {'service_id': self.service.id,
                        'container_path': '/var/data'}},
            version='1.10')
        self.request(
            'post', self.base + '/' + created['id'] + '/detach', {},
            version='1.10')
        self.request(
            'post', self.base + '/' + created['id'] + '/attach',
            {'attach': {'service_id': other.id,
                        'container_path': '/var/other'}},
            status=409, version='1.10')

    def test_apply_failure_restores_the_row(self):
        self._disk()
        created = self._create().json['storage']
        before = objects.Service.get_by_id(self.ctx, self.service.id)
        stamp = before.settings_changed_at
        self.service.engine_ref = 'app-uuid'
        self.service.save()
        target = 'coast.api.v1.storage_controllers.rpcapi.ConductorAPI'
        with mock.patch(target) as api:
            api.return_value.apply_mounts.side_effect = (
                exception.CoastException())
            self.request(
                'post', self.base + '/' + created['id'] + '/attach',
                {'attach': {'service_id': self.service.id,
                            'container_path': '/var/data'}},
                status=500, version='1.10')
        row = objects.Storage.get_by_id(self.ctx, created['id'])
        self.assertEqual(states.AVAILABLE, row.status)
        self.assertIsNone(row.container_path)
        self.assertIsNone(row.service_id)
        fresh = objects.Service.get_by_id(self.ctx, self.service.id)
        self.assertEqual(stamp, fresh.settings_changed_at)

    def test_customer_source_paths_are_worker_owned(self):
        body = self.create_body()
        body['service']['source'].update(
            build_pack='compose', compose_path='/host/compose.yaml',
            dockerfile_path='/host/Dockerfile', base_directory='/host')
        created = self.request('post', '/v1/services', body, status=202,
                               version='1.17').json['service']
        row = objects.Service.get_by_id(self.ctx, created['id'])
        source = objects.Source.get_by_id(self.ctx, row.source_id)
        self.assertEqual('/host/compose.yaml', source.compose_path)
        self.request('patch', self.path, {'service': {'source': {
            'base_directory': '/worker'}}}, version='1.17')
        self.assertEqual('/worker', objects.Source.get_by_id(
            self.ctx, self.source.id).base_directory)

    def test_drop_service_keeps_detached_api_storage(self):
        with db_api.writer.using(self.ctx):
            held = quota.reserve(self.ctx, storage_gb=7)
            self._row('tpl', 1, states.IN_USE, 'template', '/var/tpl')
            self._row('live', 2, states.IN_USE, 'api', '/var/live')
            self._row('kept', 4, states.AVAILABLE, 'api', None)
            quota.commit(self.ctx, held)
        storage_rules.drop_service_storages(self.ctx, self.service)
        rows = list(objects.StorageList.get_all(self.ctx))
        self.assertEqual(['kept'], [row.name for row in rows])
        self.assertEqual(4, quota.usage(self.ctx)['in_use']['storage_gb'])

    def test_drop_stack_releases_every_storage(self):
        with db_api.writer.using(self.ctx):
            held = quota.reserve(self.ctx, storage_gb=3)
            self._row('one', 1, states.AVAILABLE, 'api', None)
            self._row('two', 2, states.IN_USE, 'api', '/var/two')
            quota.commit(self.ctx, held)
        storage_rules.drop_stack_storages(self.ctx, self.stack)
        self.assertEqual([], list(objects.StorageList.get_all(self.ctx)))
        self.assertEqual(0, quota.usage(self.ctx)['in_use']['storage_gb'])

    def test_observed_template_volumes_are_read_only_and_not_persisted(self):
        self.service.engine_ref = 'engine-service'
        self.service.template_id = 'uptime-kuma'
        self.service.save()
        self._row('account', 1, states.IN_USE, 'template', '/data')
        self._row('removed', 1, states.IN_USE, 'template', '/old')
        self._row('web-compose-new', 2, states.IN_USE, 'template', None)
        target = 'coast.api.v1.storage_controllers.rpcapi.ConductorAPI'
        mounts = [{'name': 'engine-data', 'container_path': '/data'},
                  {'name': 'compose-extra', 'container_path': '/extra'},
                  {'name': 'compose-new', 'container_path': '/new'}]
        with mock.patch(target) as api:
            api.return_value.list_volume_mounts.return_value = mounts
            listed = self.request('get', self.base, version='1.16')
            rows = listed.json['storages']
            self.assertEqual({'/data', '/extra', '/new'},
                             {row['container_path'] for row in rows})
            self.assertEqual({'template'}, {row['source'] for row in rows})
            extra = next(row for row in rows if row['name'] == 'compose-extra')
            self.assertIsNone(extra['size_gb'])
            accounted = next(row for row in rows
                             if row['container_path'] == '/new')
            self.assertEqual(2, accounted['size_gb'])
            item = self.base + '/' + extra['id']
            shown = self.request('get', item, version='1.16').json['storage']
            self.assertEqual(extra, shown)
            for suffix, body in (('/detach', {}), ('/attach', {'attach': {
                    'service_id': self.service.id,
                    'container_path': '/new'}})):
                response = self.request('post', item + suffix, body,
                                        status=409, version='1.16')
                self.assertIn('read-only', response.json['faultstring'])
            self.request('delete', item, status=409, version='1.16')
            self.request('patch', item, {'storage': {'name': 'other'}},
                         status=409, version='1.16')
            reader = dict(self.headers, **{'X-Roles': 'reader'})
            self.request('delete', item, headers=reader, status=403,
                         version='1.16')
            foreign = dict(self.headers, **{'X-Project-Id': 'other'})
            self.request('get', item, headers=foreign, status=404,
                         version='1.16')
            self.request('get', item, status=404, version='1.15')
            page = self.request('get', self.base + '?limit=1', version='1.16')
            next_page = self.request('get', page.json['links'][0]['href'],
                                     version='1.16')
            self.assertEqual(2, len({page.json['storages'][0]['id'],
                                    next_page.json['storages'][0]['id']}))
            filtered = self.request('get', self.base + '?source=template&' +
                                    'service_id=' + self.service.id +
                                    '&fields=id,source', version='1.16')
            self.assertEqual({'id', 'source', 'links'},
                             set(filtered.json['storages'][0]))
        self.assertEqual({'account', 'removed', 'web-compose-new'},
                         {row.name for row in
                          objects.StorageList.get_all(self.ctx)})

    def test_observation_failure_returns_no_partial_storage_list(self):
        self.service.engine_ref = 'engine-service'
        self.service.save()
        self._row('coast', 1, states.IN_USE, 'api', '/data')
        target = 'coast.api.v1.storage_controllers.rpcapi.ConductorAPI'
        with mock.patch(target) as api:
            api.return_value.list_volume_mounts.side_effect = (
                driver_base.EngineUnreachable())
            response = self.request('get', self.base, status=503,
                                    version='1.16')
            self.assertNotIn('storages', response.json)
            self.request('get', self.base, version='1.15')

    def test_conductor_read_failure_returns_503_on_observed_versions(self):
        self.service.engine_ref = 'engine-service'
        self.service.save()
        target = 'coast.conductor.rpcapi.ConductorAPI._sync_call'
        for version in ('1.16', '1.24'):
            with mock.patch(target, side_effect=exception.CoastException(
                    'Conductor storage read failed.')):
                response = self.request('get', self.base, status=503,
                                        version=version)
                self.assertEqual(errors.STORAGE_UNAVAILABLE,
                                 response.json['faultstring'])
                self.assertNotIn('storages', response.json)

    def test_coast_observation_is_not_duplicated(self):
        self.service.engine_ref = 'engine-service'
        self.service.save()
        self._row('uploads', 1, states.IN_USE, 'api', '/data')
        target = 'coast.api.v1.storage_controllers.rpcapi.ConductorAPI'
        with mock.patch(target) as api:
            api.return_value.list_volume_mounts.return_value = [
                {'name': 'engine-uploads', 'container_path': '/data'}]
            result = self.request('get', self.base, version='1.16')
            rows = result.json['storages']
            self.assertEqual(1, len(rows))
            self.assertEqual('coast', rows[0]['source'])

    def test_paging_reads_accounts_beyond_database_page(self):
        self._row('first', 1, states.AVAILABLE, 'api', None)
        self._row('second', 1, states.AVAILABLE, 'api', None)
        self.config.config(max_limit=1, group='api')
        first = self.request('get', self.base, version='1.16').json
        second = self.request('get', first['links'][0]['href'],
                              version='1.16').json
        self.assertEqual({'first', 'second'}, {
            first['storages'][0]['name'], second['storages'][0]['name']})
        self.assertEqual([], second['links'])
        empty = self.request('get', self.base + '?limit=0', version='1.16')
        self.assertEqual([], empty.json['storages'])

    def test_template_docker_socket_is_visible_and_read_only(self):
        self.service.template_id = 'dozzle'
        self.service.engine_ref = 'engine-service'
        self.service.save()
        target = 'coast.api.v1.storage_controllers.rpcapi.ConductorAPI'
        with mock.patch(target) as api:
            api.return_value.list_volume_mounts.return_value = [{
                'name': 'Docker socket (from template)',
                'container_path': '/var/run/docker.sock'}]
            response = self.request('get', self.base, version='1.16')
            rows = response.json['storages']
            self.assertEqual(1, len(rows))
            row = rows[0]
            self.assertEqual('Docker socket (from template)', row['name'])
            self.assertEqual('template', row['source'])
            self.assertIsNone(row['size_gb'])
            path = self.base + '/' + row['id']
            self.request('delete', path, status=409, version='1.16')
            self.request('post', path + '/detach', {}, status=409,
                         version='1.16')
            self.request('patch', path, {'storage': {}}, status=409,
                         version='1.16')
        self.assertEqual([], list(objects.StorageList.get_all(self.ctx)))

    def test_coast_source_and_microversion_shape(self):
        created = self._create().json['storage']
        old = self.request('get', self.base, version='1.15').json['storages']
        self.assertNotIn('source', old[0])
        new = self.request('get', self.base, version='1.16').json['storages']
        self.assertEqual('coast', new[0]['source'])
        self.assertEqual(created['id'], new[0]['id'])
        for query in ('?limit=-1', '?limit=bad', '?sort_dir=bad',
                      '?sort_key=unknown', '?fields=unknown'):
            self.request('get', self.base + query, status=400, version='1.16')

    def _row(self, name, size_gb, status, origin, path):
        objects.Storage(
            context=self.ctx, stack_id=self.stack.id, name=name,
            size_gb=size_gb, status=status, origin=origin,
            service_id=self.service.id, server_id=self.server.id,
            container_path=path).create()


class TypedStorageTest(StorageAPITest):

    def _mount(self, kind='bind', **changes):
        values = dict(type=kind, service_id=self.service.id,
                      container_path='/data', read_only=False)
        values.update(host_path='/srv/data') if kind == 'bind' else (
            values.update(name='data', size_gb=10))
        values.update(changes)
        return values

    def _write(self, values, status=201, **kwargs):
        return self.request('post', self.base, {'storage': values},
                            version='1.19', status=status, **kwargs)

    def test_bind_and_volume_crud(self):
        self._disk()
        for kind in ('bind', 'volume'):
            with self.subTest(type=kind):
                created = self._write(self._mount(kind))
                self.assertEqual('true', created.headers[
                    'X-Coast-Redeploy-Required'])
                row = created.json['storage']
                self.assertEqual(kind, row['type'])
                self.assertEqual('user', row['origin'])
                self.assertEqual('/srv/data' if kind == 'bind' else 'data',
                                 row['source'])
                self.assertFalse(row['read_only'])
                self.assertEqual(0 if kind == 'bind' else 10,
                                 quota.usage(self.ctx)['in_use']['storage_gb'])
                quota.reconcile(self.ctx)
                self.assertEqual(0 if kind == 'bind' else 10,
                                 quota.usage(self.ctx)['in_use']['storage_gb'])
                item = self.base + '/' + row['id']
                changed = self.request('patch', item, {'storage': {
                    'read_only': True, 'container_path': '/new'}},
                    version='1.19').json['storage']
                self.assertTrue(changed['read_only'])
                self.assertEqual('/new', changed['container_path'])
                change = ({'host_path': '/etc'} if kind == 'bind' else
                          {'name': 'renamed'})
                updated = self.request('patch', item, {'storage': change},
                                       version='1.19').json['storage']
                self.assertEqual('/etc' if kind == 'bind' else 'renamed',
                                 updated['source'])
                listed = self.request('get', self.base + '?service_id=' +
                                      self.service.id, version='1.19').json
                self.assertEqual([updated], listed['storages'])
                self.request('delete', item, status=204, version='1.19')
                self.assertEqual(0, quota.usage(self.ctx)[
                    'in_use']['storage_gb'])
                self.request('get', item, status=404, version='1.19')

    def test_schema_and_normalized_host_path(self):
        for path in ('relative', '//srv/data', '/srv/../etc', '/srv/.',
                     '/srv//data', '/srv/data/', '/srv/ro:rw', '/srv/\\bad',
                     '/srv/\x00bad', '/srv/\nbad', '/srv/\ud800bad',
                     '/srv/\x7fbad'):
            self._write(self._mount(host_path=path), status=400)
        for changes in ({'name': 'invalid'}, {'size_gb': 1},
                        {'container_path': '/data/../etc'},
                        {'read_only': 'false'}, {'type': 'unknown'}):
            self._write(self._mount(**changes), status=400)
        for missing in ('service_id', 'host_path', 'container_path', 'type'):
            body = self._mount()
            del body[missing]
            self._write(body, status=400)
        self._write(self._mount('volume', host_path='/srv/data'), status=400)
        body = self._mount('volume')
        del body['size_gb']
        self._write(body, status=400)
        self.request('post', self.base, {'storage': self._mount()},
                     version='1.18', status=400)
        self.assertEqual([], list(objects.StorageList.get_all(self.ctx)))
        # Dedicated worker socket and system sources have no denylist.
        for index, path in enumerate(('/', '/etc', '/var/run/docker.sock')):
            self._write(self._mount(host_path=path,
                                    container_path='/data%s' % index))

    def test_typed_auth_isolation_busy_conflicts_and_quota(self):
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        self._write(self._mount(), headers=reader, status=403)
        self._write(self._mount(), headers={}, status=401)
        created = self._write(self._mount()).json['storage']
        item = self.base + '/' + created['id']
        self.request('patch', item, {'storage': {'read_only': True}},
                     headers=reader, status=403, version='1.19')
        self.request('delete', item, headers=reader, status=403,
                     version='1.19')
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        for method, body in (('get', None),
                             ('patch', {'storage': {'read_only': True}}),
                             ('delete', None)):
            self.request(method, item, body, headers=foreign,
                         status=404, version='1.19')
        self._write(self._mount(container_path='/data'), status=409)
        self.service.acquire_task(states.DEPLOYING)
        self._write(self._mount(container_path='/other'), status=409)
        self.request('patch', item, {'storage': {'read_only': True}},
                     status=409, version='1.19')
        self.request('delete', item, status=409, version='1.19')
        self.service.release_task(states.DEPLOYING)
        self._disk()
        self.config.config(storage_gb=0, group='quotas')
        self._write(self._mount('volume', container_path='/other'), status=403)
        self._write(self._mount(container_path='/other'))

    def test_mount_fields_filters_delete_header_and_invalid_edits(self):
        row = self._write(self._mount(read_only=True)).json['storage']
        item = self.base + '/' + row['id']
        filtered = self.request(
            'get', self.base + '?type=bind&origin=user&read_only=true&' +
            'fields=type,source,container_path,read_only,origin',
            version='1.19').json['storages']
        self.assertEqual(1, len(filtered))
        self.assertEqual('/srv/data', filtered[0]['source'])
        self.assertNotIn('name', filtered[0])
        for values in ({}, {'type': 'volume'}, {'size_gb': 1},
                       {'name': 'not-a-bind'}, {'host_path': '../bad'}):
            self.request('patch', item, {'storage': values},
                         status=400, version='1.19')
        self.request('post', item + '/detach', {}, status=409,
                     version='1.19')
        deleted = self.request('delete', item, status=204, version='1.19')
        self.assertEqual('true', deleted.headers['X-Coast-Redeploy-Required'])

    def test_managed_template_import_does_not_charge_volume_twice(self):
        self._disk()
        row = self._write(self._mount('volume')).json['storage']
        document = {'services': {'web': {'volumes': [{
            'type': 'volume', 'source': 'data', 'target': '/data',
            'x-coast-storage': row['id']}]}}, 'volumes': {'data': {}}}
        with db_api.writer.using(self.ctx):
            storage_rules.account_compose_volumes(
                self.ctx, self.service, document)
        self.assertEqual(1, len(objects.StorageList.get_all(self.ctx)))
        self.assertEqual(10, quota.usage(self.ctx)['in_use']['storage_gb'])

    def test_bind_file_overlap_and_worker_capacity(self):
        objects.FileMount(
            context=self.ctx, service_id=self.service.id,
            container_path='/etc/config', content_ref='opaque-ref').create()
        self._write(self._mount(container_path='/etc'), status=409)
        self._disk(size_gb=1)
        self._write(self._mount('volume'), status=409)
        self._write(self._mount())

    def test_typed_listing_has_template_bind_and_volume(self):
        self.service.engine_ref = 'engine-service'
        self.service.template_id = 'beszel'
        self.service.save()
        target = 'coast.api.v1.storage_controllers.rpcapi.ConductorAPI'
        mounts = [dict(type='bind', name='/var/run/docker.sock',
                       source='/var/run/docker.sock',
                       container_path='/var/run/docker.sock', read_only=True),
                  dict(type='volume', name='beszel-data', source='beszel-data',
                       container_path='/data', read_only=False)]
        with mock.patch(target) as api:
            api.return_value.list_volume_mounts.return_value = mounts
            listed = self.request('get', self.base + '?service_id=' +
                                  self.service.id, version='1.19').json
            rows = listed['storages']
            self.assertEqual({'bind', 'volume'}, {r['type'] for r in rows})
            bind = next(row for row in rows if row['type'] == 'bind')
            self.assertTrue(bind['read_only'])
            self.assertTrue(all(row['id'] is None and
                                row['origin'] == 'template' for row in rows))
            self.assertEqual('/var/run/docker.sock', bind['source'])
            self.assertEqual([], list(objects.StorageList.get_all(self.ctx)))


class TemplateStorageTest(app_api.AppAPITest):

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
        self.base = '/v1/stacks/' + self.stack.id + '/storages'

    def test_beszel_templates_are_creatable(self):
        for template in ('beszel', 'beszel-agent'):
            with self.subTest(template=template):
                body = self.create_body()
                body['service']['name'] = template
                body['service']['source'] = 'template:' + template
                result = self.request(
                    'post', '/v1/services', body, status=202,
                    version='1.17').json['service']
                self.assertEqual(template, result['template_id'])
                self.assertEqual(self.server.id, result['server_id'])

    def test_template_named_volume_and_host_bind_creation(self):
        body = self.create_body()
        body['service']['name'] = 'kuma'
        body['service']['source'] = 'template:uptime-kuma'
        created = self.request('post', '/v1/services', body, status=202,
                               version='1.7').json['service']
        rows = list(objects.StorageList.get_all(self.ctx))
        self.assertEqual(1, len(rows))
        self.assertEqual('template', rows[0].origin)
        self.assertEqual(states.IN_USE, rows[0].status)
        self.assertEqual('kuma-uptime-kuma-data', rows[0].name)
        self.assertEqual('/app/data', rows[0].container_path)
        item = self.base + '/' + rows[0].id
        self.request('post', item + '/detach', {}, status=409, version='1.10')
        self.request('delete', item, status=409, version='1.10')
        extra = self.request(
            'post', self.base, {'storage': {'name': 'extra', 'size_gb': 1}},
            status=201, version='1.10').json['storage']
        self.request(
            'post', self.base + '/' + extra['id'] + '/attach',
            {'attach': {'service_id': created['id'],
                        'container_path': '/var/extra'}},
            status=409, version='1.10')
        body['service']['name'] = 'mine'
        body['service']['source'] = 'template:minecraft'
        self.request('post', '/v1/services', body, status=202, version='1.17')
        self.assertEqual(
            {'kuma-uptime-kuma-data', 'extra'},
            {row.name for row in objects.StorageList.get_all(self.ctx)})
