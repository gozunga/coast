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

"""Standalone database metadata and secret references."""

from alembic import op
import sqlalchemy as sa

revision = '0017_databases'
down_revision = '0016_engine_token_rotation'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('services') as batch:
        batch.add_column(sa.Column('engine', sa.String(32), nullable=True))
        batch.add_column(sa.Column('engine_version',
                         sa.String(32), nullable=True))
        batch.add_column(sa.Column('db_name', sa.String(63), nullable=True))
        batch.add_column(
            sa.Column('db_username', sa.String(63), nullable=True))
        batch.add_column(sa.Column('public_enabled',
                         sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('public_port', sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column('public_source', sa.String(64), nullable=True))
        batch.add_column(
            sa.Column('password_ref', sa.String(255), nullable=True))
        batch.add_column(sa.Column('root_password_ref',
                         sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table('services') as batch:
        batch.drop_column('root_password_ref')
        batch.drop_column('password_ref')
        batch.drop_column('public_source')
        batch.drop_column('public_port')
        batch.drop_column('public_enabled')
        batch.drop_column('db_username')
        batch.drop_column('db_name')
        batch.drop_column('engine_version')
        batch.drop_column('engine')
