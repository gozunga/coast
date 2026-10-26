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

"""Customer named storage and the storage_gb quota."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = '0003_storages'
down_revision = '0002_console_tokens'
branch_labels = None
depends_on = None


def _timestamp_type():
    return sa.DateTime().with_variant(
        mysql.DATETIME(fsp=6), 'mysql', 'mariadb')


def upgrade():
    op.create_table(
        'storages',
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(63), nullable=False),
        sa.Column('size_gb', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('origin', sa.String(16), nullable=False),
        sa.Column('service_id', sa.String(36), nullable=True),
        sa.Column('server_id', sa.String(36), nullable=True),
        sa.Column('container_path', sa.String(255), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_storages_stack_id_stacks'),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'],
                                name='fk_storages_service_id_services'),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'],
                                name='fk_storages_server_id_servers'),
        sa.PrimaryKeyConstraint('id', name='pk_storages'),
        sa.UniqueConstraint('stack_id', 'name',
                            name='uq_storages_stack_id_name'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB',
    )
    op.create_index('ix_storages_project_id', 'storages', ['project_id'])
    op.add_column('quotas', sa.Column('storage_gb', sa.Integer(),
                                      nullable=True))
    op.add_column('quota_usages', sa.Column(
        'in_use_storage_gb', sa.Integer(), nullable=False,
        server_default=sa.text('0')))
    op.add_column('quota_usages', sa.Column(
        'reserved_storage_gb', sa.Integer(), nullable=False,
        server_default=sa.text('0')))


def downgrade():
    with op.batch_alter_table('quota_usages') as batch:
        batch.drop_column('reserved_storage_gb')
        batch.drop_column('in_use_storage_gb')
    with op.batch_alter_table('quotas') as batch:
        batch.drop_column('storage_gb')
    op.drop_index('ix_storages_project_id', table_name='storages')
    op.drop_table('storages')
