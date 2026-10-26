Live Coolify contracts
======================

Template creates send the pinned compose from
``coast/drivers/coolify/templates/compose-<version>.json`` as
``docker_compose_raw``. Do not expect a live engine to accept the pinned
template key as ``type``: the engine refreshes its template list from the
CDN on every update check and that list drifts from the pinned tag.

Run against each supported release on an isolated development controller.
Set ``COAST_CONTRACT_COOLIFY_VERSION`` to the expected release; the
release registry default applies when it is unset. Live runs happen after
deployment on the test cloud, never as part of offline development.
The suite uses the documented public API. It creates a random
``coast-contract-<random>`` project and public git and image applications.
It runs serially and registers cleanup before each mutating operation.
Do not run two copies against the same controller.

Required environment variables:

* ``COAST_CONTRACT_COOLIFY_URL``: agent URL including ``/api/v1``
  (direct mode uses the engine base URL without ``/api/v1``).
* ``COAST_CONTRACT_COOLIFY_TOKEN_FILE``: path to a readable private token
  file. The token is read inside Python, never supplied on a command line.
* ``COAST_CONTRACT_COOLIFY_WORKER_UUIDS``: comma-separated existing worker
  UUIDs. Use two workers to exercise both placements.
* ``COAST_CONTRACT_COOLIFY_PRIVATE_KEY_UUID``: existing engine SSH key UUID.
* ``COAST_CONTRACT_COOLIFY_FALLBACK_ENABLED``: original team build-server
  fallback flag, exactly ``true`` or ``false``. Coast bootstrap sets this to
  ``false``. Supply the original value for a hand-built engine. Cleanup
  restores it using PATCH; setup never calls the non-allowlisted GET /team.

The previous shared setup read GET /team solely to save the original
fallback flag for cleanup. The production driver only writes PATCH /team;
the agent deliberately denies GET /team with HTTP 404. Supplying the
original flag above removes that setup-only read without expanding the
agent allowlist.

Coast-created controller (supported production transport)
---------------------------------------------------------

Use a disposable Coast stack, with no tenant workloads, and run from an
operator-controlled host that can reach its controller management address.
No controller SSH access is needed. The default transport is ``agent``:
it uses the production driver client, verified HTTPS and Coast mTLS.
Provide these additional variables:

* ``COAST_CONTRACT_COOLIFY_CLIENT_CERT``: path to the control-plane client
  certificate (``[controller_worker] agent_client_cert``).
* ``COAST_CONTRACT_COOLIFY_CLIENT_KEY``: path to its private key
  (``[controller_worker] agent_client_key``).
* ``COAST_CONTRACT_COOLIFY_CA_CERT``: path to the Coast CA bundle
  (``[certificates] ca_certificate``).
* ``COAST_CONTRACT_COOLIFY_MGMT_NETNS``: optional control-side management
  namespace (for example ``coast-mgmt``). Empty/unset uses the runner's
  current network namespace; entering another namespace requires privileges.

Retrieve the stack's engine token from its Barbican secret reference using
operator credentials and write it to a private temporary file (mode 0600).
Use that file as TOKEN_FILE; this is the engine bearer token, not a Keystone
or agent service-user token. Obtain worker and SSH key UUIDs via the
allowlisted GET /servers and GET /security/keys calls with the same mTLS
credentials and bearer token. PRIVATE_KEY_UUID is an engine record ID;
the runner does not need the worker SSH private key itself.

For example, after securely preparing the token file and setting the
worker/key UUID variables::

    export COAST_CONTRACT_COOLIFY_TRANSPORT=agent
    export COAST_CONTRACT_COOLIFY_URL=https://controller.example.com:9443/api/v1
    export COAST_CONTRACT_COOLIFY_TOKEN_FILE=/private/engine-token
    export COAST_CONTRACT_COOLIFY_CLIENT_CERT=/private/client.pem
    export COAST_CONTRACT_COOLIFY_CLIENT_KEY=/private/client-key.pem
    export COAST_CONTRACT_COOLIFY_CA_CERT=/private/ca.pem
    export COAST_CONTRACT_COOLIFY_FALLBACK_ENABLED=false
    tox -e contract-coolify

Set ``COAST_CONTRACT_COOLIFY_VERSION`` to the stack's exact engine version.
Repeat on each supported
release from ``coast.common.engine_versions`` and after the upgrade and
rollback acceptance actions; save the test counts, skips and engine version.
Use the unchanged tox command, without transport monkeypatches. Remove the
temporary token file after the run. These contracts validate live engine
behavior; proving upgrade/rollback also requires the real stack actions and
workload continuity checks described in the engine-upgrade spec.

Direct development transport
----------------------------

For a hand-built disposable engine, explicitly set
``COAST_CONTRACT_COOLIFY_TRANSPORT=direct``. Use its verified HTTPS engine
base URL, or a private loopback HTTP tunnel to the engine API. HTTP is a
test-only override; keep the tunnel bound to loopback. Coast-created
controllers should use the agent path above. Direct mode does not require
client credentials; CA_CERT may specify a private HTTPS trust bundle.

