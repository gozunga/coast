Coast Kolla Ansible integration
===============================

Status
------

This is an overlay kolla-ansible Coast role, modeled on Octavia
stable/2026.1. It is not part of upstream kolla-ansible. A
single-controller trial has passed; multi-controller HA acceptance remains
open (Q141).
Images are built with kolla-build and the Coast overlay (Q141).
Use an isolated test cloud.

Reference sources (role interfaces are checked against the test pin):

* https://github.com/openstack/kolla-ansible/tree/stable/2026.1/ansible/roles/octavia
* https://docs.openstack.org/kolla-ansible/latest/reference/networking/octavia.html
* https://github.com/openstack/kolla-ansible/tree/7101c4cc7fe83937686a39dbe0174293423e32a2/ansible/roles/magnum
* https://github.com/openstack/kolla-ansible/tree/7101c4cc7fe83937686a39dbe0174293423e32a2/ansible/roles/service-uwsgi-config

How to try it
-------------

#. Use a separate kolla-ansible 2026.1 checkout with its normal Ansible
   environment, plugins, inventory and collection dependencies.
#. Copy ``ansible/roles/coast`` into that checkout's ``ansible/roles/``.
   Copy ``ansible/group_vars/all/coast.yml`` into its ``ansible/group_vars/all/``.
   The network variables must be in hostvars for Kolla's address filter.
   Copy ``ansible/coast.yml`` next to ``site.yml``.
   Add inventory groups ``coast-api``, ``coast-conductor``,
   ``coast-health-manager`` and ``coast-console-proxy``. Put the shell
   listener on the same hosts as the conductor. Put the Coast play file
   in the checkout's
   ``ansible/`` directory, next to ``site.yml``, so Ansible loads Kolla's
   ``group_vars/all``. A play outside that directory does not load globals
   such as ``database_enable_tls_internal``. Do not use the production
   checkout. The play must gather interface facts; see the exact play below.
#. Build the images as described in "Building images" below.
   It supplies the ``coast`` user in ``kolla``, Kolla start/config tools,
   Coast console scripts and API paste dependencies.
   The ``kolla`` group needs the standard Kolla sudoers entry for
   ``kolla_set_configs``. Names default to ``{{ docker_image_url }}coast-api``,
   ``coast-conductor``, ``coast-health-manager`` and
   ``coast-console-proxy``; ``coast_tag`` defaults
   to ``openstack_tag``. Each service has ``*_image``, ``*_tag`` and
   ``*_image_full`` variables, as Octavia does. Supply built, immutable
   ``coast_api_image_full`` and ``coast_conductor_image_full`` references
   (and ``coast_health_manager_image_full`` if enabled). Existing tagged
   or digest ``*_image`` overrides remain intact; untagged names receive
   the service tag. All three services can use the same built image.
   A disabled health-manager needs no operator image override.
#. Add the three Coast password keys described below to ``passwords.yml``.
   The role creates the database/user unless ``use_preconfigured_databases``
   is true. Messaging reuses Kolla's ``rpc_transport_url`` and
   ``notify_transport_url`` with the shared ``om_rpc_user`` and
   ``om_rpc_password`` (``rabbitmq_password``). No broker account or
   vhost is registered by Coast. Database TLS uses
   ``database_enable_tls_internal`` and ``openstack_cacert``; RabbitMQ TLS
   uses ``om_enable_rabbitmq_tls`` and ``om_rabbitmq_cacert``. Make these CA
   paths readable in each container and on hosts that run hm0 prepare.
#. Endpoints default to Kolla's internal/public protocols and FQDNs
   (API port 9600). These protocols follow
   ``kolla_enable_tls_internal`` / ``kolla_enable_tls_external``.
   Override ``coast_*_endpoint`` and ``coast_*_fqdn`` if required. Supply
   ``coast_api_paste_source`` from the installed Coast package.
   Supply ``coast_openstack_ca_source`` as a path visible inside each
   container and ``coast_openstack_cafile`` as its destination.
   Add a read-only volume per service if the source needs another mount.
