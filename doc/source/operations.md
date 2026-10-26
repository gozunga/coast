# Operations

## Deployment target
Any OpenStack cloud running **2026.1 (Gazpacho) or later** with Keystone,
Nova, Neutron, Cinder, Glance and Barbican (Ironic and Designate optional).
The reference deployment tool is **kolla-ansible**; the DevStack plugin is
for development. Use the operator's configured region and endpoint URLs.

For development, run Coast services from a venv or a container on a
controller node. See the Kolla section for the operator deployment path.

## Keystone registration (manual without the Kolla role)

```bash
openstack user create --domain Default --password-prompt coast
openstack role add --project service --user coast admin   # decision Q7, see below
openstack service create --name coast --description "App Hosting" app-hosting
openstack endpoint create --region REGION_NAME app-hosting public https://cloud.example.com:9600
openstack endpoint create --region REGION_NAME app-hosting internal https://internal.example.com:9600
openstack endpoint create --region REGION_NAME app-hosting admin https://internal.example.com:9600
```

Use your catalog region in place of `REGION_NAME`. `--domain` selects the
Keystone user domain, `--password-prompt` reads a password privately,
`--project` and `--user` scope the role grant, `--name` names the catalog
service, `--description` supplies its label, and `--region` selects the
endpoint region. The endpoint interface is the positional public, internal
or admin argument.

**Service user role (decision Q7):** the `coast` service user gets
`admin` in the `service` project, matching what Octavia, Trove and Magnum
document for their service users. Documented goal (post-1.0): define and
ship a least-privilege role (`member` in `service` plus only the specific
admin-scoped operations Coast needs: Ironic node selection, port security on
the mgmt net, FIPs on the external net, Swift temp-URL keys) and document it
here. Operators must not reuse this user for anything else.

## Agent credential

The guest agent does not use Keystone. Conductor, health-manager and
console-proxy can load a second user for agent-facing control-plane
checks. That user is separate from the `coast` service user.

Create the user in the `service` project. Give it only the custom role
`coast_agent`. Do not grant `admin`, `member`, `reader`, `manager` or
`service`. Coast policy does not name `role:coast_agent` on any API
rule. `coast:plan:get_all` allows every project token because its check
is `@`. Other Coast API rules deny this role.

Cloud calls stay on the `coast` user in `[service_auth]`. v0.1.0 stores
the agent credential and rejects a shared user name at process start.
It does not call Keystone for guest operations. Do not put this
password on a guest config drive.

```bash
openstack role create coast_agent
openstack user create --domain Default --password-prompt coast-agent
openstack role add --project service --user coast-agent coast_agent
```

Put the password in `[agent_auth]` on conductor, health-manager and
console-proxy. Do not put it in `[keystone_authtoken]`. Leave
`auth_type` unset to keep an existing deployment on the `coast` user
only. Kolla always sets `[agent_auth]`.

## Rotate the agent credential

Change the Keystone password for `coast-agent`. Then update the
`[agent_auth]` password. For Kolla, set `coast_agent_keystone_password`
in `passwords.yml` and set
`update_keystone_service_user_passwords=true`, then run the Keystone
register task again. Restart conductor, health-manager and
console-proxy. Do not rebuild guests. There is no second password
window. Guest paths do not use the loaded password. A restart reads
the new value. Do not log the password.

## Prerequisite resources (one-time, service project)

- `coast-mgmt-net` + subnet (no router), reachable from controller hosts
  via a kolla-managed bridge/VLAN like Octavia's `lb-mgmt-net`.
- SG `coast-ctl-mgmt` (rules in networking.md).
- Flavors: `coast.ctrl.small` etc., baremetal flavors for Ironic.
- Glance images `coast-coolify-controller-<ver>` and
  `coast-worker-<ver>` (private, service project, tagged).
  Build them with `tools/build-images/coast-image-create.sh`, or take
  the qcow2 artifacts from the GitLab job `build-images`. CI does not
  upload images to Glance.
  Upload commands and role tags are in `tools/build-images/README.rst`.
  Set `image_tag_controller` to `coast-coolify-controller` and
  `image_tag_worker` to `coast-worker`.
  Images must have `coast_role=controller` or `coast_role=worker`
  respectively. Build servers use the worker image. Shared tags are
  permitted; multiple active images for one role fail closed unless
  `controller_image_id` or `worker_image_id` selects exactly one match.
- Coast CA (`tools/gen-ca.sh`) -> `/etc/coast/certs/` via kolla config.
- Optional: a Designate zone for default app URLs
  (`[domains] default_suffix`, default `sslip.io`) with
  `[dns] use_designate = true`.

## Golden images and offline first boot (Q78)

Controller builds use the reviewed default lock at
`tools/build-images/elements/coast-controller/files/coolify-images.lock`.
Set build-host `COAST_COOLIFY_IMAGES_LOCK` to override it with another
reviewed file of the same six image tags and immutable SHA256 digests.
The sixth tag is `registry:3`. Its digest is the manifest-list digest.
The required tags and lock format are in `tools/build-images/README.rst`.
The build host needs Docker with permission to pull and save images.
These are build inputs, not new Coast service config options.

The DIB element checks support-file SHA256 checksums for the selected
supported release.
It pulls container images by the reviewed digest and saves them in a
checksummed archive on the image root disk. For an offline image build,
set `COAST_COOLIFY_CACHE` to the support-file cache and preload the same
digest references into build-host Docker. Keep the existing installer
checksum input. The stock installer is stored but never executed.

At boot, cloud-init consumes the config drive and mounts `/data`,
`/var/lib/docker` and, on the controller, `/var/lib/registry`.
Each mount uses `/dev/disk/by-id/coast-<cinder id>`. Cloud-init links
that path to the virtio-blk disk or the virtio-scsi disk, so
`hw_disk_bus` may be `virtio` or `scsi`. Management setup matches
`[agent] bind_ip` to the Nova network-data link MAC and
keeps both guest NICs in the main namespace. It uses the supplied static
management prefix without a default route, adds configured heartbeat host
routes, and uses environment DHCP for the stack-router default route. Controllers then run
`coast-coolify-firstboot.service`. This service requires Docker,
management setup and those mount points. It loads the archive onto the
Docker Cinder volume, generates local private secrets, starts stock
Compose definitions with loopback-only ports, then starts the pinned
registry on the environment address. It writes supported
`AUTOUPDATE=false` and root-user variables into the private `.env`. Stock
Coolify initialization disables updates and, when root creation succeeds,
registration. The API allowlist stays unset; first boot performs no engine
database access. The agent requires successful completion. No CDN
artifact download or Compose image pull occurs at boot. The version-gated
bootstrap repairs missing root-user/team membership and disables
registration before creating the token; see lifecycle.md.

Preserve the data and Docker Cinder volumes, plus the controller registry
volume, during guest replacement. Existing secrets,
SSH keys and token-bootstrap markers must not be removed. Do not print
`.env`, resolved Compose configuration, or container environment values.
Check service results with `systemctl is-active coast-guest-routes` and
`systemctl is-active coast-coolify-firstboot`. A failed setup blocks the
agent. See lifecycle.md for the full first-boot and failure contract.

## Guest egress check

Set `[controller_worker] egress_check_url` to an operator-selected HTTPS
registry URL, for example `https://registry.example.com/v2/`. Use no
credentials, query parameters or fragment. The conductor renders it into
every guest agent config. Empty (the default) checks environment routing
and DNS only, using Docker Hub for DNS, without an HTTPS request.
A 401, 500 or redirect is an acceptable probe response.

Rebuild and replace guests for the internal health egress result.
`/health` remains mTLS-only and reports pending/ok/error plus fixed failure
reasons. A fault `no internet egress via stack router` requires checking
the environment DHCP lease, router external gateway, DNS and outbound TLS.
Router/subnet pre-boot faults name the missing gateway requirement.
The check runs once per agent start, not as periodic monitoring.

## Diagnosing failures

A failed conductor operation stores a generic fault on the resource.
The tenant activity log uses that same generic text. The conductor
log and the API log add an ERROR line for the operator.

The line gives the exception class. For an OpenStack SDK
``HttpException`` or a keystoneauth HTTP error, it also gives the
status code, the HTTP method, the URL without its query string, and
the service request id when one is present. It gives the innermost
Coast file, line number, and function. It does not give the exception
text, a response body, traceback locals, or query parameters.

Search the ``coast-conductor`` and ``coast-api`` logs with the
resource id and the ``X-OpenStack-Request-Id`` value. ``debug = true``
does not add SDK request or response bodies. Do not copy these log
lines into a tenant response. The tenant fault text stays generic.

## Configuration options (summary)

Full list generated by `tox -e genconfig` -> `etc/coast/coast.conf.sample`.

