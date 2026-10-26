Coolify bootstrap prototype (Q4)
================================

Engine releases and defaults come from ``coast/common/engine_versions.py``.
Upstream source paths below refer to the selected release tag. Dated
evidence refers to the pinned release, not the new-stack default.

The maintained command now reads the supported release registry and the
immutable guest image's baked version. Run it with Coast installed or with
``PYTHONPATH=/path/to/coast``. The initial validation below records the older
release; for current acceptance use a freshly built guest for each supported
release and set the contract suite's expected version explicitly.

Status: **verified on the pinned release, 2026-10-10**.
This is an M2 experiment,
not coast-agent or a production provisioning flow. It uses only Python
3.10+ standard-library modules and the stock Docker CLI on the controller.
Do not use it against a shared or production Coolify instance.

The procedure and checks below are the dated 2026-10-10 record and name
the release used then. For a current run, use a release listed in
``coast/common/engine_versions.py`` in place of that version. The script
expects ``engine_versions.build_version()``: the guest's baked version, or
the registry default in a source tree.

Hand-built dev VMs
------------------

1. Create a dedicated Ubuntu controller VM and a separate worker VM.
   Select sizes, networks and addresses for the operator's cloud. Use
   one environment only. Limit controller ingress to the operator's SSH
   connection. Do not expose the Coolify UI or API to the internet or
   workers. Use an SSH tunnel for the UI when required. Worker ingress,
   DNS and SSH access must follow the Coolify server requirements.
2. Read the `stock installation instructions`_ and download the stock
   installer from the pinned tag. Review it before execution::

       # Set VERSION to a supported release from engine_versions.py.
       installer=https://raw.githubusercontent.com/coollabsio/coolify
       curl -fsSL "$installer/v$VERSION/scripts/install.sh" \
           -o install-coolify.sh
       chmod 0700 install-coolify.sh

   In a root shell, set ``ROOT_USERNAME``, ``ROOT_USER_EMAIL`` and
   ``ROOT_USER_PASSWORD`` from an environment-unique credential source.
   Do not put passwords in command arguments, shell history or this repo.
   Disable shell tracing. Set ``AUTOUPDATE=false`` and run::

       bash install-coolify.sh "$VERSION"
       unset ROOT_USERNAME ROOT_USER_EMAIL ROOT_USER_PASSWORD

   The positional argument pins the Coolify image. The stock installer
   still downloads supporting files and helper images from its CDN; this
   is not an offline or reproducible golden-image build. Protect
   ``/data/coolify/source/.env`` and its backups: they contain secrets.
   Confirm ``AUTOUPDATE=false`` in that file without printing the file.
   Confirm that the root user was created and public registration is off.
   The installer validates the email and password; do not proceed if root
   creation failed. Wait for local ``/api/v1/health`` to return HTTP 200.
3. On the controller, create a root-owned private directory outside the
   repo, then run the prototype::

       install -d -m 0700 /root/coast-bootstrap
       python3 tools/coolify/bootstrap_token.py \
           --output /root/coast-bootstrap/token

   The default container is ``coolify`` and the local API port is ``8000``.
   Use ``--container`` or ``--port`` if the stock instance uses other
   values. The script permits only a stock ``coollabsio/coolify:<version>``
   image, where ``<version>`` is ``engine_versions.build_version()``
   (optionally prefixed with ``docker.io/``). It checks the engine
   version before token creation, creates a root-user/root-team token with
   the ``root`` ability, calls ``POST /api/v1/enable``, then verifies
   ``GET /api/v1/version`` through ``http://127.0.0.1:8000`` by default.
   It refuses proxies and redirects. No token is sent to stdout or logs.
   The token file is owned by root with mode ``0600``. Do not use ``cat``
   or shell tracing to inspect it. API enablement uses the documented API,
   not a direct settings-table update.
4. Through an SSH tunnel, open the Coolify UI, sign in as root, and check
   that API access is enabled. Restrict the API allowlist to loopback in
   instance settings. Keep network ingress blocked even before this step.
   This prototype does not change firewall rules or API allowlists.
5. Prepare a dedicated SSH key for this environment. Install its public
   key on the worker for the Coolify SSH user. In Coolify, add the private
   key under Security, then add a server by its worker IP, SSH port, user
   and key. Select a deployment server, validate the connection, and start
   the proxy. Follow the `server instructions`_. Do not use this key or
   worker for another environment.

Repeat runs and recovery
------------------------

Repeat the same command with the same output path. It reads the existing
private file, enables the API again and verifies the version. It does not
create another token. A directory lock serializes local runs. A container
lock and fixed token name prevent duplicate creation with other paths.

A temporary container copy is written with mode ``0600`` under
``storage/app/ssh/.coast-bootstrap-token`` (the stock persistent SSH bind
mount). ``docker cp`` copies it into a private staging directory; an atomic
rename publishes the host file. After API verification, the script removes
the container copy. A failure leaves the private files for a retry. Neither
Docker nor PHP stdout/stderr is forwarded. Error messages are generic.

If the token file is empty, invalid, revoked, linked, or has unsafe
permissions, the script fails. It never overwrites or rotates it. If token
creation succeeded but the plaintext file was lost, the named token blocks
another creation. Revoke only ``coast-agent-bootstrap-v1`` through the
Coolify UI, then remove the failed prototype files and retry on the isolated
dev instance. Do not revoke other tokens. This is manual experiment recovery,
not the production lifecycle's rebuild/revert implementation.