#. Set ``coast_controller_worker_options`` to an option-name/value mapping
   from the sample config. Include network IDs, image owner/tags, flavor
   IDs, subnet pool and control-plane CIDRs.
   Use comma-separated strings for list values. Do not add secrets.
   Set catalog region/interface through ``coast_cloud_region`` and
   ``coast_cloud_interface``. Set host-specific values in inventory.
#. Generate certificates on the Ansible control host from the Coast tree::

     ansible-playbook tools/kolla/ansible/certificates.yml \
       -e coast_certificates_dir=/etc/kolla/config/coast/certs

   Check expiry without a write::

     ansible-playbook tools/kolla/ansible/certificates.yml \
       -e coast_certificates_dir=/etc/kolla/config/coast/certs \
       -e coast_check_expiry_days=30

#. Bootstrap uses the default ``kolla_start`` command with
   ``KOLLA_BOOTSTRAP`` and ``KOLLA_CONFIG_STRATEGY``. Kolla copies config
   before the startup hook runs the database upgrade. Leave
   ``coast_bootstrap_command`` empty for the default command below.
   Existing non-empty overrides run through Bash after config copying;
   shell tracing is disabled to protect credentials.
#. Run the new play with ``-e kolla_action=config`` first. Inspect rendered
   files without printing credentials. Config only writes files; restart
   handlers are skipped. Before ``-e kolla_action=deploy``, stop the old
   non-container Coast API with its service manager. Confirm its listener
   is closed on ``api_interface_address:coast_api_listen_port``. First-deploy
   precheck retains the occupied-port check; it does not stop the old API.
   Then run ``-e kolla_action=deploy``.
   Supported actions: precheck, config, register, bootstrap, deploy,
   reconfigure, upgrade, stop, check-containers and certificates. The role certificates action copies runtime files;
   the standalone play creates them.

Coast play location and facts
-----------------------------

Use the supplied ``ansible/coast.yml`` beside Kolla's ``site.yml``.
It imports Kolla's all-host fact collection, configures loadbalancers with
``loadbalancer-config`` (``haproxy-config`` and ``proxysql-config``), applies
Kolla's loadbalancer handlers, then deploys Coast. This order installs
ProxySQL user routing before database bootstrap. It uses the existing
HAProxy and ProxySQL containers. Other services' configuration is retained.
Run the play through the Kolla CLI so globals, passwords, plugins and
collections are loaded::

  kolla-ansible genconfig -i /path/to/inventory -p "$KOLLA/ansible/coast.yml" -t coast
  kolla-ansible deploy -i /path/to/inventory -p "$KOLLA/ansible/coast.yml" -t coast

For an upstream ``site.yml`` integration, add the Coast loadbalancer task
to its loadbalancer service block, and the Coast service play to its service
plays. Do not run the Coast service play alone when ProxySQL is enabled.

Building images
---------------

Coast images are built with ``kolla-build`` and an overlay, like other
OpenStack services (Q141, James 2026-10-14). The plain Dockerfile path
was removed. The overlay lives in ``tools/kolla/images``:

* ``docker/coast/coast-base`` installs Coast into the Kolla venv on top of
  an existing Kolla 2026.1 Ubuntu Noble ``openstack-base`` image, modeled
  on ``docker/octavia/octavia-base``.
* ``docker/coast/coast-api``, ``coast-conductor``,
  ``coast-health-manager`` and ``coast-console-proxy`` are thin service
  images ``FROM coast-base``
  that run as ``coast``. Their names match the role's default
  ``coast_*_image`` values.
* ``template-overrides.j2`` is the default ``--template-override``. It
  exposes ``coast_base_header``/``coast_base_footer``,
  ``coast_<service>_header``/``_footer`` and the customizable
  ``coast_base_pip_packages`` list for operator overlays.

Install ``kolla`` matching the Kolla release of the base image (22.2.0 for
2026.1) and the ``docker`` Python package, then build from any directory.
Commit source changes first::

  git rev-parse --short HEAD
  # Set IMAGE_TAG to a tag identifying this committed revision.
  tools/kolla/images/build.sh -p \
    registry.example.com:5000/kolla/openstack-base:2026.1-ubuntu-noble \
    "$IMAGE_TAG"

