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

import concurrent.futures
import datetime
import tempfile
import threading
from unittest import mock

import fixtures
from oslo_db import exception as db_exception
from oslo_db.sqlalchemy import enginefacade
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from coast.common import context
from coast.common import exception
from coast.common import states
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast.tests import base


REPOSITORIES = (
    api.stacks, api.appgroups, api.environments, api.servers, api.volumes,
    api.sources, api.services, api.deployments, api.domains, api.secrets,
    api.service_volume_mounts, api.exports, api.events, api.storages,
    api.console_tokens)


class RepositoryTest(base.TestCase):

    def setUp(self):
        super().setUp()
        self._use_facade(self.db.facade)
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(project_id='project')
        self.other = context.RequestContext(project_id='other')
        self.admin = context.RequestContext(
            project_id='project', is_admin=True)
        self.rows = self._resources(self.ctx)
        self.other_rows = self._resources(self.other)

    def test_service_update_checks_unique_only_for_changed_key(self):
        service = self.rows[api.services]
        with mock.patch.object(api.services, '_unique',
                               wraps=api.services._unique) as unique:
            api.services.update(self.ctx, service.id, {'status': 'ERROR'})
            api.services.update(self.ctx, service.id, {'name': service.name})
            unique.assert_not_called()
            sibling = self._service(self.ctx, self.rows[api.stacks],
                                    self.rows[api.sources], name='sibling')
            unique.reset_mock()
            self.assertRaises(exception.Conflict, api.services.update,
                              self.ctx, service.id, {'name': sibling.name})
            unique.assert_called_once()

    def _use_facade(self, facade):
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(facade, name)))

    def _env(self, ctx, name='env'):
        return api.stacks.create(ctx, dict(
            name=name, driver='noop', engine_version='test', plan='test',
            status='PENDING'))

    def _server(self, ctx, env, role='worker', name='server'):
        return api.servers.create(ctx, dict(
            stack_id=env.id, name=name, kind='vm', role=role,
            status='PENDING', task_state='NONE'))

    def _service(self, ctx, env, source, name='service', server=None):
        return api.services.create(ctx, dict(
            stack_id=env.id, source_id=source.id, name=name,
            server_id=server.id if server else None, ports='8080',
            auto_deploy=False, status='PENDING', task_state='NONE'))

    def _deployment(self, ctx, service):
        return api.deployments.create(ctx, dict(
            service_id=service.id, spec={'ports': '8080'}, trigger='api',
            status='PENDING'))

    def _appgroup(self, ctx, env, name='website'):
        return api.appgroups.create(ctx, dict(
            stack_id=env.id, name=name, status='PENDING',
            task_state='NONE'))

    def _app_environment(self, ctx, appgroup, name='production'):
        return api.environments.create(ctx, dict(
            appgroup_id=appgroup.id, name=name, status='PENDING',
            task_state='NONE'))

    def _resources(self, ctx):
        env = self._env(ctx)
        appgroup = self._appgroup(ctx, env)
        app_environment = self._app_environment(ctx, appgroup)
        server = self._server(ctx, env)
        volume = api.volumes.create(ctx, dict(
            stack_id=env.id, server_id=server.id, name='data', size_gb=1,
            purpose='data', status='CREATING'))
        source = api.sources.create(ctx, dict(type='git'))
        service = self._service(ctx, env, source, server=server)
        deployment = self._deployment(ctx, service)
        domain = api.domains.create(ctx, dict(
            service_id=service.id, fqdn=ctx.project_id + '.example.com',
            verification_token='test', tls='letsencrypt', auto_dns=False,
            dns_mode='manual', status='PENDING_VERIFICATION'))
        secret = api.secrets.create(ctx, dict(
            service_id=service.id, key='KEY', is_secret=True,
            value_ref='opaque-ref',
            build_time=False, version=1))
        mount = api.service_volume_mounts.create(ctx, dict(
            service_id=service.id, volume_id=volume.id, sub_path='uploads',
            container_path='/uploads', read_only=False))
        export = api.exports.create(ctx, dict(
            stack_id=env.id, status='PENDING', volume_ids=[volume.id],
            backend='swift'))
        event = api.events.create(ctx, dict(
            stack_id=env.id, resource_type='service', resource_id=service.id,
            action='create', outcome='start'))
        storage = api.storages.create(ctx, dict(
            stack_id=env.id, name='uploads', size_gb=1,
            status='AVAILABLE', origin='api'))
        token = api.console_tokens.create(ctx, dict(
            token_hash=(ctx.project_id + ('0' * 64))[:64],
            stack_id=env.id, resource_type='app', resource_id=service.id,
            user_id='user', expires_at=datetime.datetime(2026, 1, 2)))
        return dict(zip(REPOSITORIES, (
            env, appgroup, app_environment, server, volume, source,
            service, deployment, domain, secret, mount, export, event,
            storage, token)))

    def test_isolation_for_every_resource(self):
        for repo in REPOSITORIES:
            with self.subTest(resource=repo.model.__tablename__):
                row = self.other_rows[repo]
                self.assertRaises(exception.NotFound, repo.get,
                                  self.ctx, row.id)
                self.assertEqual([], repo.list(
                    self.ctx, filters={'id': row.id}))
                self.assertEqual([], repo.list(
                    self.ctx, filters={'project_id': self.other.project_id}))
                self.assertRaises(exception.NotFound, repo.update,
                                  self.ctx, row.id, {})
                self.assertRaises(exception.NotFound, repo.delete,
                                  self.ctx, row.id)
                self.assertRaises(exception.NotFound, repo.get,
                                  self.ctx, row.id, show_deleted=True)
                self.assertEqual(row.id, repo.get(self.other, row.id).id)
                self.assertEqual([self.rows[repo].id],
                                 [r.id for r in repo.list(self.ctx)])

    def test_admin_requires_explicit_all_projects(self):
        for repo in REPOSITORIES:
            with self.subTest(resource=repo.model.__tablename__):
                row = self.other_rows[repo]
                self.assertRaises(exception.NotFound, repo.get,
                                  self.admin, row.id)
                self.assertEqual(1, len(repo.list(self.admin)))
                self.assertEqual(row.id, repo.get(
                    self.admin, row.id, all_projects=True).id)
                self.assertEqual(2, len(repo.list(
                    self.admin, all_projects=True)))
                self.assertRaises(exception.NotFound, repo.update,
                                  self.admin, row.id, {})
                self.assertEqual(row.id, repo.update(
                    self.admin, row.id, {}, all_projects=True).id)
                self.assertRaises(exception.NotFound, repo.delete,
                                  self.admin, row.id)
                self.assertRaises(exception.NotAuthorized, repo.list,
                                  self.ctx, all_projects=True)
                self.assertRaises(exception.NotAuthorized, repo.get,
                                  self.ctx, row.id, all_projects=True)
                self.assertRaises(exception.NotAuthorized, repo.update,
                                  self.ctx, row.id, {}, all_projects=True)
                self.assertRaises(exception.NotAuthorized, repo.delete,
                                  self.ctx, row.id, all_projects=True)
        row = self.other_rows[api.events]
        api.events.delete(self.admin, row.id, all_projects=True)
        self.assertRaises(exception.NotFound, api.events.get,
                          self.other, row.id)

    def test_create_scope_cannot_be_spoofed(self):
        values = dict(type='git', project_id=self.other.project_id)
        self.assertRaises(exception.NotFound, api.sources.create,
                          self.ctx, values)
        self.assertRaises(exception.NotFound, api.sources.create,
                          self.admin, values)
        self.assertRaises(exception.NotAuthorized, api.sources.create,
                          self.ctx, values, all_projects=True)
        row = api.sources.create(self.admin, values, all_projects=True)
        self.assertEqual(self.other.project_id, row.project_id)
        unscoped = context.RequestContext(is_admin=True)
        self.assertRaises(exception.NotAuthorized, api.sources.list, unscoped)
        self.assertRaises(exception.Invalid, api.sources.create, unscoped,
                          {'type': 'git'}, all_projects=True)

    def test_foreign_project_parent_is_not_found(self):
        for repo in REPOSITORIES:
            row = self.rows[repo]
            for column in repo.model.__table__.columns:
                for fk in column.foreign_keys:
                    parent_repo = api._TABLES[fk.column.table.name]
                    foreign = self.other_rows[parent_repo]
                    values = {c.name: getattr(row, c.name)
                              for c in repo.model.__table__.columns
                              if c.name not in (
                                  'id', 'created_at', 'updated_at',
                                  'deleted_at', 'number')}
                    values[column.name] = foreign.id
                    with self.subTest(resource=repo.model.__tablename__,
                                      field=column.name):
                        self.assertRaises(exception.NotFound, repo.create,
                                          self.ctx, values)
                        self.assertRaises(exception.NotFound, repo.create,
                                          self.admin, values,
                                          all_projects=True)
        self.assertRaises(exception.NotFound, api.services.update,
                          self.ctx, self.rows[api.services].id,
                          {'source_id': self.other_rows[api.sources].id})

    def test_parent_stack_and_mount_placement(self):
        env = self._env(self.ctx, 'second')
        server = self._server(self.ctx, env)
        service = self.rows[api.services]
        self.assertRaises(exception.Invalid, api.services.update, self.ctx,
                          service.id,
                          {'server_id': server.id})
        volume = api.volumes.create(self.ctx, dict(
            stack_id=env.id, server_id=server.id, name='other-data',
            size_gb=1, purpose='data', status='CREATING'))
        self.assertRaises(exception.Invalid, api.service_volume_mounts.update,
                          self.ctx, self.rows[api.service_volume_mounts].id,
                          {'volume_id': volume.id})
        other_server = self._server(self.ctx, self.rows[api.stacks])
        volume = api.volumes.create(self.ctx, dict(
            stack_id=self.rows[api.stacks].id,
            server_id=other_server.id, name='other-data', size_gb=1,
            purpose='data', status='CREATING'))
        self.assertRaises(exception.Invalid, api.service_volume_mounts.update,
                          self.ctx, self.rows[api.service_volume_mounts].id,
                          {'volume_id': volume.id})
        other_service = self._service(self.ctx, self.rows[api.stacks],
                                      self.rows[api.sources], 'second')
        other_deployment = self._deployment(self.ctx, other_service)
        self.assertRaises(exception.Invalid, api.services.update, self.ctx,
                          service.id,
                          {'current_deployment_id': other_deployment.id})
        self.assertRaises(exception.Invalid, api.deployments.create, self.ctx,
                          dict(service_id=service.id, spec={},
                               trigger='rollback',
                               status='PENDING',
                               rollback_of=other_deployment.id))

    def test_pagination_filters_and_marker_for_every_resource(self):
        self.config.config(max_limit=1, group='api')
        for repo in REPOSITORIES:
            with self.subTest(resource=repo.model.__tablename__):
                rows = sorted([self.rows[repo], self.other_rows[repo]],
                              key=lambda row: row.id)
                first = repo.list(self.admin, all_projects=True, sort_key='id',
                                  limit=100)
                self.assertEqual([rows[0].id], [r.id for r in first])
                second = repo.list(self.admin, all_projects=True,
                                   sort_key='id',
                                   marker=first[0].id)
                self.assertEqual([rows[1].id], [r.id for r in second])
                self.assertEqual([], repo.list(
                    self.admin, all_projects=True, sort_key='id',
                    marker=second[0].id))
                self.assertEqual([rows[1].id], [r.id for r in repo.list(
                    self.admin, all_projects=True, sort_key='id',
                    sort_dir='desc')])
                self.assertRaises(exception.NotFound, repo.list, self.ctx,
                                  marker=self.other_rows[repo].id)
                self.assertRaises(exception.NotFound, repo.list, self.ctx,
                                  marker='missing')
                self.assertEqual([], repo.list(self.ctx, limit=0))
                self.assertRaises(exception.Invalid, repo.list, self.ctx,
                                  filters={'missing': 'value'})
                self.assertRaises(exception.Invalid, repo.list, self.ctx,
                                  sort_key='missing')
                self.assertRaises(exception.Invalid, repo.list, self.ctx,
                                  sort_dir='invalid')
                for limit in (-1, True, '1'):
                    self.assertRaises(exception.Invalid, repo.list, self.ctx,
                                      limit=limit)

    def test_nullable_sort_and_ties_do_not_skip_rows(self):
        envs = [self.rows[api.stacks]]
        for name in ('b', 'c', 'd'):
            envs.append(self._env(self.ctx, name))
        api.stacks.update(self.ctx, envs[2].id, {'description': 'same'})
        api.stacks.update(self.ctx, envs[3].id, {'description': 'same'})
        for direction in ('asc', 'desc'):
            whole = api.stacks.list(self.ctx, sort_key='description',
                                    sort_dir=direction)
            ids, marker = [], None
            while True:
                page = api.stacks.list(
                    self.ctx, sort_key='description', sort_dir=direction,
                    marker=marker, limit=1)
                if not page:
                    break
                self.assertNotIn(page[0].id, ids)
                ids.append(page[0].id)
                marker = page[0].id
            self.assertEqual([row.id for row in whole], ids)
            self.assertEqual(4, len(ids))

    def test_child_names_reserved_until_soft_delete(self):
        for repo in (api.services, api.environments, api.appgroups):
            with self.subTest(resource=repo.model.__tablename__):
                row = self.rows[repo]
                values = {column.name: getattr(row, column.name)
                          for column in repo.model.__table__.columns
                          if column.name not in (
                              'id', 'created_at', 'updated_at', 'deleted_at')}
                for status in (states.ACTIVE, states.DELETING):
                    repo.update(self.ctx, row.id, {'status': status})
                    error = self.assertRaises(
                        exception.Conflict, repo.create, self.ctx, values)
                    self.assertIn('already exists', str(error))
                    if status == states.DELETING:
                        self.assertIn('Deletion is still in progress',
                                      str(error))
                repo.delete(self.ctx, row.id)
                for attempt in range(2):
                    replacement = repo.create(self.ctx, values)
                    self.assertNotEqual(row.id, replacement.id)
                    self.assertEqual(row.name, replacement.name)
                    repo.delete(self.ctx, replacement.id)

    def test_soft_delete_visibility_and_name_reuse(self):
        for repo in (api.servers, api.deployments, api.services,
                     api.stacks):
            row = self.rows[repo]
            api_row = repo.get(self.ctx, row.id)
            repo.delete(self.ctx, row.id)
            self.assertRaises(exception.NotFound, repo.get, self.ctx, row.id)
            self.assertEqual([], repo.list(self.ctx))
            deleted = repo.get(self.ctx, row.id, show_deleted=True)
            self.assertIsNotNone(deleted.deleted_at)
            self.assertEqual(api_row.status, deleted.status)
            self.assertEqual([row.id], [r.id for r in repo.list(
                self.ctx, show_deleted=True)])
            self.assertRaises(exception.NotFound, repo.update,
                              self.ctx, row.id, {})
            self.assertRaises(exception.NotFound, repo.delete,
                              self.ctx, row.id)
            self.assertRaises(exception.NotFound, repo.list,
                              self.ctx, marker=row.id)
        env = self._env(self.ctx)
        service = self._service(self.ctx, env, self.rows[api.sources])
        self.assertNotEqual(self.rows[api.stacks].id, env.id)
        self.assertNotEqual(self.rows[api.services].id, service.id)
        api.services.delete(self.ctx, service.id)
        self._service(self.ctx, env, self.rows[api.sources])

    def test_updates_and_hard_deletes(self):
        updates = {
            api.stacks: {'description': 'changed'},
            api.servers: {'name': 'changed'},
            api.volumes: {'size_gb': 2},
            api.sources: {'branch': 'stable'},
            api.services: {'ports': '9090'},
            api.deployments: {'engine_ref': 'observed'},
            api.domains: {'dns_mode': 'designate'},
            api.secrets: {'version': 2},
            api.service_volume_mounts: {'read_only': True},
            api.exports: {'artifacts': []},
            api.events: {'outcome': 'success'},
            api.storages: {'container_path': '/var/data'},
        }
        for repo, values in updates.items():
            row = repo.update(self.ctx, self.rows[repo].id, values)
            self.assertIsNotNone(row.updated_at)
            for key, value in values.items():
                self.assertEqual(value, getattr(
                    repo.get(self.ctx, row.id), key))
        for repo in (api.events, api.exports, api.service_volume_mounts,
                     api.secrets, api.domains, api.storages):
            repo.delete(self.ctx, self.rows[repo].id)
            self.assertRaises(exception.NotFound, repo.get, self.ctx,
                              self.rows[repo].id, show_deleted=True)
        server = self._server(self.ctx, self.rows[api.stacks])
        volume = api.volumes.create(self.ctx, dict(
            stack_id=self.rows[api.stacks].id, server_id=server.id,
            name='extra', size_gb=1, purpose='data', status='CREATING'))
        source = api.sources.create(self.ctx, {'type': 'image'})
        for repo, row in ((api.volumes, volume), (api.sources, source)):
            repo.delete(self.ctx, row.id)
            self.assertRaises(exception.NotFound, repo.get, self.ctx, row.id,
                              show_deleted=True)
        api.servers.delete(self.ctx, server.id)
        self.assertRaises(exception.NotFound, api.servers.get,
                          self.ctx, server.id)
        self.assertIsNotNone(api.servers.get(
            self.ctx, server.id, show_deleted=True).deleted_at)

    def test_live_name_uniqueness_create_and_rename(self):
        self.assertRaises(exception.Conflict, self._env, self.ctx)
        env = self._env(self.ctx, 'second')
        self.assertRaises(exception.Conflict, api.stacks.update,
                          self.ctx, env.id, {'name': 'env'})
        source = self.rows[api.sources]
        self.assertRaises(exception.Conflict, self._service, self.ctx,
                          self.rows[api.stacks], source)
        service = self._service(self.ctx, self.rows[api.stacks],
                                source, 'second')
        self.assertRaises(exception.Conflict, api.services.update,
                          self.ctx, service.id, {'name': 'service'})
        self._service(self.ctx, env, source)  # Same name in another stack.
        api.services.update(self.ctx, service.id, {'name': 'second'})
        with api.reader.using(self.ctx):
            usage = self.ctx.session.query(models.QuotaUsage).filter_by(
                project_id=self.ctx.project_id).one()
            for column in models.QuotaUsage.__table__.columns:
                if column.name.startswith(('in_use_', 'reserved_')):
                    self.assertEqual(0, getattr(usage, column.name))

    def test_single_control_server_create_and_update(self):
        env = self.rows[api.stacks]
        control = self._server(self.ctx, env, role='control')
        self.assertRaises(exception.Conflict, self._server,
                          self.ctx, env, role='control')
        self.assertRaises(exception.Conflict, api.servers.update,
                          self.ctx, self.rows[api.servers].id,
                          {'role': 'control'})
        for role in ('CONTROL', 'invalid'):
            self.assertRaises(exception.Invalid, self._server,
                              self.ctx, env, role=role)
            self.assertRaises(exception.Invalid, api.servers.update,
                              self.ctx, self.rows[api.servers].id,
                              {'role': role})
        self._server(self.ctx, env, role='build')
        self._server(self.ctx, env, role='build')
        api.servers.delete(self.ctx, control.id)
        self.assertRaises(exception.NotFound, api.servers.get,
                          self.ctx, control.id)
        kept = api.servers.get(self.ctx, control.id, show_deleted=True)
        self.assertIsNotNone(kept.deleted_at)
        replacement = self._server(self.ctx, env, role='control')
        self.assertNotEqual(control.id, replacement.id)

    def test_soft_deleted_server_keeps_history_foreign_keys(self):
        server = self.rows[api.servers]
        service = self.rows[api.services]
        deployment = api.deployments.create(self.ctx, dict(
            service_id=service.id, spec={'ports': '8080'}, trigger='api',
            status='PENDING', server_id=server.id))
        api.servers.delete(self.ctx, server.id)
        self.assertRaises(exception.NotFound, api.servers.get,
                          self.ctx, server.id)
        self.assertEqual([], api.servers.list(
            self.ctx, filters={'id': server.id}))
        kept = api.servers.get(self.ctx, server.id, show_deleted=True)
        self.assertIsNotNone(kept.deleted_at)
        self.assertEqual(server.id, api.services.get(
            self.ctx, service.id).server_id)
        self.assertEqual(server.id, api.deployments.get(
            self.ctx, deployment.id).server_id)
        self.assertRaises(exception.NotFound, api.deployments.create,
                          self.ctx, dict(
                              service_id=service.id, spec={}, trigger='api',
                              status='PENDING', server_id=server.id))

    def test_deployment_numbers_include_deleted_history(self):
        service = self.rows[api.services]
        first = self.rows[api.deployments]
        self.assertEqual(1, first.number)
        second = self._deployment(self.ctx, service)
        self.assertEqual(2, second.number)
        api.deployments.delete(self.ctx, second.id)
        self.assertEqual(3, self._deployment(self.ctx, service).number)
        self.assertEqual(1, self.other_rows[api.deployments].number)
        for values in ({'number': 99}, {'spec': {'ports': 'changed'}},
                       {'trigger': 'webhook'}, {'project_id': 'other'}):
            self.assertRaises(exception.Invalid, api.deployments.update,
                              self.ctx, first.id, values)
        self.assertRaises(exception.Invalid, api.deployments.create, self.ctx,
                          dict(service_id=service.id, number=99, spec={},
                               trigger='api',
                               status='PENDING'))

    def test_cas_and_release_for_task_resources(self):
        for repo in (api.stacks, api.servers, api.services):
            row = self.rows[repo]
            model = repo.model
            acquired = api.cas_task_state(self.ctx, model, row.id,
                                          new=states.PROVISIONING)
            self.assertEqual(states.PROVISIONING, acquired.task_state)
            self.assertRaises(exception.Conflict, api.cas_task_state,
                              self.ctx, model, row.id, new=states.DEPLOYING)
            self.assertRaises(exception.Conflict, api.release_task_state,
                              self.ctx, model, row.id,
                              expected=states.DEPLOYING)
            released = api.release_task_state(self.ctx, model, row.id,
                                              expected=states.PROVISIONING)
            self.assertEqual(states.NONE, released.task_state)
            self.assertRaises(exception.NotFound, api.cas_task_state,
                              self.other, model, row.id, new=states.DEPLOYING)
            self.assertRaises(exception.Invalid, api.cas_task_state,
                              self.ctx, model, row.id, new='INVALID')
            self.assertRaises(exception.Invalid, repo.update,
                              self.ctx, row.id,
                              {'task_state': states.DEPLOYING})
        self.assertRaises(exception.Invalid, api.cas_task_state, self.ctx,
                          models.Volume, self.rows[api.volumes].id,
                          new=states.DEPLOYING)

    def test_volume_status_cas_and_extend(self):
        volume = self.rows[api.volumes]
        self.assertRaises(exception.InvalidStateTransition, api.cas_status,
                          self.ctx, models.Volume, volume.id,
                          new=states.SNAPSHOTTING, expected=states.CREATING)
        updated = api.cas_status(
            self.ctx, models.Volume, volume.id, new=states.AVAILABLE,
            expected=states.CREATING)
        self.assertEqual(states.AVAILABLE, updated.status)
        self.assertIsNotNone(updated.updated_at)
        self.assertRaises(exception.Conflict, api.cas_status, self.ctx,
                          models.Volume, volume.id, new=states.SNAPSHOTTING,
                          expected=states.CREATING)
        self.assertRaises(exception.Invalid, api.cas_status, self.ctx,
                          models.Volume, volume.id, new=states.AVAILABLE,
                          expected=states.AVAILABLE)
        self.assertRaises(exception.NotFound, api.cas_status, self.other,
                          models.Volume, volume.id, new=states.DELETING,
                          expected=states.AVAILABLE)
        grown = api.set_volume_size(self.ctx, volume.id, 2)
        self.assertEqual(2, grown.size_gb)
        for size in (2, 1, True):
            self.assertRaises(exception.Invalid, api.set_volume_size,
                              self.ctx, volume.id, size)
        self.assertEqual(2, api.volumes.get(self.ctx, volume.id).size_gb)
        self.assertRaises(exception.NotFound, api.set_volume_size,
                          self.other, volume.id, 3)

    def test_extend_locks_the_volume_row(self):
        volume = self.rows[api.volumes]
        real_get = api.volumes._get
        with mock.patch.object(api.volumes, '_get', wraps=real_get) as get:
            grown = api.set_volume_size(self.ctx, volume.id, 3)
        self.assertEqual(3, grown.size_gb)
        get.assert_called_once_with(self.ctx, volume.id, False, lock=True)

    def test_status_transition_is_checked_on_all_updates(self):
        targets = ((api.stacks, states.BUILDING, states.ACTIVE),
                   (api.servers, states.BUILDING, states.ACTIVE),
                   (api.volumes, states.AVAILABLE, states.IN_USE),
                   (api.services, states.STOPPED, states.DEGRADED),
                   (api.deployments, states.BUILDING, states.SUPERSEDED),
                   (api.domains, states.VERIFIED, states.ACTIVE),
                   (api.exports, states.SNAPSHOTTING, states.ACTIVE))
        for repo, valid, invalid in targets:
            row = self.rows[repo]
            self.assertRaises(exception.InvalidStateTransition, repo.update,
                              self.ctx, row.id, {'status': invalid})
            changed = api.update_status(self.ctx, repo.model, row.id, valid)
            self.assertEqual(valid, changed.status)
            self.assertRaises(exception.NotFound, api.update_status,
                              self.other, repo.model, row.id, valid)
            self.assertEqual(valid, repo.get(self.ctx, row.id).status)
        self.assertRaises(exception.Invalid, api.update_status, self.ctx,
                          models.Source, self.rows[api.sources].id,
                          states.ACTIVE)

    def test_secret_errors_have_no_sql_or_references(self):
        values = dict(service_id=self.rows[api.services].id, key='KEY',
                      is_secret=True,
                      value_ref='sensitive-reference', build_time=False,
                      version=1)
        with self.assertRaises(exception.Conflict) as raised:
            api.secrets.create(self.ctx, values)
        self.assertEqual('Resource conflict.', str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        with self.assertRaises(exception.Invalid):
            api.secrets.create(self.ctx, dict(values, value='plaintext'))

    def test_project_sentinel_duplicate_keeps_transaction_usable(self):
        with api.writer.using(self.ctx):
            with mock.patch.object(sa.orm.Query, 'first', return_value=None):
                row = api._lock_project(self.ctx.session, self.ctx.project_id)
            self.assertEqual(self.ctx.project_id, row.project_id)
            self.ctx.session.add(models.Source(project_id=self.ctx.project_id,
                                               type='git'))
            self.ctx.session.flush()
        self.assertEqual(2, len(api.sources.list(self.ctx)))

    def test_lock_query_uses_mysql_for_update(self):
        with api.writer.using(self.ctx):
            query = self.ctx.session.query(models.Stack).filter_by(
                project_id=self.ctx.project_id)
            locked = api._lock_query(self.ctx.session, query,
                                     models.Stack)
            sql = str(locked.statement.compile(dialect=mysql.dialect()))
            self.assertIn('FOR UPDATE', sql)
            self.assertIn('stacks.project_id =', sql)

    def test_locking_does_not_change_parent_timestamps(self):
        env = self._env(self.ctx, 'timestamp')
        self.assertIsNone(env.updated_at)
        server = self._server(self.ctx, env)
        self.assertIsNone(api.stacks.get(self.ctx, env.id).updated_at)
        api.servers.update(self.ctx, server.id, {'name': 'changed'})
        self.assertIsNone(api.stacks.get(self.ctx, env.id).updated_at)
        service = self.rows[api.services]
        before = api.services.get(self.ctx, service.id).updated_at
        self._deployment(self.ctx, service)
        self.assertEqual(before, api.services.get(
            self.ctx, service.id).updated_at)

    def test_export_volumes_must_belong_to_owner_and_stack(self):
        values = dict(stack_id=self.rows[api.stacks].id,
                      status='PENDING', backend='swift',
                      volume_ids=[self.other_rows[api.volumes].id])
        self.assertRaises(exception.NotFound, api.exports.create,
                          self.ctx, values)
        env = self._env(self.ctx, 'second')
        values.update(stack_id=env.id,
                      volume_ids=[self.rows[api.volumes].id])
        self.assertRaises(exception.Invalid, api.exports.create,
                          self.ctx, values)

    def test_duplicate_rollback_does_not_poison_outer_transaction(self):
        with api.writer.using(self.ctx):
            with self.assertRaises(exception.Conflict):
                # The caller owns the transaction and must roll it back if a
                # flush fails; a name conflict is checked before insertion.
                self._env(self.ctx)
            row = api.sources.create(self.ctx, {'type': 'image'})
        self.assertEqual('image', api.sources.get(self.ctx, row.id).type)

    def test_boolean_pagination_and_tied_sort_keys(self):
        for build_time in (False, True, True):
            api.secrets.create(self.ctx, dict(
                service_id=self.rows[api.services].id, key=str(build_time) +
                str(len(api.secrets.list(self.ctx))), is_secret=True,
                value_ref='opaque-ref', build_time=build_time, version=1))
        for direction in ('asc', 'desc'):
            whole = api.secrets.list(self.ctx, sort_key='build_time',
                                     sort_dir=direction)
            seen, marker = [], None
            for unused in range(len(whole)):
                page = api.secrets.list(self.ctx, sort_key='build_time',
                                        sort_dir=direction, marker=marker,
                                        limit=1)
                seen.extend(row.id for row in page)
                marker = page[0].id
            self.assertEqual([row.id for row in whole], seen)
            self.assertEqual([], api.secrets.list(
                self.ctx, sort_key='build_time', sort_dir=direction,
                marker=marker, limit=1))

    def test_real_sqlite_concurrent_writes(self):
        # A file DB gives competing threads independent connections. The
        # remaining tests use the required in-memory DatabaseFixture.
        with tempfile.TemporaryDirectory() as directory:
            facade = enginefacade.transaction_context()
            facade.configure(connection='sqlite:///' + directory + '/db',
                             sqlite_fk=True)
            engine = facade.writer.get_engine()
            self.addCleanup(engine.dispose)
            self._use_facade(facade)
            base_models.Base.metadata.create_all(engine)
            ctx = context.RequestContext(project_id='race')

            def race(operation):
                barrier = threading.Barrier(2)

                def run():
                    local = context.RequestContext(project_id='race')
                    barrier.wait(timeout=10)
                    try:
                        return operation(local)
                    except exception.Conflict:
                        return 'conflict'

                with concurrent.futures.ThreadPoolExecutor(2) as executor:
                    futures = [executor.submit(run) for unused in range(2)]
                    return [future.result(timeout=15) for future in futures]

            results = race(lambda local: self._env(local))
            self.assertEqual(1, results.count('conflict'))
            self.assertEqual(1, len(api.stacks.list(ctx)))
            env = api.stacks.list(ctx)[0]
            source = api.sources.create(ctx, {'type': 'git'})
            results = race(lambda local: self._service(local, env, source))
            self.assertEqual(1, results.count('conflict'))
            service = api.services.list(ctx)[0]
            results = race(lambda local: self._server(local, env, 'control'))
            self.assertEqual(1, results.count('conflict'))
            results = race(lambda local: self._deployment(local, service))
            self.assertEqual([1, 2], sorted(row.number for row in results))
            results = race(lambda local: api.cas_task_state(
                local, models.Service, service.id, new=states.DEPLOYING))
            self.assertEqual(1, results.count('conflict'))
            self.assertEqual(states.DEPLOYING,
                             api.services.get(ctx, service.id).task_state)

    def test_deadlock_retries_own_transaction_without_leaking_parameters(self):
        lock_project = api._lock_project
        attempts = []

        def deadlock_once(session, project_id):
            attempts.append(project_id)
            if len(attempts) == 1:
                session.add(models.Source(project_id=project_id, type='image'))
                session.flush()
                raise db_exception.DBDeadlock('sensitive-reference')
            return lock_project(session, project_id)

        logger = self.useFixture(fixtures.FakeLogger(name='oslo_db.api',
                                                     level='DEBUG'))
        with mock.patch.object(api, '_lock_project',
                               side_effect=deadlock_once):
            with mock.patch('oslo_db.api.time.sleep'):
                row = self._env(self.ctx, 'retry')
        self.assertEqual('retry', api.stacks.get(self.ctx, row.id).name)
        self.assertNotIn('sensitive-reference', logger.output)
        self.assertEqual(1, len(api.sources.list(self.ctx)))

    def test_top_level_create_retries_deadlock_and_rolls_back_writes(self):
        original = api.stacks._unique
        source_ids = {row.id for row in api.sources.list(self.ctx)}
        stack_ids = {row.id for row in api.stacks.list(self.ctx)}
        rolled_back_ids = []
        attempts = []

        def deadlock_once(ctx, values, *args, **kwargs):
            attempts.append(values['name'])
            if len(attempts) == 1:
                marker = models.Source(project_id=ctx.project_id, type='image')
                ctx.session.add(marker)
                ctx.session.flush()
                rolled_back_ids.append(marker.id)
                raise db_exception.DBDeadlock()
            return original(ctx, values, *args, **kwargs)

        with mock.patch.object(api.stacks, '_unique',
                               side_effect=deadlock_once) as create_attempt:
            row = self._env(self.ctx, 'top-level-create-retry')
        self.assertEqual(2, create_attempt.call_count)
        self.assertEqual(['top-level-create-retry'] * 2, attempts)
        self.assertEqual('top-level-create-retry',
                         api.stacks.get(self.ctx, row.id).name)
        self.assertEqual(stack_ids | {row.id},
                         {stack.id for stack in api.stacks.list(self.ctx)})
        persisted_ids = {source.id for source in api.sources.list(self.ctx)}
        self.assertEqual(source_ids, persisted_ids)
        self.assertEqual(1, len(rolled_back_ids))
        self.assertNotIn(rolled_back_ids[0], persisted_ids)

    def test_nested_create_defers_deadlock_to_transaction_owner(self):
        outer_attempts = []
        inner_attempts = []
        original = api.stacks._unique

        def deadlock_once(*args, **kwargs):
            inner_attempts.append(len(outer_attempts))
            if len(inner_attempts) == 1:
                raise db_exception.DBDeadlock()
            return original(*args, **kwargs)

        @api.retry_transaction
        def operation(ctx):
            outer_attempts.append(True)
            with api.writer.using(ctx):
                api.sources.create(ctx, {'type': 'image'})
                return self._env(ctx, 'nested-retry')

        before = len(api.sources.list(self.ctx))
        with mock.patch.object(api.stacks, '_unique',
                               side_effect=deadlock_once):
            row = operation(self.ctx)
        self.assertEqual([1, 2], inner_attempts)
        self.assertEqual(2, len(outer_attempts))
        self.assertEqual(before + 1, len(api.sources.list(self.ctx)))
        self.assertEqual('nested-retry', api.stacks.get(self.ctx, row.id).name)

    def test_nested_update_defers_deadlock_to_transaction_owner(self):
        self._check_write_deadlock_retry('update', nested=True)

    def test_top_level_update_retries_deadlock(self):
        self._check_write_deadlock_retry('update', nested=False)

    def test_nested_delete_defers_deadlock_to_transaction_owner(self):
        self._check_write_deadlock_retry('delete', nested=True)

    def test_top_level_delete_retries_deadlock(self):
        self._check_write_deadlock_retry('delete', nested=False)

    def _check_write_deadlock_retry(self, method, nested):
        row = self._env(self.ctx, 'write-retry')
        outer_attempts = []
        inner_attempts = []
        original = api._lock_project
        before = len(api.sources.list(self.ctx))

        def contend(*args, **kwargs):
            inner_attempts.append(len(outer_attempts))
            if len(inner_attempts) == 1:
                raise db_exception.DBDeadlock()
            return original(*args, **kwargs)

        def write(ctx):
            if method == 'update':
                return api.stacks.update(ctx, row.id, {'name': 'updated'})
            return api.stacks.delete(ctx, row.id)

        @api.retry_transaction
        def operation(ctx):
            outer_attempts.append(True)
            with api.writer.using(ctx):
                api.sources.create(ctx, {'type': 'image'})
                write(ctx)

        with mock.patch.object(api, '_lock_project', side_effect=contend):
            if nested:
                operation(self.ctx)
            else:
                write(self.ctx)
        self.assertEqual([1, 2] if nested else [0, 0], inner_attempts)
        self.assertEqual(2 if nested else 0, len(outer_attempts))
        self.assertEqual(before + int(nested), len(api.sources.list(self.ctx)))
        if method == 'update':
            self.assertEqual('updated', api.stacks.get(self.ctx, row.id).name)
        else:
            self.assertRaises(exception.NotFound, api.stacks.get,
                              self.ctx, row.id)

    def test_task_acquire_retries_complete_transaction(self):
        service = self.rows[api.services]
        original = api.services._get
        attempts = []

        def deadlock_once(ctx, id, *args, **kwargs):
            attempts.append(id)
            if len(attempts) == 2:
                # The CAS update has happened. It must roll back on retry.
                raise db_exception.DBDeadlock('sensitive-reference')
            return original(ctx, id, *args, **kwargs)

        with mock.patch.object(api.services, '_get',
                               side_effect=deadlock_once):
            with mock.patch('oslo_db.api.time.sleep'):
                row = api.cas_task_state(self.ctx, models.Service, service.id,
                                         new=states.RECONCILING,
                                         owner='domain_change')
        self.assertEqual(states.RECONCILING, row.task_state)
        self.assertEqual('domain_change', row.task_owner)
        self.assertEqual(4, len(attempts))

    def test_task_acquire_deadlock_exhaustion_is_conflict(self):
        service = self.rows[api.services]
        logger = self.useFixture(fixtures.FakeLogger(name='oslo_db.api',
                                                     level='DEBUG'))
        with mock.patch.object(api.services, '_get', side_effect=(
                db_exception.DBDeadlock('sensitive-reference'))) as get:
            with mock.patch('oslo_db.api.time.sleep'):
                self.assertRaises(exception.Conflict, api.cas_task_state,
                                  self.ctx, models.Service, service.id,
                                  new=states.RECONCILING)
        self.assertEqual(4, get.call_count)
        self.assertNotIn('sensitive-reference', logger.output)
        self.assertEqual(states.NONE,
                         api.services.get(self.ctx, service.id).task_state)

    def test_deadlock_in_outer_transaction_propagates(self):
        with api.writer.using(self.ctx):
            with mock.patch.object(
                    api, '_lock_project',
                    side_effect=db_exception.DBDeadlock('sensitive-ref')):
                with self.assertRaises(db_exception.DBDeadlock) as raised:
                    self._env(self.ctx, 'retry')
                self.assertNotIn('sensitive-ref', str(raised.exception))

    def test_admin_cas_release_and_status_require_explicit_scope(self):
        service = self.other_rows[api.services]
        self.assertRaises(
            exception.NotFound, api.cas_task_state, self.admin,
            models.Service, service.id, new=states.DEPLOYING)
        acquired = api.cas_task_state(self.admin, models.Service, service.id,
                                      new=states.DEPLOYING, all_projects=True)
        self.assertEqual(states.DEPLOYING, acquired.task_state)
        self.assertRaises(exception.NotFound, api.release_task_state,
                          self.admin, models.Service, service.id,
                          states.DEPLOYING)
        released = api.release_task_state(self.admin, models.Service,
                                          service.id,
                                          states.DEPLOYING, all_projects=True)
        self.assertEqual(states.NONE, released.task_state)
        self.assertRaises(exception.NotFound, api.update_status,
                          self.admin, models.Service, service.id,
                          states.STOPPED)
        changed = api.update_status(self.admin, models.Service, service.id,
                                    states.STOPPED, all_projects=True)
        self.assertEqual(states.STOPPED, changed.status)
        for operation, args in (
                (api.cas_task_state, (states.DEPLOYING,)),
                (api.release_task_state, (states.DEPLOYING,)),
                (api.update_status, (states.ACTIVE,))):
            self.assertRaises(exception.NotAuthorized, operation, self.ctx,
                              models.Service, service.id, *args,
                              all_projects=True)

    def test_system_fields_and_invalid_create_values_are_rejected(self):
        for values in ({}, {'name': 'bad'}, {'status': 'UNKNOWN'}):
            self.assertRaises(exception.Invalid, api.stacks.create,
                              self.ctx, values)
        for field in ('id', 'project_id', 'created_at', 'updated_at',
                      'deleted_at', 'stack_id', 'service_id'):
            self.assertRaises(exception.Invalid, api.services.update,
                              self.ctx, self.rows[api.services].id,
                              {field: 'bad'})
        self.assertRaises(exception.Invalid, api.deployments.list, self.ctx,
                          sort_key='spec')
        self.assertRaises(exception.Invalid, api.secrets.list, self.ctx,
                          filters={'value_ref': ['invalid']})

    def test_hard_delete_with_dependents_is_rejected_without_sql_leak(self):
        source = self.rows[api.sources]
        with self.assertRaises(exception.Invalid) as raised:
            api.sources.delete(self.ctx, source.id)
        self.assertEqual('Invalid request.', str(raised.exception))
        self.assertEqual(source.id, api.sources.get(self.ctx, source.id).id)
        self.assertEqual(self.rows[api.services].id,
                         api.services.get(self.ctx,
                                          self.rows[api.services].id).id)

    def test_event_can_reference_own_soft_deleted_stack(self):
        env = self.rows[api.stacks]
        service = self.rows[api.services]
        api.services.delete(self.ctx, service.id)
        api.stacks.delete(self.ctx, env.id)
        resources = (('stack', env.id), ('service', service.id))
        for resource_type, resource_id in resources:
            with self.subTest(resource_type=resource_type):
                event = api.events.create(self.ctx, dict(
                    stack_id=env.id, resource_type=resource_type,
                    resource_id=resource_id, action='delete',
                    outcome='success'))
                stored = api.events.get(self.ctx, event.id)
                self.assertEqual(env.id, stored.stack_id)
                self.assertEqual(resource_id, stored.resource_id)
        for repo in (api.servers, api.volumes, api.services, api.deployments,
                     api.domains, api.secrets, api.service_volume_mounts,
                     api.exports):
            row = self.rows[repo]
            values = {column.name: getattr(row, column.name)
                      for column in repo.model.__table__.columns
                      if column.name not in ('id', 'created_at', 'updated_at',
                                             'deleted_at', 'number')}
            with self.subTest(resource=repo.model.__tablename__):
                self.assertRaises(exception.NotFound, repo.create,
                                  self.ctx, values)

    def test_event_cannot_reference_foreign_live_or_deleted_stack(self):
        env = self.other_rows[api.stacks]
        for deleted in (False, True):
            if deleted:
                api.stacks.delete(self.other, env.id)
            for ctx in (self.ctx, self.admin):
                with self.subTest(deleted=deleted, admin=ctx.is_admin):
                    values = dict(
                        project_id=self.ctx.project_id, stack_id=env.id,
                        resource_type='stack', resource_id=env.id,
                        action='delete', outcome='success')
                    self.assertRaises(exception.NotFound, api.events.create,
                                      ctx, values)
                    self.assertRaises(exception.NotFound, api.events.create,
                                      ctx, values, all_projects=ctx.is_admin)
