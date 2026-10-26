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

"""Database backup state. Secret payloads never enter these tables."""

import sqlalchemy as sa

from coast.db import base_models as base


class BackupPolicy(base.Base, base.IdMixin, base.ProjectMixin):
    __tablename__ = 'backup_policies'
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False, unique=True)
    enabled = sa.Column(sa.Boolean, nullable=False, default=False)
    hour = sa.Column(sa.Integer, nullable=False, default=2)
    minute = sa.Column(sa.Integer, nullable=False, default=0)
    keep_daily = sa.Column(sa.Integer, nullable=False, default=14)
    keep_weekly = sa.Column(sa.Integer, nullable=False, default=8)
    engine = sa.Column(sa.String(16), nullable=False)
    container = sa.Column(sa.String(128), nullable=False)
    key_ref = sa.Column(sa.String(1024), nullable=True)
    signing_key_ref = sa.Column(sa.String(1024), nullable=True)
    last_scheduled_at = sa.Column(base.timestamp_type(), nullable=True)


class Backup(base.Base, base.IdMixin, base.ProjectMixin):
    __tablename__ = 'backups'
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False, index=True)
    kind = sa.Column(sa.String(16), nullable=False)
    status = sa.Column(sa.String(32), nullable=False)
    size_bytes = sa.Column(sa.BigInteger, nullable=True)
    started_at = sa.Column(base.timestamp_type(), nullable=True)
    finished_at = sa.Column(base.timestamp_type(), nullable=True)
    duration_seconds = sa.Column(sa.Float, nullable=True)
    error = sa.Column(sa.String(255), nullable=True)
    engine = sa.Column(sa.String(16), nullable=False)
    engine_version = sa.Column(sa.String(128), nullable=True)
    container = sa.Column(sa.String(128), nullable=False)
    sha256 = sa.Column(sa.String(64), nullable=True)
    segments = sa.Column(sa.JSON, nullable=True)
    key_ref = sa.Column(sa.String(1024), nullable=True)


class BackupJob(base.Base, base.IdMixin, base.ProjectMixin):
    __tablename__ = 'backup_jobs'
    backup_id = sa.Column(sa.String(36), sa.ForeignKey('backups.id'),
                          nullable=False)
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False)
    operation = sa.Column(sa.String(16), nullable=False)
    status = sa.Column(sa.String(32), nullable=False)
    error = sa.Column(sa.String(255), nullable=True)
    started_at = sa.Column(base.timestamp_type(), nullable=True)
    finished_at = sa.Column(base.timestamp_type(), nullable=True)
    safety_backup_id = sa.Column(sa.String(36), nullable=True)


class BackupAccount(base.Base, base.IdMixin, base.ProjectMixin):
    __tablename__ = 'backup_accounts'
    __table_args__ = (sa.UniqueConstraint('project_id'),
                      base.Base.__table_args__)
    key_ref = sa.Column(sa.String(1024), nullable=False)
