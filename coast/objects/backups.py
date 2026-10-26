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

"""Versioned database backup records."""

from oslo_versionedobjects import fields

from coast.db import api
from coast.objects import base
from coast.objects import fields as coast_fields


@base.CoastObjectRegistry.register
class BackupPolicy(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.backup_policies
    fields = {
        'service_id': fields.StringField(),
        'enabled': fields.BooleanField(),
        'hour': fields.IntegerField(),
        'minute': fields.IntegerField(),
        'keep_daily': fields.IntegerField(),
        'keep_weekly': fields.IntegerField(),
        'engine': fields.StringField(),
        'container': fields.StringField(),
        'key_ref': fields.StringField(nullable=True),
        'signing_key_ref': fields.StringField(nullable=True),
        'last_scheduled_at': fields.DateTimeField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class BackupPolicyList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = BackupPolicy
    fields = {'objects': fields.ListOfObjectsField('BackupPolicy')}


@base.CoastObjectRegistry.register
class Backup(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.backups
    fields = {
        'service_id': fields.StringField(),
        'kind': fields.StringField(),
        'status': fields.StringField(),
        'size_bytes': fields.IntegerField(nullable=True),
        'started_at': fields.DateTimeField(nullable=True),
        'finished_at': fields.DateTimeField(nullable=True),
        'duration_seconds': fields.FloatField(nullable=True),
        'error': fields.StringField(nullable=True),
        'engine': fields.StringField(),
        'engine_version': fields.StringField(nullable=True),
        'container': fields.StringField(),
        'sha256': fields.StringField(nullable=True),
        'segments': coast_fields.JSONField(nullable=True),
        'key_ref': fields.StringField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class BackupList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Backup
    fields = {'objects': fields.ListOfObjectsField('Backup')}


@base.CoastObjectRegistry.register
class BackupJob(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.backup_jobs
    fields = {
        'backup_id': fields.StringField(),
        'service_id': fields.StringField(),
        'operation': fields.StringField(),
        'status': fields.StringField(),
        'error': fields.StringField(nullable=True),
        'started_at': fields.DateTimeField(nullable=True),
        'finished_at': fields.DateTimeField(nullable=True),
        'safety_backup_id': fields.StringField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class BackupJobList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = BackupJob
    fields = {'objects': fields.ListOfObjectsField('BackupJob')}


@base.CoastObjectRegistry.register
class BackupAccount(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.backup_accounts
    fields = {
        'key_ref': fields.StringField(),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class BackupAccountList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = BackupAccount
    fields = {'objects': fields.ListOfObjectsField('BackupAccount')}
