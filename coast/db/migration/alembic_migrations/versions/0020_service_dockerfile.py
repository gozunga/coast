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

"""Store protected desired Dockerfile references."""

from alembic import op
import sqlalchemy as sa

revision = '0020_service_dockerfile'
down_revision = '0019_deploy_public_key'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('services') as batch:
        batch.add_column(sa.Column('dockerfile_ref', sa.String(255),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table('services') as batch:
        batch.drop_column('dockerfile_ref')
