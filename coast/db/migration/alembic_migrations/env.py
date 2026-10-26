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

from alembic import context

from coast.db import api
from coast.db import models


config = context.config
engine = config.attributes.get('engine')
if engine is None:
    engine = api.get_engine()

with engine.connect() as connection:
    # SQLite batch alterations rebuild tables. Referencing rows must survive
    # the temporary DROP; restore enforcement after the migration transaction.
    sqlite = connection.dialect.name == 'sqlite'
    if sqlite:
        dbapi_connection = connection.connection.driver_connection
        dbapi_connection.execute('PRAGMA foreign_keys=OFF')
    context.configure(connection=connection,
                      target_metadata=models.base_models.Base.metadata,
                      compare_type=True, compare_server_default=True)
    try:
        with context.begin_transaction():
            context.run_migrations()
            if sqlite and connection.exec_driver_sql(
                    'PRAGMA foreign_key_check').first() is not None:
                raise RuntimeError('Migration left invalid foreign keys')
    finally:
        if sqlite:
            connection.rollback()
            dbapi_connection.execute('PRAGMA foreign_keys=ON')
