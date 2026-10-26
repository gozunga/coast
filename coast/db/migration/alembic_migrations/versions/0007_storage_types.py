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

"""Typed bind mounts and read-only storage."""

from alembic import op
import sqlalchemy as sa


revision = '0007_storage_types'
down_revision = '0006_file_mounts'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('storages') as batch:
        batch.add_column(sa.Column('type', sa.String(16), nullable=False,
                                   server_default='volume'))
        batch.add_column(sa.Column(
            'host_path', sa.String(4096), nullable=True))
        batch.add_column(sa.Column('read_only', sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.alter_column('name', existing_type=sa.String(63), nullable=True)
        batch.alter_column('size_gb', existing_type=sa.Integer(),
                           nullable=True)


def downgrade():
    with op.batch_alter_table('storages') as batch:
        batch.drop_column('read_only')
        batch.drop_column('host_path')
        batch.drop_column('type')
        batch.alter_column('name', existing_type=sa.String(63), nullable=False)
        batch.alter_column('size_gb', existing_type=sa.Integer(),
                           nullable=False)
