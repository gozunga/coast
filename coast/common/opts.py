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

import re
import socket

from keystoneauth1 import loading
from keystonemiddleware import auth_token
from oslo_config import cfg
from oslo_messaging._drivers import amqp
from oslo_messaging._drivers import impl_rabbit
from oslo_messaging import opts as messaging_opts
from oslo_policy import opts as policy_opts

from coast.common import engine_versions


DEFAULT_OPTS = [
    cfg.StrOpt('host', default=socket.gethostname(),
               sample_default='conductor.example.com',
               help='Stable conductor host identity for restart recovery. '
                    'Use a different value for each conductor host.'),
    cfg.StrOpt('resource_name_prefix', default='coast',
               help='Prefix prepended to names of cloud resources Coast '
                    'creates, e.g. coast-e2e-20261010 for test runs. '
                    'Existing resources are not renamed.'),
]

API_OPTS = [
    cfg.IntOpt('max_compose_bytes', default=10000, min=1,
               help='Maximum UTF-8 compose YAML bytes. Keep within Barbican '
                    'secret and request size limits.'),
    cfg.StrOpt('auth_strategy', default='keystone',
               choices=['keystone', 'noauth'],
               help='API authentication strategy. Use noauth only for tests '
                    'and isolated development; it trusts identity headers.'),
    cfg.StrOpt('api_paste_config', default='api-paste.ini',
               help='API Paste pipeline file, relative to config directories '
                    'or an absolute path.'),
    # Operator-configurable listen address specified in operations.md.
    cfg.StrOpt('bind_host', default='0.0.0.0',  # nosec B104
               help='Address on which to bind the API.'),
    cfg.PortOpt('bind_port', default=9600,
                help='API listen port.'),
    cfg.StrOpt('public_endpoint', default=None,
               help='Public API endpoint used in links.'),
    cfg.IntOpt('max_limit', default=1000,
               help='Maximum pagination limit.'),
]

CONDUCTOR_OPTS = [
    cfg.IntOpt('service_reconcile_interval', default=600, min=1,
               help='Seconds between bounded service reconcile scan rounds. '
                    'Rounds have up to ten percent jitter; failures back off '
                    'up to one hour. No automatic restart is performed.'),
    cfg.IntOpt('workers', default=2,
               help='Number of conductor workers.'),
    cfg.IntOpt('stable_samples', default=6, min=1,
               help='Before a deployment goes ACTIVE, sample the container '
                    'status this many times, stable_sample_interval apart. '
                    'Any sample that is not running restarts the stability '
                    'window, so a fast crash loop is not taken as stable.'),
    cfg.FloatOpt('stable_sample_interval', default=2.0, min=0,
                 help='Seconds between stability samples.'),
    cfg.IntOpt('stable_seconds', default=20, min=0,
               help='Seconds a container must run without a restart before '
                    'a deployment or start becomes ACTIVE, or runtime '
                    'reconcile recovers its own ERROR status.'),
    cfg.IntOpt('deploy_timeout', default=600,
               min=1,
               help='Deployment timeout in seconds.'),
    cfg.IntOpt('deploy_unreachable_timeout', default=150, min=0,
               help='Seconds of container crash loops after rollout, or '
                    'HTTP no answer/502/503 with an HTTP health check, '
                    'before deployment failure or runtime reconcile ERROR. '
                    '0 disables this deadline; '
                    'deploy_timeout remains the final limit.'),
    cfg.BoolOpt('http_check', default=True,
                help='Before marking a started or deployed service ACTIVE, '
                     'require apps with HTTP checks to answer below HTTP 500 '
                     'at the worker floating IP. Disable when the conductor '
                     'cannot reach worker floating IPs.'),
    cfg.IntOpt('start_timeout', default=180, min=1,
               help='Seconds a started or restarted service may stay '
                    'STARTING before it is marked ERROR. Checked by the '
                    'deployment settle poll (settle_poll_interval).'),
    cfg.IntOpt('deploy_reconcile_interval', default=60, min=1,
               help='Interval in seconds for deployment recovery. Startup '
                    'also checks deployments. Unknown enqueue outcomes '
                    'fail after deploy_timeout; they are never resubmitted.'),
    cfg.IntOpt('settle_poll_interval', default=15, min=1,
               help='Interval in seconds for checking DEPLOYING and STARTING '
                    'services and moving them to ACTIVE, FAILED or ERROR. '
                    'Only services in those states are checked. Export '
                    'and snapshot housekeeping stay on '
                    'deploy_reconcile_interval.'),
    cfg.IntOpt('build_timeout', default=900,
               help='Stack and server boot timeout in seconds.'),
]

