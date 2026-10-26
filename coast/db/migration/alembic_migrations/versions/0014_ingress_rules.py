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

"""Store customer worker ingress rules."""

from alembic import op
import sqlalchemy as sa

from coast.db import base_models

revision = '0014_ingress_rules'
down_revision = '0013_task_owner'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ingress_rules',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('protocol', sa.String(4), nullable=False),
        sa.Column('port_range_min', sa.Integer),
        sa.Column('port_range_max', sa.Integer),
        sa.Column('source', sa.String(18), nullable=False),
        sa.Column('description', sa.String(255)),
        sa.Column('created_at', base_models.timestamp_type(), nullable=False),
        sa.Column('updated_at', base_models.timestamp_type()),
        sa.PrimaryKeyConstraint('id', name='pk_ingress_rules'),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_ingress_rules_stack_id_stacks'),
        mysql_engine='InnoDB', mysql_charset='utf8mb4')
    for field in ('project_id', 'stack_id'):
        op.create_index('ix_ingress_rules_' + field, 'ingress_rules', [field])


def downgrade():
    op.drop_table('ingress_rules')
