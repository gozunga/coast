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

"""Store the workload kind (app or service) on services."""

from alembic import op
import sqlalchemy as sa


revision = '0004_service_kind'
down_revision = '0003_storages'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('services', sa.Column(
        'kind', sa.String(16), nullable=False, server_default='app'))
    # Existing rows keep the kind they had: templates are services.
    op.execute("UPDATE services SET kind = 'service' "
               "WHERE template_id IS NOT NULL")


def downgrade():
    with op.batch_alter_table('services') as batch:
        batch.drop_column('kind')
