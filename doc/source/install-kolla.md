# Install Coast v0.1.0 with Kolla-Ansible

This guide is for the operator. Use OpenStack 2026.1 and an isolated
validation cloud first. Coast's role is an overlay under `tools/kolla/`,
not an upstream service enabled by stock Kolla-Ansible. A single-controller
trial has passed; multi-controller HA acceptance remains open. Do not treat
this guide as evidence of production HA acceptance.

Follow the [2026.1 Kolla quickstart](https://docs.openstack.org/kolla-ansible/2026.1/user/quickstart.html)
and [configuration guide](https://docs.openstack.org/kolla-ansible/2026.1/admin/advanced-configuration.html)
for the base cloud. Use its existing inventory, virtual environment,
collections and TLS configuration. The Coast overlay follows Octavia.
See [the role details](kolla.md) and `tools/kolla/README.rst` for the exact
integration files, full play and supported actions.

## 1. Prepare the cloud and overlay

Provide Nova, Neutron, Cinder, Glance, Keystone and Barbican. Create a
`coast` service user in the service project with the required cloud roles.
Coast stores secret values in Barbican in the service project and keeps
only references in its database. The role selects the Barbican backend
when `enable_barbican` is true. For a separately deployed Barbican, set
`coast_secrets_backend: barbican`; disabled storage makes stack creation
fail closed. See [Secrets and Barbican](secrets.md) for the full list,
payload size limits and outage behaviour. The role creates the Coast
database and database user unless preconfigured databases are enabled.
Messaging uses Kolla's existing shared RabbitMQ account.

In a separate Kolla-Ansible 2026.1 checkout (`KOLLA`), copy these files
from the Coast checkout:

```bash
cp -a tools/kolla/ansible/roles/coast "$KOLLA/ansible/roles/"
cp tools/kolla/ansible/group_vars/all/coast.yml \
  "$KOLLA/ansible/group_vars/all/coast.yml"
```

Add inventory groups `coast-api`, `coast-conductor` and
`coast-health-manager`. Use the play in `tools/kolla/README.rst` at
`$KOLLA/ansible/coast.yml`; it must gather interface and peer facts.
Wire this play into the Kolla service plays and add `coast_services` to
Kolla's HAProxy service maps. Merely setting `enable_coast` does not add
these integration points to stock Kolla.

Provide a dedicated routed management interface on conductor and
health-manager hosts. Guests have static management IPv4, no management
default route, and a separate environment NIC with DHCP. The stack router
provides external egress. Block access from stack provider networks to
OpenStack control-plane and management networks at the router/firewall.
Security groups alone do not implement that provider-network boundary.
See [networking](networking.md) and [operations](operations.md).

## 2. Set globals and passwords

Merge the following into `/etc/kolla/globals.yml`. Replace all uppercase
values with operator inputs; do not use them literally.

```yaml
enable_coast: true
coast_enable_health_manager: true
coast_network_interface: MANAGEMENT_INTERFACE
coast_hm0_manage: false
coast_tag: RELEASE_IMAGE_TAG
coast_api_paste_source: /path/to/coast/etc/coast/api-paste.ini
coast_controller_worker_options:
  mgmt_net_id: MANAGEMENT_NETWORK_UUID
  mgmt_sg_id: MANAGEMENT_SECURITY_GROUP_UUID
  external_network_id: EXTERNAL_NETWORK_UUID
  image_owner_id: SERVICE_PROJECT_UUID
  image_tag_controller: coast-coolify-controller
  image_tag_worker: coast-worker
  env_cidr_pool: OPERATOR_STACK_CIDR_POOL
```

Check option names with `tox -e genconfig` before deployment. Add the
required flavor, volume, image, CA and cloud options from the generated
sample and operations guide. Put host-specific addresses in inventory.
The VLAN interface must already exist and have routes to guest management
addresses. The development hm0 helper supports ML2/OVS only; leave it
disabled for a routed management interface and for ML2/OVN.

Store `coast_database_password`, `coast_keystone_password` and
`coast_agent_keystone_password` in protected `/etc/kolla/passwords.yml` or encrypted
Ansible variables. Generate unique credentials with the operator's secret
manager. Do not assume `kolla-genpwd` knows the overlay's additional keys.
Kolla supplies `rpc_transport_url` and `notify_transport_url` from its
`om_rpc_*` and `om_notify_*` group variables and `rabbitmq_password`.
No Coast RabbitMQ password or broker registration is required. Keep those
URLs on the Kolla controllers; never put them in guest config or user data.
A `coast_database_connection` override must have
no query string; the role adds its database TLS parameters.

The Coast API stays on the internal API network. Set
`coast_enable_external_api: true` only when it must use the external
VIP, and then use Kolla frontend TLS for that listener. The default
Coast API image serves HTTP on its backend; set
`coast_enable_tls_backend: false`
unless an operator-supplied TLS-capable image has been validated. Configure
OpenStack, database and RabbitMQ CA paths and mounts as specified in the
role guide. Do not disable certificate verification to solve a mount error.

## 3. Build the control-plane images and certificates

Use Kolla 22.2.0 for the 2026.1 image overlay. Build from a committed Coast
release revision and a matching, digest-pinned `openstack-base` image.
`BASE_IMAGE` includes the operator registry and namespace; the script
inherits both. `IMAGE_TAG` is the release tag selected by the operator.

```bash
tools/kolla/images/build.sh -p "$BASE_IMAGE" "$IMAGE_TAG"
```

This builds and pushes coast-base, coast-api, coast-conductor and
coast-health-manager. Set `coast_tag` to `IMAGE_TAG`, or set each
`coast_*_image_full` to the printed immutable reference. Configure registry
credentials through the normal Kolla image-pull settings.

Install the local certificate command into the Kolla-Ansible environment:

```bash
pip install ./tools/kolla/cli
kolla-ansible coast-certificates --coast-tools-dir "$PWD/tools"
kolla-ansible coast-certificates --coast-tools-dir "$PWD/tools" \
  --check-expiry 30
```

Keep the Coast CA separate from Kolla's frontend CA. The client CA signing
key stays on the control host. Only conductors receive the required server
signing key and client bundle. Protect source certificates and backups.

## 4. Build and upload the guest images

Use a build host with diskimage-builder, Docker and qemu-utils. Run as a
normal user with passwordless standard sudo. Supported Coolify releases
and their reviewed checksums are listed in
`coast/common/engine_versions.py`; build each image for one of them
(the default is `DEFAULT`, the version new stacks get). Workers and build
servers use the worker image of the same version. Review the support
checksums and image lock; do not use `latest`.

```bash
python3 -c 'from coast.common.engine_versions import DEFAULT; print(DEFAULT)'
# Set VERSION to the printed value.
tools/build-images/build-agent-pyz.sh /var/tmp/coast-agent.pyz
sha256sum /var/tmp/coast-agent.pyz
```

Set `AGENT_SHA256` to that checksum. Independently download and review the
stock installer for `$VERSION`; its reviewed checksum is the first entry of
`RELEASES[$VERSION]` in `engine_versions.py`, which the build uses when
`--coolify-installer-sha256` is not given. The build checks it before use. The image stages stock support files and
locked containers; first boot starts them without an engine download.

```bash
tools/build-images/coast-image-create.sh --role controller \
  --distro ubuntu-noble --format qcow2 --coolify-version "$VERSION" \
  --coolify-installer-sha256 "$INSTALLER_SHA256" \
  --agent-source /var/tmp/coast-agent.pyz --agent-sha256 "$AGENT_SHA256"
tools/build-images/coast-image-create.sh --role worker \
  --distro ubuntu-noble --format qcow2 --coolify-version "$VERSION" \
  --agent-source /var/tmp/coast-agent.pyz --agent-sha256 "$AGENT_SHA256"
```

Authenticate as the Coast service user in the service project. Record each
qcow2 checksum, then upload private Glance images with the commands in
`tools/build-images/README.rst`. The controller needs `coast_role=controller`,
`coast_engine=coolify`, `coast_engine_version=$VERSION` and the
`coast-coolify-controller` tag. The worker needs `coast_role=worker`,
`coast_engine_version=$VERSION` and the `coast-worker` tag. Retain exactly one
active image for each selected owner/tag combination. Validate a guest
boot, Cinder mount order, mTLS health and heartbeat delivery before use.

## 5. Publish plans and deploy

Set `[plans] definitions` in `/etc/kolla/config/coast.conf`. Supply a JSON
array with one entry per offered VM plan. Each entry has `name`,
`kind: "vm"`, `roles` (control, worker or build), internal Nova `flavor`
and a size `description`. Use operator-selected names and flavors. Do not
set `plans_file`: the plan API does not read it. Map each offered flavor
unambiguously to a plan for its role. See the plan catalog in operations.

Render config, then run prechecks, registration, bootstrap and deployment
through the integrated Coast play with the normal inventory, globals and
passwords. Use the play in the Kolla checkout, with `INVENTORY` set to the operator's
inventory path:

```bash
for action in config precheck register bootstrap deploy; do
  ansible-playbook -i "$INVENTORY" "$KOLLA/ansible/coast.yml" \
    -e @/etc/kolla/globals.yml -e @/etc/kolla/passwords.yml \
    -e "kolla_action=$action" || break
done
```

Use the configured Kolla Ansible plugins and collections for this command.
Check each action's result; do not proceed after a failure.
Config-only operation must leave containers running without a restart.
A failed DB migration must stop deployment. Stop any old non-container
API before cutover; the role does not free an occupied API port.

Check all enabled containers are healthy and DB migrations are at head.
Inspect `/var/log/kolla/coast/` without printing credentials. Check the
`app-hosting` catalog endpoints through HAProxy with verified TLS, then
check guest mTLS and heartbeat paths. Container health alone does not prove
that a stack can be created. HA validation requires a multi-controller run.

## 6. Create the first stack

Install the Coast client and dashboard packages matching the release into
the operator's client and Horizon environments. Enable the dashboard panel
files using that package's installation guide. Switch to tenant credentials;
choose published control and worker plans, then follow the
[user quickstart](quickstart.md). Verify stack ACTIVE, a successful git app
deployment, HTTPS, logs, Activity and deletion. Run the CLI walkthrough
in an isolated tenant project to repeat these checks with exit cleanup.

## Secret storage and service permissions

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
