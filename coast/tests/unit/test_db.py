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

import importlib
import io
from pathlib import Path
import shutil
from unittest import mock

from alembic.autogenerate import compare_metadata
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
import fixtures
from oslo_db import exception as db_exception
from oslo_db.sqlalchemy import test_migrations
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import mariadb
from sqlalchemy import orm

from coast.common import context
from coast.db import api
from coast.db import base_models
from coast.db import migration
from coast.db import models
from coast.tests import base


class MigrationTest(test_migrations.ModelsMigrationsSync, base.TestCase):

    def get_engine(self):
        return self.db.engine

    def get_metadata(self):
        return base_models.Base.metadata

    def db_sync(self, engine):
        command.upgrade(migration.get_config(engine), 'head')

    def include_object(self, object_, name, type_, reflected, compare_to):
        return name != 'alembic_version'

    def test_config_without_source_checkout(self):
        directory = Path(self.useFixture(fixtures.TempDir()).path)
        installed = directory / 'migration'
        installed.mkdir()
        shutil.copytree(Path(migration.__file__).with_name(
            'alembic_migrations'), installed / 'alembic_migrations')
        with mock.patch.object(migration, '__file__',
                               str(installed / '__init__.py')):
            config = migration.get_config(self.db.engine)
        self.assertIsNone(config.config_file_name)
        command.upgrade(config, 'head')
        with self.db.engine.connect() as connection:
            self.assertEqual(
                '0020_service_dockerfile', MigrationContext.configure(
                    connection).get_current_revision())

    def test_rotation_checkpoint_upgrade_and_downgrade(self):
        config = migration.get_config(self.db.engine)
        command.upgrade(config, '0018_service_fault')
        command.upgrade(config, '0019_deploy_public_key')
        columns = {c['name']: c for c in
                   sa.inspect(self.db.engine).get_columns('sources')}
        self.assertTrue(columns['deploy_key_rotation']['nullable'])
        command.downgrade(config, '0018_service_fault')
        self.assertNotIn('deploy_key_rotation', {c['name'] for c in
                         sa.inspect(self.db.engine).get_columns('sources')})

    def test_deploy_public_key_upgrade_and_downgrade(self):
        config = migration.get_config(self.db.engine)
        command.upgrade(config, '0018_service_fault')
        command.upgrade(config, '0019_deploy_public_key')
        columns = {c['name']: c for c in
                   sa.inspect(self.db.engine).get_columns('sources')}
        self.assertTrue(columns['deploy_public_key']['nullable'])
        self.assertIsInstance(columns['deploy_public_key']['type'], sa.Text)
        command.downgrade(config, '0018_service_fault')
        self.assertNotIn('deploy_public_key', {c['name'] for c in
                         sa.inspect(self.db.engine).get_columns('sources')})

    def test_hostname_upgrade_and_downgrade(self):
        config = migration.get_config(self.db.engine)
        command.upgrade(config, '0010_designate_dns')
        command.upgrade(config, '0011_server_hostname')
        columns = {c['name']: c for c in
                   sa.inspect(self.db.engine).get_columns('servers')}
        self.assertTrue(columns['hostname']['nullable'])
        self.assertEqual(63, columns['hostname']['type'].length)
        command.downgrade(config, '0010_designate_dns')
        self.assertNotIn('hostname', {c['name'] for c in
                         sa.inspect(self.db.engine).get_columns('servers')})

    def test_upgrade_is_repeatable(self):
        self.db_sync(self.db.engine)
        self.db_sync(self.db.engine)
        with self.db.engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={
                'compare_type': True, 'compare_server_default': True})
            self.assertEqual([], compare_metadata(ctx, self.get_metadata()))
            self.assertEqual('0020_service_dockerfile',
                             ctx.get_current_revision())

    def test_constraint_and_index_names_match_models(self):
        self.db_sync(self.db.engine)
        inspector = sa.inspect(self.db.engine)
        for table in self.get_metadata().tables.values():
            self.assertEqual(table.primary_key.name,
                             inspector.get_pk_constraint(table.name)['name'])
            self.assertEqual(
                {fk.name: (tuple(fk.column_keys), fk.referred_table.name,
                           tuple(element.column.name for element in
                                 fk.elements))
                 for fk in table.foreign_key_constraints},
                {fk['name']: (tuple(fk['constrained_columns']),
                              fk['referred_table'],
                              tuple(fk['referred_columns']))
                 for fk in inspector.get_foreign_keys(table.name)})
            self.assertEqual(
                {constraint.name: tuple(constraint.columns.keys())
                 for constraint in table.constraints
                 if isinstance(constraint, sa.UniqueConstraint)},
                {constraint['name']: tuple(constraint['column_names'])
                 for constraint in inspector.get_unique_constraints(
                     table.name)})
            self.assertEqual(
                {index.name: (tuple(index.columns.keys()), index.unique)
                 for index in table.indexes},
                {index['name']: (tuple(index['column_names']),
                                 bool(index['unique']))
                 for index in inspector.get_indexes(table.name)})

    def test_tenant_indexes_and_soft_delete(self):
        self.db_sync(self.db.engine)
        inspector = sa.inspect(self.db.engine)
        tables = self.get_metadata().tables
        self.assertEqual(25, len(tables))
        deleted = set()
        for name, table in tables.items():
            if name != 'service_metrics':
                self.assertFalse(table.c.project_id.nullable)
                indexes = inspector.get_indexes(name)
                self.assertIn(['project_id'],
                              [i['column_names'] for i in indexes])
            else:
                # Metrics inherit project ownership through their service FK.
                self.assertNotIn('project_id', table.c)
            cols = {c['name']: c for c in inspector.get_columns(name)}
            self.assertFalse(cols['created_at']['nullable'])
            self.assertTrue(cols['updated_at']['nullable'])
            self.assertTrue(inspector.get_pk_constraint(name)['name'])
            for constraint in table.constraints:
                self.assertIsNotNone(constraint.name)
            if 'deleted_at' in cols:
                deleted.add(name)
        self.assertEqual(
            {'stacks', 'appgroups', 'environments', 'servers',
             'services', 'deployments'}, deleted)
        self.assertTrue(next(iter(models.Service.__table__.c.
                                  current_deployment_id.foreign_keys)).
                        use_alter)

    def test_foreign_keys_and_uniqueness(self):
        self.db_sync(self.db.engine)
        with self.db.engine.begin() as conn:
            with self.assertRaises(db_exception.DBReferenceError):
                conn.execute(models.Event.__table__.insert().values(
                    id='event', project_id='project', stack_id='missing',
                    resource_type='service', resource_id='service',
                    action='create',
                    outcome='start'))
            conn.execute(models.Quota.__table__.insert().values(
                project_id='project'))
            with self.assertRaises(db_exception.DBDuplicateEntry):
                conn.execute(models.Quota.__table__.insert().values(
                    project_id='project'))

    def test_resource_uniqueness_and_circular_fk(self):
        self.db_sync(self.db.engine)
        with orm.Session(self.db.engine) as session:
            env = models.Stack(
                project_id='project', name='env', driver='noop',
                engine_version='test', plan='test', status='PENDING')
            source = models.Source(project_id='project', type='git')
            session.add_all([env, source])
            session.flush()
            service = models.Service(
                project_id='project', stack_id=env.id, name='service',
                source_id=source.id, ports='8080', auto_deploy=False,
                status='PENDING', task_state='NONE')
            session.add(service)
            session.flush()
            deployment = models.Deployment(
                project_id='project', service_id=service.id, number=1,
                spec={'ports': '8080'}, trigger='api', status='PENDING')
            session.add(deployment)
            session.flush()
            service.current_deployment_id = deployment.id
            session.commit()
            self.assertEqual(deployment.id, service.current_deployment_id)
            self.assertEqual({'ports': '8080'}, deployment.spec)
            values = [
                (models.Deployment, dict(
                    project_id='project', service_id=service.id,
                    number=1, spec={}, trigger='api', status='PENDING')),
                (models.Domain, dict(project_id='project',
                                     service_id=service.id,
                                     fqdn='service.example.com',
                                     verification_token='test',
                                     tls='letsencrypt', auto_dns=False,
                                     dns_mode='manual', status='ACTIVE')),
                (models.Secret, dict(project_id='project',
                                     service_id=service.id,
                                     key='KEY', is_secret=True,
                                     value_ref='opaque-ref', build_time=False,
                                     version=1)),
            ]
            for model, data in values:
                if model is not models.Deployment:
                    session.add(model(**data))
                    session.commit()
                with self.assertRaises(db_exception.DBDuplicateEntry):
                    with session.begin_nested():
                        session.add(model(**data))
                        session.flush()
            with self.assertRaises(db_exception.DBReferenceError):
                with session.begin_nested():
                    service.current_deployment_id = 'missing'
                    session.flush()

    def test_orm_defaults_and_update_timestamp(self):
        self.db_sync(self.db.engine)
        with orm.Session(self.db.engine) as session:
            quota = models.QuotaUsage(project_id='project')
            source = models.Source(project_id='project', type='git')
            session.add_all([quota, source])
            session.commit()
            self.assertIsNotNone(quota.created_at)
            self.assertIsNone(quota.updated_at)
            self.assertEqual(0, quota.in_use_apps)
            self.assertEqual('main', source.branch)
            self.assertEqual(36, len(source.id))
            quota.in_use_apps = 1
            session.commit()
            self.assertIsNotNone(quota.updated_at)

    def test_enginefacade_plumbing(self):
        self.useFixture(fixtures.MonkeyPatch(
            'coast.db.api._CONTEXT', self.db.facade))
        self.useFixture(fixtures.MonkeyPatch(
            'coast.db.api.writer', self.db.facade.writer))
        self.assertIs(self.db.engine, api.get_engine())
        session = api.get_session()
        self.addCleanup(session.close)
        self.assertEqual(1, session.execute(sa.text('SELECT 1')).scalar())
        ctx = context.RequestContext(project_id='project')
        with self.db.facade.writer.using(ctx):
            self.assertIsNotNone(ctx.session)

    def test_mysql_and_mariadb_schema_ddl_matches_models(self):
        initial = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.0001_initial')
        tokens = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0002_console_tokens')
        storages = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.0003_storages')
        kinds = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0004_service_kind')
        compose = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0005_service_compose')
        files = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.0006_file_mounts')
        storage_types = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0007_storage_types')
        backups = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0008_database_backups')
        metrics = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0009_service_metrics')
        dns = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0010_designate_dns')
        hostnames = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0011_server_hostname')
        primary = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0012_primary_domain')
        task_owner = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0013_task_owner')
        ingress = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0014_ingress_rules')
        live_names = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0015_live_names')
        rotation = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0016_engine_token_rotation')
        databases = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0017_databases')
        service_fault = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0018_service_fault')
        deploy_keys = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0019_deploy_public_key')
        dockerfile = importlib.import_module(
            'coast.db.migration.alembic_migrations.versions.'
            '0020_service_dockerfile')
        for dialect in (mysql.dialect(), mariadb.MariaDBDialect()):
            output = io.StringIO()
            ctx = MigrationContext.configure(dialect=dialect, opts={
                'as_sql': True, 'output_buffer': output})
            with Operations.context(ctx):
                initial.upgrade()
                tokens.upgrade()
                storages.upgrade()
                kinds.upgrade()
                compose.upgrade()
                files.upgrade()
                storage_types.upgrade()
                backups.upgrade()
                metrics.upgrade()
                dns.upgrade()
                hostnames.upgrade()
                primary.upgrade()
                task_owner.upgrade()
                ingress.upgrade()
                live_names.upgrade()
                rotation.upgrade()
                databases.upgrade()
                service_fault.upgrade()
                deploy_keys.upgrade()
                dockerfile.upgrade()
            actual = output.getvalue()
            expected = []
            engine = sa.create_mock_engine(
                dialect.name + '://',
                lambda ddl, *args, **kwargs: expected.append(
                    str(ddl.compile(dialect=dialect))))
            self.get_metadata().create_all(engine)

            def fold_added_columns(statements):
                # A later migration adds columns with ALTER TABLE. Model
                # create_all puts those columns on CREATE TABLE. Compare
                # the final column list, not the statement shape.
                adds = {}
                changes = {}
                kept = []
                removed = set()
                removed_columns = {}
                for statement in statements:
                    lines = [line.strip() for line in statement.splitlines()
                             if line.strip()]
                    if (len(lines) == 1 and
                            lines[0].startswith('ALTER TABLE ') and
                            ' ADD COLUMN ' in lines[0]):
                        head, column = lines[0].split(' ADD COLUMN ', 1)
                        adds.setdefault(head.split()[2], []).append(
                            column.rstrip(','))
                        continue
                    if (len(lines) == 1 and
                            lines[0].startswith('ALTER TABLE ') and
                            ' MODIFY ' in lines[0]):
                        head, column = lines[0].split(' MODIFY ', 1)
                        table = head.split()[2]
                        changes.setdefault(table, {})[column.split()[0]] = (
                            column.replace(' NULL', '').rstrip(','))
                        continue
                    if (len(lines) == 1 and
                            ' DROP COLUMN ' in lines[0]):
                        head, column = lines[0].split(' DROP COLUMN ', 1)
                        removed_columns.setdefault(head.split()[2], set()).add(
                            column.strip())
                        continue
                    if (len(lines) == 1 and
                            ' DROP INDEX uq_' in lines[0]):
                        removed.add(lines[0].split()[-1])
                        continue
                    if lines and lines[0].startswith('UPDATE '):
                        # Data backfills have no model equivalent.
                        continue
                    kept.append(statement)
                folded = []
                for statement in kept:
                    lines = [line for line in statement.splitlines()
                             if not any('CONSTRAINT ' + name + ' ' in line
                                        for name in removed)]
                    table = None
                    for line in lines:
                        stripped = line.strip()
                        if stripped.startswith('CREATE TABLE '):
                            table = stripped.split()[2]
                            break
                    if table in removed_columns:
                        names = removed_columns.pop(table)
                        lines = [line for line in lines if not (
                            line.strip() and line.strip().split()[0] in names)]
                    if table in changes:
                        columns = changes.pop(table)
                        lines = [columns.get(line.strip().split()[0], line)
                                 for line in lines if line.strip()]
                    if table in adds:
                        extra = [column + ','
                                 for column in adds.pop(table)]
                        lines = lines[:-1] + extra + lines[-1:]
                    folded.append('\n'.join(lines))
                self.assertEqual({}, adds)
                self.assertEqual({}, changes)
                return folded

            def normalize(statements):
                # SQLAlchemy can render equivalent constraints in a different
                # order. Compare each statement with all constraint names.
                # Table options (ENGINE/CHARSET) may also come in either order.
                def canon(line):
                    line = line.strip().rstrip(',;')
                    if line.startswith(')'):
                        line = ')' + ' '.join(sorted(line[1:].split()))
                    return line
                return sorted(tuple(sorted(canon(line)
                                           for line in statement.splitlines()
                                           if line.strip()))
                              for statement in statements if statement.strip())

            folded_expected = fold_added_columns(expected)
            folded_actual = fold_added_columns(actual.split(';'))
            self.assertEqual(normalize(folded_expected),
                             normalize(folded_actual))
            self.assertEqual(25, actual.count('CREATE TABLE'))
            # Preserve the existing mysql_* table options. SQLAlchemy's
            # mariadb-only dialect ignores them, as it does in the models.
            if dialect.name == 'mysql':
                self.assertEqual(25, actual.count('ENGINE=InnoDB'))
                self.assertEqual(25, actual.count('CHARSET=utf8mb4'))
            self.assertIn("health_check VARCHAR(16) NOT NULL DEFAULT 'none'",
                          actual)
            self.assertIn('unhealthy_since DATETIME(6)', actual)
            self.assertIn('running_since DATETIME(6)', actual)
            self.assertLess(actual.index('CREATE TABLE deployments'),
                            actual.index('ALTER TABLE services'))
            output.seek(0)
            output.truncate()
            with Operations.context(ctx):
                storages.downgrade()
            sql = output.getvalue()
            self.assertEqual(1, sql.count('DROP TABLE'))
            self.assertIn('storages', sql)
            output.seek(0)
            output.truncate()
            with Operations.context(ctx):
                tokens.downgrade()
            sql = output.getvalue()
            self.assertEqual(1, sql.count('DROP TABLE'))
            self.assertIn('console_tokens', sql)
            output.seek(0)
            output.truncate()
            with Operations.context(ctx):
                initial.downgrade()
            sql = output.getvalue()
            self.assertEqual(16, sql.count('DROP TABLE'))
            self.assertLess(sql.index('DROP FOREIGN KEY'),
                            sql.index('DROP TABLE'))
