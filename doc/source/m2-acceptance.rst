M2 acceptance run against dev Coolify
=====================================

Engine releases and defaults come from ``coast/common/engine_versions.py``.
Upstream source paths below refer to the selected release tag. Dated
evidence refers to the pinned release, not the new-stack default.

.. note::

   Dated acceptance evidence, not current setup instructions. Routes,
   commands and findings describe the recorded revision. Use the current
   API reference and installation guide for new runs.

MR title: **[M2] Acceptance run against dev Coolify**.
Branch: ``m2/acceptance``. Base: ``7b1da58`` on main.
Run date: 2026-10-10 UTC. This is evidence from an isolated development
engine, not a production deployment guide. Controller and worker addresses
are replaced with operator-supplied variables. Public test hostnames use
example.com placeholders. No engine token, private key or secret value is
included.

Scope and local setup
---------------------

The requests below were raw ``curl`` requests to Coast API microversion
1.0. They did not call an OSC client or substitute direct engine deploys.
The API, conductor, objects, DB and Coolify driver came from this worktree.
SQLite used the repository's migrations. ``fake://`` RPC requires one
process: the local runner started ``coast.conductor.service.Service`` and
served ``coast.api.app.load_app`` with ``coast.cmd.api.RequestHandler``.
It did not run two independent CLI processes with disconnected fake queues.

The runner used the existing ``noauth`` Paste pipeline, bound only to
loopback at port **18776**. Test requests supplied project-scoped identity
headers. A private file-backed ``SecretStore`` accepted only
``https://secrets.example.com/acceptance-token``. The DB stored that opaque
reference, not the token. The test runner selected ``ContractDriver`` from
``coast.tests.contract.coolify.test_live`` for the dev HTTP controller.
It allowed only the exact supplied controller URL through the adoption
validator. Production URL validation and the default secret store were not
changed (Q65).

The API ran in a native Python thread. The main thread waited through
``oslo.service.loopingcall``, so the existing eventlet service timer could
run. An initial blocking WSGI main thread prevented periodic reconciliation;
a restart recovered the known second deployment reference without another
enqueue. The optional oslo.service threading backend was not used: its
optional dependency was absent, and no dependency was added.
See the `oslo.service backend documentation`_.

After the token file appeared, the run moved it into a private directory.
Local Git excludes covered private artifacts. Setup commands, with
addresses removed::

   mkdir -p .secrets
   chmod 0700 .secrets
   mv .coolify-token .secrets/coolify-token
   chmod 0600 .secrets/coolify-token
   export PYTHONPATH="$PWD"
   export COAST_URL=http://localhost:18776
   # COOLIFY_URL, WORKER_IP and WORKER_UUID are supplied privately.
   # Test Python: /tmp/coast-ci-functional312/py3/bin/python (3.12.15).
   PYTHON=/tmp/coast-ci-functional312/py3/bin/python
   "$PYTHON" -c 'from coast.cmd.db_manage import main; main()' \
       --config-file .secrets/acceptance/coast.conf upgrade

Relevant private configuration (values in braces are supplied for the run)::

   [DEFAULT]
   transport_url = fake://
   [api]
   bind_host = localhost
   bind_port = 18776
   auth_strategy = noauth
   api_paste_config = {worktree}/etc/coast/api-paste.ini
   [database]
   connection = sqlite:///{worktree}/.secrets/acceptance/coast.sqlite
   [controller_worker]
   driver = coolify_v4
   enabled_drivers = coolify_v4
   [coolify]
   insecure_direct_url = true
   adopted_endpoints = {environment_id}:{COOLIFY_URL}
   [dns]
   default_app_domain = apps.example.com
   [oslo_messaging_notifications]
   driver = noop
   [conductor]
   workers = 1
   deploy_reconcile_interval = 3

