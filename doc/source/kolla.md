# Kolla Ansible integration

`tools/kolla/` holds an overlay role and a kolla-build image overlay that
deploy Coast with Kolla-Ansible 2026.1. They are not part of upstream
kolla-ansible. A single-controller deployment on the DevStack AIO has
passed (roadmap M6). Multi-controller HA acceptance is deferred until a
staging cloud exists (Q141). This page describes deployment only. The shell
session API is documented in api.md.

## Mapping to Octavia

The role follows the
[Octavia 2026.1 role](https://github.com/openstack/kolla-ansible/tree/stable/2026.1/ansible/roles/octavia).
See the [Kolla Octavia guide](https://docs.openstack.org/kolla-ansible/latest/reference/networking/octavia.html)
for its certificate workflow.

| Octavia pattern | Coast role |
| --- | --- |
| Service map and images | API, conductor, shell listener, optional health-manager |
| Action dispatch | tasks/main.yml, limited to the supported actions |
| Config rendering | coast.conf and per-service config.json templates |
| Keystone registration | service-ks-register; name coast, type app-hosting |
| Bootstrap container | KOLLA_BOOTSTRAP through the default kolla_start |
| Comparison and restart | service-check-containers and service handlers |
| Certificate preparation | Control-host play that calls gen-ca.sh |

API port **9600** matches operations.md. The shell listener uses port
**9610**. No separate coast-worker service is defined. Guest workers are
not control-plane containers.

## Certificate flow

Run `tools/kolla/ansible/certificates.yml` on the Ansible control host.
Set `coast_certificates_dir`, `coast_certificate_validity_days` and
`coast_certificate_subject_prefix`. The play calls the unchanged script.
The optional `coast_certificate_cn` maps to `--registry-host` for a registry
certificate only. The script fixes the CA and controller client CNs.

Set `coast_check_expiry_days=30` for the read-only expiry check, or run
`kolla-ansible coast-certificates --check-expiry 30` after installing the
`tools/kolla/cli` plugin into the kolla-ansible virtualenv. This mode is read-only and
fails on expired, expiring or missing material. It does not rotate files.
`kolla-ansible coast-certificates` (plugin in `tools/kolla/cli`, entry
point `kolla_ansible.cli`) runs the same play with `--coast-tools-dir`.

Only the conductor receives the server CA key. The conductor also
receives the controller client bundle and the CA certificates. The shell
listener receives the server CA certificate and the controller client
bundle, never a CA key. The health-manager receives that same client
material and never a CA key. The client CA signing key stays on the
control host. Keys and config use private file modes. The API receives
no CA keys. Registry distribution and Barbican import are not added.

## Configuration overrides, logs and health checks

The role uses Kolla's `merge_configs`. Sources are read in this order;
later values win and missing override files are skipped:

1. The role template `coast.conf.j2`.
2. `{{ node_custom_config }}/global.conf`.
3. `{{ node_custom_config }}/coast.conf`.
4. `{{ node_custom_config }}/coast/{{ item.key }}.conf`.
5. `{{ node_custom_config }}/coast/{{ inventory_hostname }}/coast.conf`.

Service keys are `coast-api`, `coast-conductor`,
`coast-health-manager` and `coast-console-proxy`. Use these files to set
existing `[plans]`
definitions, `[dns]`, `[secrets]`, `[export]` and `[DEFAULT] debug`.
Use option names from the generated Coast sample config. The conductor
renders export client certificate/key and server CA paths for its copied
files. Rendering keeps `no_log` and mode 0600. Protect source files and
backups; never use diff mode.

All services mount `kolla_logs:/var/log/kolla/` and use
`log_dir = /var/log/kolla/coast`. Kolla config permissions set that
directory's owner to `coast:coast`, recursively.

The bootstrap container uses the conductor volumes, so it mounts
`kolla_logs` too. The task keeps `no_log` because the migration
command can contain secrets. It keeps the image's default `kolla_start`
command and passes `KOLLA_BOOTSTRAP` and `KOLLA_CONFIG_STRATEGY`. Kolla
copies `coast.conf` before the image startup hook runs
`coast-db-manage --config-file /etc/coast/coast.conf upgrade`.
An empty `coast_bootstrap_command` selects this default. An existing
non-empty override runs after config copying, with shell tracing disabled.
The hook exits with the migration status and never starts the conductor.
The role checks the returned exit code; a failed migration stops the play.
The container is removed after it exits.
The log volume remains. `coast-db-manage upgrade` logs the database
revision before and after the upgrade at INFO. The same lines go to
standard output and to
`/var/log/kolla/coast/coast-db-manage.log` on the conductor host.
Run this command in the `coast_conductor` container to show the
stamped revision:

```bash
coast-db-manage --config-file /etc/coast/coast.conf current
```

Each service prefix (`coast_api`, `coast_conductor`,
`coast_health_manager`, `coast_console_proxy`) has
`<prefix>_enable_healthchecks` and
`<prefix>_healthcheck_interval`, `retries`, `start_period`, `test`
and `timeout` variables. Enablement and timings follow Kolla's
`enable_container_healthchecks` and `default_container_healthcheck_*`.
The API uses `healthcheck_curl` on its listen address and port with the
backend HTTP/TLS protocol. The conductor uses `healthcheck_port` with
its service name and `om_rpc_port` (the 2026.1 RabbitMQ RPC port
variable). The health-manager does not use RPC. Its check reads
`/proc/net/udp` for `coast_health_manager_port` (default 5565). The
Coast image does not install `iproute2`, so the check uses `python3`.
The shell listener uses `healthcheck_curl` with HTTP `GET /health`.
Its backend TLS stays off. Disable a service check with its enablement
variable.
These checks do not prove guest mTLS, heartbeat delivery or task completion.
Keystone token caching uses the Kolla memcached hosts/port and the Octavia
2026.1 memcache security settings, region and internal interface.

The production API command is `uwsgi` with the `coast-wsgi-api` entry
point. It listens only on `api_interface_address`. Copy
`tools/kolla/ansible/coast.yml` next to Kolla `site.yml` and run it with
the Kolla CLI. That play writes the standard HAProxy and ProxySQL config
before Coast bootstrap. Frontend TLS follows
`kolla_enable_tls_internal` and `kolla_enable_tls_external`. Backend TLS
follows `kolla_enable_tls_backend`. The external VIP listener is off
unless `coast_enable_external_api` is true. With that flag and
`haproxy_single_external_frontend`, the API public port follows Kolla's
shared frontend port. The Keystone public URL then uses the external
name. While the flag is false, the public URL matches the internal URL.
The shell listener stays on port 9610. HAProxy
sets a four-hour client, server and tunnel timeout on that listener.
The backend is plain HTTP and checks `GET /health`. When
`enable_haproxy` is no, the container listens on
`api_interface_address` port 9610. Host networking exposes that port.
There is no extra published container port. Put the
`coast-console-proxy` inventory group on the same hosts as
`coast-conductor` so the management address is present.

Add `coast_database_password`, `coast_keystone_password` and
`coast_agent_keystone_password` to `passwords.yml`. The role has no password
defaults. Messaging uses Kolla's `rpc_transport_url` and `notify_transport_url`,
including all broker hosts and the shared `om_rpc_user` / `om_rpc_password`
(`rabbitmq_password`). Coast does not register a broker user or vhost.
The queue settings follow the pinned Octavia 2026.1 template: queue manager,
quorum queues, transient quorum queues, and optional stream fanout with
Kolla's prefetch count. Each service has its own process name; only the API
uses `heartbeat_in_pthread`. TLS follows `om_enable_rabbitmq_tls` and
`om_rabbitmq_cacert`; it is explicitly disabled when the flag is false.
Notifications use `messagingv2` when `enable_ceilometer` is true, otherwise
`noop`, following Octavia's enabled-topic pattern. Operators can set
`coast_notification_topics` to select other enabled topics.
These settings stay on the Kolla controllers. Guest config never receives
RabbitMQ addresses or credentials. The full globals list, certificate paths
and DevStack upgrade steps are in `tools/kolla/README.rst`.

For an isolated AIO without HAProxy or TLS, set `enable_haproxy=false`
and use the API-network host address for the VIP and catalog endpoints.
Disable frontend and backend TLS. On ML2/OVN, leave
`coast_hm0_manage=false` and supply an existing routed management
interface/address. The optional hm0 attachment supports ML2/OVS only;
this role does not add an OVN host-port helper.

See the [Kolla config override guide](https://docs.openstack.org/kolla-ansible/2026.1/admin/advanced-configuration.html).

## Play prerequisites and config-only operation

Keep the Coast play in the Kolla checkout's `ansible/` directory, next to
`site.yml`, so `group_vars/all` loads. Set `gather_facts: true` or import
Kolla's `gather-facts.yml` first. Interface and peer facts are required by
`kolla_address`. See the exact play examples in `tools/kolla/README.rst`.

Images must contain the `coast` user in group `kolla` with the standard
Kolla sudoers entry for `kolla_set_configs`. Image names default to
`{{ docker_image_url }}coast-<service>`. Each service has `*_image`, `*_tag`
and `*_image_full` settings; `coast_tag` follows `openstack_tag`.
Tagged and digest `*_image` overrides remain intact. Untagged names receive
the service tag. Operators must supply built images; these defaults do not
publish images. A disabled health-manager needs no image override.

The input `coast_database_connection` must have no query string. The role
adds the TLS CA parameter when required. Config-only operation
(`kolla_action=config`) writes files and skips all container restart
handlers. Before first deployment, stop a non-container Coast API with its
service manager and confirm its listen address/port is free. The occupied
API port precheck remains in force; the role does not stop that old API.

## Trial procedure and open items

See `tools/kolla/README.rst` for the trial procedure and assumptions.
Use an isolated cloud and a separate Kolla checkout. Supply images,
inventory groups, endpoints, encrypted credentials, cloud resource options
and any required migration command override. Render config before deployment.
Do not print credentials or use Ansible diff mode.

| Item | Required work before a supported deployment |
| --- | --- |
| Images and CLI | Reviewed image definitions and action wiring |
| Database and RabbitMQ | Live grant, TLS and shard acceptance. The role bootstraps the DB user and uses Kolla's shared messaging account |
| API | Live VIP failover and container health. The role renders uWSGI, backend TLS and standard HAProxy listeners |
| Management network | Namespace/interface access and heartbeat paths |
| Health-manager | Production key lookup and durable state setup |
| Certificates | Rotation, Barbican import and registry distribution |
| Lifecycle | Prechecks, upgrade, reconfigure, stop and recovery actions |
| Verification | Ansible syntax checks and a real Kolla deployment |

YAML parsing and docs checks do not prove that containers start or a Kolla
deployment works. The single-controller trial does not complete the
roadmap item; the table above lists the remaining work.

## Service-user roles

`coast_keystone_user_roles` is the list of roles registered for the Coast
service user. It defaults to `[admin]` for 0.1.0; see
[Why the Coast service user has admin](service-permissions.md#why-the-coast-service-user-has-admin).
See the [call and policy inventory](service-permissions.md)
for minimum roles, conditional dependencies, Swift reseller requirements
and the read-only checker. Registration adds the selected grants; removing
a role from the list does not revoke an existing assignment. The operator
must validate and revoke it explicitly. Agent roles remain separate.

## Secret storage

The role selects `coast_secrets_backend: barbican` when `enable_barbican`
is true, and `disabled` otherwise. Override it explicitly when Barbican is
deployed separately; disabled storage makes secret operations fail closed.
`coast_max_secret_bytes` defaults to 20000. These variables render
`[secrets] backend` and `max_secret_bytes` in each Coast configuration.
See [payload limits](secrets.md#kolla-configuration) before raising the cap.
For token rotation options, migration and guest image requirements, see
[operations](operations.md#engine-api-token-rotation).

Use the [service permission checker](service-permissions.md#read-only-permission-checker)
with the service configuration to check representative reads. Its PASS/FAIL
labels do not prove write authorization. `coast_keystone_user_roles`
defaults to `[admin]` because the default Designate provider uses sudo.
See the [permission inventory](service-permissions.md) before reducing grants.
