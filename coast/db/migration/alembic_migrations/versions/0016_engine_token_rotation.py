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

"""Drop unused engine backup and checkpoint engine token rotation."""

from alembic import op
import sqlalchemy as sa

revision = '0016_engine_token_rotation'
down_revision = '0015_live_names'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('stacks') as batch:
        batch.drop_column('engine_backup_ref')
        batch.add_column(sa.Column('engine_token_previous_ref', sa.String(255),
                                   nullable=True))
        batch.add_column(sa.Column('engine_token_name', sa.String(80),
                                   nullable=True))
        batch.add_column(sa.Column('engine_token_previous_name', sa.String(80),
                                   nullable=True))
        batch.add_column(sa.Column('engine_token_rotation_id', sa.String(36),
                                   nullable=True))
        batch.add_column(sa.Column('engine_token_created_at', sa.DateTime(),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table('stacks') as batch:
        batch.drop_column('engine_token_created_at')
        batch.drop_column('engine_token_rotation_id')
        batch.drop_column('engine_token_previous_name')
        batch.drop_column('engine_token_name')
        batch.drop_column('engine_token_previous_ref')
        batch.add_column(sa.Column('engine_backup_ref', sa.String(255),
                                   nullable=True))