Arguments are BASE (an existing ``openstack-base`` image) and the TAG for
the Coast images. Registry and namespace are taken from BASE. ``-p``
pushes the four images with the invoking user's Docker registry
credentials; ``-o FILE`` adds an operator template override
(repeatable, merged after the default). The script prints the four image
references. It writes a temporary ``kolla-build.conf`` whose
``[coast-base]`` source is ``type = local``: a ``git archive`` of committed
HEAD. Uncommitted source, untracked files, credentials and Git history are
not in the archive. Kolla's own parent images are not rebuilt:
``COAST_KOLLA_PARENT`` pins ``coast-base`` to BASE. ``tox -e
kolla-images`` renders the overlay with ``--template-only`` in CI.

For repeatable inputs, use the same source commit and pin BASE by digest.
Both pip installs use the base's ``/requirements/upper-constraints.txt``,
as Kolla does. ``PBR_VERSION`` (default ``0.1.0``, override with
``COAST_PBR_VERSION``) supplies the package version without Git metadata.

The ``coast`` user/group use fixed UID/GID 42499. This is a development
allocation verified not to clash on the AIO, not an upstream reservation.
Conflicting IDs in another base cause the build to fail. The user joins
``kolla``, following `Octavia's image
<https://github.com/openstack/kolla/blob/stable/2026.1/docker/octavia/octavia-base/Dockerfile.j2>`_.
Coast installs into ``/var/lib/kolla/venv``. The base's ``dumb-init``
entrypoint and ``kolla_start`` command are retained. Kolla takes the
service command from the role's ``config.json`` templates.

The role defaults already use the service image names. Set
``coast_tag`` to the built tag, or set each reference explicitly::

  coast_api_image_full: registry.example.com:5000/kolla/coast-api:dev-abc1234
  coast_conductor_image_full: >-
    registry.example.com:5000/kolla/coast-conductor:dev-abc1234
  coast_health_manager_image_full: >-
    registry.example.com:5000/kolla/coast-health-manager:dev-abc1234
  coast_console_proxy_image_full: >-
    registry.example.com:5000/kolla/coast-console-proxy:dev-abc1234

The build checks ``coast-api``, ``coast-conductor``,
``coast-health-manager``, ``coast-console-proxy`` and
``coast-db-manage`` with ``--help``. It also
checks ``coast-db-manage version``, PyMySQL (supplied by the base) and the
system CA bundle. Main provides the package-relative migration path;
the wheel includes the migration modules and Mako revision template.
The default bootstrap command is
``coast-db-manage --config-file /etc/coast/coast.conf upgrade``.
The hook exits with the migration status instead of starting the conductor.
The role checks the exit code and stops the play on failure. The task keeps
``no_log`` because an override or container output can contain secrets.
Read the migration revision and masked failure details in
``/var/log/kolla/coast/coast-db-manage.log`` on the conductor host.
The shared ``kolla_logs`` volume retains this file after bootstrap exits.
A custom command must use the same logging configuration to retain its logs.

The role copies the paste file from the Ansible control host. Set
``coast_api_paste_source`` to the absolute path of this checkout's
``etc/coast/api-paste.ini`` on that host, not an image-only path. The
image copies it to ``/etc/coast/api-paste.ini`` and the package installs it
at ``/var/lib/kolla/venv/etc/coast/api-paste.ini``. The role's runtime
destination is ``/etc/coast/api-paste.ini``.

The image installs ``extend_start.sh`` as
``/usr/local/bin/kolla_extend_start``. Kolla sources it at startup to
create ``/var/log/kolla/coast`` with ``coast:coast`` ownership and mode
0755. This must happen at startup because the shared log volume hides
image-build directories. Do not set ``KOLLA_SKIP_EXTEND_START``. See the
`Kolla Images API
<https://docs.openstack.org/kolla/2026.1/admin/kolla_api.html>`_.

