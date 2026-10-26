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

"""Database firewall rules derived from project-owned desired state."""

from coast.common import states
from coast.db import api as db_api
from coast import objects


def databases(ctx, stack_id):
    with db_api.reader.using(ctx):
        return [objects.Service.obj_from_db(ctx, row) for row in
                db_api.services._query(ctx).filter_by(
                    stack_id=stack_id, kind='database').all()]


def rules(ctx, stack_id):
    # No independent rule row can survive a rolled-back or deleted database.
    # Failed creates have no working endpoint and must not retain exposure.
    return [dict(id='managed-database-' + row.id, protocol='tcp',
                 port_range_min=row.public_port,
                 port_range_max=row.public_port, source=row.public_source,
                 description='Database ' + row.name, managed=True)
            for row in databases(ctx, stack_id)
            if row.public_enabled and row.public_port and
            row.status not in (states.ERROR, states.DELETING)]