| Section | Option | Default | Notes |
|---|---|---|---|
| `[DEFAULT]` | `resource_name_prefix` | `coast` | Prefix for generated cloud resource names; see below |
| `[api]` | `bind_host` / `bind_port` | `0.0.0.0` / `9600` | |
| `[api]` | `auth_strategy` | `keystone` | `noauth` only for tests and isolated development |
| `[api]` | `api_paste_config` | `api-paste.ini` | relative to config directories or an absolute path |
| `[api]` | `public_endpoint` | none | used in links |
| `[console]` | `bind_host` / `bind_port` | `0.0.0.0` / `9610` | shell listener; Kolla binds the API address |
| `[console]` | `public_endpoint` | empty | host and port only, no scheme or path |
| `[console]` | `tls` | true | `wss` when true, `ws` when false |
| `[console]` | `token_ttl` | 60 | seconds; single-use shell token |
| `[console]` | `idle_timeout` | 900 | seconds without session data; `console_idle_timeout` |
| `[console]` | `max_duration` | 14400 | seconds; `console_max_duration` |
| `[api]` | `max_limit` | 1000 | pagination cap |
| `[database]` | `connection` | | oslo.db |
| `[keystone_authtoken]` | ... | | keystonemiddleware |
| `[service_auth]` | ... | | creds for cloud calls (service project). Do not log these values |
| `[agent_auth]` | ... | unset `auth_type` | password for `coast-agent`. Control plane only. Not used for cloud calls |
| `[nova]` `[neutron]` `[cinder]` `[glance]` `[barbican]` `[designate]` | `region_name` | none | Required. Catalog region for that service. Coast sets no region |
| same groups | `valid_interfaces` or `endpoint_type` | none | Required. Set one. `valid_interfaces` is `public`, `internal` or `admin`. `endpoint_type` is `publicURL`, `internalURL` or `adminURL` |
| same groups | `ca_certificates_file` / `insecure` | none / false | Optional TLS override for that service. Do not set both. Do not enable `insecure` in production |
| `[oslo_messaging_rabbit]` | `ssl`, `rabbit_quorum_queue` | true | control plane only |
| `[conductor]` | `workers` | 2 | |
| `[conductor]` | `deploy_timeout` | 600 | seconds; enqueue recovery grace and deployment deadline |
| `[conductor]` | `stable_seconds` | 20 | continuous running seconds required before deployment or start becomes ACTIVE |
| `[conductor]` | `deploy_unreachable_timeout` | 150 | seconds of runtime crash loops, or HTTP no answer/502/503 for apps with HTTP checks, before deployment failure; 0 disables this deadline |
| `[conductor]` | `deploy_reconcile_interval` | 60 | seconds; deployment recovery and housekeeping, also at startup |
| `[conductor]` | `settle_poll_interval` | 15 | seconds; how often DEPLOYING and STARTING services are checked for ACTIVE (bounds the status lag) |
| `[conductor]` | `build_timeout` | 900 | env/server boot |
| `[health_manager]` | `bind_ip` / `bind_port` | none / 5565 | Explicit coast-hm0 IPv4 address; never wildcard or loopback |
| `[health_manager]` | `key_cache_max_stale` | 3600 | Seconds after the 30-second key cache expiry to reuse a last good key on secret-store errors; zero disables reuse |
| `[health_manager]` | `key_lookup` | `coast.health_manager.keys.StackKeyLookup` | Overridable callable class import path; never a key value |
| `[health_manager]` | `state_path` | `/var/lib/coast/health-manager` | Private durable sequence checkpoint directory |
| `[health_manager]` | `max_packet_size` / `max_packets_per_second` | 4096 / 1000 | Datagram bytes / global processing rate |
| `[health_manager]` | `batch_size` / `batch_interval` | 100 / 1.0 | Pending server limit / flush interval in seconds |
| `[health_manager]` | `heartbeat_interval` / `heartbeat_timeout` / `failover_grace` | 10 / 60 / 120 | seconds |
| `[health_manager]` | `failover_max_concurrent` | 2 | Global claimed worker, build and controller VM failovers; minimum 1 |
| `[health_manager]` | `reconcile_interval` | 600 | |
| `[health_manager]` | `enable_failover` | true | kill switch |
| `[controller_worker]` | `driver` | `coolify_v4` | default for new stacks |
| `[controller_worker]` | `mgmt_net_id`, `mgmt_sg_id` | | |
| `[controller_worker]` | `external_network_id` | | Routers and FIPs |
| `[controller_worker]` | `env_cidr_pool` / `env_subnet_prefix` | none / 24 | Reuse the first IPv4 subnet per isolated stack router |
| `[controller_worker]` | `network_delete_attempts` / `network_delete_retry_interval` | 3 / 1.0 | Neutron cleanup attempts and delay in seconds |
| `[controller_worker]` | `image_tag_controller` / `image_tag_worker` / `image_owner_id` | | image selection by tag, owner and `coast_role` |
| `[controller_worker]` | `controller_image_id` / `worker_image_id` | none | Optional exact IDs; owner, tag, active status and role checks still apply |
| `[controller_worker]` | `controller_flavor_id` | | |
| `[controller_worker]` | `data_volume_size_gb` / `data_volume_type` | 100 / | |
| `[controller_worker]` | `egress_blocked_ports` | `25,465,587` | |
| `[controller_worker]` | `control_plane_cidrs` | none | Control-plane IPv4/IPv6 ranges excluded from environment egress; management subnets and metadata are also excluded |
| `[controller_worker]` | `dns_servers` / `ntp_servers` | none (any) | restrict guest DNS/NTP egress |
| `[controller_worker]` | `enable_baremetal` | false | Ironic servers (decision Q6) |
| `[controller_worker]` | `mgmt_netns` | `coast-mgmt` | control-side netns holding `coast-hm0`; empty uses the host namespace |
| `[controller_worker]` | `agent_port` | 9443 | mTLS |
| `[certificates]` | `ca_certificate`, `ca_private_key`, `ca_private_key_passphrase`, `signing_digest`, `cert_validity_time` | sha256 / 63072000 | Coast CA. Lifetime is seconds. The default is 730 days |
| `[quotas]` | `environments`, `servers`, `baremetal_servers`, `build_servers`, `apps`, `domains`, `volume_gb`, `storage_gb` | 1,2,0,1,10,20,200,50 | `environments` counts stacks; `apps` counts workloads; `storage_gb` accounts named storage. |
| `[plans]` | `definitions` | `[]` | JSON array of plan definitions; flavor catalog exposed to project readers from API 1.27 |
| `[plans]` | `plans_file` | `/etc/coast/plans.yaml` | Registered but not read by the plan API; configure `definitions` instead. |
| `[coolify]` | `pinned_version` | registry `DEFAULT` | supported release for new stacks only |
| `[coolify]` | `api_timeout` | 30 | Positive request timeout; bounded by EngineEndpoint.timeout |
| `[coolify]` | `insecure_direct_url` | false | Permit dev-only direct HTTPS access to each adopted endpoint; no agent mTLS, but server TLS verification stays on. Never use in production |
| `[coolify]` | `retry_attempts` / `retry_wait` | 3 / 1.0 | Idempotent requests only; maximum attempts / initial exponential delay in seconds |
| `[coolify]` | `rate_limit_attempts` / `rate_limit_timeout` | 8 / 60.0 | HTTP 429 attempt / elapsed retry budgets for all methods; in-flight requests may add `api_timeout` seconds. The controller agent passes the engine's whole-second `Retry-After` through; without it the last retry waits until half a second before `rate_limit_timeout` ends, after the engine's per-minute window; no retry (rate-limit or transient) starts at or past the budget, even after a late wake-up. The agent change needs a controller image rebuild |
| `[coolify]` | `ssh_user` | `root` | User supplied by the operator's worker image |
| `[controller_worker]` | `registry_volume_size_gb` / `registry_volume_type` | 20 / | Controller-only private registry volume. Quota is reserved before Cinder creation |
| `[controller_worker]` | `registry_port` | 5000 | Published on the controller environment address. Security groups use this value and open it only while a build server exists |
| `[controller_worker]` | `registry_image` | pinned `registry:3@sha256:...` | Must match `registry:3` in the controller image lock |
| `[controller_worker]` | `registry_gc_interval` | 3600 | Seconds between registry garbage collection on active stacks |
| `[coolify]` | `build_registry_image_template` | none | Fallback for dedicated git builds when the stack has no private registry template. Contains `{environment_id}` and `{app_id}`. Q94: `{app_id}` is the service id. Q125: the per-stack template wins when it is set |
| `[domains]` | `default_suffix` | `sslip.io` | Suffix of the default hostname. |
| `[domains]` | `skip_verification` | false | Development only: apply custom domains without DNS verification |
| `[dns]` | `use_designate` | false | create default-URL records in Designate; otherwise manual DNS with records shown |
| `[dns]` | `designate_zone_id` | none | operator zone for default URLs; required when Designate is enabled |
| `[dns]` | `record_ttl` | 300 | managed default-app DNS record TTL in seconds |
| `[export]` | `backend` (`swift`\|`s3`), `container`, `url_ttl`, `retention_hours`, `upload_via` (`guest` only; conductor relay planned for a later release), `max_size_gb`, `max_concurrent_per_project` | swift, `coast-exports`, 86400, 72, guest, 500, 1 | decision Q10 |
| `[export]` | `snapshot_retention_days` / `max_snapshot_retention_days` | 14 / 90 | snapshots kept on env delete |
| `[export]` | `s3_endpoint`, `s3_region`, `s3_credentials_ref` (Barbican) | none | S3 backend only; Swift temp-URL key also stored in Barbican |
| `[notifications]` | `exists_notification_interval` / `usage_audit_period` / `service_id` | 3600 / `hour` / none | Current-resource usage snapshots; hour/day/month UTC audit bounds |
| `[oslo_messaging_notifications]` | `driver` / `topics` / `retry` | `messagingv2` / `notifications` / `0` | Lifecycle and usage notifications. `noop` disables delivery |
| `[controller_worker]` | `offboard_snapshot_retention_days` | 14 | |

(cloud-resource-name-prefix-m3-e2e-q3)=
### Cloud resource name prefix

Set `[DEFAULT] resource_name_prefix` before creation. For example:

```ini
[DEFAULT]
resource_name_prefix = coast-e2e-20261010
```

The common helper returns `<prefix>-<kind>-<parts...>`. It permits ASCII
letters, digits and hyphens. It replaces each run of other characters
with a hyphen, strips edge hyphens from each part and omits empty parts.
Names have at most 255 characters. Longer names keep a 12-character
SHA-256 suffix to distinguish resource roles after truncation.

This change applies to environment networks, subnets, routers and security
groups, and to the name checks for worker floating IPs. The default keeps
existing names such as `coast-env-<stack-id>` unchanged. Ownership tags do
not change. Floating IPs have no generated name. DNS record names are
explicit hostnames; neither those names nor their ownership descriptions
change. Conductor cloud resource names also use this helper.

Existing resources are not renamed after creation. Keep the same prefix
for retry, adoption and cleanup of existing networks. Do not change it
while those resources exist: network lookup and FIP validation use the
configured prefix. A prefix is a label, not an isolation or ownership check.