AGENT_OPTS = [
    cfg.StrOpt('egress_check_url', default='',
               help='HTTPS registry URL from the conductor. Empty checks '
                    'environment routing and Docker Hub DNS only.'),
    cfg.IntOpt('export_auto_resume_timeout', default=900, min=1,
               help='Maximum export or engine quiesce lease in seconds. '
                    'Expired leases resume only containers stopped by that '
                    'operation.'),
    cfg.StrOpt('bind_ip', help='Required management NIC IPv4 address.'),
    cfg.PortOpt('bind_port', default=9443, help='Agent mTLS listen port.'),
    cfg.StrOpt('server_cert', help='Agent server certificate PEM file.'),
    cfg.StrOpt('server_key', secret=True,
               help='Agent server private key PEM file.'),
    cfg.StrOpt('client_ca', help='Coast client CA PEM file; no system CAs.'),
    cfg.StrOpt('server_ca', help='Existing Coast server CA public PEM file. '
               'Required for certificate rotation; never updated by HTTP.'),
    cfg.StrOpt('server_id', help='Coast server UUID for heartbeats.'),
    cfg.StrOpt('stack_id', help='Coast stack UUID.'),
    cfg.StrOpt('project_id', default='',
               help='Coast project id for this guest. Container exec fails '
                    'closed when this value is empty or does not match the '
                    'request.'),
    cfg.StrOpt('role', default='worker',
               choices=['controller', 'worker', 'build'],
               help='Guest role; only controllers can bootstrap Coolify.'),
    cfg.StrOpt('state_path', default='/data/coast-agent',
               help='Owned 0700 directory on persistent data volume for '
                    'one-shot markers and staged rotation tokens. Preserve '
                    'during guest replacement.'),
    cfg.StrOpt('runtime_path', default='/run/coast-agent',
               help='Owned 0700 volatile directory for temporary token files. '
                    'Use tmpfs; abandoned token directories are removed at '
                    'startup without resetting persistent markers.'),
    cfg.ListOpt('health_manager_ips', default=[],
                help='Health-manager IPv4 addresses; no DNS on management.'),
    cfg.PortOpt('heartbeat_port', default=5565,
                help='Heartbeat destination port.'),
    cfg.StrOpt('heartbeat_key', secret=True,
               help='Per-stack 32-byte HMAC key in hexadecimal.'),
    cfg.IntOpt('heartbeat_interval', default=10, min=1,
               help='Heartbeat send interval in seconds.'),
    cfg.IntOpt('metrics_max_workers', default=8, min=1, max=64,
               help='Maximum concurrent Docker container statistics reads.'),
    cfg.IntOpt('metrics_interval', default=30, min=30,
               help='Seconds between worker CPU and memory samples.'),
    cfg.IntOpt('max_body_size', default=1048576, min=1,
               help='Maximum HTTP request or engine response body bytes.'),
    cfg.IntOpt('request_timeout', default=30, min=1,
               help='Total request and engine proxy time limit in seconds.'),
    cfg.IntOpt('bootstrap_timeout', default=600, min=1,
               help='Bootstrap response deadline in seconds; local steps '
                    'also have fixed subprocess and API time limits.'),
    cfg.IntOpt('exec_idle_timeout', default=900, min=1,
               help='Idle seconds before a container exec session closes.'),
    cfg.IntOpt('exec_max_duration', default=14400, min=1,
               help='Maximum seconds for one container exec session.'),
    cfg.IntOpt('max_connections', default=16, min=1,
               help='Maximum connections, including TLS handshakes.'),
]

