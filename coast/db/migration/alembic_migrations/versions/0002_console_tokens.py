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

"""Add single-use console session tokens."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = '0002_console_tokens'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def _timestamp_type():
    return sa.DateTime().with_variant(
        mysql.DATETIME(fsp=6), 'mysql', 'mariadb')


def upgrade():
    op.create_table(
        'console_tokens',
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('resource_type', sa.String(16), nullable=False),
        sa.Column('resource_id', sa.String(36), nullable=False),
        sa.Column('command', sa.JSON(), nullable=True),
        sa.Column('user_id', sa.String(255), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_console_tokens_stack_id_stacks'),
        sa.PrimaryKeyConstraint('id', name='pk_console_tokens'),
        sa.UniqueConstraint('token_hash',
                            name='uq_console_tokens_token_hash'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_console_tokens_project_id', 'console_tokens',
                    ['project_id'])
    op.create_index('ix_console_tokens_stack_id', 'console_tokens',
                    ['stack_id'])
    op.create_index('ix_console_tokens_resource_id', 'console_tokens',
                    ['resource_id'])


def downgrade():
    op.drop_table('console_tokens')
