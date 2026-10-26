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

"""Remove name constraints that incorrectly include deleted rows."""

from alembic import op

revision = '0015_live_names'
down_revision = '0014_ingress_rules'
branch_labels = None
depends_on = None


TABLES = (('appgroups', 'stack_id'), ('environments', 'appgroup_id'))


def upgrade():
    for table, parent in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint('uq_%s_%s_name' % (table, parent),
                                  type_='unique')


def downgrade():
    for table, parent in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.create_unique_constraint('uq_%s_%s_name' % (table, parent),
                                           [parent, 'name'])
