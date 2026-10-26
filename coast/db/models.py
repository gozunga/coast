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

import sqlalchemy as sa

from coast.db.backup_models import Backup  # noqa: F401
from coast.db.backup_models import BackupAccount  # noqa: F401
from coast.db.backup_models import BackupJob  # noqa: F401
from coast.db.backup_models import BackupPolicy  # noqa: F401
from coast.db import base_models


class Stack(base_models.Base,
            base_models.IdMixin,
            base_models.ProjectMixin,
            base_models.SoftDeleteMixin):
    __tablename__ = 'stacks'
    default_domain = sa.Column(sa.String(253), nullable=True)
    dns_zone_id = sa.Column(sa.String(36), nullable=True)
    dns_trust_id = sa.Column(sa.String(255), nullable=True)
    name = sa.Column(sa.String(255), nullable=False)
    description = sa.Column(sa.String(255), nullable=True)
    driver = sa.Column(sa.String(64), nullable=False)
    engine_version = sa.Column(sa.String(64), nullable=False)
    plan = sa.Column(sa.String(64), nullable=False)
    status = sa.Column(sa.String(32), nullable=False)
    task_state = sa.Column(sa.String(32), nullable=False, default='NONE')
    task_owner = sa.Column(sa.String(64), nullable=True)
    task_started_at = sa.Column(base_models.timestamp_type(), nullable=True)
    conductor_host = sa.Column(sa.String(255), nullable=True)
    fault = sa.Column(sa.Text, nullable=True)
    network_id = sa.Column(sa.String(36), nullable=True)
    subnet_id = sa.Column(sa.String(36), nullable=True)
    router_id = sa.Column(sa.String(36), nullable=True)
    sg_ingress_id = sa.Column(sa.String(36), nullable=True)
    sg_mgmt_id = sa.Column(sa.String(36), nullable=True)
    engine_ref = sa.Column(sa.String(255), nullable=True)
    engine_token_ref = sa.Column(sa.String(255), nullable=True)
    heartbeat_key_ref = sa.Column(sa.String(255), nullable=True)
    engine_token_previous_ref = sa.Column(sa.String(255), nullable=True)
    engine_token_name = sa.Column(sa.String(80), nullable=True)
    engine_token_previous_name = sa.Column(sa.String(80), nullable=True)
    engine_token_rotation_id = sa.Column(sa.String(36), nullable=True)
    engine_token_created_at = sa.Column(sa.DateTime(), nullable=True)
    ssh_key_ref = sa.Column(sa.String(255), nullable=True)
    registry_secret_ref = sa.Column(sa.String(255), nullable=True)
    ssh_public_key = sa.Column(sa.Text, nullable=True)
    previous_image_id = sa.Column(sa.String(36), nullable=True)
    previous_engine_version = sa.Column(sa.String(64), nullable=True)
    upgrade_snapshots = sa.Column(sa.JSON, nullable=True)
    upgrade_checkpoint = sa.Column(sa.JSON, nullable=True)


class Appgroup(base_models.Base,
               base_models.IdMixin,
               base_models.ProjectMixin,
               base_models.SoftDeleteMixin):
    __tablename__ = 'appgroups'
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    description = sa.Column(sa.String(255), nullable=True)
    status = sa.Column(sa.String(32), nullable=False)
    task_state = sa.Column(sa.String(32), nullable=False, default='NONE')
    engine_ref = sa.Column(sa.String(255), nullable=True)