The image also creates ``/etc/coast``, private ``/etc/coast/certs`` and
private state under ``/var/lib/coast/health-manager``. No operator keys or
certificates enter the image. The role copies conductor Coast CA material
under ``/etc/coast/certs``. Supply and mount OpenStack, database and
RabbitMQ CA files as described above. The system CA bundle does not
replace these operator inputs.

On the Docker host, check the package before deployment::

  docker run --rm "$image" coast-api --help
  docker run --rm "$image" coast-conductor --help
  docker run --rm "$image" coast-health-manager --help
  docker run --rm "$image" coast-console-proxy --help
  docker run --rm "$image" coast-db-manage version

Run the role's ``config``, ``precheck`` and ``deploy`` actions on the
isolated AIO. Check API access and service logs under
``/var/log/kolla/coast`` without printing secrets. The API runs under uWSGI through
``coast-wsgi-api``. The Kolla openstack-base image supplies uWSGI and its
Python plugin; no new package dependency is added.

Assumptions
-----------

* API port 9600 matches operations.md; 9520 is not used.
  The shell listener uses port 9610.
* There is no coast-worker console script. Guest workers are not control
  plane containers. Services are API, conductor, the shell listener and
  health-manager.
* Health-manager is disabled by default.
  ``coast_health_manager_key_lookup`` is optional. The default is
  ``coast.health_manager.keys.StackKeyLookup``. Override it to use
  another lookup class. The bind IP defaults to the local
  ``coast_network_interface_address``. The heartbeat UDP port defaults
  to 5565 (``coast_health_manager_port``). Guest heartbeat destinations
  default to the management addresses of the ``coast-health-manager``
  group. Establish management interface and container access first.
* ``coast_coolify_build_registry_image_template`` is unset by default.
  When set, the conductor gets ``[coolify] build_registry_image_template``
  (see operations.md). A stack with a private registry uses its
  environment address instead. This value is the fallback when that
  per-stack template is empty. The role does not choose a registry.
* The API binds only to ``api_interface_address:coast_api_listen_port``.
  HAProxy publishes the internal VIP listener. The external VIP listener
  stays off unless ``coast_enable_external_api`` is true. Frontend TLS
  follows ``kolla_enable_tls_internal`` and ``kolla_enable_tls_external``;
  backend TLS follows ``kolla_enable_tls_backend``.
* Backend certificate lookup follows Magnum: host/service, host, service,
  then ``kolla_tls_backend_cert`` / ``kolla_tls_backend_key`` under
  ``kolla_certificates_dir``. Use ``coast-cert.pem`` / ``coast-key.pem`` for
  service files. The role copies only API leaf material into the API
  config directory. uWSGI reads it under ``/etc/coast/certs/``. Keep Kolla's
  backend verification enabled and supply certificates valid for each API
  address. Kolla's HAProxy CA bundle must trust their issuing CA.
* Kolla globals and plugins are available. The overlay uses the 2026.1
  interfaces of ``service-ks-register``, ``service-check-containers`` and
  ``kolla_container``. Bootstrap runs once on the first conductor host.
* ``gen-ca.sh`` fixes the CA and controller client CNs.
  ``coast_certificate_cn`` sets only the optional registry leaf CN through
  ``--registry-host``. It is not a general CN override. For example, use
  ``registry.example.com`` for a registry certificate.
* Config files contain required service credentials, as in Kolla. Protect
  host files and backups. Tenant secrets stay in Barbican. Never print
  resolved config or use Ansible diff mode.
* This page describes deployment. The shell-session API is in
  ``doc/source/api.md``.

Configuration overrides, logs and health checks
-----------------------------------------------

The role uses Kolla's ``merge_configs``. Sources are read in this order;
later values win and missing override files are skipped:

#. The role template ``coast.conf.j2``.
#. ``{{ node_custom_config }}/global.conf``.
#. ``{{ node_custom_config }}/coast.conf``.
#. ``{{ node_custom_config }}/coast/{{ item.key }}.conf``.
#. ``{{ node_custom_config }}/coast/{{ inventory_hostname }}/coast.conf``.