HEALTH_MANAGER_OPTS = [
    cfg.IntOpt('cert_rotation_interval', default=3600, min=1,
               help='Seconds between bounded agent certificate checks. '
                    'Rotate serverAuth leaves at 80 percent of validity.'),
    cfg.IntOpt('key_cache_max_stale', default=3600, min=0,
               help='Maximum seconds after heartbeat key cache expiry to use '
                    'the last good key when secret storage is unavailable.'),
    cfg.StrOpt('key_lookup',
               default='coast.health_manager.keys.StackKeyLookup',
               help='Import path of a no-argument callable class. Instances '
                    'take a stack UUID and return 32 raw HMAC key '
                    'bytes, or None. Use '
                    'coast.health_manager.keys.StackKeyLookup for Barbican '
                    'refs. '
                    'Operators can override this lookup. Never put key '
                    'contents in this option.'),
    cfg.StrOpt('state_path', default='/var/lib/coast/health-manager',
               help='Private durable directory for replay sequence '
                    'checkpoints. Preserve across service restart.'),
    cfg.IntOpt('max_packet_size', default=4096, min=256, max=65507,
               help='Maximum heartbeat datagram size including HMAC.'),
    cfg.IntOpt('max_packets_per_second', default=1000, min=1,
               help='Global heartbeat processing limit per second. Excess '
                    'packets are dropped before parsing or key lookup.'),
    cfg.IntOpt('batch_size', default=100, min=1,
               help='Maximum number of pending server last-seen writes.'),
    cfg.FloatOpt('batch_interval', default=1.0, min=0.5,
                 help='Maximum normal delay in seconds before a batch write.'),
    cfg.StrOpt('bind_ip', default=None,
               help='Management address for the UDP listener.'),
    cfg.PortOpt('bind_port', default=5565,
                help='Heartbeat UDP port.'),
    cfg.IntOpt('heartbeat_interval', default=10, min=1,
               help='Heartbeat and stale-check interval in seconds.'),
    cfg.IntOpt('heartbeat_timeout', default=60, min=1,
               help='Heartbeat timeout and startup grace in seconds.'),
    cfg.IntOpt('failover_grace', default=120, min=0,
               help='Failover grace period in seconds.'),
    cfg.IntOpt('admin_failover_stuck_timeout', default=600, min=1,
               help='Seconds since the last server update before an admin '
                    'may retry a stuck FAILOVER/FAILING_OVER task.'),
    cfg.IntOpt('failover_max_concurrent', default=2, min=1,
               help='Global maximum of claimed worker, build and controller '
                    'VM failovers; shared by all health managers and '
                    'conductors.'),
    cfg.IntOpt('reconcile_interval', default=600,
               help='Reconcile interval in seconds.'),
    cfg.BoolOpt('enable_failover', default=True,
                help='Enable automatic failover.'),
]