class Environment(base_models.Base,
                  base_models.IdMixin,
                  base_models.ProjectMixin,
                  base_models.SoftDeleteMixin):
    __tablename__ = 'environments'
    appgroup_id = sa.Column(
        sa.String(36), sa.ForeignKey('appgroups.id'),
        nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    description = sa.Column(sa.String(255), nullable=True)
    status = sa.Column(sa.String(32), nullable=False)
    task_state = sa.Column(sa.String(32), nullable=False, default='NONE')
    engine_ref = sa.Column(sa.String(255), nullable=True)


class Server(base_models.Base,
             base_models.IdMixin,
             base_models.ProjectMixin,
             base_models.SoftDeleteMixin):
    __tablename__ = 'servers'
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    hostname = sa.Column(sa.String(63), nullable=True)
    kind = sa.Column(sa.String(16), nullable=False)
    role = sa.Column(sa.String(16), nullable=False)
    status = sa.Column(sa.String(32), nullable=False)
    task_state = sa.Column(sa.String(32), nullable=False)
    flavor_id = sa.Column(sa.String(36), nullable=True)
    image_id = sa.Column(sa.String(36), nullable=True)
    compute_id = sa.Column(sa.String(36), nullable=True)
    ironic_node_id = sa.Column(sa.String(36), nullable=True)
    mgmt_ip = sa.Column(sa.String(64), nullable=True)
    private_ip = sa.Column(sa.String(64), nullable=True)
    floating_ip = sa.Column(sa.String(64), nullable=True)
    floating_ip_id = sa.Column(sa.String(36), nullable=True)
    agent_cert_serial = sa.Column(sa.String(64), nullable=True)
    agent_cert_expires_at = sa.Column(sa.DateTime, nullable=True)
    last_heartbeat_at = sa.Column(sa.DateTime, nullable=True)
    engine_ref = sa.Column(sa.String(255), nullable=True)
    fault = sa.Column(sa.Text, nullable=True)


class Volume(base_models.Base, base_models.IdMixin, base_models.ProjectMixin):
    __tablename__ = 'volumes'
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False)
    server_id = sa.Column(sa.String(36), sa.ForeignKey('servers.id'),
                          nullable=True)
    name = sa.Column(sa.String(255), nullable=False)
    size_gb = sa.Column(sa.Integer, nullable=False)
    volume_type = sa.Column(sa.String(64), nullable=True)
    mount_path = sa.Column(sa.String(255), nullable=False, default='/data')
    purpose = sa.Column(sa.String(16), nullable=False)
    cinder_volume_id = sa.Column(sa.String(36), nullable=True)
    status = sa.Column(sa.String(32), nullable=False)


class Source(base_models.Base, base_models.IdMixin, base_models.ProjectMixin):
    __tablename__ = 'sources'
    type = sa.Column(sa.String(16), nullable=False)
    repo_url = sa.Column(sa.String(1024), nullable=True)
    branch = sa.Column(sa.String(255), nullable=True, default='main')
    build_pack = sa.Column(sa.String(32), nullable=True)
    dockerfile_path = sa.Column(sa.String(255), nullable=True)
    compose_path = sa.Column(sa.String(255), nullable=True)
    base_directory = sa.Column(sa.String(255), nullable=True)
    image = sa.Column(sa.String(1024), nullable=True)
    credentials_ref = sa.Column(sa.String(255), nullable=True)
    deploy_public_key = sa.Column(sa.Text(), nullable=True)
    deploy_key_rotation = sa.Column(sa.JSON, nullable=True)


class Service(base_models.Base,
              base_models.IdMixin,
              base_models.ProjectMixin,
              base_models.SoftDeleteMixin):
    __tablename__ = 'services'
    default_dns_zone_id = sa.Column(sa.String(36), nullable=True)
    default_dns_trust_id = sa.Column(sa.String(255), nullable=True)
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False)
    environment_id = sa.Column(
        sa.String(36),
        sa.ForeignKey('environments.id',
                      name='fk_services_environment_id'),
        nullable=True)
    name = sa.Column(sa.String(63), nullable=False)
    description = sa.Column(sa.String(255), nullable=True)
    source_id = sa.Column(sa.String(36), sa.ForeignKey('sources.id'),
                          nullable=False)
    file_mount_secret_refs = sa.Column(sa.JSON, nullable=True)
    template_id = sa.Column(sa.String(255), nullable=True)
    compose_ref = sa.Column(sa.String(255), nullable=True)
    engine = sa.Column(sa.String(32), nullable=True)
    engine_version = sa.Column(sa.String(32), nullable=True)
    db_name = sa.Column(sa.String(63), nullable=True)
    db_username = sa.Column(sa.String(63), nullable=True)
    public_enabled = sa.Column(sa.Boolean(), nullable=True)
    public_port = sa.Column(sa.Integer(), nullable=True)
    public_source = sa.Column(sa.String(64), nullable=True)
    password_ref = sa.Column(sa.String(255), nullable=True)
    root_password_ref = sa.Column(sa.String(255), nullable=True)
    # Workload kind is chosen at create time.
    dockerfile_ref = sa.Column(sa.String(255), nullable=True)
    kind = sa.Column(sa.String(16), nullable=False, server_default='app',
                     default='app')
    server_id = sa.Column(sa.String(36), sa.ForeignKey('servers.id'),
                          nullable=True)
    ports = sa.Column(sa.String(255), nullable=True)
    fault = sa.Column(sa.Text, nullable=True)
    reconcile_fault = sa.Column(sa.Text, nullable=True)
    unhealthy_since = sa.Column(base_models.timestamp_type(), nullable=True)
    running_since = sa.Column(base_models.timestamp_type(), nullable=True)
    replicas = sa.Column(sa.Integer, nullable=False, default=1)
    cpu_limit = sa.Column(sa.Float, nullable=True)
    memory_limit_mb = sa.Column(sa.Integer, nullable=True)
    health_check = sa.Column(sa.String(16), nullable=False,
                             server_default='none', default='http')
    health_check_port = sa.Column(sa.Integer, nullable=True)
    healthcheck_path = sa.Column(sa.String(255), nullable=True)
    auto_deploy = sa.Column(sa.Boolean, nullable=False)
    status = sa.Column(sa.String(32), nullable=False)
    task_state = sa.Column(sa.String(32), nullable=False)
    task_owner = sa.Column(sa.String(64), nullable=True)
    task_started_at = sa.Column(base_models.timestamp_type(), nullable=True)
    current_deployment_id = sa.Column(
        sa.String(36), sa.ForeignKey('deployments.id', use_alter=True),
        nullable=True)
    engine_ref = sa.Column(sa.String(255), nullable=True)
    settings_changed_at = sa.Column(sa.DateTime, nullable=True)
    default_domain_enabled = sa.Column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true())
    default_domain = sa.Column(sa.String(253), nullable=True)
    webhook_nonce = sa.Column(sa.String(64), nullable=True)


