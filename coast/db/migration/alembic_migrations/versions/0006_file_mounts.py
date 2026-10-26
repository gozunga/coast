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

"""Container config file mount references."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = '0006_file_mounts'
down_revision = '0005_service_compose'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('services', sa.Column(
        'file_mount_secret_refs', sa.JSON(), nullable=True))
    op.create_table(
        'file_mounts',
        sa.Column('service_id', sa.String(36), nullable=False),
        sa.Column('container_path', sa.String(255), nullable=False),
        sa.Column('content_ref', sa.String(1024), nullable=False),
        sa.Column('resource_name', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=False),
        sa.Column('updated_at', sa.DateTime().with_variant(
            mysql.DATETIME(fsp=6), 'mysql', 'mariadb'), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'],
                                name='fk_file_mounts_service_id_services'),
        sa.PrimaryKeyConstraint('id', name='pk_file_mounts'),
        sa.UniqueConstraint('service_id', 'container_path',
                            name='uq_file_mounts_service_id_container_path'),
        mysql_charset='utf8mb4', mysql_engine='InnoDB')
    op.create_index('ix_file_mounts_project_id', 'file_mounts', ['project_id'])


def downgrade():
    op.drop_table('file_mounts')
    with op.batch_alter_table('services') as batch:
        batch.drop_column('file_mount_secret_refs')