CONTROLLER_WORKER_OPTS = [
    cfg.StrOpt('admin_ssh_key_name', default='',
               help='Nova keypair name owned by the service project for '
                    'operator SSH to controller, worker and build VMs. '
                    'Empty disables key injection. This option does not open '
                    'SSH in management security groups.'),
    cfg.StrOpt('egress_check_url', default='',
               help='HTTPS registry URL for guest first-start egress checks. '
                    'Any HTTP response is success. Empty checks environment '
                    'routing and Docker Hub DNS without HTTPS. Use no '
                    'credentials, query parameters or fragment.'),
    cfg.IntOpt('volume_delete_timeout', default=300, min=1,
               help='Maximum seconds to wait for confirmed Cinder deletion.'),
    cfg.IntOpt('volume_delete_poll_interval', default=5, min=1,
               help='Seconds between Cinder volume deletion checks.'),
    cfg.IntOpt('build_timeout', default=900, min=1,
               help='Controller boot, volume, agent health and engine server '
                    'validation timeout in seconds.'),
    cfg.IntOpt('build_poll_interval', default=5, min=1,
               help='Controller provisioning poll interval in seconds.'),
    cfg.IntOpt('docker_volume_size_gb', default=20, min=1,
               help='Managed Docker volume size in GiB; conductor '
                    'accounts for this capacity before Cinder creation.'),
    cfg.IntOpt('template_storage_gb', default=1, min=1,
               help='Accounted size in GiB for each named volume declared '
                    'by a template service.'),
    cfg.StrOpt('docker_volume_type',
               help='Managed Docker Cinder volume type.'),
    cfg.IntOpt('registry_volume_size_gb', default=20, min=1,
               help='Private registry volume size in GiB. The conductor '
                    'accounts for this capacity before Cinder creation. '
                    'The volume is attached only to the controller.'),
    cfg.StrOpt('registry_volume_type',
               help='Private registry Cinder volume type.'),
    cfg.PortOpt('registry_port', default=5000,
                help='Private registry TCP port on the controller '
                     'environment address. Security group rules use this '
                     'same value. The registry accepts connections from '
                     'the stack environment subnet only when the stack '
                     'has a build server.'),
    cfg.StrOpt(
        'registry_image',
        default=('registry:3@sha256:ddf754342cfc8acc51a56d5d0ab6af0682646186'
                 '4460636d8bd5c546dab2a7b8'),
        help='Pinned registry image reference. The value must match '
             'registry:3 in the controller image lock. First boot refuses '
             'a different reference.'),
    cfg.IntOpt('registry_gc_interval', default=3600, min=1,
               help='Seconds between private registry garbage collection '
                    'on active stacks.'),
    cfg.StrOpt('agent_client_cert',
               help='Conductor clientAuth certificate PEM file.'),
    cfg.StrOpt('agent_client_key', secret=True,
               help='Conductor clientAuth private key PEM file.'),
    cfg.StrOpt('agent_client_ca',
               help='Coast client CA PEM file delivered to guest agents.'),
    cfg.ListOpt('health_manager_ips', default=[],
                help='Health-manager management IPv4 addresses for guests.'),
    cfg.ListOpt('enabled_drivers', default=['noop'],
                help='Allow-list of engine driver entry point names.'),
    cfg.StrOpt('driver', default='coolify_v4',
               help='Default driver for new stacks.'),
    cfg.StrOpt('mgmt_net_id', default=None,
               help='Management network ID.'),
    cfg.StrOpt('mgmt_sg_id', default=None,
               help='Management security group ID.'),
    cfg.StrOpt('external_network_id', default=None,
               help='External network ID for routers and floating IPs.'),
    cfg.StrOpt('env_cidr_pool', default=None,
               help='Operator IPv4 CIDR pool for stack subnets. Each '
                    'isolated router reuses the first subnet in this pool.'),
    cfg.IntOpt('env_subnet_prefix', default=24, min=1, max=30,
               help='IPv4 prefix length for stack subnets.'),
    cfg.IntOpt('network_delete_attempts', default=3, min=1,
               help='Maximum attempts for transient Neutron delete errors.'),
    cfg.FloatOpt('network_delete_retry_interval', default=1.0, min=0.0,
                 help='Delay in seconds between Neutron cleanup attempts.'),
    cfg.StrOpt('image_tag_controller', default=None,
               help='Controller image tag.'),
    cfg.StrOpt('image_tag_worker', default=None,
               help='Worker image tag.'),
    cfg.StrOpt('controller_image_id', default=None,
               help='Optional controller image ID; owner, tag, active status '
                    'and coast_role=controller are still required.'),
    cfg.StrOpt('worker_image_id', default=None,
               help='Optional worker/build image ID; owner, tag, active '
                    'status and coast_role=worker are still required.'),
    cfg.StrOpt('image_owner_id', default=None,
               help='Image owner project ID.'),
    cfg.StrOpt('controller_flavor_id', default=None,
               help='Controller flavor ID.'),
    cfg.IntOpt('data_volume_size_gb', default=100,
               help='Default data volume size in GiB.'),
    cfg.StrOpt('data_volume_type', default=None,
               help='Default Cinder volume type.'),
    cfg.ListOpt('egress_blocked_ports', default=['25', '465', '587'],
                help='Blocked guest egress ports.'),
    cfg.ListOpt('control_plane_cidrs', default=[],
                help='IPv4/IPv6 control-plane CIDRs excluded from guest '
                     'environment-NIC egress. Include internal API, database '
                     'and message-broker ranges. Management subnet ranges '
                     'and IPv4 metadata are always excluded. Empty excludes '
                     'only management and metadata ranges.'),
    cfg.IntOpt('ingress_rules_per_stack', default=50, min=0,
               help='Maximum customer ingress rules per stack.'),
    cfg.IntOpt('network_repair_interval', default=600, min=0,
               help='Seconds between security group repair sweeps that '
                    'apply control-plane egress exclusions to existing '
                    'stacks. 0 disables the sweep. It runs on its own timer, '
                    'not in the service reconcile loop.'),
    cfg.IntOpt('network_repair_timeout', default=120, min=1,
               help='Time budget in seconds for one security group repair '
                    'sweep. Unfinished stacks continue on the next sweep.'),
    cfg.ListOpt('dns_servers', default=[],
                help='Allowed guest DNS servers; empty allows any.'),
    cfg.ListOpt('ntp_servers', default=[],
                help='Allowed guest NTP servers; empty allows any.'),
    cfg.BoolOpt('enable_baremetal', default=False,
                help='Enable Ironic bare metal servers.'),
    cfg.StrOpt('mgmt_netns', default='coast-mgmt',
               help='Control-side management network namespace. An empty '
                    'value uses the host namespace without setns.'),
    cfg.PortOpt('agent_port', default=9443,
                help='Guest agent mTLS port.'),
    cfg.IntOpt('offboard_snapshot_retention_days', default=14,
               help='Default snapshot retention on delete in days.'),
]

CERTIFICATES_OPTS = [
    cfg.StrOpt('ca_certificate', default=None,
               help='Path to the Coast CA certificate.'),
    cfg.StrOpt('ca_private_key', default=None,
               help='Path to the Coast CA private key.'),
    cfg.StrOpt('ca_private_key_passphrase', default=None, secret=True,
               help='Passphrase for the Coast CA private key.'),
    cfg.StrOpt('signing_digest', default='sha256',
               choices=['sha256', 'sha384', 'sha512'],
               help='Digest used to sign Coast leaf certificates.'),
    cfg.IntOpt('cert_validity_time',
               default=730 * 24 * 60 * 60,
               min=1,
               help='Lifetime of issued leaf certificates, in seconds. '
                    'The default is 730 days.'),
]

