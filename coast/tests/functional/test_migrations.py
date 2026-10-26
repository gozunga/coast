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

from alembic.autogenerate import compare_metadata
from alembic import command
from alembic.migration import MigrationContext
from oslo_db.sqlalchemy import test_fixtures
from oslo_db.sqlalchemy import test_migrations
from oslo_utils import timeutils
import sqlalchemy as sa

from coast.db import base_models
from coast.db import migration
from coast.tests import base


class SQLiteMigrationTest(test_migrations.ModelsMigrationsSync, base.TestCase):

    def get_engine(self):
        return self.db.engine

    def get_metadata(self):
        return base_models.Base.metadata

    def db_sync(self, engine):
        command.upgrade(migration.get_config(engine), 'head')

    def include_object(self, object_, name, type_, reflected, compare_to):
        return name != 'alembic_version'

    def test_service_fault_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0017_databases')
        table = sa.Table('services', sa.MetaData(), autoload_with=engine)
        stack = sa.Table('stacks', sa.MetaData(), autoload_with=engine)
        source = sa.Table('sources', sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(stack.insert().values(
                id='stack', project_id='project', name='stack', plan='small',
                driver='noop', engine_version='test', status='ACTIVE',
                task_state='NONE', created_at=timeutils.utcnow()))
            connection.execute(source.insert().values(
                id='source', project_id='project', type='git',
                created_at=timeutils.utcnow()))
            connection.execute(table.insert().values(
                id='database', project_id='project', stack_id='stack',
                source_id='source', name='database', kind='database',
                status='ERROR', task_state='NONE', replicas=1,
                auto_deploy=False, created_at=timeutils.utcnow()))
        command.upgrade(config, 'head')
        table = sa.Table('services', sa.MetaData(), autoload_with=engine)
        self.assertTrue(table.c.fault.nullable)
        self.assertIsInstance(table.c.fault.type, sa.Text)
        with engine.begin() as connection:
            self.assertIsNone(connection.scalar(sa.select(table.c.fault)))
            connection.execute(table.update().values(fault='Safe reason'))
        command.downgrade(config, '0017_databases')
        columns = sa.inspect(engine).get_columns('services')
        self.assertNotIn('fault', {column['name'] for column in columns})

    def test_deploy_public_key_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0018_service_fault')
        command.upgrade(config, '0019_deploy_public_key')
        columns = {c['name']: c for c in
                   sa.inspect(engine).get_columns('sources')}
        self.assertTrue(columns['deploy_public_key']['nullable'])
        self.assertIsInstance(columns['deploy_public_key']['type'], sa.Text)
        command.downgrade(config, '0018_service_fault')
        self.assertNotIn('deploy_public_key', {c['name'] for c in
                         sa.inspect(engine).get_columns('sources')})

    def test_database_columns_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0016_engine_token_rotation')
        before = {column['name'] for column in
                  sa.inspect(engine).get_columns('services')}
        command.upgrade(config, '0017_databases')
        added = {column['name'] for column in
                 sa.inspect(engine).get_columns('services')} - before
        self.assertEqual({
            'engine', 'engine_version', 'db_name', 'db_username',
            'public_enabled', 'public_port', 'public_source',
            'password_ref', 'root_password_ref'}, added)
        command.downgrade(config, '0016_engine_token_rotation')
        self.assertEqual(before, {column['name'] for column in
                                  sa.inspect(engine).get_columns('services')})

    def test_engine_rotation_upgrade_removes_backup_preserves_token(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0015_live_names')
        metadata = sa.MetaData()
        metadata.reflect(bind=engine)
        with engine.begin() as connection:
            connection.execute(metadata.tables['stacks'].insert().values(
                id='stack', project_id='project', name='stack', plan='small',
                driver='noop', engine_version='test', status='ACTIVE',
                task_state='NONE', created_at=timeutils.utcnow(),
                engine_token_ref='token-ref', engine_backup_ref='backup-ref'))
        command.upgrade(config, 'head')
        metadata = sa.MetaData()
        metadata.reflect(bind=engine)
        stack = metadata.tables['stacks']
        self.assertNotIn('engine_backup_ref', stack.c)
        with engine.connect() as connection:
            row = connection.execute(sa.select(stack)).mappings().one()
        self.assertEqual('token-ref', row['engine_token_ref'])
        self.assertIsNone(row['engine_token_previous_ref'])
        self.assertIsNone(row['engine_token_rotation_id'])
        self.assertIsNone(row['engine_token_name'])

    def test_live_names_upgrade_preserves_deleted_rows(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0014_ingress_rules')
        metadata = sa.MetaData()
        metadata.reflect(bind=engine)
        now = timeutils.utcnow()
        with engine.begin() as connection:
            connection.execute(metadata.tables['stacks'].insert().values(
                id='stack', project_id='project', name='stack', plan='small',
                driver='noop', engine_version='test', status='ACTIVE',
                task_state='NONE', created_at=now))
            for table, parent in (('appgroups', 'stack_id'),
                                  ('environments', 'appgroup_id')):
                connection.execute(metadata.tables[table].insert().values(
                    id=table, project_id='project', name='reusable',
                    status='DELETING', task_state='NONE', created_at=now,
                    deleted_at=now,
                    **{parent: 'stack' if table == 'appgroups'
                       else 'appgroups'}))
        command.upgrade(config, 'head')
        with engine.begin() as connection:
            if engine.dialect.name == 'sqlite':
                self.assertEqual(1, connection.exec_driver_sql(
                    'PRAGMA foreign_keys').scalar())
            for table, parent in (('appgroups', 'stack_id'),
                                  ('environments', 'appgroup_id')):
                connection.execute(metadata.tables[table].insert().values(
                    id=table + '-new', project_id='project', name='reusable',
                    status='ACTIVE', task_state='NONE', created_at=now,
                    **{parent: 'stack' if table == 'appgroups'
                       else 'appgroups'}))
                self.assertEqual(2, connection.scalar(sa.select(
                    sa.func.count()).select_from(metadata.tables[table])))
            # Remove replacements before restoring the old constraints.
            for table in ('environments', 'appgroups'):
                connection.execute(metadata.tables[table].delete().where(
                    metadata.tables[table].c.id == table + '-new'))
        command.downgrade(config, '0014_ingress_rules')
        for table in ('appgroups', 'environments'):
            self.assertEqual(1, len(sa.inspect(engine).get_unique_constraints(
                table)))

    def test_upgrade_to_head_is_repeatable(self):
        engine = self.get_engine()
        self.assertEqual([], sa.inspect(engine).get_table_names())
        self.db_sync(engine)
        self.db_sync(engine)
        with engine.connect() as connection:
            self.assertEqual(
                '0020_service_dockerfile', MigrationContext.configure(
                    connection).get_current_revision())
        inspector = sa.inspect(engine)
        for table in self.get_metadata().tables.values():
            expected = {fk.name: (tuple(fk.column_keys),
                                  fk.referred_table.name)
                        for fk in table.foreign_key_constraints}
            actual = {fk['name']: (tuple(fk['constrained_columns']),
                                   fk['referred_table'])
                      for fk in inspector.get_foreign_keys(table.name)}
            self.assertEqual(expected, actual, table.name)

    def test_dockerfile_reference_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0019_deploy_public_key')
        self.assertNotIn('dockerfile_ref', {
            item['name']
            for item in sa.inspect(engine).get_columns('services')})
        command.upgrade(config, 'head')
        column = next(item for item in sa.inspect(engine).get_columns(
            'services') if item['name'] == 'dockerfile_ref')
        self.assertTrue(column['nullable'])
        self.assertEqual(255, column['type'].length)
        command.downgrade(config, '0019_deploy_public_key')
        self.assertNotIn('dockerfile_ref', {
            item['name']
            for item in sa.inspect(engine).get_columns('services')})

    def test_compose_reference_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0004_service_kind')
        self.assertNotIn('compose_ref', {
            item['name']
            for item in sa.inspect(engine).get_columns('services')})
        command.upgrade(config, 'head')
        column = next(
            item for item in sa.inspect(engine).get_columns('services')
            if item['name'] == 'compose_ref')
        self.assertTrue(column['nullable'])
        self.assertEqual(255, column['type'].length)
        command.downgrade(config, '0004_service_kind')
        self.assertNotIn('compose_ref', {
            item['name']
            for item in sa.inspect(engine).get_columns('services')})

    def test_storage_types_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0006_file_mounts')
        command.upgrade(config, 'head')
        columns = {item['name']: item for item in
                   sa.inspect(engine).get_columns('storages')}
        self.assertTrue(columns['name']['nullable'])
        self.assertTrue(columns['size_gb']['nullable'])
        self.assertEqual(4096, columns['host_path']['type'].length)
        self.assertFalse(columns['type']['nullable'])
        self.assertFalse(columns['read_only']['nullable'])
        command.downgrade(config, '0006_file_mounts')
        columns = {item['name']: item for item in
                   sa.inspect(engine).get_columns('storages')}
        self.assertNotIn('type', columns)
        self.assertNotIn('host_path', columns)
        self.assertNotIn('read_only', columns)
        self.assertFalse(columns['name']['nullable'])
        self.assertFalse(columns['size_gb']['nullable'])

    def test_metrics_upgrade_after_database_backups(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0008_database_backups')
        tables = set(sa.inspect(engine).get_table_names())
        self.assertIn('backups', tables)
        self.assertNotIn('service_metrics', tables)
        command.upgrade(config, 'head')
        self.assertEqual(tables | {'service_metrics', 'ingress_rules'},
                         set(sa.inspect(engine).get_table_names()))
        command.downgrade(config, '0008_database_backups')
        self.assertEqual(tables, set(sa.inspect(engine).get_table_names()))

    def test_designate_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0009_service_metrics')
        self.assertNotIn('zone_id', {c['name'] for c in
                         sa.inspect(engine).get_columns('domains')})
        command.upgrade(config, 'head')
        columns = {c['name']: c for c in
                   sa.inspect(engine).get_columns('domains')}
        for name in ('zone_id', 'managed_records', 'status_reason',
                     'dns_previous', 'verification_started_at'):
            self.assertTrue(columns[name]['nullable'])
        command.downgrade(config, '0009_service_metrics')
        self.assertNotIn('zone_id', {c['name'] for c in
                         sa.inspect(engine).get_columns('domains')})

    def test_primary_domain_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0011_server_hostname')
        metadata = sa.MetaData()
        metadata.reflect(bind=engine)
        rows = {
            'stacks': dict(id='stack', name='stack', plan='small',
                           driver='noop', engine_version='test',
                           status='ACTIVE', task_state='NONE'),
            'sources': dict(id='source', type='git'),
            'services': dict(id='app', stack_id='stack', name='web',
                             source_id='source', replicas=1,
                             auto_deploy=False, status='ACTIVE',
                             task_state='NONE'),
            'domains': dict(id='domain', service_id='app',
                            fqdn='www.example.com', verification_token='token',
                            tls='none', auto_dns=False, dns_mode='manual',
                            status='ACTIVE')}
        with engine.begin() as connection:
            for table, values in rows.items():
                connection.execute(metadata.tables[table].insert().values(
                    project_id='project', created_at=timeutils.utcnow(),
                    **values))
        command.upgrade(config, 'head')
        metadata = sa.MetaData()
        with engine.connect() as connection:
            for table, name, expected in (
                    ('services', 'default_domain_enabled', True),
                    ('domains', 'primary', False)):
                reflected = sa.Table(table, metadata, autoload_with=engine)
                self.assertEqual(expected, connection.scalar(
                    sa.select(reflected.c[name])))
                self.assertFalse(reflected.c[name].nullable)
        command.downgrade(config, '0011_server_hostname')
        for table, name in (('services', 'default_domain_enabled'),
                            ('domains', 'primary')):
            self.assertNotIn(name, {c['name'] for c in
                             sa.inspect(engine).get_columns(table)})

    def test_task_owner_upgrade_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        command.upgrade(config, '0012_primary_domain')
        metadata = sa.MetaData()
        stack = sa.Table('stacks', metadata, autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(stack.insert().values(
                id='stack', project_id='project', name='stack', plan='small',
                driver='noop', engine_version='test', status='ACTIVE',
                task_state='NONE', created_at=timeutils.utcnow()))
        for table in ('services', 'stacks'):
            self.assertNotIn('task_owner', {c['name'] for c in
                             sa.inspect(engine).get_columns(table)})
        command.upgrade(config, 'head')
        for table in ('services', 'stacks'):
            columns = {c['name']: c for c in
                       sa.inspect(engine).get_columns(table)}
            self.assertTrue(columns['task_owner']['nullable'])
            self.assertEqual(64, columns['task_owner']['type'].length)
            self.assertTrue(columns['task_started_at']['nullable'])
        stack = sa.Table('stacks', sa.MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            self.assertEqual((None, None), tuple(connection.execute(
                sa.select(stack.c.task_owner, stack.c.task_started_at)).one()))
        command.downgrade(config, '0012_primary_domain')
        for table in ('services', 'stacks'):
            columns = {c['name'] for c in
                       sa.inspect(engine).get_columns(table)}
            self.assertNotIn('task_owner', columns)
            self.assertNotIn('task_started_at', columns)

    def test_fresh_install_and_downgrade(self):
        engine = self.get_engine()
        config = migration.get_config(engine)
        self.assertEqual([], sa.inspect(engine).get_table_names())
        command.upgrade(config, 'head')
        with engine.connect() as connection:
            ctx = MigrationContext.configure(connection, opts={
                'compare_type': True, 'compare_server_default': True})
            self.assertEqual([], compare_metadata(ctx, self.get_metadata()))
            self.assertEqual('0020_service_dockerfile',
                             ctx.get_current_revision())
        command.downgrade(config, 'base')
        self.assertEqual(['alembic_version'],
                         sa.inspect(engine).get_table_names())
        with engine.connect() as connection:
            self.assertIsNone(MigrationContext.configure(
                connection).get_current_revision())
        command.upgrade(config, 'head')


class MySQLMigrationTest(test_fixtures.OpportunisticDBTestMixin,
                         SQLiteMigrationTest):
    FIXTURE = test_fixtures.MySQLOpportunisticFixture

    def get_engine(self):
        return self._get_default_provisioned_db().engine