For the dev driver lane, import the token into Barbican without displaying
it and pass only its secret reference to Coast. Preserve the host file
until retry testing is complete, then delete it after secure transfer.
Never commit token files, the Coolify ``.env``, root credentials or SSH keys.

Live verification and remaining checks
-------------------------------------

* Run against the pinned stock release; verify root user ID 0 and
  root team ID 0, the
  ``root`` ability and API enablement. Do not print the token.
* Repeat with the same output path and confirm exactly one named token in
  the UI. Check mode ``0600`` with ``stat``. Save stdout/stderr and confirm
  they contain no token (success produces no output).
* Check the API version at ``http://127.0.0.1:8000/api/v1/version`` using a
  client that reads the token file internally, not a command-line header.
* Interrupt after token creation and before host-file publication; retry
  and confirm that the same token is copied. Test failure recovery above.
* Validate the worker by IP. Confirm that controller UI/API access from
  the worker and internet remains blocked.

On 2026-10-10, the prototype passed on the pinned stock container: both
runs exited 0 with empty stdout/stderr; the root-owned host file had mode
0600; local version authentication returned HTTP 200 and the pinned
release; the repeat
run reused the same token with exactly one named token; the container copy
was absent. Root user/team ID 0 and the ``root`` ability were confirmed.
The test token and host file were then removed. The existing token record
was unchanged. See ``doc/source/driver-interface.md`` for the evidence.

The M2 Q4 checkbox is complete. Interrupted-publication recovery and network
isolation checks above were not exercised by this verification. This does
not implement or validate the production coast-agent lifecycle.

Sources
-------

* `API authorization`_: documented UI token creation and token abilities.
* ``app/Models/User.php`` at the selected upstream tag: ``createToken``
  reads ``session('currentTeam')``;
  the prototype sets it explicitly rather than relying on UI state.
* ``database/seeders/RootUserSeeder.php`` at the selected upstream tag:
  root ID 0, team ID 0 and owner membership.
* ``routes/api.php`` at the selected upstream tag: enablement accepts
  a token even when API access is off.

.. _stock installation instructions: https://coolify.io/docs/get-started/installation
.. _server instructions: https://coolify.io/docs/servers
.. _API authorization: https://coolify.io/docs/api-reference/authorization


DEV ONLY: Q13 plain-HTTP registry
---------------------------------

James approved this isolated development experiment on 2026-10-10.
It is not a production provisioning procedure. Production stacks use the
Q125 registry on the controller environment address, with TLS signed by
the Coast CA. Never copy the insecure setting into images, operator
defaults or production hosts. This registry has no authentication.
Restrict port 5000 to the development VMs using cloud security groups.

Keep the development SSH key in a private directory, mode ``0600``, and
exclude it from Git. Use ``StrictHostKeyChecking=accept-new`` with a private
``UserKnownHostsFile``. Do not print keys, API tokens or Docker credentials.
Use the operator's configured controller address below, not a fixed cloud
address. Run commands as root over the approved SSH connection.

On all three VMs, back up ``/etc/docker/daemon.json`` with mode ``0600``
before changing it. If absent, record that fact for rollback. Parse the
existing JSON and merge the controller's ``host:5000`` into the
``insecure-registries`` list; preserve every other setting. Validate the JSON,
then restart Docker. This interrupts containers, so use a maintenance window.
Wait for the controller's Coolify, database, Redis and sentinel containers
to become healthy and for the authenticated public version API to respond.
Do not inspect or change Coolify internals.

On the controller only::

    install -d -m 0750 /data/registry
    docker run -d --name coast-dev-registry --restart=always \
        -p 5000:5000 -v /data/registry:/var/lib/registry registry:2

From both workers, check ``http://<controller-host>:5000/v2/``. A timeout
requires an operator security-group/network check; do not open public
access as a workaround. Tag a small image with a unique development probe
repository, push it, remove the local tag, then pull it again. Successful
pull after local removal proves registry connectivity on that worker.

For the contracts, set ``COAST_CONTRACT_COOLIFY_BUILD_REGISTRY_TEMPLATE``
to ``<controller-host>:5000/{environment_id}/{app_id}`` and set
``COAST_CONTRACT_COOLIFY_RUN_SERVER_UUID`` to the controller's existing
Coolify server UUID. The workers must have no hosted resources before they
are temporarily switched to build-only roles. The controller is the test
run server. Image names are configured through the driver's existing option;
Coolify still requires a Docker login file on each builder. Use the
documented ``POST /servers/{uuid}/registries`` login API on both builders
and the run server with explicit non-secret development placeholder
username/password values. An unauthenticated registry accepts the login;
Docker creates the required file for the configured SSH user. Never print
or read that file into test evidence. Preserve any existing Docker logins.

Keep the approved dev registry and daemon settings for repeat contracts.
To remove the experiment, stop/remove only ``coast-dev-registry``, restore
each original daemon file (or remove it if originally absent), restart Docker
and verify Coolify health. Remove ``/data/registry`` only after confirming
that its development images are no longer needed. Preserve unrelated
containers, daemon settings and credentials.

References: `Docker insecure registries`_ and `Coolify registry setup`_.

.. _Docker insecure registries: https://docs.docker.com/reference/cli/dockerd/#insecure-registries
.. _Coolify registry setup: https://coolify.io/docs/applications/builds/docker-registries