QUOTAS_OPTS = [
    cfg.IntOpt('environments', default=1,
               help='Default per-project quota; -1 means unlimited.'),
    cfg.IntOpt('servers', default=2,
               help='Default per-project quota; -1 means unlimited.'),
    cfg.IntOpt('baremetal_servers', default=0,
               help='Default per-project quota; -1 means unlimited.'),
    cfg.IntOpt('build_servers', default=1,
               help='Default per-project quota; -1 means unlimited.'),
    cfg.IntOpt('apps', default=10,
               help='Default per-project quota; -1 means unlimited.'),
    cfg.IntOpt('domains', default=20,
               help='Default per-project quota; -1 means unlimited.'),
    cfg.IntOpt('volume_gb', default=200,
               help='Default per-project quota; -1 means unlimited.'),
    cfg.IntOpt('storage_gb', default=50,
               help='Default per-project quota for customer named storage '
                    'in GiB; -1 means unlimited.'),
]

TEMPLATES_OPTS = [
    cfg.IntOpt('cache_ttl', default=3600, min=0,
               help='Raw template catalog cache lifetime in seconds. Zero '
                    'disables caching. Each API worker keeps its own '
                    'thread-safe in-memory cache by driver and engine '
                    'version. Authorization applies on every '
                    'request.'),
]

PLANS_OPTS = [
    cfg.StrOpt('definitions', default='[]',
               help='JSON array of operator plans: name, kind (vm or '
                    'baremetal), roles (control, worker or build), nullable '
                    'description and internal Nova flavor name or ID. '
                    'Flavor is never returned by the API. Empty lists '
                    'advertise no plans. plans_file is not read by this API.'),
    cfg.StrOpt('plans_file', default='/etc/coast/plans.yaml',
               help='Path to the operator plan definitions.'),
]

COOLIFY_OPTS = [
    cfg.DictOpt('adopted_endpoints', default={},
                help='M2 development only: stack ID to HTTPS engine '
                     'URL mapping. Replaced by agent endpoints in M3.'),
    cfg.BoolOpt('insecure_direct_url', default=False,
                help='Permit direct engine access without agent mTLS for '
                     'isolated M2 development only. TLS verification remains '
                     'enabled; never enable in production.'),
    # A regex, not choices: the sample config must not freeze the release
    # list or default from engine_versions.py into a literal.
    cfg.StrOpt('pinned_version', default=engine_versions.DEFAULT,
               regex=r'\A(%s)\Z' % '|'.join(
                   re.escape(v)
                   for v in engine_versions.SUPPORTED_ENGINE_VERSIONS),
               sample_default='<newest release in engine_versions.py>',
               help='Supported Coolify release selected for new stacks only. '
                    'Must be one of SUPPORTED_ENGINE_VERSIONS in '
                    'coast/common/engine_versions.py; the default is the '
                    'newest one.'),
    cfg.IntOpt('api_timeout', default=30, min=1,
               help='Engine API timeout in seconds.'),
    cfg.IntOpt('retry_attempts', default=3, min=1,
               help='Maximum attempts for idempotent API requests only.'),
    cfg.FloatOpt('retry_wait', default=1.0, min=0.0,
                 help='Initial exponential retry delay in seconds.'),
    cfg.IntOpt('rate_limit_attempts', default=8, min=1,
               help='Maximum attempts after HTTP 429, including POSTs.'),
    cfg.FloatOpt('rate_limit_timeout', default=60.0, min=0.0,
                 help='Maximum elapsed seconds before starting another engine '
                      'retry. An in-flight request may add api_timeout.'),
    cfg.StrOpt('ssh_user', default='root',
               help='SSH user on stack workers and build servers.'),
    cfg.StrOpt('build_registry_image_template', default=None,
               help='Fallback registry image name template for dedicated '
                    'builds. Must contain {environment_id} and {app_id}. '
                    'A stack with a private registry uses '
                    '<environment-address>:<port>/{environment_id}/{app_id} '
                    'instead. This option applies only when that per-stack '
                    'template is empty.'),
]

