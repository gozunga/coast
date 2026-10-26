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

"""Resource schemas; fields mirror the database columns."""

from oslo_utils import versionutils
from oslo_versionedobjects import fields

from coast.common import exception
from coast.common import states
from coast.db import api
from coast.objects import base
from coast.objects import fields as coast_fields


@base.CoastObjectRegistry.register
class Stack(base.OwnedTaskObject):
    # Version 1.0: Initial version.
    # Version 1.1: HMAC and engine backup references (Q86).
    # Version 1.2: Conductor task owner.
    # Version 1.3: Private registry secret reference (Q125).
    # Version 1.6: Task holder diagnostics.
    # Version 1.7: Two-phase engine token rotation; remove engine backup.
    VERSION = '1.7'
    repository = api.stacks
    fields = {
        'default_domain': fields.StringField(nullable=True),
        'dns_zone_id': fields.StringField(nullable=True),
        'dns_trust_id': fields.StringField(nullable=True),
        'name': fields.StringField(),
        'description': fields.StringField(nullable=True),
        'driver': fields.StringField(),
        'engine_version': fields.StringField(),
        'plan': fields.StringField(),
        'status': coast_fields.EnvironmentStateField(),
        'task_state': coast_fields.TaskStateField(),
        'task_owner': fields.StringField(nullable=True),
        'task_started_at': fields.DateTimeField(nullable=True),
        'conductor_host': fields.StringField(nullable=True),
        'fault': fields.StringField(nullable=True),
        'network_id': fields.StringField(nullable=True),
        'subnet_id': fields.StringField(nullable=True),
        'router_id': fields.StringField(nullable=True),
        'sg_ingress_id': fields.StringField(nullable=True),
        'sg_mgmt_id': fields.StringField(nullable=True),
        'engine_ref': coast_fields.ReferenceField(nullable=True),
        'engine_token_ref': coast_fields.ReferenceField(nullable=True),
        'heartbeat_key_ref': coast_fields.ReferenceField(nullable=True),
        'engine_token_previous_ref': coast_fields.ReferenceField(
            nullable=True),
        'engine_token_name': fields.StringField(nullable=True),
        'engine_token_previous_name': fields.StringField(nullable=True),
        'engine_token_rotation_id': fields.StringField(nullable=True),
        'engine_token_created_at': fields.DateTimeField(nullable=True),
        'ssh_key_ref': coast_fields.ReferenceField(nullable=True),
        'registry_secret_ref': coast_fields.ReferenceField(nullable=True),
        'ssh_public_key': fields.StringField(nullable=True),
        'previous_image_id': fields.StringField(nullable=True),
        'previous_engine_version': fields.StringField(nullable=True),
        'upgrade_snapshots': fields.DictOfStringsField(nullable=True),
        'upgrade_checkpoint': coast_fields.JSONField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
        'deleted_at': fields.DateTimeField(nullable=True),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        target = versionutils.convert_version_to_tuple(target_version)
        if target < (1, 7):
            for name in ('engine_token_previous_ref', 'engine_token_name',
                         'engine_token_previous_name',
                         'engine_token_rotation_id',
                         'engine_token_created_at'):
                primitive.pop(name, None)
        if target < (1, 6):
            primitive.pop('task_owner', None)
            primitive.pop('task_started_at', None)
        if target < (1, 4):
            for name in ('previous_image_id', 'previous_engine_version',
                         'upgrade_snapshots', 'upgrade_checkpoint'):
                primitive.pop(name, None)
        if target < (1, 3):
            primitive.pop('registry_secret_ref', None)
        if target < (1, 2):
            primitive.pop('conductor_host', None)
        if target < (1, 1):
            primitive.pop('heartbeat_key_ref', None)


@base.CoastObjectRegistry.register
class StackList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Stack
    fields = {'objects': fields.ListOfObjectsField('Stack')}


@base.CoastObjectRegistry.register
class Appgroup(base.TaskObject):
    VERSION = '1.0'
    repository = api.appgroups
    fields = {
        'stack_id': fields.StringField(),
        'name': fields.StringField(),
        'description': fields.StringField(nullable=True),
        'status': coast_fields.AppgroupStateField(),
        'task_state': coast_fields.TaskStateField(),
        'engine_ref': coast_fields.ReferenceField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
        'deleted_at': fields.DateTimeField(nullable=True),
    }


@base.CoastObjectRegistry.register
class AppgroupList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Appgroup
    fields = {'objects': fields.ListOfObjectsField('Appgroup')}


@base.CoastObjectRegistry.register
class Environment(base.TaskObject):
    VERSION = '1.0'
    repository = api.environments
    fields = {
        'appgroup_id': fields.StringField(),
        'name': fields.StringField(),
        'description': fields.StringField(nullable=True),
        'status': coast_fields.AppEnvironmentStateField(),
        'task_state': coast_fields.TaskStateField(),
        'engine_ref': coast_fields.ReferenceField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
        'deleted_at': fields.DateTimeField(nullable=True),
    }


@base.CoastObjectRegistry.register
class EnvironmentList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Environment
    fields = {'objects': fields.ListOfObjectsField('Environment')}


@base.CoastObjectRegistry.register
class Server(base.TaskObject):
    # Version 1.0: Initial version
    # Version 1.1: Added deleted_at so server history rows can stay
    # Version 1.2: Saved immutable guest hostname
    VERSION = '1.2'
    repository = api.servers
    fields = {
        'stack_id': fields.StringField(),
        'name': fields.StringField(),
        'hostname': fields.StringField(nullable=True),
        'kind': fields.StringField(),
        'role': fields.StringField(),
        'status': coast_fields.ServerStateField(),
        'task_state': coast_fields.TaskStateField(),
        'flavor_id': fields.StringField(nullable=True),
        'image_id': fields.StringField(nullable=True),
        'compute_id': fields.StringField(nullable=True),
        'ironic_node_id': fields.StringField(nullable=True),
        'mgmt_ip': fields.StringField(nullable=True),
        'private_ip': fields.StringField(nullable=True),
        'floating_ip': fields.StringField(nullable=True),
        'floating_ip_id': fields.StringField(nullable=True),
        'agent_cert_serial': fields.StringField(nullable=True),
        'agent_cert_expires_at': fields.DateTimeField(nullable=True),
        'last_heartbeat_at': fields.DateTimeField(nullable=True),
        'engine_ref': coast_fields.ReferenceField(nullable=True),
        'fault': fields.StringField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
        'deleted_at': fields.DateTimeField(nullable=True),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        # Drop fields added after 1.0. See oslo.versionedobjects usage.
        target = versionutils.convert_version_to_tuple(target_version)
        if target < (1, 2):
            primitive.pop('hostname', None)
        if target < (1, 1):
            primitive.pop('deleted_at', None)


@base.CoastObjectRegistry.register
class ServerList(base.RepositoryObjectList):
    # Version 1.0: Initial version. Contained Server is <= 1.2.
    # The list schema did not change. Each child carries its own version.
    VERSION = '1.0'
    object_class = Server
    fields = {'objects': fields.ListOfObjectsField('Server')}


@base.CoastObjectRegistry.register
class Volume(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.volumes
    fields = {
        'stack_id': fields.StringField(),
        'server_id': fields.StringField(nullable=True),
        'name': fields.StringField(),
        'size_gb': fields.IntegerField(),
        'volume_type': fields.StringField(nullable=True),
        'mount_path': fields.StringField(),
        'purpose': fields.StringField(),
        'cinder_volume_id': fields.StringField(nullable=True),
        'status': coast_fields.VolumeStateField(),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }

    def transition_status(self, status):
        # No task_state column exists. A repeated action is not a same-state
        # no-op; only a forward edge is compare-and-swapped.
        if self.obj_what_changed():
            raise exception.Invalid()
        states.check_transition('volume', self.status, status)
        if self.status == status:
            raise exception.Conflict()
        return self._from_db(api.cas_status(
            self._context, self.repository.model, self.id, new=status,
            expected=self.status))

    def extend_to(self, size_gb):
        if self.obj_what_changed():
            raise exception.Invalid()
        return self._from_db(api.set_volume_size(
            self._context, self.id, size_gb))


@base.CoastObjectRegistry.register
class VolumeList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Volume
    fields = {'objects': fields.ListOfObjectsField('Volume')}


@base.CoastObjectRegistry.register
class Storage(base.RepositoryObject):
    VERSION = '1.1'
    repository = api.storages
    fields = {
        'stack_id': fields.StringField(),
        'type': fields.StringField(),
        'host_path': fields.StringField(nullable=True),
        'read_only': fields.BooleanField(),
        'name': fields.StringField(nullable=True),
        'size_gb': fields.IntegerField(nullable=True),
        'status': coast_fields.StorageStateField(),
        'origin': fields.StringField(),
        'service_id': fields.StringField(nullable=True),
        'server_id': fields.StringField(nullable=True),
        'container_path': fields.StringField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class StorageList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Storage
    fields = {'objects': fields.ListOfObjectsField('Storage')}


@base.CoastObjectRegistry.register
class FileMount(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.file_mounts
    fields = {
        'service_id': fields.StringField(),
        'container_path': fields.StringField(),
        'content_ref': coast_fields.ReferenceField(),
        'resource_name': fields.StringField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class FileMountList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = FileMount
    fields = {'objects': fields.ListOfObjectsField('FileMount')}


@base.CoastObjectRegistry.register
class Source(base.RepositoryObject):
    VERSION = '1.2'
    repository = api.sources
    fields = {
        'type': fields.StringField(),
        'repo_url': fields.StringField(nullable=True),
        'branch': fields.StringField(nullable=True),
        'build_pack': fields.StringField(nullable=True),
        'dockerfile_path': fields.StringField(nullable=True),
        'compose_path': fields.StringField(nullable=True),
        'base_directory': fields.StringField(nullable=True),
        'image': fields.StringField(nullable=True),
        'credentials_ref': coast_fields.ReferenceField(nullable=True),
        'deploy_public_key': fields.StringField(nullable=True),
        'deploy_key_rotation': coast_fields.JSONField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if versionutils.convert_version_to_tuple(target_version) < (1, 1):
            primitive.pop('deploy_public_key', None)
        if versionutils.convert_version_to_tuple(target_version) < (1, 2):
            primitive.pop('deploy_key_rotation', None)


@base.CoastObjectRegistry.register
class SourceList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Source
    fields = {'objects': fields.ListOfObjectsField('Source')}


@base.CoastObjectRegistry.register
class Service(base.OwnedTaskObject):
    # Version 1.0: Initial version
    # Version 1.1: Nullable environment_id (Q94). Existing rows stay empty.
    # Version 1.2: Nullable template_id. Existing rows stay empty.
    # Version 1.3: STARTING and DEPLOYING status values.
    # Version 1.4: settings_changed_at for pending changes.
    # Version 1.5: default_domain, the Coast-owned default hostname.
    # Version 1.6: webhook_nonce for per-app webhook token regeneration.
    # Version 1.7: Optional HTTP check mode and port.
    # Version 1.8: Nullable ports and reconcile-owned fault.
    # Version 1.9: Durable runtime confirmation timestamps.
    # Version 1.10: Stored kind (app or service).
    # Version 1.11: Protected desired compose reference.
    # Version 1.12: File mount secret reference history.
    # Version 1.14: Optional default address.
    # Version 1.15: Task holder diagnostics.
    # Version 1.16: Standalone database metadata and credential references.
    # Version 1.17: Customer-safe database task fault.
    # Version 1.18: Protected desired Dockerfile reference.
    VERSION = '1.18'
    repository = api.services

    @property
    def is_engine_service(self):
        return bool(self.template_id or Source.get_by_id(
            self._context, self.source_id,
            all_projects=self._context.is_admin).type == 'compose')

    fields = {
        'fault': fields.StringField(nullable=True),
        'engine': fields.StringField(nullable=True),
        'engine_version': fields.StringField(nullable=True),
        'db_name': fields.StringField(nullable=True),
        'db_username': fields.StringField(nullable=True),
        'public_enabled': fields.BooleanField(nullable=True),
        'public_port': fields.IntegerField(nullable=True),
        'public_source': fields.StringField(nullable=True),
        'password_ref': coast_fields.ReferenceField(nullable=True),
        'root_password_ref': coast_fields.ReferenceField(nullable=True),

        'default_dns_zone_id': fields.StringField(nullable=True),
        'default_dns_trust_id': fields.StringField(nullable=True),
        'stack_id': fields.StringField(),
        'environment_id': fields.StringField(nullable=True),
        'name': fields.StringField(),
        'description': fields.StringField(nullable=True),
        'source_id': fields.StringField(),
        'template_id': fields.StringField(nullable=True),
        'compose_ref': coast_fields.ReferenceField(nullable=True),
        'dockerfile_ref': coast_fields.ReferenceField(nullable=True),
        'file_mount_secret_refs': coast_fields.JSONField(nullable=True),
        'kind': fields.StringField(default='app'),
        'server_id': fields.StringField(nullable=True),
        'ports': fields.StringField(nullable=True),
        'reconcile_fault': fields.StringField(nullable=True),
        'unhealthy_since': fields.DateTimeField(nullable=True),
        'running_since': fields.DateTimeField(nullable=True),
        'replicas': fields.IntegerField(),
        'cpu_limit': fields.FloatField(nullable=True),
        'memory_limit_mb': fields.IntegerField(nullable=True),
        'health_check': fields.StringField(),
        'health_check_port': fields.IntegerField(nullable=True),
        'healthcheck_path': fields.StringField(nullable=True),
        'auto_deploy': fields.BooleanField(),
        'status': coast_fields.AppStateField(),
        'task_state': coast_fields.TaskStateField(),
        'task_owner': fields.StringField(nullable=True),
        'task_started_at': fields.DateTimeField(nullable=True),
        'current_deployment_id': fields.StringField(nullable=True),
        'engine_ref': coast_fields.ReferenceField(nullable=True),
        'settings_changed_at': fields.DateTimeField(nullable=True),
        'default_domain_enabled': fields.BooleanField(default=True),
        'default_domain': fields.StringField(nullable=True),
        'webhook_nonce': fields.StringField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
        'deleted_at': fields.DateTimeField(nullable=True),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        target = versionutils.convert_version_to_tuple(target_version)
        if target < (1, 17):
            primitive.pop('fault', None)
        if target < (1, 18):
            primitive.pop('dockerfile_ref', None)
        if target < (1, 15):
            primitive.pop('task_owner', None)
            primitive.pop('task_started_at', None)
        if target < (1, 14):
            primitive.pop('default_domain_enabled', None)
        if target < (1, 12):
            primitive.pop('file_mount_secret_refs', None)
        if target < (1, 11):
            primitive.pop('compose_ref', None)
        if target < (1, 10):
            primitive.pop('kind', None)
        if target < (1, 9):
            primitive.pop('unhealthy_since', None)
            primitive.pop('running_since', None)
        if target < (1, 8):
            primitive.pop('reconcile_fault', None)
            if 'ports' in primitive and primitive['ports'] is None:
                primitive['ports'] = ''
        if target < (1, 7):
            primitive.pop('health_check', None)
            primitive.pop('health_check_port', None)
        if target < (1, 6):
            primitive.pop('webhook_nonce', None)
        if target < (1, 5):
            primitive.pop('default_domain', None)
        if target < (1, 4):
            primitive.pop('settings_changed_at', None)
        if target < (1, 2):
            primitive.pop('template_id', None)
        if target < (1, 1):
            primitive.pop('environment_id', None)


@base.CoastObjectRegistry.register
class ServiceList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Service
    fields = {'objects': fields.ListOfObjectsField('Service')}


@base.CoastObjectRegistry.register
class Deployment(base.RepositoryObject):
    # Version 1.1: Add the driver-reported resolved reference (Q33).
    # Version 1.2: Durable sanitized runtime output.
    VERSION = '1.2'
    repository = api.deployments
    fields = {
        'service_id': fields.StringField(),
        'number': fields.IntegerField(),
        'source_ref': coast_fields.ReferenceField(nullable=True),
        'resolved_ref': coast_fields.ReferenceField(nullable=True),
        'spec': coast_fields.JSONField(),
        'image_digest': fields.StringField(nullable=True),
        'server_id': fields.StringField(nullable=True),
        'trigger': fields.StringField(),
        'rollback_of': fields.StringField(nullable=True),
        'status': coast_fields.DeploymentStateField(),
        'fault': fields.StringField(nullable=True),
        'engine_ref': coast_fields.ReferenceField(nullable=True),
        'log_ref': coast_fields.ReferenceField(nullable=True),
        'runtime_log': fields.StringField(nullable=True),
        'started_at': fields.DateTimeField(nullable=True),
        'finished_at': fields.DateTimeField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
        'deleted_at': fields.DateTimeField(nullable=True),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if versionutils.convert_version_to_tuple(target_version) < (1, 2):
            primitive.pop('runtime_log', None)
        if versionutils.convert_version_to_tuple(target_version) < (1, 1):
            primitive.pop('resolved_ref', None)


@base.CoastObjectRegistry.register
class DeploymentList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Deployment
    fields = {'objects': fields.ListOfObjectsField('Deployment')}


@base.CoastObjectRegistry.register
class Domain(base.RepositoryObject):
    # Version 1.2: Primary custom domain.
    VERSION = '1.2'
    repository = api.domains
    fields = {
        'primary': fields.BooleanField(default=False),
        'verification_started_at': fields.DateTimeField(nullable=True),
        'dns_previous': coast_fields.JSONField(nullable=True),
        'zone_id': fields.StringField(nullable=True),
        'dns_trust_id': fields.StringField(nullable=True),
        'managed_records': coast_fields.JSONField(nullable=True),
        'status_reason': fields.StringField(nullable=True),
        'record_type': fields.StringField(nullable=True),
        'service_id': fields.StringField(),
        'fqdn': fields.StringField(),
        'verification_token': fields.StringField(),
        'verified_at': fields.DateTimeField(nullable=True),
        'tls': fields.StringField(),
        'auto_dns': fields.BooleanField(),
        'dns_mode': fields.StringField(),
        'status': coast_fields.DomainStateField(),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if versionutils.convert_version_to_tuple(target_version) < (1, 2):
            primitive.pop('primary', None)


@base.CoastObjectRegistry.register
class DomainList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Domain
    fields = {'objects': fields.ListOfObjectsField('Domain')}


@base.CoastObjectRegistry.register
class Secret(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.secrets
    fields = {
        'service_id': fields.StringField(),
        'key': fields.StringField(),
        'is_secret': fields.BooleanField(),
        'value_ref': coast_fields.ReferenceField(),
        'build_time': fields.BooleanField(),
        'version': fields.IntegerField(),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class SecretList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Secret
    fields = {'objects': fields.ListOfObjectsField('Secret')}


@base.CoastObjectRegistry.register
class AppVolumeMount(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.service_volume_mounts
    fields = {
        'service_id': fields.StringField(),
        'volume_id': fields.StringField(),
        'sub_path': fields.StringField(),
        'container_path': fields.StringField(),
        'read_only': fields.BooleanField(),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class AppVolumeMountList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = AppVolumeMount
    fields = {'objects': fields.ListOfObjectsField('AppVolumeMount')}


@base.CoastObjectRegistry.register
class Export(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.exports
    fields = {
        'stack_id': fields.StringField(),
        'status': coast_fields.ExportStateField(),
        'volume_ids': coast_fields.JSONField(),
        'backend': fields.StringField(),
        'artifacts': coast_fields.JSONField(nullable=True),
        'object_expires_at': fields.DateTimeField(nullable=True),
        'fault': fields.StringField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class ExportList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Export
    fields = {'objects': fields.ListOfObjectsField('Export')}


@base.CoastObjectRegistry.register
class Event(base.RepositoryObject):
    # Version 1.1: nullable structured timeline detail.
    VERSION = '1.1'
    repository = api.events
    fields = {
        'stack_id': fields.StringField(nullable=True),
        'resource_type': fields.StringField(),
        'resource_id': fields.StringField(),
        'action': fields.StringField(),
        'outcome': fields.StringField(),
        'message': fields.StringField(nullable=True),
        'detail': coast_fields.JSONField(nullable=True),
        'request_id': fields.StringField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if target_version == '1.0':
            primitive.pop('detail', None)


@base.CoastObjectRegistry.register
class EventList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = Event
    fields = {'objects': fields.ListOfObjectsField('Event')}


@base.CoastObjectRegistry.register
class ConsoleToken(base.RepositoryObject):
    VERSION = '1.0'
    repository = api.console_tokens
    fields = {
        'token_hash': fields.StringField(),
        'stack_id': fields.StringField(),
        'resource_type': fields.StringField(),
        'resource_id': fields.StringField(),
        'command': coast_fields.JSONField(nullable=True),
        'user_id': fields.StringField(),
        'expires_at': fields.DateTimeField(),
        'used_at': fields.DateTimeField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }


@base.CoastObjectRegistry.register
class ConsoleTokenList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = ConsoleToken
    fields = {'objects': fields.ListOfObjectsField('ConsoleToken')}


@base.CoastObjectRegistry.register
class StackDeleteSnapshot(base.RepositoryObject):
    # Version 1.1: Durable retention and expiry.
    VERSION = '1.1'
    repository = api.stack_delete_snapshots
    fields = {
        'retention_days': fields.IntegerField(nullable=True),
        'expires_at': fields.DateTimeField(nullable=True),
        'stack_id': fields.StringField(),
        'volume_id': fields.StringField(),
        'cinder_volume_id': fields.StringField(),
        'cinder_snapshot_id': fields.StringField(),
        'status': fields.StringField(),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
        'id': fields.StringField(),
        'project_id': fields.StringField(),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if target_version == '1.0':
            primitive.pop('retention_days', None)
            primitive.pop('expires_at', None)


@base.CoastObjectRegistry.register
class StackDeleteSnapshotList(base.RepositoryObjectList):
    VERSION = '1.0'
    object_class = StackDeleteSnapshot
    fields = {'objects': fields.ListOfObjectsField('StackDeleteSnapshot')}


@base.CoastObjectRegistry.register
class Quota(base.CoastObject):
    # Version 1.1: storage_gb for customer named volumes.
    VERSION = '1.1'
    fields = {
        'project_id': fields.StringField(),
        'environments': fields.IntegerField(nullable=True),
        'servers': fields.IntegerField(nullable=True),
        'apps': fields.IntegerField(nullable=True),
        'domains': fields.IntegerField(nullable=True),
        'volume_gb': fields.IntegerField(nullable=True),
        'build_servers': fields.IntegerField(nullable=True),
        'baremetal_servers': fields.IntegerField(nullable=True),
        'storage_gb': fields.IntegerField(nullable=True),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if target_version == '1.0':
            primitive.pop('storage_gb', None)


@base.CoastObjectRegistry.register
class QuotaUsage(base.CoastObject):
    # Version 1.1: storage_gb counters.
    VERSION = '1.1'
    fields = {
        'project_id': fields.StringField(),
        'in_use_environments': fields.IntegerField(),
        'reserved_environments': fields.IntegerField(),
        'in_use_servers': fields.IntegerField(),
        'reserved_servers': fields.IntegerField(),
        'in_use_apps': fields.IntegerField(),
        'reserved_apps': fields.IntegerField(),
        'in_use_domains': fields.IntegerField(),
        'reserved_domains': fields.IntegerField(),
        'in_use_volume_gb': fields.IntegerField(),
        'reserved_volume_gb': fields.IntegerField(),
        'in_use_build_servers': fields.IntegerField(),
        'reserved_build_servers': fields.IntegerField(),
        'in_use_baremetal_servers': fields.IntegerField(),
        'reserved_baremetal_servers': fields.IntegerField(),
        'in_use_storage_gb': fields.IntegerField(),
        'reserved_storage_gb': fields.IntegerField(),
        'created_at': fields.DateTimeField(),
        'updated_at': fields.DateTimeField(nullable=True),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if target_version == '1.0':
            primitive.pop('in_use_storage_gb', None)
            primitive.pop('reserved_storage_gb', None)


@base.CoastObjectRegistry.register
class QuotaList(base.CoastObjectList):
    VERSION = '1.0'
    fields = {'objects': fields.ListOfObjectsField('Quota')}


@base.CoastObjectRegistry.register
class QuotaUsageList(base.CoastObjectList):
    VERSION = '1.0'
    fields = {'objects': fields.ListOfObjectsField('QuotaUsage')}
