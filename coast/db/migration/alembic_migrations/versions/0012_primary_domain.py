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

"""Add primary domains and the default address switch."""

from alembic import op
import sqlalchemy as sa

revision = '0012_primary_domain'
down_revision = '0011_server_hostname'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('services', sa.Column(
        'default_domain_enabled', sa.Boolean(), nullable=False,
        server_default=sa.true()))
    op.add_column('domains', sa.Column(
        'primary', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('domains', 'primary')
    op.drop_column('services', 'default_domain_enabled')