DOMAINS_OPTS = [
    cfg.ListOpt('nameservers', default=[],
                help='DNS nameserver addresses for domain checks. Empty uses '
                     'the system resolver configuration.'),
    cfg.IntOpt('poll_interval', default=30, min=1,
               help='Domain verification poll interval in seconds.'),
    cfg.IntOpt('verification_timeout', default=86400, min=1,
               help='TXT ownership verification timeout in seconds.'),
    cfg.IntOpt('activation_timeout', default=3600, min=1,
               help='Routing and TLS activation timeout in seconds.'),
    cfg.StrOpt(
        'default_suffix', default='sslip.io',
        help='Suffix for the default service hostname. The form is '
             '<name>-<id4>.<dashed-ip>.<suffix> when the worker has a '
             'floating IP, otherwise <name>-<id4>.<suffix>. An empty '
             'value disables the default hostname.'),
    cfg.BoolOpt(
        'skip_verification', default=False,
        help='Development only: apply custom domains without DNS '
             'verification'),
]

WEBHOOKS_OPTS = [
    cfg.StrOpt(
        'secret_key', secret=True, default=None,
        help='Key that derives each app webhook token. Empty disables '
             'webhooks. Changing it invalidates every webhook URL.'),
    cfg.StrOpt(
        'public_url', default=None,
        help='Public base URL of the Coast API used in webhook URLs, for '
             'example https://coast.example.com. Defaults to the request '
             'host.'),
    cfg.IntOpt('max_body_size', default=1048576, min=1024,
               help='Largest accepted webhook body in bytes.'),
]

DNS_OPTS = [
    cfg.StrOpt('credential_provider', default='service_account',
               choices=['service_account', 'trust'],
               help='Customer DNS credentials: service-account sudo or '
                    'a Keystone trust supplied on each zone link.'),
    cfg.IntOpt('record_ttl', default=300, min=1,
               help='TTL in seconds for managed default-service DNS records.'),
    cfg.BoolOpt('use_designate', default=False,
                help='Manage default service DNS records with Designate.'),
    cfg.StrOpt('designate_zone_id', default=None,
               help='Designate zone ID for default service URLs.'),
]

EXPORT_OPTS = [
    cfg.IntOpt('archive_timeout', default=3600, min=1,
               help='Archive creation and upload time limit in seconds.'),
    cfg.IntOpt('max_object_size_gb', default=5, min=1,
               help='Maximum compressed single-object upload size in GiB. '
                    'Set no higher than the storage backend limit.'),
    cfg.StrOpt('client_certificate',
               help='Coast client CA-issued certificate PEM file for export '
                    'agent requests.'),
    cfg.StrOpt('client_key', secret=True,
               help='Private key PEM file for export agent requests.'),
    cfg.StrOpt('server_ca',
               help='Coast server CA PEM file for export agent verification.'),
    cfg.StrOpt('backend', default='swift', choices=['swift', 's3'],
               help='Object storage backend.'),
    cfg.StrOpt('container', default='coast-exports',
               help='Swift container or S3 bucket for export objects.'),
    cfg.IntOpt('url_ttl', default=86400,
               help='Download URL lifetime in seconds.'),
    cfg.IntOpt('retention_hours', default=72,
               help='Export object retention in hours.'),
    cfg.StrOpt('upload_via', default='guest', choices=['guest'],
               help='Component that uploads export data. Only guest is '
                    'supported: the guest agent uploads each volume image '
                    'to a pre-signed object-store URL, so guests must reach '
                    'the Swift or S3 endpoint. Conductor relay upload is '
                    'not supported yet (gozunga/coast#87).'),
    cfg.IntOpt('max_size_gb', default=500,
               help='Maximum export size in GiB.'),
    cfg.IntOpt('max_concurrent_per_project', default=1,
               help='Maximum concurrent exports per project.'),
    cfg.IntOpt('snapshot_retention_days', default=14,
               help='Default snapshot retention in days.'),
    cfg.IntOpt('max_snapshot_retention_days', default=90,
               help='Maximum snapshot retention in days.'),
    cfg.StrOpt('s3_endpoint', default=None,
               help='HTTPS S3 API endpoint for the S3 backend. Do not put '
                    'credentials in this URL. Coast uses path-style '
                    'addressing.'),
    cfg.StrOpt('s3_region', default=None,
               help='SigV4 signing region for the S3 endpoint.'),
    cfg.StrOpt('s3_credentials_ref', default=None,
               help='Barbican text secret reference for S3 credentials. '
                    'The payload is JSON with access_key_id and '
                    'secret_access_key only. Coast does not read '
                    'environment or instance credentials.'),
]

NOTIFICATIONS_OPTS = [
    cfg.IntOpt('exists_notification_interval', default=3600, min=1,
               help='Usage audit sweep interval in seconds. Each process '
                    'emits once per completed audit period.'),
    cfg.StrOpt('usage_audit_period', default='hour',
               choices=['hour', 'day', 'month'],
               help='Previous complete UTC calendar audit period, as in '
                    'Nova instance_usage_audit_period. Snapshots contain '
                    'current resources, not deleted-resource history.'),
    cfg.StrOpt('service_id', default=None,
               help='Service identifier for usage notifications.'),
]