The token file and directory had modes 0600 and 0700. Test scripts and raw
captures were private local artifacts, not committed source. The local
runner supplied these test-only seams before starting the services::

   from pathlib import Path
   from coast.api import secret_store
   from coast.common import endpoints
   from coast.drivers import loader
   from coast.tests.contract.coolify.test_live import ContractDriver

   class FileStore(secret_store.SecretStore):
       def get(self, context, reference):
           if reference != 'https://secrets.example.com/acceptance-token':
               raise ValueError('Unknown test secret reference')
           return Path('.secrets/coolify-token').read_text().strip()

   secret_store.STORE = FileStore()
   original_validate = endpoints.validate_url
   endpoints.validate_url = lambda value: (
       value if value == COOLIFY_URL else original_validate(value))
   original_loader = loader.load_driver
   loader.load_driver = lambda name, conf: (
       ContractDriver(conf) if name == 'coolify_v4'
       else original_loader(name, conf))

The operator command was the worktree's ``coast.cmd.manage.main`` with
those seams loaded. A run-owned project was first created through the
public ``POST /api/v1/projects`` API. Adoption then used::

   "$PYTHON" /tmp/coast-m2-acceptance/local.py adopt \
       --config-file .secrets/acceptance/coast.conf \
       adopt-environment --project-id acceptance-project \
       --name acceptance --url "$COOLIFY_URL" \
       --token-ref https://secrets.example.com/acceptance-token \
       --server-ip "$WORKER_IP" --server-engine-ref "$WORKER_UUID" \
       --engine-ref "$PROJECT_UUID"

The same private runner then started the local services::

   "$PYTHON" /tmp/coast-m2-acceptance/local.py serve \
       --config-file .secrets/acceptance/coast.conf

The resulting environment ID was
``4d6b6dcc-0c70-4099-8e55-b9cd8298929b``; its worker ID was
``7b089ccb-3dc2-4349-a491-fd156da3c961``. The engine version check
returned **the pinned release**. The supplied existing worker was used;
no worker or key was created for this acceptance app.

The first app create failed because the driver located servers by
``coast-<Coast-server-id>``, not the adopted server ``engine_ref``.
The existing worker was temporarily renamed through documented
``PATCH /api/v1/servers/{uuid}``, as the contract suite does (Q66).
Its original name was saved privately and restored before the live suite.
That run used the Q66 rename. Q68 now places an appgroup on the
recorded Coolify server uuid when the Coast server has an
``engine_ref``. A later run does not need this rename.

Raw Coast requests and results
-------------------------------

All Coast requests used this raw curl wrapper::

   coast_curl() {
       curl -sS --fail-with-body \
           -H 'X-Project-Id: acceptance-project' \
           -H 'X-User-Id: acceptance-user' \
           -H 'X-Roles: reader,member,manager' \
           -H 'OpenStack-API-Version: app-hosting 1.0' \
           -H 'Content-Type: application/json' "$@"
   }

Create body::

   {"app": {
     "name": "coast-accept-cc708b",
     "server_id": "7b089ccb-3dc2-4349-a491-fd156da3c961",
     "ports": "3000", "deploy": true,
     "source": {
       "type": "git",
       "repo_url": "https://github.com/coollabsio/coolify-examples",
       "branch": "main", "build_pack": "nixpacks",
       "base_directory": "/javascript/runtimes/nodejs"
     }
   }}

Requests (the IDs below came from the response bodies)::

   coast_curl -X POST --data-binary @create.json "$COAST_URL/v1/apps"
   APP=00b2433e-18a1-4dd9-83f4-694d138dd183
   FIRST=792a71d2-32df-4186-9b8e-0799db0da63e
   coast_curl "$COAST_URL/v1/apps/$APP/deployments/$FIRST"
   DOMAIN=coast-accept-cc708b-00b2.apps.example.com
   curl -sS -o first-body.json \
       -w '%{http_code} %{ssl_verify_result}\n' "https://$DOMAIN"
   coast_curl -X POST -d '{"deployment":{
     "source_ref":"ff425d7999a5bb92bc0e977e89567d68c2a6b45b"}}' \
       "$COAST_URL/v1/apps/$APP/deployments"
   SECOND=81bf9bf3-d2a6-4475-87cd-f57aaf1f447c
   coast_curl "$COAST_URL/v1/apps/$APP/deployments/$SECOND"
   curl -sS -o second-body.json "https://$DOMAIN"
   coast_curl -X POST -d '{"rollback":{}}' \
       "$COAST_URL/v1/apps/$APP/deployments/$FIRST/action"
   ROLLBACK=ec50e80f-b20c-43eb-a047-2923fdd79f34
   coast_curl "$COAST_URL/v1/apps/$APP/deployments/$ROLLBACK"
   curl -sS -o rollback-body.json \
       -w '%{http_code} %{ssl_verify_result}\n' "https://$DOMAIN"
   cmp first-body.json rollback-body.json
   coast_curl "$COAST_URL/v1/apps/$APP/deployments/$FIRST/log"
   coast_curl "$COAST_URL/v1/apps/$APP/logs"

