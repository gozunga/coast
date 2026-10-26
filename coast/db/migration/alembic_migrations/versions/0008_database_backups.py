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

"""Database backup policies, records and durable operations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = '0008_database_backups'
down_revision = '0007_storage_types'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'backup_accounts',
        sa.Column('key_ref', sa.String(1024), nullable=False),
        sa.Column('created_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=False),
        sa.Column('updated_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_backup_accounts'),
        sa.UniqueConstraint('project_id',
                            name='uq_backup_accounts_project_id'),
        mysql_charset='utf8mb4', mysql_engine='InnoDB')
    op.create_index('ix_backup_accounts_project_id', 'backup_accounts',
                    ['project_id'])
    op.create_table(
        'backup_policies',
        sa.Column('service_id', sa.String(length=36), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('hour', sa.Integer(), nullable=False),
        sa.Column('minute', sa.Integer(), nullable=False),
        sa.Column('keep_daily', sa.Integer(), nullable=False),
        sa.Column('keep_weekly', sa.Integer(), nullable=False),
        sa.Column('engine', sa.String(length=16), nullable=False),
        sa.Column('container', sa.String(length=128), nullable=False),
        sa.Column('key_ref', sa.String(length=1024), nullable=True),
        sa.Column('signing_key_ref', sa.String(length=1024), nullable=True),
        sa.Column('last_scheduled_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('created_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=False),
        sa.Column('updated_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(['service_id'],
                                ['services.id'],
                                name='fk_backup_policies_service_id_services'),
        sa.PrimaryKeyConstraint('id', name='pk_backup_policies'),
        sa.UniqueConstraint('service_id',
                            name='uq_backup_policies_service_id'),
        mysql_charset='utf8mb4', mysql_engine='InnoDB')
    op.create_index('ix_backup_policies_project_id', 'backup_policies',
                    ['project_id'])
    op.create_table(
        'backups',
        sa.Column('service_id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('started_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('finished_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('error', sa.String(length=255), nullable=True),
        sa.Column('engine', sa.String(length=16), nullable=False),
        sa.Column('engine_version', sa.String(length=128), nullable=True),
        sa.Column('container', sa.String(length=128), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('segments', sa.JSON(), nullable=True),
        sa.Column('key_ref', sa.String(length=1024), nullable=True),
        sa.Column('created_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=False),
        sa.Column('updated_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(['service_id'],
                                ['services.id'],
                                name='fk_backups_service_id_services'),
        sa.PrimaryKeyConstraint('id', name='pk_backups'),
        mysql_charset='utf8mb4', mysql_engine='InnoDB')
    op.create_index('ix_backups_service_id', 'backups',
                    ['service_id'])
    op.create_index('ix_backups_project_id', 'backups',
                    ['project_id'])
    op.create_table(
        'backup_jobs',
        sa.Column('backup_id', sa.String(length=36), nullable=False),
        sa.Column('service_id', sa.String(length=36), nullable=False),
        sa.Column('operation', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('error', sa.String(length=255), nullable=True),
        sa.Column('started_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('finished_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('safety_backup_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=False),
        sa.Column('updated_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(['backup_id'],
                                ['backups.id'],
                                name='fk_backup_jobs_backup_id_backups'),
        sa.ForeignKeyConstraint(['service_id'],
                                ['services.id'],
                                name='fk_backup_jobs_service_id_services'),
        sa.PrimaryKeyConstraint('id', name='pk_backup_jobs'),
        mysql_charset='utf8mb4', mysql_engine='InnoDB')
    op.create_index('ix_backup_jobs_project_id', 'backup_jobs',
                    ['project_id'])


def downgrade():
    op.drop_table('backup_jobs')
    op.drop_table('backups')
    op.drop_table('backup_policies')
    op.drop_table('backup_accounts')