Optional settings:

* ``COAST_CONTRACT_COOLIFY_TIMEOUT``: polling deadline in seconds (900).
* ``COAST_CONTRACT_COOLIFY_GIT_URL``: public repository URL; must contain
  the Node example at ``/javascript/runtimes/nodejs`` on branch ``main``.
  Default: https://github.com/coollabsio/coolify-examples.
* ``COAST_CONTRACT_COOLIFY_SECOND_GIT_REF``: a different full commit SHA
  for the second deployment. Default: the example repository's layout
  commit ``ff425d7999a5bb92bc0e977e89567d68c2a6b45b``. Set this variable
  when using a different repository. Rollback must restore the first SHA,
  not just report another successful deployment.

Run ``tox -e contract-coolify``. Missing required settings skip live tests.
Invalid supplied settings fail instead of silently skipping.
The local transport test still checks that production rejects HTTP.

Plain HTTP is supported only by a test-local transport override (Q60).
Production transport, including direct development mode, requires verified
HTTPS. No production option or security behavior changes.

The dev engine rejects duplicate SSH keys (Q61). It also rejects new
deployment-only servers when there is no usable build server (Q62).
The suite validates the existing worker registrations, temporarily renames
workers to deterministic Coast names, and restores their names during
cleanup. Registration restarts their proxies. A disposable, unvalidated
server record with a documentation-only IP tests unregister; it is never
contacted. Existing workers and the existing SSH key are never deleted.
The team build-server fallback flag is restored after bootstrap tests.

Mounts are created and removed before deployment, so no host directory or
persistent volume is created. Example domains are removed before deploying
and do not require DNS or certificates. The suite checks engine state,
not public HTTPS service delivery. A process kill can prevent cleanup;
inspect and remove only this run's project/apps and restore worker names
and the team flag before another run.

GitLab keeps the existing manual job. Configure the required variables in
CI/CD settings. Use a protected file-type token variable, mask its value,
and never enable debug tracing. The variable expands to a path, not a token.
Do not store a token in YAML or upload it as an artifact. To record fixtures,
set ``COAST_CONTRACT_COOLIFY_RECORD=1`` before running
``tox -e contract-coolify``. Run from a clean worktree. The suite writes
sanitized request/response pairs to the unit fixture directory only after
the full lifecycle and registered cleanup pass. Raw bodies stay in memory;
headers are not recorded. Inspect the diff and run the fixture secret-scan
unit test before committing. Recording replaces ``live.json`` and
``v4.4.3.json``; ``requests.json`` remains the pinned OpenAPI request contract
and ``synthetic.json`` contains explicit offline edge cases.

Multiple build servers (Q13)
-----------------------------

The optional build-role tests use GET /servers/{uuid}/resources for safe
role conversion and placement checks through the production agent mTLS
transport. The controller agent must include the read-only inventory route;
older agents reject it. Update the controller image/agent before rerunning;
do not substitute direct transport or skip enabled contracts.

The separate build-server test also requires:

* ``COAST_CONTRACT_COOLIFY_BUILD_REGISTRY_TEMPLATE``: an image name with
  both ``{environment_id}`` and ``{app_id}``, such as
  ``registry.example.com/{environment_id}/{app_id}``.
* ``COAST_CONTRACT_COOLIFY_RUN_SERVER_UUID``: a distinct existing run
  server, not one of the workers that will become build-only servers.
* ``COAST_CONTRACT_COOLIFY_REGISTRY_URL``: registry base URL for read-only
  tag verification; default is HTTPS at the template's registry host.
  The isolated approved dev registry may use HTTP through an SSH tunnel
  bound to loopback on the test host; no public registry ingress is needed.
  This endpoint must expose the test repository's tag list without credentials.

The regular worker UUID list supplies at least two dedicated dev workers.
The test refuses build-role conversion if either worker hosts resources.
It preserves the original roles and names, disables team fallback through
bootstrap, configures the existing driver registry option and deploys a
public git app with ``use_build_server=true``. It verifies builder selection,
image push and pull messages, the registry's commit tag, ACTIVE app status,
placement on the distinct run server and no resources on either builder.
It also tests fresh deployment-only registration with the builders present.
All run-owned app/server/project records are removed during teardown; the
original worker roles, run-server name and team fallback flag are restored.
The registry and Docker daemon settings are operator-managed dev setup,
not test-owned resources. Registry image data remains for operator cleanup.

Coolify picks a usable build server at random; the public API does not
select one for an individual build. The test runs at least two builds and
continues until both builders have been observed or the sampling bound is
reached. Set ``COAST_CONTRACT_COOLIFY_BUILD_ATTEMPTS`` to change that bound
(default eight, minimum two). Build cache is disabled so every deployment
exercises a build rather than only reusing an image. Test details report
which builder ran each build and the final coverage. Seeing only one
builder in a bounded random sample is not a contract failure; the live MR
verification must report the actual observed coverage, not claim both ran.