Observed results, UTC:

* 16:41:00: create returned HTTP **202**, with app and deployment IDs.
* 16:42:58: first deployment **ACTIVE**;
  ``resolved_ref=4c57d5980fc9268546530080cce45aaf4a7471d2``.
* 16:43:10: HTTPS returned **200**, ``ssl_verify_result=0``;
  body ``{"hello":"from nodejs #2"}``. No ``-k`` was used.
* 16:43:27: second deployment request returned HTTP **202**.
* 16:46:52: Coast reported the second deployment **ACTIVE**;
  ``resolved_ref=ff425d7999a5bb92bc0e977e89567d68c2a6b45b``.
  Engine status was already ``finished`` before the local timer restart.
* 16:46:52: rollback request returned HTTP **202**.
* 16:47:14: rollback **ACTIVE**;
  ``source_ref`` and ``resolved_ref`` both equaled the first full SHA.
  ``rollback_of`` equaled the first deployment ID; app current deployment
  equaled the rollback ID. The second deployment became **ROLLED_BACK**.
* 16:48:05: after rollback, HTTPS returned **200**, verification **0**, and the
  first and rollback response bytes matched. The SHA256 of both bodies was
  ``4b33e14f06dfee486ba74f3f96df5c6807059b64aa3b96bf0271b10b50b51b88``.
  Both selected source commits serve the same sample body; distinct source
  revisions are proved by Coast's reported full SHAs, not body difference.
* The certificate issuer was **Let's Encrypt YR2**. The verified DNS SAN
  was the exact sslip domain. Validity: 2026-10-10 15:44:30 UTC to
  2027-01-08 15:44:29 UTC. The contract suite's proxy restart caused one
  transient certificate mismatch; a fresh post-rollback request then passed
  normal certificate verification. Failed curl output was not used as
  successful body evidence.
* 16:47:51: deployment log returned HTTP **200**, ``complete=true``,
  **154 lines**; appgroup logs returned HTTP **200**, **7 lines**.
  A safe runtime example was ``{"stream":"stdout","line":"hello"}``.
  Returned records retained ``ts``, ``stream`` and ``line``.

Defects fixed within the existing contract
------------------------------------------

Before the fix, Coast returned HTTP 200 with empty lines for both real
Coolify log endpoints. The existing API specification already requires
live driver logs through a bounded synchronous RPC call. The fix connects
that path, reloads resources by request project in the conductor, applies
``since`` filtering, and preserves driver secret masking. Noop still
returns the M1 empty payloads. ``tail=0`` returns no lines.

A failed create without ``engine_ref`` also could not be deleted: status
lookup required a reference before idempotent driver deletion was reached.
Deletion now invokes deterministic-name cleanup even when the reference is
missing. Regression tests cover real log content, completion, pending
references, timestamp filtering, zero tail, secret masking, backend error
sanitization, cross-project isolation and failed-create deletion. No API
fields, DB columns, driver methods or production security defaults changed.

Live contract suite, cleanup and gates
--------------------------------------

The live suite was run without fixture recording. Private file settings::

   export COAST_CONTRACT_COOLIFY_URL="$COOLIFY_URL"
   export COAST_CONTRACT_COOLIFY_TOKEN_FILE="$PWD/.secrets/coolify-token"
   export COAST_CONTRACT_COOLIFY_WORKER_UUIDS="$WORKER_UUID"
   export COAST_CONTRACT_COOLIFY_PRIVATE_KEY_UUID="$EXISTING_KEY_UUID"
   tox -e contract-coolify

