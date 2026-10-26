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

from oslo_db.sqlalchemy import models
from oslo_utils import timeutils
from oslo_utils import uuidutils
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy import orm


def timestamp_type():
    # MariaDB DATETIME without fsp stores whole seconds. MySQL and MariaDB
    # use DATETIME(6). SQLite keeps the generic DateTime type.
    # https://docs.sqlalchemy.org/en/20/dialects/mysql.html
    return sa.DateTime().with_variant(
        mysql.DATETIME(fsp=6), 'mysql', 'mariadb')


class Base(models.ModelBase, orm.DeclarativeBase):
    metadata = sa.MetaData(naming_convention={
        'pk': 'pk_%(table_name)s',
        'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
        'uq': 'uq_%(table_name)s_%(column_0_N_name)s',
        'ix': 'ix_%(table_name)s_%(column_0_N_name)s',
        'ck': 'ck_%(table_name)s_%(column_0_name)s',
    })
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    created_at = sa.Column(timestamp_type(), nullable=False,
                           default=timeutils.utcnow)
    updated_at = sa.Column(timestamp_type(), nullable=True,
                           onupdate=timeutils.utcnow)


class IdMixin:
    id = sa.Column(sa.String(36), primary_key=True,
                   default=uuidutils.generate_uuid)


class ProjectMixin:
    project_id = sa.Column(sa.String(36), nullable=False, index=True)


class SoftDeleteMixin:
    deleted_at = sa.Column(timestamp_type(), nullable=True)