class Deployment(base_models.Base,
                 base_models.IdMixin,
                 base_models.ProjectMixin,
                 base_models.SoftDeleteMixin):
    __tablename__ = 'deployments'
    runtime_log = sa.Column(sa.Text, nullable=True)
    __table_args__ = (
        sa.UniqueConstraint('service_id', 'number'),
        base_models.Base.__table_args__,
    )
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False, index=True)
    number = sa.Column(sa.Integer, nullable=False)
    source_ref = sa.Column(sa.String(255), nullable=True)
    resolved_ref = sa.Column(sa.String(255), nullable=True)
    spec = sa.Column(sa.JSON, nullable=False)
    image_digest = sa.Column(sa.String(255), nullable=True)
    server_id = sa.Column(sa.String(36), sa.ForeignKey('servers.id'),
                          nullable=True)
    trigger = sa.Column(sa.String(16), nullable=False)
    rollback_of = sa.Column(sa.String(36), sa.ForeignKey('deployments.id'),
                            nullable=True)
    status = sa.Column(sa.String(32), nullable=False)
    fault = sa.Column(sa.Text, nullable=True)
    engine_ref = sa.Column(sa.String(255), nullable=True)
    log_ref = sa.Column(sa.String(255), nullable=True)
    started_at = sa.Column(sa.DateTime, nullable=True)
    finished_at = sa.Column(sa.DateTime, nullable=True)


class Domain(base_models.Base, base_models.IdMixin, base_models.ProjectMixin):
    __tablename__ = 'domains'
    primary = sa.Column(sa.Boolean, nullable=False, default=False,
                        server_default=sa.false())
    verification_started_at = sa.Column(sa.DateTime, nullable=True)
    dns_previous = sa.Column(sa.JSON, nullable=True)
    zone_id = sa.Column(sa.String(36), nullable=True)
    dns_trust_id = sa.Column(sa.String(255), nullable=True)
    managed_records = sa.Column(sa.JSON, nullable=True)
    status_reason = sa.Column(sa.String(255), nullable=True)
    record_type = sa.Column(sa.String(8), nullable=True)
    __table_args__ = (
        sa.UniqueConstraint('fqdn'),
        base_models.Base.__table_args__,
    )
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False)
    fqdn = sa.Column(sa.String(253), nullable=False)
    verification_token = sa.Column(sa.String(64), nullable=False)
    verified_at = sa.Column(sa.DateTime, nullable=True)
    tls = sa.Column(sa.String(16), nullable=False)
    auto_dns = sa.Column(sa.Boolean, nullable=False)
    dns_mode = sa.Column(sa.String(16), nullable=False)
    status = sa.Column(sa.String(32), nullable=False)


