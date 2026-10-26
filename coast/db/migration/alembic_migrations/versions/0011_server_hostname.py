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

"""Persist guest hostnames independently of resource display names."""

from alembic import op
import sqlalchemy as sa

revision = '0011_server_hostname'
down_revision = '0010_designate_dns'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('servers', sa.Column(
        'hostname', sa.String(63), nullable=True))


def downgrade():
    op.drop_column('servers', 'hostname')