Service keys are ``coast-api``, ``coast-conductor``,
``coast-health-manager`` and ``coast-console-proxy``. Use these files
to set existing ``[plans]``
definitions, ``[dns]``, ``[secrets]``, ``[export]`` and ``[DEFAULT] debug``.
Use option names from the generated Coast sample config. The conductor
renders export client certificate/key and server CA paths for its copied
files. Rendering keeps ``no_log`` and mode 0600. Protect source files and
backups; never use diff mode.

All services mount ``kolla_logs:/var/log/kolla/`` and use
``log_dir = /var/log/kolla/coast``. Kolla config permissions set that
directory's owner to ``coast:coast``, recursively.

Each service prefix (``coast_api``, ``coast_conductor``,
``coast_health_manager``, ``coast_console_proxy``) has
``<prefix>_enable_healthchecks`` and
``<prefix>_healthcheck_interval``, ``retries``, ``start_period``, ``test``
and ``timeout`` variables. Enablement and timings follow Kolla's
``enable_container_healthchecks`` and ``default_container_healthcheck_*``.
The API uses ``healthcheck_curl`` on its listen address and port with the
backend HTTP/TLS protocol. The conductor uses ``healthcheck_port`` with
its service name and ``om_rpc_port`` (the 2026.1 RabbitMQ RPC port
variable). The health-manager does not use RPC. Its check reads
``/proc/net/udp`` for ``coast_health_manager_port`` (default 5565). The
Coast image does not install ``iproute2``, so the check uses ``python3``.
The shell listener uses ``healthcheck_curl`` with HTTP ``GET /health``.
Its backend TLS stays off.
Disable a service check with its enablement variable.
These checks do not prove guest mTLS, heartbeat delivery or task completion.
Keystone token caching uses the Kolla memcached hosts/port and the Octavia
2026.1 memcache security settings, region and internal interface.

Certificate flow
----------------

The standalone play calls the unchanged ``tools/gen-ca.sh``. Set
``coast_certificate_validity_days`` and ``coast_certificate_subject_prefix``
for generation. Expiry mode is read-only and fails on expiring or missing
material. The ``kolla-ansible coast-certificates`` command runs this play. Install
the plugin into the kolla-ansible virtualenv with
``pip install tools/kolla/cli``, then run
``kolla-ansible coast-certificates --coast-tools-dir /path/to/coast/tools``
to generate, or add ``--check-expiry 30`` for the read-only check.
``--coast-certificates-dir`` defaults to ``/etc/kolla/config/coast/certs``;
``COAST_TOOLS_DIR`` may replace ``--coast-tools-dir``. Normal Ansible check mode skips generation,
but permits an explicit expiry check. Repeated generation keeps files.

Only conductor hosts receive the server CA certificate/key, client CA
certificate and controller client bundle. The client CA signing key stays
on the control host. Private files use mode 0600 in mode 0700 directories.
API and health-manager do not receive CA keys. Optional registry files
stay on the control host. Rotation, registry distribution and Barbican
import require a reviewed deployment design.

Management network
------------------

Coast uses a dedicated management VLAN, like Octavia/Trove management,
not ``api_network``. Set ``coast_network_interface`` to the host VLAN
interface on every conductor and health-manager host. Leave
``coast_hm0_manage=false``. The group-vars default is ``{{ network_interface }}``;
it is a fallback, not a production network isolation policy.
``coast_network_address_family`` is ``ipv4`` because Coast guest management
currently supports IPv4 only. ``coast_network_interface_address`` uses
``{{ 'coast_network' | kolla_address }}``, as Octavia does. Gather interface
facts before rendering config. API listeners and HAProxy/catalog endpoints
stay on the API network.

The rendered ``[controller_worker] mgmt_netns`` is empty for both VLAN and
hm0 host-interface paths. Conductor mTLS clients use this host namespace;
there is no conductor management listener or source-bind config option.
The host routes to guest management addresses through the selected VLAN.
Keep forwarding off and apply the host firewall isolation in operations.md.
``[controller_worker] health_manager_ips`` advertises the health-manager
management addresses to guests. Override ``coast_health_manager_ips`` for
an explicit destination list, or ``coast_health_manager_bind_ip`` for a
local bind override; use only addresses on the management network.

