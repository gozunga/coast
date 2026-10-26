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

"""Initial v0.1.0 schema; pre-release databases must be recreated."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def _timestamp_type():
    return sa.DateTime().with_variant(
        mysql.DATETIME(fsp=6), 'mysql', 'mariadb')


def upgrade():
    op.create_table(
        'quota_usages',
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('in_use_environments', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('reserved_environments', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('in_use_servers', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('reserved_servers', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('in_use_apps', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('reserved_apps', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('in_use_domains', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('reserved_domains', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('in_use_volume_gb', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('reserved_volume_gb', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('in_use_build_servers', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('reserved_build_servers', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('in_use_baremetal_servers', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('reserved_baremetal_servers', sa.Integer(),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.PrimaryKeyConstraint('project_id', name='pk_quota_usages'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_quota_usages_project_id', 'quota_usages',
                    ['project_id'])
    op.create_table(
        'quotas',
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('environments', sa.Integer(), nullable=True),
        sa.Column('servers', sa.Integer(), nullable=True),
        sa.Column('apps', sa.Integer(), nullable=True),
        sa.Column('domains', sa.Integer(), nullable=True),
        sa.Column('volume_gb', sa.Integer(), nullable=True),
        sa.Column('build_servers', sa.Integer(), nullable=True),
        sa.Column('baremetal_servers', sa.Integer(), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.PrimaryKeyConstraint('project_id', name='pk_quotas'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_quotas_project_id', 'quotas', ['project_id'])
    op.create_table(
        'sources',
        sa.Column('type', sa.String(16), nullable=False),
        sa.Column('repo_url', sa.String(1024), nullable=True),
        sa.Column('branch', sa.String(255), nullable=True),
        sa.Column('build_pack', sa.String(32), nullable=True),
        sa.Column('dockerfile_path', sa.String(255), nullable=True),
        sa.Column('compose_path', sa.String(255), nullable=True),
        sa.Column('base_directory', sa.String(255), nullable=True),
        sa.Column('image', sa.String(1024), nullable=True),
        sa.Column('credentials_ref', sa.String(255), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_sources'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_sources_project_id', 'sources', ['project_id'])
    op.create_table(
        'stacks',
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('driver', sa.String(64), nullable=False),
        sa.Column('engine_version', sa.String(64), nullable=False),
        sa.Column('plan', sa.String(64), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('task_state', sa.String(32), nullable=False),
        sa.Column('conductor_host', sa.String(255), nullable=True),
        sa.Column('fault', sa.Text(), nullable=True),
        sa.Column('network_id', sa.String(36), nullable=True),
        sa.Column('subnet_id', sa.String(36), nullable=True),
        sa.Column('router_id', sa.String(36), nullable=True),
        sa.Column('sg_ingress_id', sa.String(36), nullable=True),
        sa.Column('sg_mgmt_id', sa.String(36), nullable=True),
        sa.Column('engine_ref', sa.String(255), nullable=True),
        sa.Column('engine_token_ref', sa.String(255), nullable=True),
        sa.Column('heartbeat_key_ref', sa.String(255), nullable=True),
        sa.Column('engine_backup_ref', sa.String(255), nullable=True),
        sa.Column('ssh_key_ref', sa.String(255), nullable=True),
        sa.Column('registry_secret_ref', sa.String(255), nullable=True),
        sa.Column('ssh_public_key', sa.Text(), nullable=True),
        sa.Column('previous_image_id', sa.String(36), nullable=True),
        sa.Column('previous_engine_version', sa.String(64), nullable=True),
        sa.Column('upgrade_snapshots', sa.JSON(), nullable=True),
        sa.Column('upgrade_checkpoint', sa.JSON(), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('deleted_at', _timestamp_type(), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_stacks'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_stacks_project_id', 'stacks', ['project_id'])
    op.create_table(
        'appgroups',
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('task_state', sa.String(32), nullable=False),
        sa.Column('engine_ref', sa.String(255), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('deleted_at', _timestamp_type(), nullable=True),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_appgroups_stack_id_stacks'),
        sa.PrimaryKeyConstraint('id', name='pk_appgroups'),
        sa.UniqueConstraint('stack_id', 'name',
                            name='uq_appgroups_stack_id_name'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_appgroups_project_id', 'appgroups', ['project_id'])
    op.create_index('ix_appgroups_stack_id', 'appgroups', ['stack_id'])
    op.create_table(
        'events',
        sa.Column('stack_id', sa.String(36), nullable=True),
        sa.Column('resource_type', sa.String(32), nullable=False),
        sa.Column('resource_id', sa.String(36), nullable=False),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('outcome', sa.String(16), nullable=False),
        sa.Column('message', sa.String(1024), nullable=True),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('request_id', sa.String(64), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_events_stack_id_stacks'),
        sa.PrimaryKeyConstraint('id', name='pk_events'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_events_project_id', 'events', ['project_id'])
    op.create_table(
        'exports',
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('volume_ids', sa.JSON(), nullable=False),
        sa.Column('backend', sa.String(16), nullable=False),
        sa.Column('artifacts', sa.JSON(), nullable=True),
        sa.Column('object_expires_at', sa.DateTime(), nullable=True),
        sa.Column('fault', sa.Text(), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_exports_stack_id_stacks'),
        sa.PrimaryKeyConstraint('id', name='pk_exports'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_exports_project_id', 'exports', ['project_id'])
    op.create_table(
        'servers',
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('task_state', sa.String(32), nullable=False),
        sa.Column('flavor_id', sa.String(36), nullable=True),
        sa.Column('image_id', sa.String(36), nullable=True),
        sa.Column('compute_id', sa.String(36), nullable=True),
        sa.Column('ironic_node_id', sa.String(36), nullable=True),
        sa.Column('mgmt_ip', sa.String(64), nullable=True),
        sa.Column('private_ip', sa.String(64), nullable=True),
        sa.Column('floating_ip', sa.String(64), nullable=True),
        sa.Column('floating_ip_id', sa.String(36), nullable=True),
        sa.Column('agent_cert_serial', sa.String(64), nullable=True),
        sa.Column('agent_cert_expires_at', sa.DateTime(), nullable=True),
        sa.Column('last_heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('engine_ref', sa.String(255), nullable=True),
        sa.Column('fault', sa.Text(), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('deleted_at', _timestamp_type(), nullable=True),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_servers_stack_id_stacks'),
        sa.PrimaryKeyConstraint('id', name='pk_servers'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_servers_project_id', 'servers', ['project_id'])
    op.create_index('ix_servers_stack_id', 'servers', ['stack_id'])
    op.create_table(
        'stack_delete_snapshots',
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('volume_id', sa.String(36), nullable=False),
        sa.Column('cinder_volume_id', sa.String(36), nullable=False),
        sa.Column('cinder_snapshot_id', sa.String(36), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('retention_days', sa.Integer(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ['stack_id'], ['stacks.id'],
            name='fk_stack_delete_snapshots_stack_id_stacks'),
        sa.PrimaryKeyConstraint('id', name='pk_stack_delete_snapshots'),
        sa.UniqueConstraint(
            'stack_id', 'volume_id',
            name='uq_stack_delete_snapshots_stack_id_volume_id'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index(
        'ix_stack_delete_snapshots_expires_at',
        'stack_delete_snapshots', ['expires_at'])
    op.create_index(
        'ix_stack_delete_snapshots_project_id',
        'stack_delete_snapshots', ['project_id'])
    op.create_index(
        'ix_stack_delete_snapshots_stack_id',
        'stack_delete_snapshots', ['stack_id'])
    op.create_table(
        'environments',
        sa.Column('appgroup_id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('task_state', sa.String(32), nullable=False),
        sa.Column('engine_ref', sa.String(255), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('deleted_at', _timestamp_type(), nullable=True),
        sa.ForeignKeyConstraint(['appgroup_id'], ['appgroups.id'],
                                name='fk_environments_appgroup_id_appgroups'),
        sa.PrimaryKeyConstraint('id', name='pk_environments'),
        sa.UniqueConstraint('appgroup_id', 'name',
                            name='uq_environments_appgroup_id_name'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_environments_appgroup_id', 'environments',
                    ['appgroup_id'])
    op.create_index('ix_environments_project_id', 'environments',
                    ['project_id'])
    op.create_table(
        'volumes',
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('server_id', sa.String(36), nullable=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('size_gb', sa.Integer(), nullable=False),
        sa.Column('volume_type', sa.String(64), nullable=True),
        sa.Column('mount_path', sa.String(255), nullable=False),
        sa.Column('purpose', sa.String(16), nullable=False),
        sa.Column('cinder_volume_id', sa.String(36), nullable=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'],
                                name='fk_volumes_server_id_servers'),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_volumes_stack_id_stacks'),
        sa.PrimaryKeyConstraint('id', name='pk_volumes'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_volumes_project_id', 'volumes', ['project_id'])
    op.create_table(
        'services',
        sa.Column('stack_id', sa.String(36), nullable=False),
        sa.Column('environment_id', sa.String(36), nullable=True),
        sa.Column('name', sa.String(63), nullable=False),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('source_id', sa.String(36), nullable=False),
        sa.Column('template_id', sa.String(255), nullable=True),
        sa.Column('server_id', sa.String(36), nullable=True),
        sa.Column('ports', sa.String(255), nullable=True),
        sa.Column('reconcile_fault', sa.Text(), nullable=True),
        sa.Column('unhealthy_since', _timestamp_type(), nullable=True),
        sa.Column('running_since', _timestamp_type(), nullable=True),
        sa.Column('replicas', sa.Integer(), nullable=False),
        sa.Column('cpu_limit', sa.Float(), nullable=True),
        sa.Column('memory_limit_mb', sa.Integer(), nullable=True),
        sa.Column('health_check', sa.String(16), server_default='none',
                  nullable=False),
        sa.Column('health_check_port', sa.Integer(), nullable=True),
        sa.Column('healthcheck_path', sa.String(255), nullable=True),
        sa.Column('auto_deploy', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('task_state', sa.String(32), nullable=False),
        sa.Column('current_deployment_id', sa.String(36), nullable=True),
        sa.Column('engine_ref', sa.String(255), nullable=True),
        sa.Column('settings_changed_at', sa.DateTime(), nullable=True),
        sa.Column('default_domain', sa.String(253), nullable=True),
        sa.Column('webhook_nonce', sa.String(64), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('deleted_at', _timestamp_type(), nullable=True),
        *([sa.ForeignKeyConstraint(
            ['current_deployment_id'], ['deployments.id'],
            name='fk_services_current_deployment_id_deployments')]
          if op.get_bind().dialect.name == 'sqlite' else []),
        sa.ForeignKeyConstraint(['environment_id'], ['environments.id'],
                                name='fk_services_environment_id'),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'],
                                name='fk_services_server_id_servers'),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id'],
                                name='fk_services_source_id_sources'),
        sa.ForeignKeyConstraint(['stack_id'], ['stacks.id'],
                                name='fk_services_stack_id_stacks'),
        sa.PrimaryKeyConstraint('id', name='pk_services'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_services_project_id', 'services', ['project_id'])
    op.create_table(
        'app_volume_mounts',
        sa.Column('service_id', sa.String(36), nullable=False),
        sa.Column('volume_id', sa.String(36), nullable=False),
        sa.Column('sub_path', sa.String(255), nullable=False),
        sa.Column('container_path', sa.String(255), nullable=False),
        sa.Column('read_only', sa.Boolean(), nullable=False),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ['service_id'], ['services.id'],
            name='fk_app_volume_mounts_service_id_services'),
        sa.ForeignKeyConstraint(['volume_id'], ['volumes.id'],
                                name='fk_app_volume_mounts_volume_id_volumes'),
        sa.PrimaryKeyConstraint('id', name='pk_app_volume_mounts'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_app_volume_mounts_project_id', 'app_volume_mounts',
                    ['project_id'])
    op.create_table(
        'deployments',
        sa.Column('runtime_log', sa.Text(), nullable=True),
        sa.Column('service_id', sa.String(36), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('source_ref', sa.String(255), nullable=True),
        sa.Column('resolved_ref', sa.String(255), nullable=True),
        sa.Column('spec', sa.JSON(), nullable=False),
        sa.Column('image_digest', sa.String(255), nullable=True),
        sa.Column('server_id', sa.String(36), nullable=True),
        sa.Column('trigger', sa.String(16), nullable=False),
        sa.Column('rollback_of', sa.String(36), nullable=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('fault', sa.Text(), nullable=True),
        sa.Column('engine_ref', sa.String(255), nullable=True),
        sa.Column('log_ref', sa.String(255), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('deleted_at', _timestamp_type(), nullable=True),
        sa.ForeignKeyConstraint(['rollback_of'], ['deployments.id'],
                                name='fk_deployments_rollback_of_deployments'),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'],
                                name='fk_deployments_server_id_servers'),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'],
                                name='fk_deployments_service_id_services'),
        sa.PrimaryKeyConstraint('id', name='pk_deployments'),
        sa.UniqueConstraint('service_id', 'number',
                            name='uq_deployments_service_id_number'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_deployments_project_id', 'deployments', ['project_id'])
    op.create_index('ix_deployments_service_id', 'deployments', ['service_id'])
    op.create_table(
        'domains',
        sa.Column('service_id', sa.String(36), nullable=False),
        sa.Column('fqdn', sa.String(253), nullable=False),
        sa.Column('verification_token', sa.String(64), nullable=False),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('tls', sa.String(16), nullable=False),
        sa.Column('auto_dns', sa.Boolean(), nullable=False),
        sa.Column('dns_mode', sa.String(16), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'],
                                name='fk_domains_service_id_services'),
        sa.PrimaryKeyConstraint('id', name='pk_domains'),
        sa.UniqueConstraint('fqdn', name='uq_domains_fqdn'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_domains_project_id', 'domains', ['project_id'])
    op.create_table(
        'secrets',
        sa.Column('service_id', sa.String(36), nullable=False),
        sa.Column('key', sa.String(255), nullable=False),
        sa.Column('is_secret', sa.Boolean(), nullable=False),
        sa.Column('value_ref', sa.String(255), nullable=False),
        sa.Column('build_time', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', _timestamp_type(), nullable=False),
        sa.Column('updated_at', _timestamp_type(), nullable=True),
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'],
                                name='fk_secrets_service_id_services'),
        sa.PrimaryKeyConstraint('id', name='pk_secrets'),
        sa.UniqueConstraint('service_id', 'key',
                            name='uq_secrets_service_id_key'),
        mysql_charset='utf8mb4',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_secrets_project_id', 'secrets', ['project_id'])
    if op.get_bind().dialect.name != 'sqlite':
        op.create_foreign_key(
            'fk_services_current_deployment_id_deployments',
            'services', 'deployments', ['current_deployment_id'], ['id'])


def downgrade():
    # Break the circular FK before dropping either table on MySQL/MariaDB.
    if op.get_bind().dialect.name != 'sqlite':
        op.drop_constraint(
            'fk_services_current_deployment_id_deployments',
            'services', type_='foreignkey')
    op.drop_table('secrets')
    op.drop_table('domains')
    op.drop_table('deployments')
    op.drop_table('app_volume_mounts')
    op.drop_table('services')
    op.drop_table('volumes')
    op.drop_table('environments')
    op.drop_table('stack_delete_snapshots')
    op.drop_table('servers')
    op.drop_table('exports')
    op.drop_table('events')
    op.drop_table('appgroups')
    op.drop_table('stacks')
    op.drop_table('sources')
    op.drop_table('quotas')
    op.drop_table('quota_usages')