class Secret(base_models.Base, base_models.IdMixin, base_models.ProjectMixin):
    __tablename__ = 'secrets'
    __table_args__ = (
        sa.UniqueConstraint('service_id', 'key'),
        base_models.Base.__table_args__,
    )
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False)
    key = sa.Column(sa.String(255), nullable=False)
    is_secret = sa.Column(sa.Boolean, nullable=False)
    value_ref = sa.Column(sa.String(255), nullable=False)
    build_time = sa.Column(sa.Boolean, nullable=False)
    version = sa.Column(sa.Integer, nullable=False)


class AppVolumeMount(base_models.Base,
                     base_models.IdMixin,
                     base_models.ProjectMixin):
    __tablename__ = 'app_volume_mounts'
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False)
    volume_id = sa.Column(sa.String(36), sa.ForeignKey('volumes.id'),
                          nullable=False)
    sub_path = sa.Column(sa.String(255), nullable=False)
    container_path = sa.Column(sa.String(255), nullable=False)
    read_only = sa.Column(sa.Boolean, nullable=False)


class Storage(base_models.Base, base_models.IdMixin, base_models.ProjectMixin):
    """Service host bind or named volume on a dedicated worker."""

    __tablename__ = 'storages'
    __table_args__ = (
        sa.UniqueConstraint('stack_id', 'name',
                            name='uq_storages_stack_id_name'),
        base_models.Base.__table_args__,
    )
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False)
    type = sa.Column(sa.String(16), nullable=False, default='volume',
                     server_default='volume')
    host_path = sa.Column(sa.String(4096), nullable=True)
    read_only = sa.Column(sa.Boolean, nullable=False, default=False,
                          server_default=sa.false())
    name = sa.Column(sa.String(63), nullable=True)
    size_gb = sa.Column(sa.Integer, nullable=True)
    status = sa.Column(sa.String(32), nullable=False)
    origin = sa.Column(sa.String(16), nullable=False)
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=True)
    server_id = sa.Column(sa.String(36), sa.ForeignKey('servers.id'),
                          nullable=True)
    container_path = sa.Column(sa.String(255), nullable=True)


class FileMount(base_models.Base, base_models.IdMixin,
                base_models.ProjectMixin):
    """Container file content is held in Barbican, never in this table."""

    __tablename__ = 'file_mounts'
    __table_args__ = (
        sa.UniqueConstraint('service_id', 'container_path',
                            name='uq_file_mounts_service_id_container_path'),
        base_models.Base.__table_args__,
    )
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False)
    container_path = sa.Column(sa.String(255), nullable=False)
    content_ref = sa.Column(sa.String(1024), nullable=False)
    resource_name = sa.Column(sa.String(255), nullable=True)


class Export(base_models.Base, base_models.IdMixin, base_models.ProjectMixin):
    __tablename__ = 'exports'
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False)
    status = sa.Column(sa.String(32), nullable=False)
    volume_ids = sa.Column(sa.JSON, nullable=False)
    backend = sa.Column(sa.String(16), nullable=False)
    artifacts = sa.Column(sa.JSON, nullable=True)
    object_expires_at = sa.Column(sa.DateTime, nullable=True)
    fault = sa.Column(sa.Text, nullable=True)


class Event(base_models.Base, base_models.IdMixin, base_models.ProjectMixin):
    __tablename__ = 'events'
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=True)
    resource_type = sa.Column(sa.String(32), nullable=False)
    resource_id = sa.Column(sa.String(36), nullable=False)
    action = sa.Column(sa.String(64), nullable=False)
    outcome = sa.Column(sa.String(16), nullable=False)
    message = sa.Column(sa.String(1024), nullable=True)
    detail = sa.Column(sa.JSON, nullable=True)
    request_id = sa.Column(sa.String(64), nullable=True)


class ConsoleToken(base_models.Base, base_models.IdMixin,
                   base_models.ProjectMixin):
    """One single-use console session grant. The secret is not stored."""

    __tablename__ = 'console_tokens'
    __table_args__ = (
        sa.UniqueConstraint('token_hash',
                            name='uq_console_tokens_token_hash'),
        base_models.Base.__table_args__,
    )
    token_hash = sa.Column(sa.String(64), nullable=False)
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False, index=True)
    resource_type = sa.Column(sa.String(16), nullable=False)
    resource_id = sa.Column(sa.String(36), nullable=False, index=True)
    command = sa.Column(sa.JSON, nullable=True)
    user_id = sa.Column(sa.String(255), nullable=False)
    # Plain DateTime, like exports.object_expires_at. Not a row timestamp.
    expires_at = sa.Column(sa.DateTime, nullable=False)
    used_at = sa.Column(sa.DateTime, nullable=True)