Use ``tox -e contract-coolify -- BuildServersTest`` for only this contract.
Do not enable fixture recording for this test. Refer to
``tools/coolify/README.rst`` for the approved DEV ONLY registry setup.

Selection reference: https://coolify.io/docs/knowledge-base/server/build-server

Local agent bootstrap contract
------------------------------

On a fresh disposable controller with the selected stock release installed,
root-user setup complete, loopback API access permitted, and no prior
Coast bootstrap token, run::

    COAST_CONTRACT_COOLIFY_AGENT_BOOTSTRAP=1 tox -e contract-coolify -- test_agent_bootstrap

Run as root on that guest only. The test creates and rotates a root token;
it verifies that the old token is rejected and the new token works on
http://127.0.0.1:8000. It leaves the new managed token in the engine.
Destroy the disposable controller after this destructive contract test.
No token or backup is written to the test output.

M4 comparison contracts (c4rc2)
-------------------------------

Run ``tox -e contract-coolify -- test_reconcile`` in the existing manual
CI job, against each isolated supported engine release. Without the required
settings, these tests skip. This command does not run the conductor loop.
Activity events, scan bounds and no-restart behavior are unchanged
(Q110/Q112). The approved compose domain fix uses the engine-specific
field and fails closed on unknown or ambiguous target maps.

The git, image and compose tests use the required settings above. Each test
creates one application, checks clean and changed env/domain observations,
preserves Q52 rejection of non-empty env assignments, and verifies running
and stopped status. Comparison does not start or restart a stopped workload.
Setup deploys and stops the test application explicitly. Compose deployment
loads the repository file before the test seeds its known ``api`` domain
target through the existing public PATCH route. Production does not select
an unlisted compose target. Polling uses the
existing finite timeout; there are no unbounded deployment retries. Cleanup
removes the run-owned application and its parent records. Response recording
is disabled for these tests, including compose responses.

Compose uses the example repository's
``/internal/compose-parser-tests/docker-compose-simple.yaml`` on ``main``.
A custom ``COAST_CONTRACT_COOLIFY_GIT_URL`` must supply this path as well as
the Node example used by the git contract. Domains use ``example.com``.
Git/image test domains are cleared before deployment. Compose deploys first
without test domains, then clears them after comparison. No DNS or
certificates are required.

The separate template test requires URL, TOKEN_FILE, the selected transport
credentials and ``COAST_CONTRACT_COOLIFY_TEMPLATE_UUID`` (no worker/key or
fallback input): an existing, idle template service
on the disposable engine, with at least one application. It uses GET only,
does not deploy, stop, change settings or delete the template, and does not
record generated values. Keep this resource idle during the test so that
status and settings do not change between observations. Template coverage
skips separately if its UUID is unset. The operator owns its cleanup.

These tests provide executable contracts, not evidence of a completed live
run. Live git/image/compose and read-only template results must be recorded
from the manual job before claiming pinned-engine validation.

API request schemas were checked against:
https://github.com/coollabsio/coolify/blob/v4.4.3/openapi.json

Acceptance evidence for issue 56
-------------------------------

Status: **open; live transport validation is not complete**. The offline
``tox -e contract-coolify`` run passed the local HTTP-rejection check and
skipped all 12 live tests because live settings were absent. It did not
observe an engine version and provides no evidence that agent mTLS works
against either required release or after an engine transition.

Independent post-merge deployment acceptance owns the real-stack runs and
is the gate for closing issue 56. This change supplies the runnable suite;
it does not claim real-stack validation. Acceptance should run the unchanged
suite using the agent transport documented above and attach four real,
unmocked results:

* Source release: expected and observed engine version, full test results,
  every skip reason and cleanup outcome.
* Target release: the same evidence on a controller created on that release.
* After upgrade: stack action outcome, expected and observed engine version,
  full test results, every skip reason and cleanup outcome.
* After rollback: the same evidence after the real rollback action.

Select the source and target from ``coast.common.engine_versions``. Preserve
sanitized test output for each run; do not attach credentials or tokens.
Optional skips must identify the missing setting or deliberately excluded
local-only contract. Missing all live credentials is not release validation.
The real transition checks must also record workload continuity separately.

Dedicated builders in shared live fixtures
-----------------------------------------

The lifecycle and git/compose comparison contracts also need
``COAST_CONTRACT_COOLIFY_BUILD_REGISTRY_TEMPLATE`` when the controller has
any dedicated build server. Supply a reachable registry image template
containing both ``{environment_id}`` and ``{app_id}``, for example
``registry.example.com/{environment_id}/{app_id}``. Configure the disposable
build and worker servers' registry access before running the suite.
The shared fixture passes this input to the real driver. Without it,
only contracts requiring a Git build skip with an explicit prerequisite
reason; image, template and transport contracts still run. Invalid supplied
templates and real engine failures remain failures.