# Official service types. Group names are the service project names.
SERVICE_TYPE_DEFAULTS = {
    'nova': 'compute',
    'neutron': 'network',
    'cinder': 'block-storage',
    'glance': 'image',
    'barbican': 'key-manager',
    'designate': 'dns',
}


def _service_extra_opts():
    """Per-service options that keystoneauth adapter options do not cover.

    endpoint_type is the Octavia name. valid_interfaces is the keystoneauth
    name. The client factory accepts one of them.
    """
    return [
        cfg.StrOpt(
            'endpoint_type',
            help='Catalog interface for this service. Use publicURL, '
                 'internalURL or adminURL. Leave this empty when '
                 'valid_interfaces is set.'),
        cfg.StrOpt(
            'ca_certificates_file',
            help='CA certificate file for this service. When unset, the '
                 '[service_auth] cafile value is used.'),
        cfg.BoolOpt(
            'insecure', default=False,
            help='Disable TLS certificate verification for this service. '
                 'Do not enable this option in production.'),
    ]


def service_client_opts(service_type):
    """Return fresh adapter and Coast options for one service group."""
    options = loading.get_adapter_conf_options(include_deprecated=False)
    for option in options:
        if option.dest == 'service_type':
            option.default = service_type
    return options + _service_extra_opts()


SECRETS_OPTS = [
    cfg.IntOpt('max_secret_bytes', default=20000, min=1,
               help='Maximum UTF-8 bytes per secret payload. Match the '
                    'Barbican max_allowed_secret_in_bytes setting.'),
    cfg.IntOpt('engine_token_rotation_days', default=30, min=0,
               help='Rotate engine API tokens after this many days. '
                    'Zero disables automatic rotation.'),
    cfg.IntOpt('rotation_max_concurrent', default=2, min=1,
               help='Maximum concurrent engine API token rotations across '
                    'all conductors, including operator requests.'),
    cfg.StrOpt('backend', default='disabled',
               choices=['disabled', 'barbican'],
               help='Secret storage backend. Disabled fails closed. Barbican '
                    'uses the service_auth credentials and barbican options.'),
]


CONSOLE_OPTS = [
    # The session listener is plain HTTP. A front proxy terminates TLS.
    cfg.StrOpt('bind_host', default='0.0.0.0',  # nosec B104
               help='Address on which the console session service listens.'),
    cfg.PortOpt('bind_port', default=9610,
                help='Console session listen port.'),
    cfg.StrOpt('public_endpoint', default='',
               help='Public host and optional port for console session '
                    'URLs, without a scheme. Example: '
                    'console.example.com:9610.'),
    cfg.BoolOpt('tls', default=True,
                help='Use wss in session URLs. Set false only when the '
                     'public listener is not TLS.'),
    cfg.IntOpt('token_ttl', default=60, min=1,
               help='Seconds a console session token stays valid.'),
    cfg.IntOpt('idle_timeout', default=900, min=1,
               help='Seconds of no session data before the session closes. '
                    'This is console_idle_timeout.'),
    cfg.IntOpt('max_duration', default=14400, min=1,
               help='Maximum session length in seconds. '
                    'This is console_max_duration.'),
]


BACKUP_OPTS = [
    cfg.StrOpt('region_name', help='Object storage catalog region; defaults '
               'to the configured Nova region.'),
    cfg.StrOpt('interface', default='public',
               choices=['public', 'internal', 'admin'],
               help='Swift catalog interface reachable by workers.'),
    cfg.IntOpt('poll_interval', default=60, min=1,
               help='Seconds between durable backup job and schedule scans.'),
    cfg.IntOpt('segment_bytes', default=67108864, min=1048576,
               max=4294967296, help='Encrypted bytes per Swift segment.'),
    cfg.IntOpt('max_segments', default=1024, min=1, max=4096,
               help='Maximum segments per backup; bounds dump size and URLs.'),
    cfg.IntOpt('url_ttl', default=7200, min=60,
               help='Temporary link lifetime; must exceed command timeout.'),
]


METRICS_OPTS = [
    cfg.IntOpt('detail_retention', default=86400, min=86400,
               help='Seconds to keep 30-second samples; at least 24 hours.'),
    cfg.IntOpt('five_minute_retention', default=604800, min=604800,
               help='Seconds to keep five-minute averages; at least 7 days.'),
    cfg.IntOpt('hourly_retention', default=2592000, min=2592000,
               help='Seconds to keep hourly averages; at least 30 days.'),
    cfg.IntOpt('cleanup_interval', default=300, min=1,
               help='Seconds between metrics rollup and cleanup passes.'),
    cfg.IntOpt('batch_size', default=100, min=1, max=1000,
               help='Maximum buckets rolled up or rows deleted per pass.'),
]


