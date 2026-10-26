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

"""Import resource classes to register all supported wire objects."""

from coast.objects import backups
from coast.objects import resources


def register_all():
    """Resources are registered when this package is imported."""


Stack = resources.Stack
Appgroup = resources.Appgroup
Environment = resources.Environment
Server = resources.Server
Volume = resources.Volume
Storage = resources.Storage
FileMount = resources.FileMount
Source = resources.Source
Service = resources.Service
Deployment = resources.Deployment
Domain = resources.Domain
Secret = resources.Secret
AppVolumeMount = resources.AppVolumeMount
Export = resources.Export
Event = resources.Event
ConsoleToken = resources.ConsoleToken
StackDeleteSnapshot = resources.StackDeleteSnapshot
Quota = resources.Quota
QuotaUsage = resources.QuotaUsage
StackList = resources.StackList
AppgroupList = resources.AppgroupList
EnvironmentList = resources.EnvironmentList
ServerList = resources.ServerList
VolumeList = resources.VolumeList
StorageList = resources.StorageList
FileMountList = resources.FileMountList
SourceList = resources.SourceList
ServiceList = resources.ServiceList
DeploymentList = resources.DeploymentList
DomainList = resources.DomainList
SecretList = resources.SecretList
AppVolumeMountList = resources.AppVolumeMountList
ExportList = resources.ExportList
EventList = resources.EventList
ConsoleTokenList = resources.ConsoleTokenList
StackDeleteSnapshotList = resources.StackDeleteSnapshotList
QuotaList = resources.QuotaList
QuotaUsageList = resources.QuotaUsageList

BackupPolicy = backups.BackupPolicy
BackupPolicyList = backups.BackupPolicyList
Backup = backups.Backup
BackupList = backups.BackupList
BackupJob = backups.BackupJob
BackupJobList = backups.BackupJobList
BackupAccount = backups.BackupAccount
BackupAccountList = backups.BackupAccountList
