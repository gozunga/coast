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

"""Per-container CPU and memory history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = '0009_service_metrics'
down_revision = '0008_database_backups'
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime().with_variant(
        mysql.DATETIME(fsp=6), 'mysql', 'mariadb')
    op.create_table(
        'service_metrics',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('service_id', sa.String(36), nullable=False),
        sa.Column('container_name', sa.String(128), nullable=False),
        sa.Column('resolution', sa.Integer(), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('cpu_percent', sa.Float(), nullable=False),
        sa.Column('memory_bytes', sa.BigInteger(), nullable=False),
        sa.Column('memory_limit_bytes', sa.BigInteger(), nullable=True),
        sa.Column('sample_count', sa.Integer(), nullable=False),
        sa.Column('created_at', timestamp, nullable=False),
        sa.Column('updated_at', timestamp, nullable=True),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'],
                                name='fk_service_metrics_service_id_services'),
        sa.PrimaryKeyConstraint('id', name='pk_service_metrics'),
        sa.UniqueConstraint(
            'service_id', 'container_name', 'resolution', 'timestamp',
            name='uq_service_metrics_bucket'),
        mysql_charset='utf8mb4', mysql_engine='InnoDB')
    op.create_index('ix_service_metrics_service_resolution_timestamp',
                    'service_metrics',
                    ['service_id', 'resolution', 'timestamp'])
    op.create_index('ix_service_metrics_resolution_timestamp',
                    'service_metrics', ['resolution', 'timestamp'])


def downgrade():
    op.drop_table('service_metrics')
