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

"""Record service and stack task holders."""

from alembic import op
import sqlalchemy as sa

from coast.db import base_models

revision = '0013_task_owner'
down_revision = '0012_primary_domain'
branch_labels = None
depends_on = None


def upgrade():
    for table in ('services', 'stacks'):
        op.add_column(table, sa.Column('task_owner', sa.String(64)))
        op.add_column(table, sa.Column(
            'task_started_at', base_models.timestamp_type()))


def downgrade():
    for table in ('services', 'stacks'):
        op.drop_column(table, 'task_started_at')
        op.drop_column(table, 'task_owner')