class StackDeleteSnapshot(base_models.Base, base_models.IdMixin,
                          base_models.ProjectMixin):
    """One kept Cinder snapshot for a volume deleted with its stack.

    volume_id is not a foreign key. The volume row is hard-deleted.
    This row stays so a retry does not create a second snapshot.
    """

    __tablename__ = 'stack_delete_snapshots'
    __table_args__ = (
        sa.UniqueConstraint(
            'stack_id', 'volume_id',
            name='uq_stack_delete_snapshots_stack_id_volume_id'),
        base_models.Base.__table_args__,
    )
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False, index=True)
    volume_id = sa.Column(sa.String(36), nullable=False)
    cinder_volume_id = sa.Column(sa.String(36), nullable=False)
    cinder_snapshot_id = sa.Column(sa.String(36), nullable=False)
    status = sa.Column(sa.String(32), nullable=False)

    retention_days = sa.Column(sa.Integer, nullable=True)
    expires_at = sa.Column(sa.DateTime, nullable=True, index=True)


class Quota(base_models.Base):
    __tablename__ = 'quotas'
    project_id = sa.Column(sa.String(36),
                           nullable=False, primary_key=True, index=True)
    environments = sa.Column(sa.Integer, nullable=True)
    servers = sa.Column(sa.Integer, nullable=True)
    apps = sa.Column(sa.Integer, nullable=True)
    domains = sa.Column(sa.Integer, nullable=True)
    volume_gb = sa.Column(sa.Integer, nullable=True)
    build_servers = sa.Column(sa.Integer, nullable=True)
    baremetal_servers = sa.Column(sa.Integer, nullable=True)
    storage_gb = sa.Column(sa.Integer, nullable=True)


class QuotaUsage(base_models.Base):
    __tablename__ = 'quota_usages'
    project_id = sa.Column(sa.String(36),
                           nullable=False, primary_key=True, index=True)
    in_use_environments = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    reserved_environments = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    in_use_servers = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    reserved_servers = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    in_use_apps = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    reserved_apps = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    in_use_domains = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    reserved_domains = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    in_use_volume_gb = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    reserved_volume_gb = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    in_use_build_servers = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    reserved_build_servers = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    in_use_baremetal_servers = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    reserved_baremetal_servers = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    in_use_storage_gb = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)
    reserved_storage_gb = sa.Column(
        sa.Integer, nullable=False, server_default=sa.text('0'), default=0)


class ServiceMetric(base_models.Base, base_models.IdMixin):
    __tablename__ = 'service_metrics'
    __table_args__ = (
        sa.UniqueConstraint('service_id', 'container_name', 'resolution',
                            'timestamp', name='uq_service_metrics_bucket'),
        sa.Index('ix_service_metrics_service_resolution_timestamp',
                 'service_id', 'resolution', 'timestamp'),
        sa.Index('ix_service_metrics_resolution_timestamp',
                 'resolution', 'timestamp'),
        base_models.Base.__table_args__,
    )
    service_id = sa.Column(sa.String(36), sa.ForeignKey('services.id'),
                           nullable=False)
    container_name = sa.Column(sa.String(128), nullable=False)
    resolution = sa.Column(sa.Integer, nullable=False)
    timestamp = sa.Column(sa.DateTime, nullable=False)
    cpu_percent = sa.Column(sa.Float, nullable=False)
    memory_bytes = sa.Column(sa.BigInteger, nullable=False)
    memory_limit_bytes = sa.Column(sa.BigInteger, nullable=True)
    sample_count = sa.Column(sa.Integer, nullable=False, default=1)


class IngressRule(base_models.Base, base_models.IdMixin,
                  base_models.ProjectMixin):
    __tablename__ = 'ingress_rules'
    stack_id = sa.Column(sa.String(36), sa.ForeignKey('stacks.id'),
                         nullable=False, index=True)
    protocol = sa.Column(sa.String(4), nullable=False)
    port_range_min = sa.Column(sa.Integer, nullable=True)
    port_range_max = sa.Column(sa.Integer, nullable=True)
    source = sa.Column(sa.String(18), nullable=False)
    description = sa.Column(sa.String(255), nullable=True)
