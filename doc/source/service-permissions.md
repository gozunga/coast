# Service-user OpenStack permissions

This inventory covers the service-session call sites for issue #73 at
OpenStack 2026.1, the release selected by the Kolla overlay and `tox.ini`.
Policy references below use the upstream `stable/2026.1` implementations,
with secure RBAC defaults and Keystone's standard role implications
(`admin` implies `manager`, `member` and `reader`). Operators must check
local policy and role implications before changing grants. `service` is
not a substitute for `member`; no Coast call needs a system-scoped token.

## Ownership and default grant

`[service_auth]` owns infrastructure in the service project. A tenant's
`project_id` in tags, Cinder metadata or ownership descriptions is
attribution, not a Keystone ownership assignment. No infrastructure create
sets a tenant `project_id`. RPC strips tenant tokens. Switching these
calls to tenant tokens would change Q7 and the ownership contract, so this
card does not do so. See [security](security.md#service-credentials).

Swift tenant-account database backups additionally need `ResellerAdmin`
with Swift's default middleware configuration. **`admin` alone is not
enough.** This is an optional feature grant, not a new default. Kolla does
not silently grant reseller access. RGW has its own authorization settings;
it must be validated separately against the configured backend.

## Why the Coast service user has admin

For 0.1.0, the Coast service user keeps the Keystone `admin` role in the
service project (`coast_keystone_user_roles: [admin]`). This is the 0.1.0
release decision.
The default `[dns] credential_provider = service_account`, registered in
`coast/common/opts.py`, selects `ServiceAccountProvider` in
`coast/network/dns_credentials.py`. It sends `X-Auth-Sudo-Project-ID` with
the customer project ID for the linked-zone calls in
`coast/network/customer_dns.py` (`CustomerDNS.zones`, `ensure`, `delete`).
Designate's default `use_sudo` rule allows only `role:admin`; service-project
`member` alone cannot manage these customer DNS zones. See the
[Designate policy source](https://github.com/openstack/designate/blob/stable/2026.1/designate/common/policies/context.py)
and the cross-project inventory below.

The grant is project-scoped on the service project, not a system-scoped
assignment. Nevertheless, upstream default admin rules in Nova, Neutron,
Cinder, Glance, Designate and Barbican authorize cross-project reads and
writes in many cases, subject to each service's policies and ownership or
ACL checks. **This is effectively cloud admin.** Protect the credentials
accordingly: Kolla's `coast_keystone_password` comes from `passwords.yml`
and is rendered into `[service_auth]` in control-plane `coast.conf` files.
The role renders those files with mode 0600; the Coast service processes
and privileged host/container administrators can read them. Restrict the
deployment password file and configuration copies to authorized operators.
See [service credentials](security.md#service-credentials) and
[secrets outside Barbican](secrets.md#secrets-outside-barbican).

Other conditional admin dependencies already recorded in the inventory
are private image/flavor visibility without service-project access,
zero-root-disk image boots, admin locks and unlock overrides, management
security-group rules in another project, fixed-IP/port-security operations
without network ownership, and Designate recordsets below the server's
minimum TTL (`use_low_ttl`). These must be addressed before removing admin;
ordinary service-project CRUD alone does not establish that it is safe.
Swift tenant-account backups separately require `ResellerAdmin` under the
default middleware configuration; admin does not supply that permission.

### Alternatives after 0.1.0 (untested)

These alternatives are not the default and are untested for 0.1.0.
For an operator testing a reduced set after 0.1.0,
`[member, swiftoperator]` covers service-project CRUD and Swift exports only when management resources
belong to that project, images/flavors are accessible, image boots have
nonzero root disks, and DNS TTL meets the server minimum. Add
`ResellerAdmin` only if tenant-account database backups are enabled. This
role has account-wide powers; it is not a narrow backup permission.

Alternatively, `[dns] credential_provider = trust` selects `TrustProvider`
in `coast/network/dns_credentials.py`. Supply a pre-existing linked-zone
trust ID delegating member access in the customer project to the Coast
service user as trustee. The provider checks the authenticated project and
sends no sudo header. Coast neither creates nor revokes these trusts.
This removes the customer-DNS sudo dependency, not the other conditional
requirements in the inventory; DNS TTL must meet the Designate minimum.

To retain service-account sudo without Keystone admin, an operator can
override Designate policy to grant `use_sudo` and only the needed DNS rules
to a dedicated role such as `coast_dns`. The inventory requires:

```yaml
use_sudo: "role:admin or role:coast_dns"
get_zone: "role:admin or role:coast_dns or role:reader and project_id:%(project_id)s"
find_zones: "role:admin or role:coast_dns or role:reader and project_id:%(project_id)s"
get_recordsets: "role:admin or role:coast_dns or role:reader and project_id:%(project_id)s"
find_recordsets: "role:admin or role:coast_dns or role:reader and project_id:%(project_id)s"
create_recordset: "role:admin or role:coast_dns or role:member and project_id:%(project_id)s"
update_recordset: "role:admin or role:coast_dns or role:member and project_id:%(project_id)s"
delete_recordset: "role:admin or role:coast_dns or role:member and project_id:%(project_id)s"
```

These rules authorize cross-project DNS broadly, just as sudo does today.
If low TTLs are required, also extend `use_low_ttl` to that dedicated role;
otherwise retain the default rule. Prefer trusts for delegated access.
This card installs no external-service policy overrides. For conditional
Nova zero-disk image boots, `os_compute_api:servers:create:zero_disk_flavor`
would need a dedicated-role override, or choose nonzero-root-disk plans.
For private operator images/flavors, share/grant access to the service
project instead of weakening list policies. For management resources,
service-project ownership avoids overrides to the inventoried Neutron rules.
Swift's reseller requirement is middleware configuration, not an oslo.policy
rule; changing it does not narrow account-wide access. Moving backups into
the service account would change their storage contract and is outside #73.

Grant the dedicated role to the Coast service user in the service project
and set `coast_keystone_user_roles: [coast_dns, member]`, adding only any
feature-specific roles required above. Kolla `service-ks-register` only
adds grants; removing `admin` from the list **does not revoke it**. After
preparing the policies and resource access, manually revoke the existing
`admin` grant. Verify the resulting credentials with
`tools/check_service_permissions.py`, including the customer-project DNS
probe, and a DNS create/read/update/delete round-trip. The checker is
read-only and cannot prove DNS writes; checks made while admin is still
granted do not validate the reduced role set. Never grant system scope to
work around a project-scoped policy failure. Agent credentials stay
`coast_agent` only.

Dropping admin, through a Designate policy override, is planned after
0.1.0.

## Inventory: service-project resources

Paths in the tables are relative to `coast/`. A row lists SDK methods,
then all direct callers sharing the same policy. Wait helpers repeat GETs;
find helpers may list then GET. No payload returned by these calls should
be printed by a permission checker. R = project `reader`; M = project
`member` (including implied R). These are minimum defaults for resources
owned by the authenticated service project, unless a condition is stated.

### Nova and Glance

Source: [Nova policies](https://github.com/openstack/nova/tree/stable/2026.1/nova/policies)
and [Glance image policies](https://github.com/openstack/glance/blob/stable/2026.1/glance/policies/image.py).

| SDK calls | File:function | Upstream policy | Minimum |
|---|---|---|---|
| `servers`, `get_server`, `wait_for_server`, `wait_for_delete` | `conductor/controller.py:wait_server,delete_server`; `conductor/worker.py:owned_guest,replacement,stopped,delete_guest`; `conductor/engine_upgrade.py:RebuildCloud.wait_server`; `conductor/server_remove.py:guest`; `conductor/stack_teardown.py:_coolify`; `common/janitor.py:report` | `os_compute_api:servers:index`, `:detail`, `:show` | R; no `get_all_tenants` |
| `create_server` with tags, config drive, explicit ports and volume mappings | `conductor/controller.py:boot` (inherited by worker/upgrade flows) | `os_compute_api:servers:create`, `:create:attach_network`, `:create:attach_volume` | M; zero-root-disk image boots additionally require `os_compute_api:servers:create:zero_disk_flavor` (admin) |
| `delete_server` | `conductor/controller.py:delete_server`; `conductor/worker.py:delete_guest` | `os_compute_api:servers:delete` | M |
| `stop_server`, `start_server` | `conductor/worker.py:stopped,restore`; `conductor/server_failover.py:revert_fence` | `os_compute_api:servers:stop`, `:start` | M |
| `lock_server`, `unlock_server` | `conductor/worker.py:stopped,delete_guest,restore` | `os_compute_api:os-lock-server:lock`, `:unlock`; `:unlock:unlock_override` when overriding an admin lock | M for owner locks; admin for an admin lock or override |
| `server_interfaces`, `create_server_interface`, `delete_server_interface` | `conductor/worker.py:detach,restore` | `os_compute_api:os-attach-interfaces:list`, `:create`, `:delete` | R for list; M for create/delete |
| `volume_attachments`, `create_volume_attachment`, `delete_volume_attachment` | `conductor/worker.py:detach,restore`; `conductor/exports.py:ExportFlow.create,cleanup` | `os_compute_api:os-volumes-attachments:index`, `:create`, `:delete` | R for list; M for create/delete; no swap API |
| `find_flavor` (list/show) | `conductor/controller.py:image_flavor`; `conductor/worker.py:image_flavor`; `api/v1/flavor_controllers.py:resolve`; `api/v1/stack_controllers.py:flavor_to_plan`; `api/v1/resize.py:selection` | No oslo.policy rule for [list/show](https://github.com/openstack/nova/blob/stable/2026.1/nova/api/openstack/compute/flavors.py); Nova flavor visibility filtering (private access is not `os-flavor-access`, which governs granting/listing access) | Any authenticated project token for public or project-accessible flavors; admin can see otherwise private flavors |
| `get_keypair` (public key only) | `conductor/worker.py:config_drive` | `os_compute_api:os-keypairs:show` = admin or matching `user_id` | Authenticated keypair owner; keypairs are user-owned, not project-shared |
| `images` | `conductor/controller.py:select_image` | `get_images`; Glance visibility filtering | R; operator-owned images must be public/shared with the service project, or admin is needed to see another project's private image |

### Neutron

Source: [Neutron policy modules](https://github.com/openstack/neutron/tree/stable/2026.1/neutron/conf/policies).
The dynamic helpers `network/stack.py:_find,_ensure,_delete` and
`network/tags.py:create_tagged` expand to the named resource methods below.

| SDK calls | File:function | Upstream policy | Minimum |
|---|---|---|---|
| `networks`, `get_network`, `create_network`, `delete_network` | `network/stack.py:create,delete,_find,_ensure`; `network/fip.py:ensure`; `common/janitor.py:report` | `get_network`, `create_network`, `create_network:port_security_enabled`, `delete_network` | R read; M write; network owner for port security |
| `subnets`, `get_subnet`, `create_subnet`, `delete_subnet` | `network/stack.py:management_cidrs,create,delete,_find,_ensure`; `conductor/controller.py:check_router`; `common/janitor.py:report` | `get_subnet`, `create_subnet`, `delete_subnet` | R read; M write on owned network |
| `routers`, `get_router`, `create_router`, `delete_router`, `add_interface_to_router`, `remove_interface_from_router` | `network/stack.py:create,delete,_find,_ensure`; `conductor/controller.py:check_router`; `common/janitor.py:report` | `get_router`, `create_router`, `create_router:external_gateway_info`, `create_router:external_gateway_info:network_id`, `delete_router`, `add_router_interface`, `remove_router_interface` | R read; M write; external network must be accessible; no explicit `enable_snat`, HA or distributed flag |
| `ports`, `get_port`, `create_port`, `delete_port` | `conductor/controller.py:port,delete_ports`; `conductor/worker.py:retained_ports,detach`; `network/stack.py:create`; `network/fip.py:ensure`; `common/janitor.py:report` | `get_port`, `create_port`, `create_port:fixed_ips`, `create_port:fixed_ips:subnet_id`, `create_port:fixed_ips:ip_address`, `create_port:port_security_enabled`, `create_port:security_groups`, `delete_port` | R read; M write on owned networks; explicit fixed IP and port-security fields need network-owner M or admin/service |
| `security_groups`, `get_security_group`, `create_security_group`, `delete_security_group` | `network/stack.py:create,delete,_find,_ensure`; `network/fip.py:ensure`; `common/janitor.py:report` | `get_security_group`, `create_security_group`, `delete_security_group` | R read; M write |
| `security_group_rules`, `create_security_group_rule`, `delete_security_group_rule` | `network/stack.py:_sync_rules,repair_peers,delete` | `get_security_group_rule`, `create_security_group_rule`, `delete_security_group_rule` | SG-owner R read; SG-owner M write; admin when configured management SG belongs to another project |
| `address_groups`, `create_address_group`, `add_addresses_to_address_group`, `remove_addresses_from_address_group`, `delete_address_group` | `network/stack.py:ensure_egress_group,delete` | `get_address_group`, `create_address_group`, `add_addresses`, `remove_addresses`, `delete_address_group` | R read; M write |
| `ips`, `get_ip`, `create_ip`, `update_ip`, `delete_ip` | `network/fip.py:_find,ensure,delete`; `conductor/engine_upgrade.py:prepare`; `conductor/server_failover.py:prepare`; `conductor/controller_failover.py:prepare`; `common/janitor.py:report` | `get_floatingip`, `create_floatingip`, `create_floatingip:port_id`, `update_floatingip`, `update_floatingip:port_id`, `delete_floatingip` | R read; M write; accessible external network |
| `set_tags` after creation of network, subnet, router, port, SG or FIP | `network/tags.py:create_tagged`, called from `network/stack.py:_ensure,create`, `network/fip.py:ensure`, `conductor/controller.py:port` | `update_network:tags`, `update_subnet:tags`, `update_router:tags`, `update_port:tags`, `update_security_group:tags`, `update_floatingip:tags` | Owner M |

Management networks and SGs are operator-provisioned. For M-only access,
put them in the service project; sharing a network alone does not satisfy
the explicit fixed-IP/port-security network-owner checks. Coast does not
set port bindings, provider-network fields, shared flags or security-group
rule tags. Adding `service` indiscriminately would grant broader network
access and would not solve the Designate sudo requirement.

### Cinder and Barbican

Sources: [Cinder policies](https://github.com/openstack/cinder/tree/stable/2026.1/cinder/policies)
and [Barbican secrets](https://github.com/openstack/barbican/blob/stable/2026.1/barbican/common/policies/secrets.py).

| SDK calls | File:function | Upstream policy | Minimum |
|---|---|---|---|
| `volumes`, `get_volume`, volume wait helpers | `conductor/controller.py:find_volume,wait_volume,delete_volume`; `conductor/volume_delete.py:verify,check_dependencies,wait`; `conductor/exports.py:_discover_recovery_resources,ExportFlow.create,cleanup`; `conductor/engine_upgrade.py:prepare`; `conductor/controller_failover.py:prepare`; `conductor/stack_teardown.py:_upgrade_storage,_volumes`; `common/janitor.py:report` | `volume:get_all`, `volume:get` | R; no all-tenants query |
| `create_volume` (including clones from snapshot), `delete_volume` | `conductor/controller.py:volume,delete_volume`; `conductor/exports.py:ExportFlow.create,cleanup`; `conductor/engine_upgrade.py:restore_snapshots`; `conductor/volume_delete.py:delete`; `conductor/stack_teardown.py:_upgrade_storage,_volumes` | `volume:create`, `volume:delete` | M; accessible volume type; no force-delete or cascade |
| `snapshots`, `get_snapshot`, snapshot wait helpers | `conductor/exports.py:Snapshots.execute,_discover_recovery_resources,ExportFlow.cleanup`; `conductor/engine_upgrade.py:snapshots,restore_snapshots`; `conductor/volume_delete.py:check_dependencies`; `conductor/stack_teardown.py:_snapshot_one,_upgrade_storage,_volumes,housekeep`; `common/janitor.py:report` | `volume:get_all_snapshots`, `volume:get_snapshot` | R |
| `create_snapshot`, `delete_snapshot` | `conductor/exports.py:Snapshots.execute,revert,ExportFlow.cleanup`; `conductor/engine_upgrade.py:snapshots,complete`; `conductor/stack_teardown.py:_snapshot_one,_upgrade_storage,housekeep` | `volume:create_snapshot`, `volume:delete_snapshot` | M; `force=True` permits an in-use snapshot, not an admin-only force-delete |
| `create_secret`, `get_secret` metadata and lazy payload GET, `delete_secret` | `common/secrets.py:BarbicanSecretStore._call` (all conductor/registry/API secret operations route here) | `secrets:post`, `secret:get`, `secret:decrypt`, `secret:delete` | M in service project; creator or non-private secret, or applicable ACL for reads; no cross-project payload access |
| `secrets` metadata list | `common/janitor.py:report` | `secrets:get` | M with secure RBAC defaults; reader alone is insufficient |

There are no real Cinder extend, reset-status, force-detach or attachment
API calls here. `conductor/cloud.py:FakeCloud` is the offline fake, not a
service-session client. Nova performs volume attachment operations.

### Designate, Swift exports and authentication

Sources: [Designate policies](https://github.com/openstack/designate/tree/stable/2026.1/designate/common/policies),
[Swift keystoneauth middleware](https://github.com/openstack/swift/blob/stable/2026.1/swift/common/middleware/keystoneauth.py),
and [Keystone trust authentication](https://docs.openstack.org/keystone/2026.1/admin/identity-use-trusts.html).

| SDK/HTTP calls | File:function | Default policy | Minimum |
|---|---|---|---|
| `get_zone`, `recordsets` | `network/dns.py:ensure,delete` via `ServiceProjectProvider` | `get_zone`, `get_recordsets`, `find_recordsets` | R in configured operator-zone project (service project), or admin |
| `create_recordset`, `update_recordset`, `delete_recordset` | `network/dns.py:ensure,delete` | `create_recordset`, `update_recordset`, `delete_recordset` | M owning zone; `use_low_ttl` additionally requires admin if configured TTL is below Designate minimum |
| `get_account_metadata` (HEAD), `set_object_metadata` (POST), `get_object_metadata` (HEAD), `delete_object` (DELETE) | `conductor/exports.py:SwiftStorage.__init__,retain,urls,delete` | Swift keystoneauth account ownership and `operator_roles`; object/container ACLs where applicable | `swiftoperator` or `admin` in service project with upstream default operator roles; not automatically M |
| Token authentication POST `/v3/auth/tokens`, catalog endpoint discovery, `get_project_id` | `common/clients.py:_load_auth,get_session,get_connection`; `common/janitor.py:report`; `api/v1/stack_controllers.py:inventory`; `conductor/backup_storage.py:proxy_for` | Keystone authentication, no project-list policy; catalog normally embedded in token response | Valid enabled user/project and project role grant; endpoint discovery itself is local |

Swift export TempURL signing is local. Guests use signed URLs; those
requests do not carry the service session. The optional S3 export backend
uses configured S3 keys and botocore, not Keystone. No Glance image writes,
Keystone project lookups/role grants/trust creation, tenant quota reads,
hypervisor queries, flavor-access grants or Octavia API calls exist in the
runtime service-session paths. Kolla's deployment-time registration uses
the operator's credentials, not `[service_auth]`.

## Inventory: cross-project operations

| Calls | File:function | Default upstream policy | Minimum |
|---|---|---|---|
| HEAD/POST account metadata at `AUTH_<tenant-project>`; container PUT/DELETE; object HEAD, listing GET, SLO PUT and DELETE | `conductor/backup_storage.py:proxy_for,SwiftStorage.prepare,complete,delete`; `conductor/backups.py:drop_service` | Swift keystoneauth `reseller_admin_role` (default `ResellerAdmin`); SLO middleware uses the same authorization | Project token with `ResellerAdmin`; container ACLs cannot authorize account TempURL-key metadata writes |
| `zones`, `recordsets`, `create_recordset`, `update_recordset`, `delete_recordset` with `X-Auth-Sudo-Project-ID` | `network/customer_dns.py:zones,ensure,delete`; `network/dns_credentials.py:ServiceAccountProvider.proxy` | `use_sudo` = `role:admin`, plus `find_zones`, `get_recordsets`, `find_recordsets`, `create_recordset`, `update_recordset`, `delete_recordset`; `use_low_ttl` if applicable | Project admin with defaults; service project M cannot satisfy tenant ownership checks |
| Trust-scoped token POST and same linked-zone DNS calls | `network/dns_credentials.py:TrustProvider.proxy`; `network/customer_dns.py:zones,ensure,delete` | Keystone validates pre-existing trust, trustee and delegated roles; normal Designate project rules, no `use_sudo` | M delegated in tenant project, including implied reader; configured TTL must satisfy Designate minimum |

## Inventory: admin-only reads

None are requested as standalone runtime operations. Janitor
`common/janitor.py:report` explicitly passes `project_id` to Neutron and
`all_projects=False` to Nova/Cinder, and Barbican lists the token project.
Coast DB queries with `all_projects=True` are not cloud inventories.
Nova may include admin-only response attributes with an admin token, but
Coast does not depend on hypervisor, host-status or migration fields.
Private image/flavor visibility and operator-owned management network
access are the conditional read dependencies described above; project
filters alone do not make an inaccessible resource visible.

## Read-only permission checker

Run with an installed Coast Python environment and the service config:

```console
python tools/check_service_permissions.py --config-file /etc/coast/coast.conf
python tools/check_service_permissions.py --config-file /etc/coast/coast.conf \
  --tenant-project-id <tenant-uuid> [--dns-trust-id <linked-zone-trust-id>]
```

The first command lists service-project servers, networks, volumes,
images, secret metadata and zones. The second also HEADs the tenant Swift
account and lists tenant zones using the configured sudo/trust provider.
Supply an existing tenant account; a 404 is a failure, not proof of access.
There is no admin-only inventory to probe. Optional cross-project probes
are only included when `--tenant-project-id` is supplied. Omission does not
establish cross-project permission. Each executed call prints a fixed
`PASS`/`FAIL` label (for example, `PASS own/barbican/secrets` or
`FAIL cross/designate/zones`); all passing probes exit 0, and any failure
exits 1. A missing service endpoint fails
that service's probe. SDK logging is disabled and results, exception text,
account metadata and secret payloads are never printed. Pagination is
consumed so a deferred 403 fails the probe.

The checker is entirely read-only: it has no write-probe mode and rejects
`--write-probe`. It does not create resources, issue DB queries, or read
Barbican payloads. Read PASS results cannot prove create/delete, sudo
record writes, account metadata writes, image visibility for a configured
image, or every conditional policy above. PM deployment and lifecycle
validation after merge remain necessary and outside this coding lane.