Host management port (development without a VLAN)
-------------------------------------------------

On DevStack or a development cloud without a VLAN, set
``coast_network_interface=coast-hm0`` and ``coast_hm0_manage=true``.
Before the first deployment, set the host-specific
``coast_network_interface_address`` to ``coast_hm0_address`` in inventory:
the interface has no facts until the helper creates it. Keep that override
for repeatable first-boot config. No address is supplied by the role.

``coast_hm0_manage`` defaults to false. To enable it on each conductor or
health-manager host, supply ``coast_hm0_cidr``, a distinct
``coast_hm0_address``, ``coast_hm0_mtu`` from the management network,
``coast_hm0_bridge`` (the existing Neutron integration bridge), and
``coast_hm0_binding_host`` (the Neutron binding host name). No address,
CIDR or MTU is assumed. The backend is ML2/OVS only.

Install Coast in a host virtual environment, then install the unchanged
``tools/coast-hm0.py`` at ``coast_hm0_script``. Set ``coast_hm0_python``
to that environment's Python. The host needs access to service credentials
and CA files referenced by the rendered config. The helper reuses or creates
owned Neutron resources, including the fixed-IP host port and peer-rule
security group. Set ``mgmt_net_id`` and ``mgmt_sg_id`` in
``coast_controller_worker_options`` to its returned network and group IDs;
prepare these IDs before stack provisioning. No guest resource is changed.

Host mode sets ``[controller_worker] mgmt_netns`` to an empty string.
Do not override it with a namespace. The health-manager bind IP defaults
to the same fixed IP. Before enabling management, apply the host firewall
isolation from operations.md and confirm it with
``coast_hm0_host_firewall_ready=true``. This is an operator assertion, not
a firewall implementation. Do not also attach this Neutron port to the
namespace-based veth created by the helper's ``plug`` action.

Deploy, reconfigure and upgrade stop management socket users, call the helper's ``prepare``
action, and install ``coast-hm0-setup`` and ``coast-hm0.service``. The
script uses Kolla's OVS container to create an internal port, with the
Neutron iface-id and MAC, fixed IP and explicit MTU. It rejects a conflicting
existing interface, disables forwarding on that interface, adds no default
route, and waits for the existing bridge. The systemd unit restores the
attachment at boot with a 180-second timeout. Configure the operator's
container startup order so this unit succeeds before Coast starts. After
an OVS container restart, restart this unit before Coast. Disabling the
variable or running ``stop`` preserves the port and boot unit; removal is
an explicit operator operation after Coast management users stop.

Verification
------------

Use a temporary Ansible environment outside the Coast tree and a separate
Kolla 2026.1 checkout. Install that checkout in the environment with
``pip install -e "$KOLLA"`` so its filter plugins can import ``kolla_ansible``.
The syntax harness imports every action, including
the dynamically included management tasks. It does not execute them::

  export ANSIBLE_LOCAL_TEMP=/tmp/coast-ansible-local
  export ANSIBLE_REMOTE_TEMP=/tmp/coast-ansible-remote
  export ANSIBLE_ROLES_PATH="$PWD/tools/kolla/ansible/roles:$KOLLA/ansible/roles"
  export ANSIBLE_LIBRARY="$KOLLA/ansible/library"
  export ANSIBLE_FILTER_PLUGINS="$KOLLA/ansible/filter_plugins"
  export ANSIBLE_ACTION_PLUGINS="$KOLLA/ansible/action_plugins"
  ansible-playbook --syntax-check -i localhost, tools/kolla/tests/syntax.yml
  ansible-playbook --syntax-check -i localhost, tools/kolla/ansible/certificates.yml
  ansible-playbook -i localhost, tools/kolla/tests/render.yml

``tox -e kolla-role`` runs the same three checks with a pinned
kolla-ansible; CI runs it on every pipeline. The render harness needs
``ANSIBLE_ROLES_PATH`` to include ``tools/kolla/ansible/roles``.