DATABASE_OPTS = [
    cfg.IntOpt('public_port_min', default=15432, min=1024, max=65535,
               help='First port for automatic database public allocation.'),
    cfg.IntOpt('public_port_max', default=15999, min=1024, max=65535,
               help='Last port for automatic database public allocation.'),
]


COAST_OPTS = {
    'databases': DATABASE_OPTS,
    'backup': BACKUP_OPTS,
    'DEFAULT': DEFAULT_OPTS,
    'agent': AGENT_OPTS,
    'metrics': METRICS_OPTS,
    'secrets': SECRETS_OPTS,
    'api': API_OPTS,
    'console': CONSOLE_OPTS,
    'conductor': CONDUCTOR_OPTS,
    'health_manager': HEALTH_MANAGER_OPTS,
    'controller_worker': CONTROLLER_WORKER_OPTS,
    'certificates': CERTIFICATES_OPTS,
    'quotas': QUOTAS_OPTS,
    'plans': PLANS_OPTS,
    'templates': TEMPLATES_OPTS,
    'coolify': COOLIFY_OPTS,
    'dns': DNS_OPTS,
    'domains': DOMAINS_OPTS,
    'webhooks': WEBHOOKS_OPTS,
    'export': EXPORT_OPTS,
    'notifications': NOTIFICATIONS_OPTS,
}


def list_opts():
    """Return all Coast options, including service authentication options."""
    service_groups = [
        (group, service_client_opts(service_type))
        for group, service_type in SERVICE_TYPE_DEFAULTS.items()]

    def auth_options():
        return (loading.get_auth_common_conf_options() +
                loading.get_auth_plugin_conf_options('password') +
                loading.get_session_conf_options())

    return list(COAST_OPTS.items()) + service_groups + [
        ('service_auth', auth_options()),
        ('agent_auth', auth_options())]


def list_messaging_opts():
    """Return modified public option copies for sample generation only.

    The generator uses this entry point because messaging list_opts returns
    copies; ConfigOpts.set_default cannot change those sample definitions.
    Do not register these modified copies at runtime: driver registration
    requires unchanged option definitions.
    """
    options = messaging_opts.list_opts()
    for group, group_opts in options:
        if group == 'oslo_messaging_rabbit':
            cfg.set_defaults(group_opts, ssl=True, rabbit_quorum_queue=True)
        elif group == 'oslo_messaging_notifications':
            # Sample only. Runtime defaults are set on the live ConfigOpts.
            cfg.set_defaults(group_opts, driver=['messagingv2'], retry=0)
    return options


def register_opts(conf):
    """Register unchanged library options and conf-local runtime defaults."""
    for group, options in COAST_OPTS.items():
        conf.register_opts(
            options, group=None if group == 'DEFAULT' else group)
    for group, service_type in SERVICE_TYPE_DEFAULTS.items():
        # Adapter options let openstacksdk read region and interface.
        loading.register_adapter_conf_options(
            conf, group, include_deprecated=False)
        conf.register_opts(_service_extra_opts(), group=group)
        conf.set_default('service_type', service_type, group=group)
    for group, options in auth_token.list_opts():
        conf.register_opts(options, group=group)
    # Keystoneauth loads plugin options only when authentication is used.
    for group in ('service_auth', 'agent_auth'):
        loading.register_auth_conf_options(conf, group)
        loading.register_session_conf_options(conf, group)
    for group, options in messaging_opts.list_opts():
        if group == 'oslo_messaging_rabbit':
            # Sample copies diverge when a deprecated option is read. Use
            # the driver's definitions so later transports can register them.
            options = amqp.amqp_opts + impl_rabbit.rabbit_opts
        conf.register_opts(options, group=group)
    # Keep option definitions equal to those registered later by the driver.
    conf.set_default('ssl', True, group='oslo_messaging_rabbit')
    conf.set_default('rabbit_quorum_queue', True,
                     group='oslo_messaging_rabbit')
    # api.md: messagingv2 when the operator does not choose a driver.
    # retry 0: a broker outage must not block an API request.
    conf.set_default('driver', ['messagingv2'],
                     group='oslo_messaging_notifications')
    conf.set_default('retry', 0, group='oslo_messaging_notifications')


def set_defaults(conf=cfg.CONF):
    """Apply Coast defaults at startup and during sample generation."""
    policy_opts.set_defaults(conf, enforce_scope=True,
                             enforce_new_defaults=True)
