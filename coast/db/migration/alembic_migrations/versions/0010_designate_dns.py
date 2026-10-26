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

"""Customer Designate zone links and managed DNS observations."""

from alembic import op
import sqlalchemy as sa

revision = '0010_designate_dns'
down_revision = '0009_service_metrics'
branch_labels = None
depends_on = None


def upgrade():
    for table, columns in _COLUMNS.items():
        for name, kind in columns:
            op.add_column(table, sa.Column(name, kind, nullable=True))


def downgrade():
    for table, columns in reversed(list(_COLUMNS.items())):
        for name, kind in reversed(columns):
            op.drop_column(table, name)


_COLUMNS = {
    'stacks': [('default_domain', sa.String(253)),
               ('dns_zone_id', sa.String(36)),
               ('dns_trust_id', sa.String(255))],
    'services': [('default_dns_zone_id', sa.String(36)),
                 ('default_dns_trust_id', sa.String(255))],
    'domains': [('verification_started_at', sa.DateTime),
                ('zone_id', sa.String(36)), ('dns_trust_id', sa.String(255)),
                ('managed_records', sa.JSON),
                ('status_reason', sa.String(255)),
                ('record_type', sa.String(8)), ('dns_previous', sa.JSON)],
}