The render harness runs real config-only tasks and verifies that all
restart handlers are skipped, including an enabled health-manager. It also
checks image defaults and tagged/digest/full overrides, config merging, override precedence,
missing override files, export certificate paths, logging and health checks.

On an isolated cloud, run ``precheck``, then ``deploy``. Check both catalog
endpoints and HAProxy frontends. Check DB and RabbitMQ TLS without printing
credentials. Compare the Neutron port's binding host, MAC and fixed IP with
``ip address show coast-hm0`` and the OVS Interface record. Check the MTU,
agent mTLS health and heartbeat reception. Restart the management unit and
repeat these checks. Verify host firewall isolation before adding guests.
Run ``reconfigure``, then ``upgrade`` with a reviewed migration command;
upgrade must complete DB sync before container restart. Finally run ``stop``
and check that only Coast containers stop. These live checks remain open.

Operator inputs for v0.1.0
-------------------------

In addition to the cloud's existing Kolla settings, add these keys to
``globals.yml`` (replace each angle-bracket value)::

  enable_coast: "yes"
  enable_haproxy: "yes"
  coast_tag: "<immutable-built-image-tag>"
  coast_api_paste_source: "<absolute-Coast-checkout>/etc/coast/api-paste.ini"
  coast_openstack_ca_source: "<CA-file-path-visible-inside-containers>"
  coast_openstack_cafile: "/etc/coast/cloud-ca.pem"
  coast_network_interface: "<dedicated-management-interface>"
  coast_controller_worker_options:
    mgmt_net_id: "<management-network-UUID>"
    mgmt_sg_id: "<management-security-group-UUID>"

Retain the existing complete controller-worker options for image selection,
flavors and networking. The mapping above only shows the management fields;
it is not a complete cloud provisioning configuration. Retain existing
image overrides, service CA mounts, plans, DNS, export settings and Coast
guest certificates. Enable and configure the health-manager as described
above if it is in use. API port defaults to 9600; the API listen port can
be changed separately with ``coast_api_listen_port``. When
``haproxy_single_external_frontend`` is true, the public catalog port
follows ``haproxy_single_external_frontend_public_port``. The shell
listener stays on port 9610. Its HAProxy backend is plain HTTP, checks
``GET /health``, and uses a four-hour tunnel timeout. When
``enable_haproxy`` is no, the container listens on
``api_interface_address`` port 9610. Host networking exposes that port.

Set the normal Kolla TLS flags to match the operator's certificates::

  kolla_enable_tls_internal: "yes"
  kolla_enable_tls_external: "yes"
  kolla_enable_tls_backend: "yes"

These are production TLS settings, not a request to generate certificates.
Use Kolla's normal frontend PEMs, backend leaf/key files and trusted CA
bundle. See `Kolla TLS configuration
<https://docs.openstack.org/kolla-ansible/2026.1/admin/tls.html>`_.
Do not use the Coast guest CA as the Kolla CA. ``enable_proxysql`` follows
the existing cloud setting; Coast does not require it to be enabled.

Add these keys to ``/etc/kolla/passwords.yml`` with independent generated
values. The role supplies no password defaults::

  coast_database_password: "<generated-database-password>"
  coast_keystone_password: "<generated-Keystone-password>"
  coast_agent_keystone_password: "<generated-agent-password>"

``coast_agent_keystone_password`` is the ``coast-agent`` user. The role
creates that user in the service project with role ``coast_agent``
only. It is not the ``coast`` admin user. Do not reuse
``coast_keystone_password``. The guest config drive does not receive
this password. See ``doc/source/operations.md`` for rotation.

Use ``kolla-genpwd -p /etc/kolla/passwords.yml`` after adding empty keys to
fill them, or use the operator's secret manager. Protect the file and its
backups; do not commit it. For an existing deployment, preserve the current
Coast database and Keystone passwords. Kolla updates an existing Keystone
user password only with ``update_keystone_service_user_passwords=true``.
The role follows Octavia's quorum queue and stream fanout settings.
Notifications use ``messagingv2`` when ``enable_ceilometer`` is true,
otherwise ``noop``. Override ``coast_notification_topics`` to select other
enabled topics. RabbitMQ config stays on Kolla controllers and is never
sent to guest VMs.
With preconfigured databases, provision the DB/user and grants externally;
with ``use_common_mariadb_user=true``, use that account's existing password.