Option registration uses the public
[oslo.config configuration API](https://docs.openstack.org/oslo.config/latest/configuration/index.html).

## OpenStack service clients

`coast.common.clients` builds an openstacksdk Connection for Nova,
Neutron, Cinder, Glance and Barbican. Authentication comes from
`[service_auth]` and is the Coast user in the `service` project. The
session is cached. Services that use the same TLS settings share that
session. Each service group supplies the catalog region and interface.
A different `ca_certificates_file` or `insecure` value uses its own
session. The factory does not contact the cloud when it builds the
client.

Set `region_name` on every service group. Also set `valid_interfaces`
or `endpoint_type`. Coast does not choose a region or a public
interface. Missing authentication, region or interface fails closed.
When `endpoint_type` is set and `valid_interfaces` is empty, Coast
copies that one interface into `valid_interfaces` for openstacksdk.
Do not set the two options to different values.

`[service_auth]` `cafile` and `insecure` verify TLS when the service
group does not set `ca_certificates_file` or `insecure`. Coast does
not log passwords, tokens or secret option values.

Coast does not build a user-scoped connection. The RPC serializer removes
API tenant tokens before conductor handoff. Cloud resources are created
in the service project. `[keystone_authtoken]` is for API token validation;
it is not a fallback source for infrastructure credentials.
`[agent_auth]` is not a source for these clients either.
See [Credentials](security.md#credentials) for service ownership,
tenant attribution and secret handling.

## Database schema management

Create an empty database and configure `[database] connection` in the
operator's configuration file before starting Coast services. MySQL and
MariaDB tables use InnoDB and utf8mb4, with a primary key on every table
for Galera. SQLite is supported for development and unit tests.

```bash
coast-db-manage --config-file /etc/coast/coast.conf upgrade
coast-db-manage --config-file /etc/coast/coast.conf current
coast-db-manage version
```

`upgrade [revision]` defaults to `head` and can be run again safely.
`current` shows the database revision; `version` shows the packaged schema
head without connecting to the database. Downgrades are not supported.
Back up the database before an upgrade.

Developers can create a migration with `coast-db-manage revision -m
"description"`, with optional `--autogenerate`. Review all generated changes
before committing. Never edit a merged migration.

## Coast CA

Coast has its own CA. Keep it separate from the Kolla CA. Octavia uses
the same split. `tools/gen-ca.sh` creates the material with OpenSSL.
The script is non-interactive. A second run keeps the existing keys.
`--force` replaces them. Private keys are mode 0600. The script does
not print private keys.

```bash
tools/gen-ca.sh --output-dir /etc/coast/certs
```

| File | Mode | Use |
|---|---|---|
| `coast_server_ca.cert.pem` | 0644 | Server CA certificate |
| `coast_server_ca.key.pem` | 0600 | Server CA private key |
| `coast_client_ca.cert.pem` | 0644 | Client CA certificate |
| `coast_client_ca.key.pem` | 0600 | Client CA private key |
| `coast_client.cert-and-key.pem` | 0600 | Controller client certificate and key |

The output directory is mode 0700. Optional registry certificate:

```bash
tools/gen-ca.sh --output-dir /etc/coast/certs \
    --registry-host registry.example.com
```

The server CA signs `coast_registry.cert.pem`. The registry key is
`coast_registry.key.pem` (mode 0600). `--force` also removes an
existing registry certificate because the CA key changes. Pass
`--registry-host` again to issue a new one.

| Flag | Meaning |
|---|---|
| `--output-dir DIR` | Required output directory |
| `--days DAYS` | Validity of new certificates. Default 730 |
| `--key-type rsa\|ec` | Key algorithm. Default rsa |
| `--key-size BITS` | rsa: 2048, 3072, or 4096. ec: 256 or 384 |
| `--subject-prefix DN` | Subject prefix. Default `/O=Coast` |
| `--registry-host HOST` | Also issue a registry server certificate |
| `--force` | Replace existing Coast certificate files |
| `--check-expiry DAYS` | Read-only expiry check |

Point `[certificates] ca_certificate` and `ca_private_key` at the CA
that must sign leaf certificates. Use the server CA for server leaves
and the client CA for agent leaves. `signing_digest` is `sha256`,
`sha384`, or `sha512`. `cert_validity_time` is the leaf lifetime in
seconds. `coast.common.tls` issues each leaf without a CSR. A server
leaf uses serverAuth. An agent leaf uses clientAuth. The common name
is the server id. The subject alternative name is the management IP.
The serial number is random.

### Expiry check

`--check-expiry DAYS` does not create or change files. Stdout has one
line per certificate:

```text
OK coast_server_ca.cert.pem 2028-10-10T00:00:00Z
EXPIRING coast_client_ca.cert.pem 2026-10-20T00:00:00Z
```

The timestamp is the UTC `notAfter` time in ISO-8601. The check
includes the registry certificate when that file is present.

| Exit | Meaning |
|---|---|
| 0 | Every certificate is valid beyond `DAYS` |
| 1 | One or more certificates expire within `DAYS` |
| 2 | Usage error, missing openssl, or missing certificate |

The `kolla-ansible coast-certificates` command (`tools/kolla/cli`) wraps
`tools/gen-ca.sh` unchanged, including `--check-expiry`.

(kolla-m6)=
## Kolla
- Images: `coast-base`, `coast-api`, `coast-conductor`,
  `coast-health-manager` in a kolla fork/overlay (`kolla` `--template-override`).
- Role `ansible/roles/coast` in a kolla-ansible overlay: DB bootstrap,
  Keystone registration, HAProxy frontend `:9600`, shared Kolla messaging,
  config templates, mgmt-net interface on controllers (reuse Octavia's
  `octavia_network_interface` pattern), TLS for internal/external endpoints
  with the kolla CA (API) and Coast CA (guests) kept separate.
- the API uses uWSGI and `coast-wsgi-api`, bound only to
  `api_interface_address`. Backend TLS follows `kolla_enable_tls_backend`.
  Use the supplied `tools/kolla/ansible/coast.yml` beside Kolla `site.yml`;
  it applies standard HAProxy and ProxySQL config before Coast bootstrap.
  The external VIP listener is off unless `coast_enable_external_api`
  is true. Internal VIP TLS follows the normal Kolla frontend flag.
  Database/user bootstrap follows Magnum; preconfigured DBs stay external.
- `globals.yml`: `enable_coast: "yes"`, `enable_haproxy: "yes"`, the built
  `coast_tag`, API paste source, OpenStack CA paths, and the existing
  management/provisioning inputs. Add `coast_database_password`,
  `coast_keystone_password` and `coast_agent_keystone_password`
  to `passwords.yml`.
  No password defaults are supplied. Preserve existing DB/Keystone values
  during cutover. Messaging uses Kolla's shared `rpc_transport_url` and
  `notify_transport_url`, without a Coast broker account or vhost. See `tools/kolla/README.rst` for exact inputs,
  certificate paths, CLI commands and DevStack upgrade steps.
- copy `tools/kolla/ansible/group_vars/all/coast.yml`
  with the role. Set `coast_network_interface` to a dedicated management
  VLAN interface and leave `coast_hm0_manage=false`. The default is
  `"{{ network_interface }}"`, not `api_interface`. The IPv4 address uses
  `'coast_network' | kolla_address`, following
  [Kolla Octavia group variables](https://github.com/openstack/kolla-ansible/blob/stable/2026.1/ansible/group_vars/all/octavia.yml).
  Coast management currently supports IPv4 only.
  The health-manager bind IP and the guest heartbeat destination list use
  these management addresses. API and HAProxy endpoints stay on the API
  network. The conductor uses existing host-namespace mTLS client sockets;
  host routing selects the management interface. Apply the host firewall
  isolation below. No conductor listener or source-bind option is added.
- For DevStack/development without a VLAN, enable `coast_hm0_manage`, set
  `coast_network_interface=coast-hm0`, and set each host's
  `coast_network_interface_address` to its operator-selected
  `coast_hm0_address` before the interface exists. Both Kolla paths render
  an empty `mgmt_netns`. See `tools/kolla/README.rst` for required inputs.

(runbooks-to-write-as-features-land)=
## Recovery procedures

The sections below cover heartbeat/failover diagnosis, interrupted exports,
retained-volume deletion, token and certificate rotation, engine upgrade
and rollback, and stack deletion. Inspect task holders, activity events and
ownership tags before recovery; do not clear claims to bypass uncertain work.

(api-app-m1)=
## API app

Use `coast-wsgi-api` with a production WSGI server. Use `coast-api` for
isolated development; it runs a single-process wsgiref server on the
configured `[api] bind_host` and `bind_port`. Place `api-paste.ini` beside
`coast.conf`, or set `[api] api_paste_config` to an absolute path.

The default `[api] auth_strategy = keystone` uses keystonemiddleware on
versioned API routes. Configure credentials in `[keystone_authtoken]`.
`GET /` discovery and `/healthcheck` are public. `/v1` exposes version discovery and controllers for stacks, workloads,
plans, flavors, quotas and the other documented resources. The maximum
REST microversion is 1.28.

Never use `auth_strategy = noauth` on an exposed service. It skips token
validation and trusts identity headers for isolated tests and development.
`[api] public_endpoint` sets discovery links; otherwise the app uses the
request URL. Enable `[oslo_middleware] enable_proxy_headers_parsing` only
behind a trusted proxy that removes client-supplied forwarding headers.


## Operator plan catalog (Q34, revisable contract)

Set `[plans] definitions` to a JSON array. Each entry requires `name`,
`kind`, `roles` and internal `flavor`; optional `description` defaults null.
Use `vm` or `baremetal` for kind and `control`, `worker`, `build` for roles.
Names must be unique. There are no deployment-specific default plans.
The operator supplies the Nova flavor name or ID. API 1.27 exposes resolved
VM sizes and role-compatible plan names through `GET /v1/flavors`.

```ini
[plans]
definitions = []
```

`GET /v1/plans` reads this option, not a database or `plans_file`, and returns
only the approved public fields sorted by name. Invalid definitions produce
a sanitized 500. Stack and server creation validate the selected plan
and map its internal flavor through the configured catalog.

Give every plan a plain `description` with its size, for example
`"App engine server: 2 vCPU / 2 GB RAM / 8 GB disk"`. The CLI shows it. The
devstack deployment sets these descriptions in its `coast.conf` override.

The dashboard offers Nova flavors instead of plans, and
stack create maps a flavor name or ID to the one VM plan for that role
that uses it. Every flavor users should pick must therefore appear in
exactly one plan per role; other flavors are rejected with 400.

## Project quotas

The eight `[quotas]` defaults apply unless an admin sets per-project
limits through `/v1/quotas`. Null restores a default, -1 means unlimited,
and 0 prevents new allocations. DELETE resets all project overrides;
it does not reset usage or remove the quota_usages lock sentinel.
Lowering a limit does not remove existing resources. Reservations remain
held until the conductor commits or rolls them back.
See api.md for payloads, counting rules and completion requirements.

(m2-adopt-an-existing-engine)=
## Adopt an existing engine

This operator-only command needs local Coast configuration and DB access.
Use it only for an isolated development engine. It does not provision
cloud resources, register engine servers, or fetch the engine token.
The existing engine must belong to one tenant stack only. The command
creates a stack row.

```bash
coast-manage --config-file /etc/coast/coast.conf adopt-stack \
  --project-id <project-id> --name existing \
  --url https://engine.example.com \
  --token-ref https://secrets.example.com/v1/secrets/<secret-id> \
  --server-ip 192.0.2.1 --server-role worker \
  --engine-ref <engine-project-id> --server-engine-ref <engine-server-id>
```

Pass a Barbican secret URL, never the token. The command stores only the
secret reference. It creates ACTIVE stack and server rows in one
transaction, charges quotas, and emits create notifications after commit.
It always creates one `kind=byo`, `role=control` row; the optional server is
`worker` (default) or `build`. Existing engine refs are optional CLI inputs,
not new API fields. Supply refs when the engine resources already exist.
The plan is `adopted`; driver and version use the configured driver and pin.
Duplicate names and quota failures leave no partial rows.

The command prints the stack ID and an `adopted_endpoints` entry.
Merge this entry into the mapping on every conductor and restart it:

```ini
[controller_worker]
enabled_drivers = noop,coolify_v4
[coolify]
insecure_direct_url = true
adopted_endpoints = <stack-id>:https://engine.example.com
```

The allow-list defaults to `noop`; the endpoint mapping defaults to empty.
Direct access defaults to disabled. HTTPS certificate verification uses the
system CA file or directory (`SSL_CERT_FILE` / `SSL_CERT_DIR`); a missing
trust path fails closed. URLs must not contain credentials, queries, fragments or commas.
The Coolify client adds `/api/v1` to direct URLs with no path. URLs with
an explicit API path use that path unchanged.
Missing URL mappings, token refs, secret storage, plugins or supported
versions fail closed. Production secret storage fails closed unless
`[secrets] backend = barbican` and the service-project Barbican client is
configured. Managed stacks use agent/port-derived mTLS endpoints instead
of direct mappings. Never enable direct access in production.

`--config-file` selects operator configuration; `--project-id` supplies
the tenant owner, `--name` the stack name, `--url` the existing HTTPS engine
endpoint, and `--token-ref` its Barbican reference. `--server-ip` adds an
existing server at that address; `--server-role` selects worker or build.
`--engine-ref` and `--server-engine-ref` record existing engine identifiers.
These operator-only identifiers must come from inventory.

(barbican-secrets-m3)=
## Barbican secrets

See [Secrets and Barbican](secrets.md) for the full inventory, Barbican
payload limits, outage behaviour and auditing.

Set `[secrets] backend = barbican` on the API, conductor and health
manager. The default
`disabled` fails closed; there is no production in-memory fallback.
Configure `[service_auth]` for the Coast service user in the service project
and `[barbican]` region and catalog interface as described above. Tenant
request credentials never select the Barbican project.

All values use `text/plain` opaque secrets. The SDK returns the secret URL;
Coast stores that reference, not the payload. Reads and deletes extract the
UUID and use the configured catalog endpoint, never the URL's host. Delete
ignores missing secrets for revert. Backend exceptions are replaced with a
fixed error and are not logged.

Standalone provisioning helpers generate an Ed25519 private key in OpenSSH
PEM format and an OpenSSH public key, and a 32-byte random heartbeat key
encoded as hex text. Heartbeat consumers must decode it with `bytes.fromhex`
before HMAC-SHA256. The engine token helper stores the token returned by the
agent over mTLS; it does not generate or bootstrap an engine token.

Stack create checkpoints each reference on the `stacks` row before the
next step, reuses it on retry and deletes it on revert. The health
manager's default `[health_manager] key_lookup`
(`coast.health_manager.keys.StackKeyLookup`) reads
`stacks.heartbeat_key_ref`. Coast names each Barbican secret, sets user
metadata for attribution and restricts read access with an ACL (see
[Secrets](secrets.md)). Tenant reference lookup is project-scoped in Coast;
Barbican authorization is service-project scoped.

SDK reference:
https://docs.openstack.org/openstacksdk/latest/user/proxies/key_manager.html

(guest-agent-m3)=
## Guest agent

Run `coast-agent --config-file /etc/coast/coast.conf` as the guest service.
The image lane supplies the config drive and namespace setup. Keep the
configuration file private because it contains the per-stack HMAC
key. No CA private key or root credentials belong in this configuration.

Required `[agent]` options: `bind_ip`, `server_cert`, `server_key`,
`client_ca`, `server_id`, `stack_id`, `heartbeat_key` (64 hex digits),
and `health_manager_ips` (IPv4 literals). Set `role=controller` only on the
controller; the default is `worker`. `role=build` uses worker behavior.
The operator must mount the persistent data volume before agent startup.
Preserve the owned 0700 `state_path` (default `/data/coast-agent`) during
replacement. Bootstrap markers contain no credentials. Token rotation also keeps a
private staged replacement token under `state_path` until revocation is
checkpointed. Keep `runtime_path` (default
`/run/coast-agent`) on tmpfs with owned 0700 permissions. Temporary token
files live there, not on the persistent data volume; startup removes
abandoned token directories without resetting one-shot markers.

Guest sockets bind `[agent] bind_ip` in the main network namespace.
Control-side sockets use
`[controller_worker] mgmt_netns` (`coast-mgmt` by default; an explicit empty
value uses the host namespace).

Effective guest defaults: `bind_port=9443`,
`heartbeat_port=5565`, `heartbeat_interval=10`, `max_body_size=1048576`,
`request_timeout=30`, `bootstrap_timeout=600`, `max_connections=16`.
The request timeout bounds header/body receipt and the entire engine
exchange, not just idle reads. TLS handshakes have the same idle timeout
and are included in the connection limit. The bootstrap response deadline
is separate; local subprocess/API operations also have fixed timeouts.

The proxy accepts only the pinned driver's method/path combinations under
`/api/v1/`. It excludes UI routes, API enable/disable, instance settings,
cloud credentials, team/user management and integration tokens. It rejects
encoded paths, traversal and ambiguous HTTP framing. It forwards only
Authorization, Content-Type and Accept; it returns only Content-Type.
It never forwards cookies, internal hostnames, forwarding headers or
redirect Locations. Engine error bodies are discarded. Responses use
`Cache-Control: no-store` and close the connection.

Project environment access through the agent uses this closed list
(base path `/api/v1`):

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/projects/{project}/environments` | List or create environments |
| DELETE | `/projects/{project}/environments/{environment}` | Delete an empty environment before project delete |

Environment DELETE uses the public engine API. Coast checks for placed
workloads before cleanup; an engine-reported nonempty environment is also
refused with `DriverError`.
This cleanup uses the existing agent allow-list.

`GET /health` reports agent liveness, not engine readiness. Bootstrap and
rotation require JSON with `engine_version` matching the guest image's
baked release. HTTP 412 means the
pinned local engine, root ownership or loopback API preconditions
failed; no engine exception text is returned. HTTP 409 means a one-shot
bootstrap or a different pending rotation prevents the request. Do not
delete markers. Rebuild a failed fresh controller; retry token rotation
through the [operator command](#engine-api-token-rotation) using the durable
protocol in [secrets](secrets.md#rotation). Never share its credentials with
tenants.

The service uses the existing oslo.service backend and does not monkey-patch
sockets or threads. The HTTPS server uses native stdlib threads. Namespace
entry/restore does not yield to an event loop. No optional backend or extra
dependency is required.

Transport design follows the
[Python SSL reference](https://docs.python.org/3.12/library/ssl.html)
and the
[Octavia heartbeat envelope](https://docs.openstack.org/octavia/latest/_modules/octavia/amphorae/backends/health_daemon/status_message.html).
Coast uses uncompressed JSON and a binary digest rather than Octavia's
compressed default; Q79 records this bounded wire format.

## Container exec

The guest agent accepts `GET /v1/exec` on the management mTLS listener
for every guest role. The first text frame is JSON. The fields are
`project_id`, `resource_type` (`app` or `service`), `resource_uuid`,
optional `command`, `cols`, and `rows`. `command` null starts
`/bin/sh` with a terminal. A list runs that argv once, without a
terminal.

The agent starts a process only in one running container that has the
app engine labels for that resource and this guest's project. It does
not start a host shell. A refused session closes with code 4403 and
the reason `Refused`.

`[agent] project_id` is written on the config drive from the stack
project. An empty value fails exec closed and does not stop the agent.
`exec_idle_timeout` defaults to 900 seconds. `exec_max_duration`
defaults to 14400 seconds.

Binary frames carry process bytes. A text frame
`{"type":"resize","cols":80,"rows":24}` changes the terminal size.
`{"type":"exit","code":0}` reports the process status. The agent does
not log keystrokes, commands, or process output.

App engine label keys are `coolify.managed=true`,
`coolify.type=application` with `coolify.applicationUuid`, or
`coolify.type=service` with `coolify.service.subUuid`. When the sub
label is absent, `coolify.serviceUuid` selects the service container.
A numeric id label is not accepted. Zero or several matches are
refused.

Controller first boot does not publish the built-in terminal port.
Stack create and service reconcile keep `is_terminal_enabled` false on
every app engine server. Deploy log streaming stays available.

(controller-management-interface-m3-coast-hm0)=
## Controller management interface

Use `tools/coast-hm0.py` on each conductor/health-manager host. This follows
Octavia's [o-hm0 OVS veth pattern](https://docs.openstack.org/octavia/latest/install/install-ubuntu.html),
but the Coast end lives in `coast-mgmt`, not the host root namespace.
The current tool supports Neutron ML2/OVS only. The operator supplies an
existing Neutron OVS integration bridge on that host, with the operator's
configured bridge name. A provider bridge alone is not sufficient: the
Neutron OVS agent must bind the port and apply security-group filtering.
The tool does not create a provider bridge, VLAN mapping, tunnel or router.
For another backend, the operator must supply equivalent port plumbing.
Do not attach a host management NIC or a default route to this namespace.

1. Install Coast and the operator tools `ip`, `sysctl` and `ovs-vsctl`.
   Use `[service_auth]` service-project credentials and the configured
   `[neutron]` region, catalog interface and TLS settings. Do not use a
   tenant token. Choose a management CIDR and a distinct fixed IPv4 address
   for each host; keep it separate from tenant and host-service networks.
2. Prepare the shared network, subnet, default-deny control SG and host
   port. Supply values from the operator's deployment configuration:

   ```bash
   /path/to/coast-venv/bin/python tools/coast-hm0.py prepare \
     --config-file /etc/coast/coast.conf --cidr MANAGEMENT_CIDR \
     --address HOST_MANAGEMENT_IP --host BINDING_HOST
   ```

   Replace the uppercase placeholders with operator configuration values.
   `--config-file` selects configuration, `--cidr` sets the management
   subnet, `--address` selects this host's fixed IP, and `--host` supplies
   its Neutron binding host.

   The subnet has no gateway, DHCP or router. Guest first boot must use
   static management addresses from config drive. Save the returned IDs,
   MAC and address/prefix in operator configuration, not in Coast's DB.
   Management resource names use `[DEFAULT] resource_name_prefix`.
   The default keeps the existing names and host suffix unchanged.
   Ownership and configuration checks still apply on repeated calls.
   Set `[controller_worker] mgmt_net_id` and `mgmt_sg_id` to the returned
   network and SG IDs. Existing stack network steps add the Q72
   reciprocal SG rules: ingress 5565/udp and egress 9443/tcp, each limited
   to that environment's agent SG. Do not add broad CIDR rules. The port
   owner is `Coast:health-mgr`, following `Octavia:health-mgr`; never use
   Neutron's trusted `network:` prefix, which can bypass SG filtering.
3. Stop Coast management socket users before plumbing or re-plumbing.
   Run as root on the host named by `BINDING_HOST`:

   ```bash
   sudo /path/to/coast-venv/bin/python tools/coast-hm0.py plug \
     --bridge INTEGRATION_BRIDGE --port-id MANAGEMENT_PORT_ID \
     --mac MANAGEMENT_PORT_MAC --address MANAGEMENT_ADDRESS_WITH_PREFIX
   ```

   `--bridge` selects the integration bridge; `--port-id` and `--mac`
   identify the prepared Neutron port; `--address` supplies its IP/prefix.

   The root-side veth `coast-bhm0` is an unnumbered OVS port; `coast-hm0`
   is its namespace peer with Neutron's MAC and fixed IP. Forwarding and
   IPv6 are disabled in the namespace. No default route is installed.
   Run plug after Neutron/OVS startup at every host boot, before Coast
   starts. Integrate it with the operator's service manager; Kolla also supplies
   host-interface wiring through its Coast role. Prepare is resumable and rejects foreign names or drift.
   Plug removes a partial veth when OVS attachment fails. A retry checks
   the existing Neutron port identity before reapplying its address.
4. Set `[controller_worker] mgmt_netns = coast-mgmt` and
   `[health_manager] bind_ip` to this host's fixed IP. The service needs
   permission to enter the namespace (`CAP_SYS_ADMIN` for Linux setns).
   Do not run the entire service with `ip netns exec`: DB and RabbitMQ
   connections must stay in the host namespace. Coast creates only agent
   client and heartbeat sockets inside management, then restores its native
   thread namespace. Conductor flow callers can use
   `coast.common.management.socket_for_agent` for an mTLS client socket;
   they must still configure client/server TLS verification. Production
   conductor-to-agent transport uses this socket factory in the stack flows.

(host-namespace-management-port-m3-c4hn)=
### Host-namespace management port

The default remains `[controller_worker] mgmt_netns = coast-mgmt`.
Operators can instead put the management interface in the host network
namespace, as Octavia does with its OVS internal health-manager port.
Set an explicit empty value (do not omit the option):

```ini
[controller_worker]
mgmt_netns =
```

In this mode, the conductor agent client, production Coolify client and
health-manager socket factory do not call `setns`. They use the current
host namespace. Run these services in the host namespace, not with
`ip netns exec`. Guest NICs stay in the main namespace.
Guest first boot configures management host routes without a default gateway
and environment DHCP with the stack-router default route.

Use the existing service-project Neutron management port and its binding
host, MAC and fixed IP. Stop management socket users before changing the
interface. On that binding host, with OVS and Neutron running, use the
following pattern instead of the dedicated-namespace `plug` command:

```bash
sudo ovs-vsctl add-port br-int coast-hm0 -- set Interface coast-hm0 \
  type=internal external_ids:iface-id=MANAGEMENT_PORT_ID \
  external_ids:iface-status=active \
  external_ids:attached-mac=MANAGEMENT_PORT_MAC
sudo ip link set dev coast-hm0 address MANAGEMENT_PORT_MAC
sudo ip address add MANAGEMENT_ADDRESS_WITH_PREFIX dev coast-hm0
sudo ip link set dev coast-hm0 up
```

For OVS, `--` separates the port addition from interface settings.
`type=internal` creates the host interface and `external_ids` attach
Neutron identity. `ip link set ... address` sets its MAC; `ip address add`
sets its address/prefix.
Here `MANAGEMENT_PORT_ID`, `MANAGEMENT_PORT_MAC` and
`MANAGEMENT_ADDRESS_WITH_PREFIX` come from
that Neutron port. `br-int` is the integration bridge; use the operator's
configured integration bridge name if it differs. This binds the OVS
internal interface to the Neutron port through `iface-id`, `iface-status`
and `attached-mac`. Do not also attach the same port to a veth. Remove
the old attachment before switching modes. Do not install a default route
on the management interface. Set `[health_manager] bind_ip` to its fixed
IP. Restore this interface setup after OVS/Neutron startup at each host
boot, before starting Coast services.

Host mode does not provide the dedicated namespace's host isolation.
Keep IP forwarding disabled for management traffic and use host firewall
rules to block management-network access to RabbitMQ, the database and
internal APIs. Preserve the Neutron peer security-group rules described
above: 5565/udp inbound from agents and 9443/tcp outbound to agents.
Do not replace them with broad CIDR rules. Namespace entry permission is
not required for host mode, but interface setup still requires root.

Verify the interface with `ovs-vsctl list Interface coast-hm0` and
`ip address show dev coast-hm0`. Check the Neutron port's binding host,
MAC and fixed IP against these results. Verify agent mTLS health and
heartbeat reception after restarting Coast. To revert, stop the services,
remove the internal port with `ovs-vsctl del-port br-int coast-hm0`, run
the dedicated-namespace setup above and restore `mgmt_netns = coast-mgmt`.

### Heartbeat receiver and key lookup

The built-in Barbican lookup is the default. To set it explicitly:

```ini
[health_manager]
key_lookup = coast.health_manager.keys.StackKeyLookup
```

Operators can override this default. An explicitly empty value prevents
startup.
The class takes no constructor arguments. Its instance receives the canonical
stack UUID and returns exactly 32 raw key bytes. It reads the live stack with
an admin service context, then uses `stacks.heartbeat_key_ref` and the existing
secret store. Set `[secrets] backend = barbican` and configure the Coast
service credentials and database connection on the health-manager host.
Barbican access uses the service project, not tenant credentials.

The lookup decodes the Q77 payload from 64 hex digits. It caches up to 1024
keys in process memory for 30 seconds, using a monotonic clock. Each lookup
checks the live stack and its reference in the DB. A changed reference, a
deleted or unknown stack, or a missing reference invalidates the cached
key. On secret-store errors the last good key remains usable for up to
`[health_manager] key_cache_max_stale` seconds after expiry (default 3600).
Set zero to disable stale reuse. An invalid payload is not an outage.
Payload changes at the same reference are visible after at most 30 seconds.
Missing keys, invalid payloads and lookup failures reject packets without an
update or an exception containing the key. Keys and backend errors are never
logged. Do not store raw keys in configuration, files, logs or DB rows.

An operator can still configure another no-argument callable class. Its
instance must return 32 raw bytes, or `None` when no key is available.
The initial migration (`0001_initial`) creates the nullable
`stacks.heartbeat_key_ref` field approved in Q86. The unused engine backup
reference from Q86 is removed by the engine-token rotation migration (#74).

The receiver accepts the Q79 JSON plus binary HMAC envelope. It rejects
invalid fields, duplicate JSON keys, invalid HMAC, timestamps at least
60 seconds away from receiver time, non-increasing sequences, and a source
IP that differs from the live server's `mgmt_ip`. The live stack must
match the server. Controller, worker and build roles use the same checks.
The global size/rate limits run before parsing and secret lookup. Set the
rate limit above the expected aggregate guest heartbeat rate with headroom.

Create `state_path` with mode 0700, owned by the service user. Checkpoints
contain only server UUIDs and sequence numbers, never secrets. Keep this
storage through restarts and host replacement. Each health-manager has its
own checkpoint directory; a file lock rejects concurrent users of the
same directory.
Removing checkpoints loses replay protection for packets still within the
60-second freshness window. Remove obsolete server checkpoints only when
that server has been deleted and its final heartbeat is older than this
window. Do not remove live-server checkpoints during key rotation.

The receiver checkpoints before accepting a packet, then batches writes to
the existing `servers.last_heartbeat_at` field using receiver UTC time.
It rechecks live ownership and management IP when it writes. A failed DB
batch is retained within the configured size limit for retry. Last-seen
never moves backwards. A crash between checkpoint and batch write can lose
that last-seen update; the next fresh sequence recovers it. There are no
status transitions in the receiver itself. The stale checker below
updates statuses independently and dispatches eligible failovers to the
conductor. The conductor runs service reconciliation.

(periodic-stale-detection-m4-c4hm)=
### Periodic stale detection

The health-manager checks at most 100 eligible servers every
`heartbeat_interval` seconds (default 10), using a rotating ID cursor.
A full sweep of a large fleet takes multiple intervals. An oslo.service timer runs the checker independently of the native receiver
thread. The checker casts worker/build failover after the grace period;
only the conductor makes engine and cloud calls.
After startup, the checker waits `heartbeat_timeout` seconds (default 60)
to allow incoming heartbeats to refresh timestamps. The receiver and checker
share the heartbeat key cache. On secret-store errors a last good key remains
usable for up to `key_cache_max_stale` seconds after cache expiry. If no usable
key exists, defer stale changes and failover dispatch for that stack until
lookup recovers, with a rate-limited warning. See [secrets](secrets.md).

An ACTIVE server becomes UNHEALTHY when its last recorded heartbeat is
strictly older than the timeout. A server with no recorded heartbeat uses
its creation time. A fresh heartbeat returns UNHEALTHY to ACTIVE on its
next check. Guests cannot set the recorded time; the receiver uses UTC.

The owning stack becomes DEGRADED if any live server is not ACTIVE.
This includes a stale controller; stack UNHEALTHY is not part of the
current status contract. The stack returns to ACTIVE only when all live
servers are ACTIVE and its fault is the health-manager's fixed
`Server heartbeat timeout.` message. Unrelated faults and DEGRADED states
are preserved. Coolify environment, appgroup and service rows do not
change. Notifications use `coast.server.update.{start,end}` and the Q47
stack alias `coast.environment.update.{start,end}`, after commit and only
when a status changes. Delivery is best-effort.

Deleted resources, transitional resource statuses, and servers or stacks
with active task states are excluded. Lock-time checks prevent overlap
with provisioning, deployment, upgrade or deletion. Checks are idempotent;
a failed item is retried on a later sweep without blocking other items.
Automatic worker/build VM failover now uses `enable_failover` as its kill
switch. `failover_grace` starts at the durable UNHEALTHY episode marker,
not at the last heartbeat. `failover_max_concurrent` limits claimed work
across all health managers and conductors; configure the same value on
all control-plane nodes. Use zero grace only when immediate replacement
after stale detection is required. Claims hold the existing stack task,
so one stack has at most one replacement. Queued casts hold no capacity.

Q114 is **RESOLVED (James 2026-10-13)**: no automatic replacement; operator action. Workers with any
live template service stay UNHEALTHY and get one operator-action event
per unhealthy episode. Repeated sweeps and health-manager restarts do not
repeat it. Do not bypass the proxy to recover them. Bare-metal
re-provision is not implemented. Controller VM failover is
the following section.

Use the recorded image/flavor and retain both Cinder volumes, both ports,
fixed addresses and any worker FIP. The controller must be ACTIVE, the
stack must have no active task, and services must be unlocked. The flow
supports at most 100 live services per stack and 100 attached volumes per
server; larger stacks are skipped. Cloud waits use `build_timeout` and
workload waits share `deploy_timeout`. Unsupported image-digest pinning,
unknown engine observations, or uncertain enqueue outcomes fail closed.

On failure, inspect the server failover events and the stored server
`delete.cleanup` receipt containing the old Nova reference. The existing
event API rule hides that internal message; use operator maintenance
access to the Coast DB, not to the engine DB. Replacement Nova names use the
failover receipt ID with the configured resource prefix. Never start the
old VM while a replacement might still write the retained volumes. An
unknown or failed replacement cleanup keeps the old guest stopped and
retains all cloud resources for operator recovery. No automatic retry
runs for a server in ERROR. Same-host restart releases interrupted claims
and records ERROR without repeating cloud moves or deployment enqueues.
Keep the conductor host identity stable and distinct between hosts;
cross-host takeover is not supported. Existing admin failover API
completion for worker and build servers is unchanged by the worker lane.

Controller VM failover uses the same grace, global claim cap and stack
task. It rebuilds the Coolify VM on its recorded image and keeps all
three volumes, the ports and the floating IP. It does not change
`engine_version`. A missing recorded image does not rebuild the VM. The
server stays UNHEALTHY and one operator event is recorded. The admin
action returns HTTP 409 and does not change the server. A bare-metal
controller becomes ERROR with one operator event and is not rebuilt.
Set `[health_manager] failover_max_concurrent` on every control-plane
node. The cap counts worker, build and controller claims together.

Failover decision logs use the stable ``Failover:`` prefix and
``key=value`` fields (names and values containing whitespace are quoted).
Health-manager INFO lines identify ``unhealthy``
(heartbeat timeout) and ``dispatch`` (grace expired) decisions with the
stack ID, server ID/name/role, compute ID and last heartbeat timestamp.
A missing heartbeat is logged as ``None``; creation time is the timeout
fallback. WARNING lines explain template-worker, bare-metal and busy-stack
skips once per reason per unhealthy episode, including across restarts.
Global capacity and final admission are decided by the conductor; its
WARNING lines explain refusals using the same episode deduplication.
Conductor INFO ``start`` and ``end`` lines bracket admitted worker, build
and controller replacements. End lines include ``outcome``, ``duration``
in seconds and ``old_compute_id`` / ``new_compute_id``. A failed attempt
that restored its original guest can have equal old and new IDs.

### Verification and removal

After plug, check `ip -n coast-mgmt address show coast-hm0`,
`ip -n coast-mgmt route show` and
`ip netns exec coast-mgmt sysctl net.ipv4.ip_forward`.
Only the connected management route must be present; forwarding must be 0.
Check that the root-side veth has no IP and that the Neutron port is ACTIVE.
With a configured Q86 lookup, send a real agent heartbeat and check that
only that server's `last_heartbeat_at` advances. Send a replay and a packet
with a wrong HMAC; neither must advance last-seen. Use the agent's mTLS
9443 health endpoint with the Coast client certificate to check reachability.
A tenant workload must not reach the management network or host services.
These cloud checks require the operator's staging cloud; unit tests do not
replace them.

To revert host plumbing, stop the health-manager and conductor, then run:

```bash
sudo ovs-vsctl --if-exists del-port INTEGRATION_BRIDGE coast-bhm0
sudo ip -n coast-mgmt link delete coast-hm0
sudo ip netns delete coast-mgmt
```

`--if-exists` permits removal when the port is already absent; `ip -n`
selects the namespace for interface deletion.
Delete only this host's Neutron port with the returned ID after detaching
it. Keep the shared network, subnet and SG while another host or guest uses
them. The operator owns final removal of these shared resources. No tool
operation changes `tools/build-images/` or engine internals.


### Export snapshot consistency (Q91)

Set `[export] client_certificate`, `client_key` and `server_ca` to the
control-side client certificate/private key and Coast server CA PEM files.
The snapshot helper requires all three and verifies the management IP
against the server certificate. It uses the control-side management socket
factory; it never uses a guessed endpoint, guest RPC or a system-CA fallback.
This helper adds no secret-reference field. Q86 is RESOLVED: stack rows
keep `heartbeat_key_ref` and engine token rotation checkpoints.

`[agent] export_auto_resume_timeout` is the maximum quiesce lease in seconds
(default 900). The agent checks deadlines each second and retries failed
starts without discarding the stop record. Preserve `export-leases` below
`[agent] state_path` across restart and guest replacement. These files
contain identifiers and deadlines only, not secrets or signed URLs.

Export creation uses these quiesce/resume tasks around Cinder snapshots,
then archives selected volumes on the guests and uploads through signed
Swift or S3 URLs. Every selected volume owner is quiesced and resumed;
worker and build agents do not bootstrap or proxy engine requests.
See lifecycle.md for the internal request contract.

(stack-provider-network-isolation-m3-c4sg)=
## Stack provider-network isolation

The provider/physnet network used by stacks **MUST** block access to
OpenStack control-plane and management networks at the router/firewall.
Coast security groups also exclude `[controller_worker] control_plane_cidrs`,
management subnets and metadata from environment egress . Set every
control-plane CIDR, including internal APIs, databases and message brokers.
Allow sufficient Neutron
rule quota for CIDR subtraction; see networking.md. Agent-management
remote-group rules for TCP 9443 and UDP 5565 are unchanged.

### Host firewall rules

Security groups do not replace the host firewall. On each control host,
and on the provider router, drop packets whose source is a guest floating
IP, a guest SNAT address, or the management network when the destination
is a control-plane port.

The health-manager listens on UDP port 5565 and only on its management
address. Keep that port open for sources on the management network.
Drop UDP port 5565 from guest floating-IP and SNAT sources.

The prefixes below are documentation ranges from RFC 5737. Replace them
with the ranges for the cloud. Replace the port list with every
control-plane service port in use. The example includes identity,
compute, volume, image, key manager, the Coast API, Neutron, the message
broker, the database and the token cache.

```
table inet coast_cp {
    chain input {
        type filter hook input priority 0; policy accept;
        ip saddr { 198.51.100.0/24, 203.0.113.0/24 } \
            tcp dport { 3306, 5000, 5672, 8774, 8776, \
                        9292, 9311, 9600, 9696, 11211 } drop
        ip saddr 192.0.2.0/24 \
            tcp dport { 3306, 5000, 5672, 8774, 8776, \
                        9292, 9311, 9600, 9696, 11211 } drop
        ip saddr { 198.51.100.0/24, 203.0.113.0/24 } \
            udp dport 5565 drop
    }
    chain forward {
        type filter hook forward priority 0; policy accept;
        ip saddr { 198.51.100.0/24, 203.0.113.0/24 } \
            tcp dport { 3306, 5000, 5672, 8774, 8776, \
                        9292, 9311, 9600, 9696, 11211 } drop
        ip saddr 192.0.2.0/24 \
            tcp dport { 3306, 5000, 5672, 8774, 8776, \
                        9292, 9311, 9600, 9696, 11211 } drop
        ip saddr { 198.51.100.0/24, 203.0.113.0/24 } \
            udp dport 5565 drop
    }
}
```

| Prefix | Role in this example |
| --- | --- |
| `198.51.100.0/24` | Guest floating IPs |
| `203.0.113.0/24` | Guest SNAT addresses |
| `192.0.2.0/24` | Management network |

`192.0.2.0/24` is also a documentation prefix here. Do not copy it into
a cloud. The same drops apply when the host uses iptables:

```
iptables -A INPUT -s 198.51.100.0/24 -p tcp -m multiport \
    --dports 3306,5000,5672,8774,8776,9292,9311,9600,9696,11211 -j DROP
iptables -A INPUT -s 203.0.113.0/24 -p tcp -m multiport \
    --dports 3306,5000,5672,8774,8776,9292,9311,9600,9696,11211 -j DROP
iptables -A INPUT -s 192.0.2.0/24 -p tcp -m multiport \
    --dports 3306,5000,5672,8774,8776,9292,9311,9600,9696,11211 -j DROP
iptables -A INPUT -s 198.51.100.0/24 -p udp --dport 5565 -j DROP
iptables -A INPUT -s 203.0.113.0/24 -p udp --dport 5565 -j DROP
iptables -A FORWARD -s 198.51.100.0/24 -p tcp -m multiport \
    --dports 3306,5000,5672,8774,8776,9292,9311,9600,9696,11211 -j DROP
iptables -A FORWARD -s 203.0.113.0/24 -p tcp -m multiport \
    --dports 3306,5000,5672,8774,8776,9292,9311,9600,9696,11211 -j DROP
iptables -A FORWARD -s 192.0.2.0/24 -p tcp -m multiport \
    --dports 3306,5000,5672,8774,8776,9292,9311,9600,9696,11211 -j DROP
iptables -A FORWARD -s 198.51.100.0/24 -p udp --dport 5565 -j DROP
iptables -A FORWARD -s 203.0.113.0/24 -p udp --dport 5565 -j DROP
```

Apply the same policy on the FORWARD chain of a router that carries
guest traffic. Do not add a default route on the management interface.
The Coast API listens on the internal API address. Kolla does not publish
it on the external VIP unless `coast_enable_external_api` is true.
DevStack starts `coast-wsgi-api` under uWSGI on `HOST_IP`. See
`devstack/README.md`.

In the iptables examples, `-A` appends to the named INPUT or FORWARD
chain, `-s` matches a source CIDR, `-p` selects TCP or UDP, `-m multiport`
enables a list of destination ports, `--dports` supplies that list,
`--dport` selects one port, and `-j DROP` discards matching packets.

(m3-controller-stack-provisioning)=
## Controller stack provisioning

Configure the service-project clients, the Barbican backend, the operator
management network and security group, the stack CIDR pool, the external
network, and the Coast CA first. Configure either the dedicated control-side
management namespace or the host management interface described above. The
controller image must satisfy the first-boot and mount order in
lifecycle.md, including the Q78 installation prerequisites.

| Group | Option | Requirement/default |
|---|---|---|
| controller_worker | image_tag_controller, image_owner_id | Required. Exactly one active image with this tag, owner and `coast_role=controller`; `controller_image_id` can narrow selection |
| plans | definitions | Required matching VM plan with the control role. The flavor is internal |
| certificates | ca_certificate, ca_private_key | Server CA signing material. Only its public certificate is client trust |
| controller_worker | agent_client_cert, agent_client_key | Required conductor clientAuth leaf PEM paths, signed by the client CA |
| controller_worker | agent_client_ca | Required public client CA PEM delivered to each agent |
| controller_worker | health_manager_ips | Required management IPv4 literal list. No DNS on management |
| controller_worker | build_timeout | 900 seconds per cloud wait, agent health, bootstrap or engine server validation operation |
| controller_worker | build_poll_interval | 5 seconds |
| controller_worker | docker_volume_size_gb | 20 GiB, reserved in quota before Cinder creation |
| controller_worker | docker_volume_type | Operator Cinder type. Unset uses the cloud default |
| controller_worker | registry_volume_size_gb | 20 GiB controller registry volume, reserved in quota before Cinder creation |
| controller_worker | registry_volume_type | Operator Cinder type. Unset uses the cloud default |
| controller_worker | registry_port | 5000. Same value as the build-server security group rules |
| controller_worker | registry_image | Pinned `registry:3` reference from the controller image lock |
| controller_worker | registry_gc_interval | 3600 seconds |

Provisioning uses `[controller_worker] build_timeout`; deployment
recovery separately uses conductor timeouts. The controller data volume uses the size
accepted by the API and `[controller_worker] data_volume_type`. The
conductor does not count that size again. Controllers do not consume
the worker server quota.

Do not enable `[coolify] insecure_direct_url` for a provisioned stack.
Later engine calls use the controller's saved management IP, its agent
proxy, and the Barbican token reference. SDK HTTP payload logging is
disabled for Nova and Barbican because those payloads contain identity
material. This uses the documented
[keystoneauth adapter logger](https://docs.openstack.org/keystoneauth/latest/api/keystoneauth1.adapter.html).

The schema includes a nullable heartbeat secret reference and engine token
rotation checkpoints. Stack object versions before 1.7 omit rotation fields. Run
`coast-db-manage upgrade` before controller provisioning.

(m3-worker-and-build-vm-provisioning)=
### Worker and build VM provisioning

Server add uses the existing controller management transport and CA options,
plus `image_tag_worker`, `image_owner_id` and a matching role-aware VM plan
in `[plans] definitions`. Set `[coolify] ssh_user` to the golden image's
documented engine SSH user. The config drive installs the stack public key
for that user; private keys remain in Barbican.

The existing `data_volume_size_gb`, `data_volume_type`,
`docker_volume_size_gb` and `docker_volume_type` options also select the
worker/build volume layout. Each add accounts for both volumes separately.
Workers require `external_network_id`; build servers have no floating IP.

Remove is limited to servers without placed services or volume mounts.
It retains data volumes and deletes managed Docker volumes. Retained
volumes remain visible in the stack and consume quota. The separate
real-cloud volume-delete flow checks ownership, confirms Cinder absence
and releases quota only after deletion, using durable cleanup receipts. Review ERROR rows and tagged resources after a failed revert.
Do not replay a process-crashed attempt before operator inspection.

## Retained volume deletion waits and recovery

Set `[controller_worker] volume_delete_timeout` (default 300 seconds)
and `volume_delete_poll_interval` (default 5 seconds). Both must be at
least one second. These options bound the Cinder absence wait, not the
SDK connection timeout. A timeout keeps the volume row and charged quota;
it does not force storage deletion.

For known failures, resolve attachments and dependencies before another
authorized volume DELETE. For interrupted work, inspect the project-scoped
`delete.cleanup` event receipt and its exact Cinder reference. Do not clear
`RECONCILING` or substitute a volume by name. Stop the old worker before
operator-led replay. Use `DeleteVolume.execute` with the original accepted
resource and receipt revision, and `replay=True`, in the service's trusted
maintenance context. This is an internal recovery interface, not a tenant
API or automatic recovery service. Terminal receipt replay needs no cloud
request and retries pending notifications only. Keep completion receipts;
removing them also removes replay and accounting protection.

## Conductor host identity

`[DEFAULT] host` defaults to the local hostname. Keep it stable across
conductor restarts and distinct between conductor hosts. Startup releases
only interrupted stack tasks recorded with this identity .

(optional-admin-ssh-key-m3-c4ak)=
## Optional admin SSH key

`[controller_worker] admin_ssh_key_name` is a Nova keypair name. Its default
is empty, which disables operator SSH access. The operator must create the
keypair with the service user's credentials in the configured service
project before setting this option. Keep the private key outside Coast.
Coast does not store it or give it to cloud users.

When set, Coast passes the name as Nova `key_name` for new controller,
worker and build-server VMs, both during stack create and server add.
The guest image must support Nova public-key injection through cloud-init.
Use the image's default user for admin SSH. Coast enables that user in
the config drive; password login stays disabled. For workers and build
servers, use `ubuntu` with sudo on the golden image, not `root`. Coast reads
the public key through Nova's service-project keypair API and puts it only
in the default user's cloud-init `ssh_authorized_keys`. Automatic metadata
key import is disabled on these guests so the admin key cannot reach root.
Root's authorized-keys file is replaced with only the stack engine key when
`[coolify] ssh_user=root`; it is empty for a non-root engine SSH user.
Custom images must have a non-root default user with sudo and no separate
metadata-key copy hook. The supplied image elements have no such hook.
Controller key injection is unchanged.
This option does not replace the per-stack Coolify key stored through the
Barbican `ssh_key_ref` reference. It does not change existing VMs.

API 1.25 keeps management security groups restricted to agent HTTPS and
heartbeats. Setting this keypair option does not open management SSH.
Use the operator's console access for recovery. Environment NIC rules
still allow app engine server SSH to workers and build servers.
No portal access, credential handout or tenant API is added (Q104).

For Kolla, set `coast_admin_ssh_key_name` to this keypair name. It defaults
to empty. The role renders `admin_ssh_key_name` only when the value is not
empty. Do not also set it in `coast_controller_worker_options`.

This follows Octavia's
[amp_ssh_key_name option](https://docs.openstack.org/octavia/latest/configuration/configref.html)
and the SDK's
[Nova server API](https://docs.openstack.org/openstacksdk/latest/user/resources/compute/v2/server.html).

(stack-deletion-with-an-unreachable-engine-m4-c4ud)=
## Stack deletion with an unreachable engine

Whole-stack deletion continues cloud teardown when engine cleanup raises
`EngineUnreachable`. This includes connection and TLS failures, such as an
expired agent certificate, and the driver's HTTP 429 and 5xx responses.
HTTP 401/403 (`EngineAuthError`) and other engine errors still stop deletion.
The existing stack delete API and retry procedure are unchanged.

Coast records one durable `delete.engine.skip` stack event with outcome
`failure` and a fixed message. It does not report engine cleanup success or
clear engine references for the skip. A retry attempts engine cleanup again.
Cloud errors still set ERROR. The delete-end notification is sent only after
all required teardown steps complete; quota is not released for skipped
engine cleanup alone. A requested export must succeed before deletion.

The private registry VM and volume are removed by cloud teardown, so stack
deletion does not require repository GC. Individual service deletion still
requires GC. Volume ownership, attachment and dependency checks remain in
force. Requested offboarding snapshots keep their normal retention receipts.
Do not force-delete retained volumes or upgrade snapshots to complete this
flow. Review them through the existing operator procedures.

Ownership tags and metadata remain on resources until cloud deletion. The
janitor report stays read-only: live ERROR owners can exclude incomplete
cleanup from orphan results, and retained-volume rows and snapshot receipts
protect retained storage. A clean report alone is not proof of teardown.
Expired certificates use the operator recovery procedure below; TLS
verification remains required.

## Stack-delete snapshot retention (Q106)

The conductor checks expired offboarding snapshots at startup and on
`[conductor] deploy_reconcile_interval` (default 60 seconds). Each run
handles at most 100 rows, oldest expiry first. Cinder deletes are
asynchronous: later sweeps observe incomplete deletions without blocking
the recovery timer. Errors retain the receipt for another run.
The receipt is removed only after Cinder confirms that the snapshot is
gone. This does not change quota or billing semantics.

Retention starts when stack teardown completes, not when the snapshot is
created. Failed teardown keeps snapshots without expiry. Existing rows
without expiry remain for operator review; no default is applied to them.
For manual cleanup, review the owner project, stack and cloud snapshot
IDs in `stack_delete_snapshots`. Use service-project Cinder credentials
to restore or delete the selected snapshot. After confirming that Cinder
returns 404, an operator can remove only its matching Coast receipt using
the project's normal controlled database maintenance procedure. Do not
remove receipts for an unfinished teardown that can still be retried.


(s3-guest-export-m3-g4s3)=
## S3 guest export

Set `[export] backend = s3`, `s3_endpoint` to an HTTPS origin, `s3_region`
to the SigV4 region, and `container` to the bucket name. Do not put
credentials in the endpoint URL. Store a Barbican text secret whose payload
is JSON with exactly these fields:

```json
{"access_key_id": "...", "secret_access_key": "..."}
```

Set `s3_credentials_ref` to that reference. Coast does not read environment
variables, shared AWS files, or instance credentials. It does not accept a
session token in the secret. Addressing is path-style, so the endpoint host
can be a custom TLS name. The bucket must already exist. Coast does not
create it and does not install lifecycle rules.

The guest PUT is the same `POST /v1/export` request used for Swift. The
presigned PUT signs `Content-Type: application/gzip` and the host. The guest
sends `Content-Length` as well; that header is not signed. Download URLs are
presigned GET URLs. They contain the access key id in `X-Amz-Credential`.
Conductor logs mask that field, `X-Amz-Signature`, and
`X-Amz-Security-Token`. The secret key is not part of the URL and must not
be logged.

Presigned URL lifetime cannot exceed seven days. `url_ttl` and
`archive_timeout` stay inside that limit with the defaults. A larger
computed lifetime fails closed.

If `backend` changes, housekeeping and restart recovery do not delete
objects for the previous backend. Set `backend` back to that value and let
the sweep run. Upload is guest-only: `upload_via` accepts only `guest`, and
a `conductor` value fails at config load. Conductor relay upload is planned
for a later release.

(interrupted-swiftrgw-and-s3-export-recovery-m3-c4xr-g4s3)=
## Interrupted Swift/RGW and S3 export recovery

Restart the complete conductor service on the same configured host.
The startup scan records interrupted exports for the configured
`[export] backend` before stack task recovery. The existing
`[conductor] deploy_reconcile_interval` timer handles
at most 100 attempts per run, with failed attempts queued for later runs.
There is no cross-host takeover and no new recovery timer or config option.

Resume is attempted immediately. For started exports, clone, snapshot and
object deletion wait for the greater of `[export] archive_timeout` and
`[conductor] build_timeout` after restart. This protects devices and object
names from old in-flight operations. Allow this grace period plus timer
intervals and cloud delete waits before expecting ERROR. Recovery discards
the interrupted archive; cloud users must request a new export afterwards.

Failed cleanup keeps the export non-terminal and retains any held stack task.
Check agent mTLS reachability and Docker resume, Cinder availability,
clone attachment ownership, and Swift/RGW or S3 access. Fix the cause
and let the next timer run retry. Do not clear the task or delete a
source volume to force completion. Another same-host restart retains
cleanup eligibility.
Terminal exports are not swept by this recovery process; normal retention
housekeeping still handles expired archives for the configured backend.
Conductor-relay uploads remain fail-closed. Objects for a backend that is
not the configured value wait until an operator selects that backend again.

Cloud discovery and deletion use the documented
[Cinder proxy API](https://docs.openstack.org/openstacksdk/latest/user/proxies/block_storage_v3.html)
and clone detachment uses the
[Nova proxy API](https://docs.openstack.org/openstacksdk/latest/user/proxies/compute.html).

(service-reconciliation-m4-q110q112)=
## Service reconciliation

`[conductor] service_reconcile_interval` defaults to 600 seconds (minimum 1).
Passes scan up to 100 candidates with a 30-second scheduling budget; current
HTTP calls retain their finite timeout/retry limits. Pages continue every
60 seconds (or the interval if smaller); completed rounds wait the interval
plus up to 10 percent jitter. Large rounds take longer. Only ACTIVE services
on idle ACTIVE stacks take the existing service CAS. Known env/domain drift
is repaired; unknown/unsupported settings produce sanitized activity errors.
Running mismatch retains ACTIVE intent without start/restart/deploy calls.
Failures back off up to one hour plus jitter; the 100-entry cache can reset
on eviction/restart. Inspect service activity and
`coast.service.reconcile.{start,end,error}` notifications. Healthy checks are
quiet. Correct access/settings through existing operator procedures; no
missing-resource recreation or task recovery changes are included.


Compose domain reconciliation uses the pinned engine's
`docker_compose_domains` target map. Non-empty domains require a sole
existing target with domains, or a sole map entry when none has domains.
Empty or ambiguous maps require an operator check; Coast does not parse
YAML or choose an unlisted service. Empty desired domains clear all known
targets. Missing/malformed observations fail closed. Existing engine targets
are a prerequisite for assigning domains to a new compose workload.
## Refresh default hostnames

`coast-manage dns refresh-default-domains` updates the stored default
hostname and the published domain list. It does not redeploy.

```bash
coast-manage --config-file /etc/coast/coast.conf dns \
  refresh-default-domains
coast-manage --config-file /etc/coast/coast.conf dns \
  refresh-default-domains --dry-run
```

`--service` limits the run to one service. This local command needs the
Coast configuration and database. It has no tenant API route and adds no
policy. Do not give tenants these files. Stdout is one JSON list. Logs
go to stderr. `--dry-run` reports changes and does not save or publish
domains. A failed service is `ok: false`. The exit code is 1 when any
row failed.


(read-only-janitor-report-m4-c4cr)=
## Read-only janitor report

Operators can compare cloud inventory with Coast records:

```bash
coast-manage --config-file /etc/coast/coast.conf janitor-report
```

This local operator command requires access to the Coast configuration,
database and configured service-project credentials. Do not give cloud
users or tenants access to these files. It uses `[service_auth]` and the
existing `[nova]`, `[neutron]`, `[cinder]` and `[barbican]` endpoint options.
It has no tenant API route and adds no policy. It does not delete resources,
change DB rows, fetch secret payloads or call guests or the engine.

The command lists Nova servers, Cinder volumes and snapshots, Neutron
ports, networks, subnets, routers, security groups and floating IPs, and
Barbican secret metadata. Nova and Cinder lists do not use all-project
mode. Neutron lists filter by the authenticated service project. Barbican
lists use the same service-project credentials. SDK pagination is retained.
This covers the resource kinds in `tools/e2e/janitor-check.sh`; that script
remains the separate post-delete check for specified stack/server IDs.

A candidate must have an exact `coast` tag, Cinder `coast=true` metadata,
or a name that starts with the configured `resource_name_prefix` followed
by `-`. The prefix uses the same normalization as resource creation.
An empty normalized prefix is an error. Unmarked resources are not
reported. Barbican secrets use the fixed `coast:` name prefix; see
[Secrets and Barbican](secrets.md#auditing). New secrets have ownership
metadata and a private read ACL.

The report excludes candidates with a direct DB reference, a live owner
UUID in a name/tag/metadata, or a known attachment/network/volume reference.
Live means not soft-deleted; PENDING, ERROR and busy records still count.
All tenant projects in the Coast DB are checked. Kept volume rows protect
retained volumes. Snapshot retention receipts protect their recorded
snapshots even after stack deletion. Saved secret references and historical
deployment specs also protect secrets. Live-owner protection is conservative:
a marked infrastructure resource can be excluded even without its own direct
DB reference. A `coast:` secret instead requires both live ownership and a
saved DB reference. Missing secret references appear as UUIDs in the
`missing_secrets` list and count as findings for exit status 1.

The command reads DB references before and after cloud listing and keeps
both sets. This reduces false reports during provisioning or deletion,
but it is not an atomic cloud/DB snapshot. Run it again after active flows
finish and check each candidate before any separate operator action.
Do not use the output as an automatic deletion list. The command is read-only and has no periodic deletion worker.

Output is one JSON object with `complete`, per-kind `scanned` counts, and
an `orphans` list sorted by `resource_type` and `id`, plus `missing_secrets`. Candidate entries
contain only the resource type and ID, not names, metadata or secret URLs.
The command sets `use_stderr=true` so console logs do not mix with JSON.
If `log_config_append` is set, oslo.log ignores that option; configure the
custom console handlers to use stderr, not stdout.
Exit status is 0 for a complete report with no candidates, 1 for a complete
report with candidates, and 2 if a DB/cloud/configuration query fails. On
failure, stdout is empty and stderr contains a fixed error, with no
exception details. A failed report must not be read as a clean inventory.

Certificate rotation uses the separate agent certificate-update flow below.

SDK list contracts:
[Compute](https://docs.openstack.org/openstacksdk/latest/user/proxies/compute.html),
[Network](https://docs.openstack.org/openstacksdk/latest/user/proxies/network.html),
[Block Storage](https://docs.openstack.org/openstacksdk/latest/user/proxies/block_storage_v3.html),
[Key Manager](https://docs.openstack.org/openstacksdk/latest/user/proxies/key_manager.html).


(agent-certificate-rotation-m4-c4rt)=
## Agent certificate rotation

Set `[health_manager] cert_rotation_interval` (default 3600 seconds) for the
bounded certificate sweep. Health-manager and conductor both need the existing
`[controller_worker] agent_client_cert`, `agent_client_key`, management socket
configuration, and `[certificates] ca_certificate` (the server CA public file).
The conductor alone needs the existing server-CA signing key and its configured
passphrase to issue replacements. No new CA is generated.

Config drives for all roles set `[agent] server_ca=/etc/coast/server-ca.crt`
and deliver the **existing server CA public certificate**, not its private key.
The agent endpoint never accepts or changes this trust file. It continues to
use `client_ca` for incoming mTLS client authentication. Missing `server_ca`
configuration rejects rotation with HTTP 412 and a clear configuration error,
before changing the installed pair, saved pair or pending marker. Install the
existing public certificate, set the option, and restart the agent if needed.
Follow the recovery procedure below for the durable failure receipt; verify
the correction before allowing another attempt.

Agent rotation state is under `[agent] state_path/certificates` (owned 0700).
`previous.crt` and `previous.key` are the saved previous pair (0600).
A `pending` marker means installation is uncertain: later TLS handshakes fail
closed and agent restart fails. Stop the agent, inspect the installed pair and
saved pair, and restore a matching, valid serverAuth identity signed by the
existing server CA, with the recorded UUID CN and management IP SAN. Keep
private-key files 0600. Remove `pending` only after both files are verified,
then restart and authenticate a fresh management connection.

Control-plane rotation receipts are ordinary existing server activity events,
with action `rotate_certificate`, keyed by server UUID and old serial. A failed
or interrupted receipt prevents automatic reissuance. Rotation claims only the
server RECONCILING task. It does not take or release the stack task, so a
deployment lock cannot starve renewal and rotation cannot clear that lock.
An interrupted server RECONCILING task stays held. There is no automatic
takeover. Follow
[Expired agent certificate recovery](#expired-agent-certificate-recovery)
for controlled repair. Leave stack tasks and unrelated server tasks unchanged.
Keep failure history. Do not change receipts or tasks while a conductor attempt
is running.
No new recovery API is added.

When rotation fails, look in the `coast-conductor` log for the ERROR
line that names the exception class, the Coast location, the server
id, the stack id, and whether the failure was before or after the
start receipt.


## Expired agent certificate recovery

Q136 (James, 2026-10-13 16:36 CT): use this manual operator procedure.
No new `coast-manage` command, tenant API, or warning config option is added.
Console or admin SSH repair comes first. Controller replacement with a fresh
config-drive identity is separate, later work.

An expired leaf prevents management mTLS health checks and engine cleanup.
Never disable TLS verification to repair it. Do not use tenant credentials,
the engine SSH key, or the engine token. Do not create a new CA with
`tools/gen-ca.sh --force`. Leave server/client CA trust, Coast client identity,
bootstrap state, and heartbeat keys unchanged.

### Stop concurrent work and record the identity

1. Arrange an operator maintenance window. Stop all conductor and
   health-manager workers that can rotate or change the server. Wait for
   in-flight operations to finish. Stopping the periodic sweep alone is not
   sufficient: queued or running conductor work can still change files.
   Keep workers stopped until TLS verification and DB repair are complete.
2. Through controlled operator DB access, record the server's `project_id`,
   `stack_id`, `id`, `compute_id`, `mgmt_ip`, `agent_cert_serial`, expiry,
   status, and task. Record the failed/interrupted rotation event IDs,
   request IDs, and messages. The certificate CN is the Coast `servers.id`
   UUID, **not** Nova `compute_id`. Its IP SAN is the recorded `mgmt_ip`.
3. Confirm that the guest and management port belong to that record. Stop
   if identity or ownership is uncertain, or delete/failover is in progress.
   Use rotation events, request IDs, worker logs, and maintenance records to
   prove ownership of a server `RECONCILING` task. The task value alone is
   not proof. Stop on ambiguous ownership; never force-clear a task.
4. Use the operator console or existing
   [admin SSH path](#optional-admin-ssh-key-m3-c4ak). Verify the SSH host key
   and use the image's default user with sudo from the management network.
   If admin SSH was not configured, use the console. Do not open a tenant
   access path. Avoid session recordings or shell history that retain keys;
   never paste private material into tickets or logs.

### Issue and install the replacement leaf

1. On a trusted control-plane host, load the existing Coast **server** CA
   certificate/key from `[certificates]`. Check that the key matches the CA,
   the CA is currently valid, and it is trusted by this guest and the Coast
   management client. If the CA has expired, stop: trust repair is outside
   this procedure. Never copy its private key or passphrase to the guest.
2. Load the operator's Coast configuration and use the existing Python
   issuer `coast.common.tls.issue_server_certificate(server_id,
   management_ip, conf=conf, validity_seconds=seconds)` with the recorded
   UUID and IP. Set `seconds` to the smaller of configured
   `cert_validity_time` and the remaining whole seconds to the CA's UTC
   expiry. Allow a safety margin for issuance time. Check that the issued
   leaf expires no later than the CA. Stop if no positive lifetime remains.
   The helper does **not** cap lifetime at CA expiry itself: the operator
   must calculate and verify the limit. Use the
   [X.509 reference](https://cryptography.io/en/stable/x509/reference/)
   for UTC validity and certificate inspection.
3. Keep the returned PEM pair in a new operator-owned staging directory,
   mode 0700, preferably private tmpfs. Set umask 077 and file modes 0600.
   Do not print the key or store it in the DB. Record only the new decimal
   serial, CN, IP SAN, and UTC expiry. Reuse the pair after interrupted
   delivery; check the guest before issuing any further replacement.
4. Stop `coast-agent` with the deployment's service manager. Inspect the
   installed pair and rotation state. Correct the original failure cause,
   including `[agent] bind_ip` if needed; it must match the recorded IP.
   Preserve the previous pair in private storage for investigation.
5. Before installation, validate the replacement CA signature, UUID CN,
   single management IP SAN, current validity, serverAuth usage, non-CA
   basic constraint, and private-key match. Deliver through verified admin
   SSH or controlled console file transfer. Install at the existing
   `[agent] server_cert` and `server_key` paths with the agent's existing
   owner and mode 0600. Keep staging/backup directories mode 0700.
   Replace both files durably while the agent is stopped, then validate
   the installed pair again.
6. If `[agent] state_path/certificates/pending` exists, remove only this
   certificate-install marker after both files pass validation. Leave token
   and bootstrap markers and heartbeat checkpoints unchanged. Restart the
   agent. If startup fails, keep workers stopped and investigate.

### Verify a fresh authenticated connection

From a configured control-plane management host, use the existing management
socket or namespace, Coast client certificate/key, server CA, recorded IP,
and `[controller_worker] agent_port`. Keep CA and IP verification enabled.
Use a new connection, not a pooled connection or saved TLS session.

The existing Python `coast.conductor.agent_client.AgentClient(conf,
management_ip)` provides `certificate_connection(server_id)` for a fresh
authenticated connection. Compare the returned leaf with the installed
replacement: exact certificate bytes, decimal serial, UUID CN, management IP
SAN, and UTC expiry must match. Its signature, validity, and serverAuth checks
must pass. Then use a fresh client to call `request('GET', '/health')` over
mTLS. Require HTTP success and the documented `status: ok` response.
`/health` proves agent liveness, not engine readiness.

If either check fails, do not update the DB or close the rotation record.
Keep workers stopped and investigate files, routing, bind address, trust,
and the original failure. Never retry with insecure TLS.

### Perform the controlled DB repair

Use the operator's controlled DB recovery procedure with a backup and an
approved maintenance record. There is no recovery CLI or automatic ownership
check. Keep workers stopped. Lock and re-read the server, stack, and affected
rotation events in one transaction. Filter by recorded project, stack, and
server IDs. Stop if identity, serial, task, or lifecycle state changed.

1. Update only `servers.agent_cert_serial` and `agent_cert_expires_at` with
   the verified decimal serial and UTC expiry. Leave status, fault, IP,
   stack task, and unrelated desired state unchanged.
2. Close only the identified failed/interrupted rotation record through
   existing event fields. Append a closure note to its `message`, with the
   recovery request ID and verified serial/expiry. Preserve the original
   message and `outcome`; never label the failed rotation successful.
   Keep both the start receipt and completion/failure event. If the message
   limit cannot hold both history and the note, leave the original message
   intact and put the closure reference in the audit event instead.
3. Add an audit event using existing `events` fields: fresh UUID, the same
   project/stack/server, `resource_type=server`, `action=recover_certificate`,
   `outcome=success`, and the recovery request ID. Its non-secret `message`
   records the operator maintenance reference, old/new serials, verified
   expiry, affected event IDs, and task ownership evidence.
4. Release the server `RECONCILING` task to `NONE` only when evidence proves
   it belongs to this rotation and no attempt is running. Leave `NONE`
   unchanged. Stop on ambiguous ownership. Never clear another operation's
   task or the stack task.

The rotation blocker is an event UUID derived from the server UUID and **old
serial**. Current code checks for receipt existence, not its outcome or closure
note. Publishing the verified **new serial** selects a new receipt key. Keep
the old receipt as history; do not delete it to bypass the check. Commit the
identity update, closure note, audit event, and any proven task release
together. Roll back on a failed check.

Read back the committed values. Securely remove control-side and guest private
key staging copies and transfer artifacts using the deployment's disposal
procedure. Plain deletion does not guarantee erasure on persistent or snapshot
storage. Keep only the active pair and approved private investigation backups.
Resume workers and check authenticated health and normal rotation eligibility.
Verify engine access separately before retrying engine-dependent operations.


(engine-upgrade-by-controller-rebuild-m4)=
## Engine upgrade by controller rebuild

Run `coast-db-manage upgrade` before starting services on the current schema. Controller golden images must include the updated agent with the
controller-only engine quiesce/resume routes. Publish a new image only after
its engine version is in the driver's exact supported list and its locked
archive and API contracts pass release checks. This feature does not add a
new supported Coolify version.

The customer or an admin submits
`{"upgrade":{"image_id":"<published controller image UUID>"}}` to
`POST /v1/stacks/{id}/action`. Coast rebuilds the controller, then each
worker and build VM, one at a time. The first failure stops the sequence.
Publish the worker image for the target engine version and select it with
`[controller_worker] worker_image_id`, or keep exactly one active image
with the configured worker tag. A newer publication of the same engine
version is allowed; the controller image must have a later Glance creation
time and a different UUID. Each replacement retains its volumes, ports
and addresses. The owner/admin rollback
body is `{"upgrade_rollback":{}}`; it restores the previous image with three
snapshot clones. See [the engine contract](design/engine-upgrade.md).

Snapshots remain billable until the next successful upgrade replaces them.
Rollback retains displaced volumes; failed attempts retain partial snapshots
and clones. Use the private stack checkpoint, cloud ownership metadata, and
`coast-manage janitor-report` to inspect these resources. Do not remove them
until their recovery use is ruled out. A lost VM-create reply can require
operator inspection before rollback; do not boot the old image against disks
that the new engine may have migrated. Stack deletion removes verified
stack-owned upgrade snapshots and displaced rollback disks after fencing
the guests. Delete-retention snapshots keep
their retention policy. Unowned snapshots still block deletion.


## Template catalog cache

`[templates] cache_ttl` sets the raw template catalog cache lifetime in
seconds (default 3600, minimum 0). Set it to 0 to disable caching.
Each API worker keeps a separate in-memory cache keyed by driver and
engine version. A thread lock protects cache reads and fills. No shared
cache service is required because the catalog comes from static,
versioned package files. Restart API workers after replacing catalog files
if the new files must take effect before the TTL expires.
Project authorization runs on each request. Catalogs have no workload safety filter. List responses provide
an ETag for the visible list and support `If-None-Match` with HTTP 304.

## Git push webhooks

Set `[webhooks] secret_key` to a long random value to turn on
`/v1/hooks/...`. Each app token is HMAC-SHA256 of that key, the service ID and its webhook nonce,
so rotating the key rotates every app URL. Set `[webhooks] public_url` when
clients reach the API through a different host than the request shows.

## Engine API token rotation

To start or retry rotation:

```console
$ coast-manage --config-file /etc/coast/coast.conf rotate-engine-token <stack>
```

Automatic rotation uses
`[secrets] engine_token_rotation_days` (30; 0 disables) and
`rotation_max_concurrent` (2). See [the rotation protocol](secrets.md#rotation)
for eligibility, checkpoints, locking and retry behavior. Rebuild guest
images with the create/revoke agent protocol before enabling rotation.

## Compose editor size limit

`[api] max_compose_bytes` sets the maximum UTF-8 YAML size accepted by
the editor (default 10000 bytes; minimum 1). The effective limit is the
smaller of this option and `[secrets] max_secret_bytes` (default 20000).
Oversized content returns HTTP 400 before storage. See
[secret payload limits](secrets.md#kolla-configuration) for env values,
file mounts and Barbican request overhead.
Save does not deploy; use the service deployments API after saving.


## Database backup operations

Backups require Swift-compatible object storage in the cloud (Swift or RGW
with its Swift API enabled). They are not verified end to end in 0.1.0;
full verification is tracked for 0.1.1 in issue #124.

See [Database backups](contributor/specs/database-backups.md) for the
API contract, durable operations, storage permissions, secrets and limits.

## Service metrics retention

Workers collect container CPU and memory every `[agent] metrics_interval`
seconds (default 30, minimum 30). A larger interval leaves gaps in the
30-second series. No extra agent credential or listener is needed.
The agent uses up to `[agent] metrics_max_workers` (default 8) concurrent
reads so that one container does not delay the whole worker. Increase this
limit for workers with enough containers to exceed the sample interval.
The agent uses Docker Engine API 1.44 `stats?stream=false`, which waits for
two CPU readings. `one-shot=true` would skip that second reading.
See the [Docker statistics API](https://docs.docker.com/reference/api/engine/version/v1.44/#tag/Container/operation/ContainerStats).

The health manager runs rollup and cleanup every `[metrics]
cleanup_interval` seconds (default 300). Each pass processes at most
`[metrics] batch_size` buckets per rollup stage and that many expired hourly
rows (default 100, maximum 1000). Monitor DB growth and increase the batch
size or decrease the cleanup interval if a backlog grows. Each bucket is
committed atomically. Concurrent receivers, sweepers and service deletion
serialize on the service row. A failed pass retries on the next timer.

| Option in `[metrics]` | Default seconds | Purpose |
| --- | --- | --- |
| `detail_retention` | 86400 | Keep 30-second samples for 24 hours |
| `five_minute_retention` | 604800 | Keep 5-minute averages to 7 days |
| `hourly_retention` | 2592000 | Keep hourly averages to 30 days |

These values are also their minimums, so the API windows remain available.
Retention ages use the original sample time. Only complete UTC buckets
roll up, so detail can remain for up to one extra target bucket plus the
cleanup delay. Increase these values only with a storage capacity plan.
Apply schema migration `0009_service_metrics` before starting the updated
health manager. Service deletion removes all of its metrics in the same
transaction. Samples do not change desired state or emit state events.

## Customer DNS configuration

If Designate is added to a cloud after Coast is running, restart the Coast
API and conductor. For kolla-ansible, run
`kolla-ansible reconfigure -t coast`, or
`docker restart coast_api coast_conductor`. Until then, Coast reports DNS
as not available.

API 1.22 can manage zones in customer projects. The operator-zone options
remain off by default and continue to use the service project.

| Option | Default | Purpose |
|---|---|---|
| `[dns] credential_provider` | `service_account` | customer-project sudo, or `trust` for linked-zone Keystone trusts |
| `[dns] record_ttl` | `300` | TTL for all Coast-managed recordsets |

Configure `[designate]` region, catalog interface and TLS settings as for
other cloud clients. For the default provider, grant Designate sudo to the
Coast service user. For trust authentication, configure Keystone v3 service
credentials and supply a customer-project trust reference on each link.
See [customer DNS](networking.md#customer-designate-zones-issue-21-api-122).
Coast does not use Neutron DNS integration or create reverse records.
DNS quotas belong to the customer project. Allow for negative DNS caching
and certificate authority rate limits when creating many hostnames.


## Worker ingress management (API 1.25)

`[controller_worker] ingress_rules_per_stack` limits custom worker ingress
rules per stack (default 50, minimum 0). The existing network repair timer
applies changes; keep it enabled for customer rules to reach Neutron.
The agent management group now always has only its two service rules;
`admin_ssh_key_name` installs a key but no longer opens management TCP/22.
Use the operator's console access for guest recovery.


### Synchronous conductor RPC locks (issue 47)

When using `[oslo_messaging_rabbit] use_queue_manager=true`, configure
`[oslo_concurrency] lock_path` to a private directory writable by the Coast
service user. Reply queue allocation requires external locks before sending
synchronous reads. Without this setting, storage reads for deployed apps
fail before reaching the conductor. Empty stacks do not need that RPC call.
The Kolla role sets `/var/lib/coast/locks`; container startup creates it with
mode 0700 and Coast ownership. Non-Kolla operators must create their chosen
directory with equivalent permissions. Cast-only handoffs do not exercise
reply queue allocation.

### Database public port allocation

`[databases] public_port_min` and `public_port_max` bound automatic public
port allocation (inclusive; defaults 15432 and 15999). Both accept ports
1024-65535. Configure the minimum no greater than the maximum. Explicit
ports can be outside this range, subject to reserved ports and collisions.