The single supplied worker was tested. Multi-build-server acceptance remains
its separate roadmap item. The suite temporarily changes worker names and
proxy state and restores them during registered cleanup. It does not prove
production authentication, provisioning or guest network isolation.

At 16:49:59 UTC, the pinned contract suite exited **0**::

   Ran: 2 tests in 138.0093 sec.
   Passed: 2; Skipped: 0; Failed: 0
   contract-coolify: OK

At 16:49:53 UTC, raw Coast DELETE requests for the successful acceptance
app and the earlier failed app returned HTTP **202**. At 16:50:40 UTC,
project-scoped GET requests returned **404** for both. Engine-side cleanup
removed the run-owned project through the documented project DELETE API.
A separate public-API check at 16:50:33 UTC confirmed project GET **404**,
no acceptance apps or domain, no contract projects, and the original worker
name restored. The existing worker and SSH key were retained. The local
API/conductor runner was stopped through its private stop-file loop.

Acceptance result: **all five requested checks passed**, with the local
setup limits stated above. This does not complete the separate
multi-build-server roadmap item.

.. list-table:: Acceptance results
   :header-rows: 1
   :widths: 10 35 55

   * - Check
     - Result
     - Evidence
   * - 1
     - PASS: public Node app deploy
     - Coast ACTIVE and a full resolved commit SHA.
   * - 2
     - PASS: public HTTPS domain
     - HTTP 200, certificate verification 0, Let's Encrypt issuer.
   * - 3
     - PASS: second ref and rollback
     - Different second SHA; rollback restored first SHA and response bytes.
   * - 4
     - PASS: both Coast log endpoints
     - 154 deployment lines and 7 runtime lines through live RPC.
   * - 5
     - PASS: pinned live contract suite
     - the pinned Coolify release; 2 passed, 0 skipped, 0 failed.

Review findings and questions
-----------------------------

.. list-table:: Review
   :header-rows: 1
   :widths: 12 38 50

   * - Status
     - Finding or question
     - Resolution or remaining work
   * - RESOLVED
     - Q65: local auth, token storage and HTTP
     - Existing noauth, loopback, private test store and contract override.
   * - RESOLVED
     - Q66: dev worker naming choice
     - Temporarily renamed through public API; original name restored.
   * - RESOLVED
     - Q68: general adopted-server placement
     - Placement uses the recorded Coolify server uuid. Name lookup
       remains only when that uuid is absent. A missing target fails
       closed.
   * - RESOLVED
     - Q67: real log endpoints returned no content
     - Existing API contract implemented through bounded live RPC.
   * - RESOLVED
     - Failed-create deletion required an absent engine ref
     - Idempotent deterministic-name deletion; regression test passes.
   * - RESOLVED
     - Local periodic timer blocked by WSGI main thread
     - Native API thread and cooperative oslo.service main-thread wait.
   * - RESOLVED
     - Transient certificate mismatch during proxy startup/restart
     - Fresh verified HTTPS request passed; no insecure TLS bypass.
   * - DEFERRED
     - Pre-existing Q59: uncertain enqueue discovery
     - Deferred (future). Reference-only recovery stays in force.
       This run used known refs.

Tox gates
---------

The required final gate command was::

   tox -e py3,pep8,docs,genconfig,genpolicy

Final results (the unchanged code was not re-tested after documentation
format fixes)::

   py3: OK; 558 passed, 0 skipped, 0 failed
   pep8: OK; flake8, bandit and doc8
   docs: OK; sphinx-build -W
   genconfig: OK
   genpolicy: OK

The first combined gate found short RST heading underlines. Those headings
were corrected; only the affected pep8/docs environments were repeated.
The initial focused test also exposed an incorrect assertion about the
last HTTP request after log masking fetched environment variables. The
assertion was removed; the content, masking and isolation checks passed.
Private runner scripts initially sat under the worktree and were included
by flake8. They were moved to the private temporary runner directory.
No test-only security override or credential file was added to Git.

.. _oslo.service backend documentation: https://docs.openstack.org/oslo.service/2026.1/user/backend.html