Upgrade from the DevStack layout
--------------------------------

#. Back up the Coast DB, current configuration, service credential refs,
   Coast CA files and health-manager state. Keep the existing DB and
   Barbican deployment; this is a service deployment change, not a data
   reset. If the DB host changes, restore the DB backup into the target
   database before bootstrap; the role does not copy data between DBs.
#. Build the Coast images from the reviewed commit and copy the updated
   role, network group vars and supplied play into Kolla. Keep the same
   inventory service hosts and management routes. Add the inputs above.
   For a direct-host AIO, move catalog FQDNs/VIPs to the Kolla VIPs and
   reserve a VIP distinct from each host's API address before deployment.
#. Run the ``genconfig`` command above. Review private files on disk
   without Ansible diff mode. Confirm the API command is ``uwsgi`` and
   its listener is the API interface address. Confirm TLS certificate
   identity, CA trust and HAProxy/ProxySQL config before cutover.
#. Schedule an API outage. Stop and disable the old DevStack Coast systemd
   services (API, conductor and health-manager if enabled), or stop the
   existing Coast containers through the role's ``stop`` action. Let active operations finish before stopping services. Do not
   change the RPC vhost during this cutover. Preserve
   the health-manager state volume and management interface. Check that
   the old API port has closed. The precheck does not stop old processes.
#. Run the ``deploy`` command above for the first cutover. It applies VIP
   listeners and ProxySQL routing, registers Keystone,
   creates the DB/user if needed, then runs DB upgrade before Coast starts.
   On later releases, use the same command with ``upgrade`` in place of
   ``deploy``. Migrations must succeed; do not bypass a failed bootstrap.
#. Check both internal and public catalog URLs through the VIP with trusted
   CA verification. An unauthenticated version discovery request must work;
   an authenticated stack list must return the same project resources as
   before. Confirm each container is healthy, the DB revision is at head,
   and conductor/health-manager logs have no credential or connection errors.
   Check guest mTLS and heartbeat reception. Reconfigure twice and confirm
   the second run has no configuration changes or service restarts.
#. If cutover fails, stop the new services and restore the prior service
   configuration and catalog URLs. Restore the DB backup before old code
   starts if schema compatibility is not established. Keep the old image
   references until the operator has accepted the new deployment.

Multi-controller HA and live TLS acceptance still require a staging cloud.
Offline syntax/render checks do not prove VIP failover or live DB grants.

Secret storage and permissions
------------------------------

``coast_secrets_backend`` defaults to ``barbican`` when ``enable_barbican``
is true and ``disabled`` otherwise. ``coast_max_secret_bytes`` defaults to
20000. The role renders these as ``[secrets] backend`` and
``max_secret_bytes`` in each service configuration. Disabled storage fails
closed. The API checks UTF-8 byte length before storage: env values use
the configured cap, file mounts use the smaller of that cap and 65536,
and compose uses the smaller of that cap and ``[api] max_compose_bytes``.
Oversized payloads return HTTP 400; exactly the limit is accepted. See
`secret configuration <../../doc/source/secrets.md#kolla-configuration>`_.

``coast_keystone_user_roles`` defaults to ``[admin]`` because the default
Designate provider uses sudo. Registration adds grants without revoking old
ones. See the `permission inventory and read-only checker
<../../doc/source/service-permissions.md>`_ before changing roles. Run
``python tools/check_service_permissions.py --config-file /etc/coast/coast.conf``
in an installed Coast environment. It prints fixed PASS/FAIL labels and
returns 1 for any failed probe; passing reads do not prove write access.

Token rotation requires updated guest images and migration
``0016_engine_token_rotation``. See `operator instructions
<../../doc/source/operations.md#engine-api-token-rotation>`_ for configuration
and the command, and `secret lifecycle
<../../doc/source/secrets.md#rotation>`_ for checkpoints and retries.
