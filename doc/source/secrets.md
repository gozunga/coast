# Secrets and Barbican

This page lists every secret Coast handles, where it is stored, who can
read it, when Coast creates, reads and deletes it, and what happens when
Barbican is unavailable. Module paths are relative to the repository root.

## Set app and service environment variables

```console
$ openstack coast env set --service web APP_MODE=production --build-time
$ openstack coast env set --service web API_TOKEN --secret --no-build-time
$ openstack coast env list --service web --long
$ openstack coast env unset --service web API_TOKEN
$ openstack coast deploy web
```

`--service` selects an app or service by name. `--build-time` exposes values
at build and runtime; `--no-build-time` restricts them to runtime. Use
runtime-only credentials because builds can copy values into image layers
or output. `--secret` makes later reads write-only: a bare key prompts
without echo, keeping its value out of shell history. `--long` adds the
Build time column. `unset` removes the named key; redeploy to apply changes.
Template variables are runtime-only. Non-template workloads default to
build-time at API 1.6 and later; explicitly select flags for credentials.

Every value, including a non-secret value, goes to Barbican. Coast stores
its reference in `secrets.value_ref`, together with the key, flags and
version. The API returns `value: null` for a secret; the CLI displays
`********` for every value, secret or plain, in `env list` table output
and with `-f json` (JSON output). There is no `env show` command. Plain
values are readable through the API; secret values are never returned.
Removing a key does not discard a reference pinned by a retained
deployment. See [Lifecycle](#lifecycle) for pruning and deletion.

To rotate an env secret, set the same key again with `--secret` and a bare
key for a hidden prompt, then deploy:

```console
$ openstack coast env set --service web API_TOKEN --secret --no-build-time
$ openstack coast deploy web
```

`env unset --service web API_TOKEN` removes it from desired configuration;
deploy applies the removal. Rotation and unset preserve old values referenced
by retained deployment snapshots, regardless of status. Deployment history
is not automatically trimmed; unreferenced versions are pruned after commit.
App or stack deletion removes retained references.

## Summary

- Coast stores secret payloads in Barbican, in the **service project**,
  using the `[service_auth]` credentials of the `coast` user. A tenant
  request never selects the Barbican project or credentials
  (`coast/common/secrets.py`, `BarbicanSecretStore`).
- Coast database rows store only the Barbican secret reference (the full
  `https://<barbican>/v1/secrets/<uuid>` URL), never the payload.
- Some credentials are not in Barbican: service passwords and the
  webhook key are in `coast.conf`, the Coast CA keys are files on the
  conductor hosts, and per-guest keys are in the guest's config drive.
  See [Secrets outside Barbican](#secrets-outside-barbican).
- With `[secrets] backend = disabled` (the default), every secret
  operation fails, so stack creation and app configuration fail closed.
  There is no in-memory or database fallback.

## Backend and client

Set `[secrets] backend = barbican` on the API, conductor and health manager.
Configure `[service_auth]` authentication for the Coast service project
(`auth_type`, `auth_url`, project/user names or IDs, domains and password
for password authentication). Set `[barbican] region_name` and either
`valid_interfaces` or `endpoint_type`; use `ca_certificates_file` to select
a CA bundle, otherwise `[service_auth] cafile` applies. Keep TLS verification
enabled. The service user needs secret create/read/delete, ACL update and
metadata update permissions under the deployed Barbican policy. The Kolla
default service-project role is `admin`; tenant roles do not grant access
to these service-owned secrets.

`[secrets] backend` selects the store (`coast/common/opts.py`,
`coast/api/secret_store.py`):

| Value | Behaviour |
|---|---|
| `disabled` (default) | Store methods fail with `Secret storage is not configured.`; the call wrapper returns the generic `Secret storage operation failed.` error. |
| `barbican` | `coast.common.secrets.BarbicanSecretStore` |

`BarbicanSecretStore` (`coast/common/secrets.py`):

- Opens the Barbican proxy with `clients.get_sensitive_proxy('barbican')`.
  This uses the `[service_auth]` session and the `[barbican]` region,
  interface and CA options, and disables SDK request logging.
- Creates every secret with `payload_content_type='text/plain'` and
  `secret_type='opaque'`. Binary values (keys, tokens) are encoded as text
  before storage. Coast sets no expiration. Every create requires a typed
  `SecretIdentity` with stack scope, kind, owner ID and tenant project ID
  where known. The name is `coast:<stack_id>:<kind>:<owner_id>` and user
  metadata contains `coast-stack`, `coast-kind`, `coast-owner` and
  `coast-project` where known. Names contain identifiers only, never payloads,
  environment variable names or values.
- Immediately after create, sets the `read` ACL to `project-access: false`
  with only the `[service_auth]` user's Keystone ID in `users`, then sets
  metadata. These calls use the validated catalog endpoint and the same
  service-project proxy with HTTP logging disabled.
- Validates each stored reference before use: the scheme, host and port
  must match the catalog endpoint, the path must be `<catalog>/v1/secrets/<uuid>`,
  and userinfo, query strings and fragments are rejected. Only the UUID is
  passed to the SDK, so a tampered database reference cannot redirect
  Coast to another host.
- If the reference returned on create fails validation, or ACL or metadata
  setup fails, Coast attempts to delete the new secret and fails the put.
  No reference is returned until all three checks succeed. If cleanup itself
  fails during an outage, the named secret can be found by the janitor.
- Deletes with `ignore_missing=True`, so revert and teardown can repeat.
- Replaces every SDK error with `Secret storage operation failed.` and logs
  nothing, because SDK errors can contain payloads or credentials. The
  `secret_store.call()` wrapper applies the same rule to any backend.

The API and the conductor both use the store. The health manager uses it
through `coast.health_manager.keys.StackKeyLookup` (`[health_manager] key_lookup`).

## Secret inventory

Columns: **Ref** is the database column holding the Barbican reference.
**Read by** is the code that fetches the payload. **Deleted by** is the
code that deletes the Barbican secret.

### Per stack

Stack secrets use the stack ID as both scope and owner. Their kinds are
`ssh-key`, `heartbeat-key`, `registry` and `engine-token`
in inventory order below.

The four primary references and the previous-token recovery reference
are on the `stacks` row (`coast/db/models.py`).
`coast/conductor/stack_create.py` creates them; each reference is
checkpointed to the row as soon as it exists, and `revert_credentials`
deletes all four if stack creation fails.

| Secret | Ref | Created | Read by | Deleted by |
|---|---|---|---|---|
| SSH private key (Ed25519, OpenSSH format) for the stack's servers | `ssh_key_ref` | `credentials` step, `secrets.create_ssh_keypair` | `server_add.py` and `server_failover.py`, passed to the engine's `register_server` | stack delete (`stack_teardown.py`, `_secrets`) |
| Heartbeat HMAC key (32 random bytes, hex; bounded in-memory stale-on-error cache) | `heartbeat_key_ref` | `credentials` step, `secrets.create_hmac_key` | `boot` steps in `stack_create.py`, `server_add.py`, `server_failover.py`, `engine_upgrade.py` (written to the config drive); health manager `StackKeyLookup` | stack delete |
| Registry credential, JSON `{username, password}` | `registry_secret_ref` | `credentials` step (`coast/conductor/registry.py`) | `registry.py`: bcrypt htpasswd and registry config for the app engine server; `attach()` gives the username and password to the engine | stack delete |
| Engine API token | `engine_token_ref` | `token` step, `secrets.store_engine_token`. The guest agent creates the token locally and returns it once over mTLS (`/v1/bootstrap`) | Every engine call: `manager.py` `_endpoint`, `stack_create.py` `engine` step, failover and upgrade flows | stack delete |
| Previous engine API token during rotation | `engine_token_previous_ref` | Atomic checkpoint when the replacement becomes `engine_token_ref` | Rotation recovery only | Rotation, after the agent confirms revocation; stack delete |

The public half of the SSH key is stored in plain text in
`stacks.ssh_public_key` and written to the worker's `authorized_keys` by
cloud-init (`coast/conductor/worker.py`).

### Per app (service)

These secrets use the app's stack ID as scope and the service ID as owner.
Kinds include `env`, `compose`, `dockerfile`, `deploy-key` and `file-mount`. Env names are omitted from
Barbican attribution. Multiple saved versions may have the same name;
references, rather than names, select payloads.

| Secret | Ref | Created | Read by | Deleted by |
|---|---|---|---|---|
| Environment variable value, secret or not | `secrets.value_ref` | `PATCH` or `PUT .../env` (`coast/api/v1/service_controllers.py`, `write_env`). Each changed value gets a **new** Barbican secret; earlier versions stay for rollback | `manager.py` `_variables` at deploy time and `_log_masks` to redact values from logs; `server_failover.py` during redeploy; `GET .../env` for values not marked secret | app and stack delete remove all versions; unreferenced versions are pruned after commit |
| Compose YAML | `services.compose_ref` and `deployments.spec.compose_ref` | Compose `PUT`, template import (`manager.py`, `import_compose`). Each save creates a new secret | `manager.py` `_push_compose`, compose `GET` | app and stack delete; unreferenced versions are pruned after commit |
| Config-file mount content | `file_mounts.content_ref`, listed in `services.file_mount_secret_refs` | Every file mount create or update (`coast/api/v1/file_mount_controllers.py`) creates a new secret and appends it to the list | `manager.py` `_apply_file_mounts`; `_log_masks` | app and stack delete remove current and deployment-held versions; unreferenced content is pruned after commit |
| Git deploy private key | `sources.credentials_ref` | Generated ed25519 key or validated upload (`coast/common/secrets.py`, `store_deploy_key`) | App creation and key rotation in the conductor; never returned by the API | Replaced keys after successful rotation; app and stack delete, including pending cleanup |
| Dockerfile text | `services.dockerfile_ref` and `deployments.spec.dockerfile_ref` | Dockerfile source create and editor PUT | Dockerfile GET and deployment sync | App and stack delete; unreferenced versions are pruned after commit |

The API returns environment variables marked secret with `value: null`
and never returns file mount content. A failed env write attempts to delete the
secrets it created in that request (`secret_writes`); failed cleanup can
leave an orphan for operator investigation. Before creating any
secrets, env writes validate every submitted value against the configured
payload limit. File mount create/update and compose saves also validate
UTF-8 byte length before storage; no reference is created for a rejected
payload. See [Kolla configuration](#kolla-configuration) for the caps.

### Database backups

Database backups require Swift-compatible object storage in the cloud. They are not verified end to
end in 0.1.0; full verification is tracked for 0.1.1 in issue #124.

From `coast/db/backup_models.py`, `coast/conductor/backups.py` and
`coast/conductor/backup_storage.py`:

| Secret | Ref | Created | Read by | Deleted by |
|---|---|---|---|---|
| Backup encryption key per backup policy (32 random bytes, base64) | `backup_policies.key_ref` | first capture (`backups.capture`) | capture and restore; sent to the guest agent over mTLS | app delete and stack delete (`backups.drop_service`) |
| Encryption key recorded on each backup | `backups.key_ref` | capture | restore | `backups.drop_service` |
| Swift temp-URL signing key, one per customer project | `backup_accounts.key_ref`; the policy's `signing_key_ref` points to it | first backup in the project (`backup_storage.account_key`) | `backup_storage.py` `urls`, to sign short-lived object URLs for the guest | never; it is reused for the project's lifetime |

Backup encryption keys use kind `backup-key`, the app's stack scope and
backup policy ID as owner. Project signing keys use `backup-signing-key`
and the tenant project ID as both scope and owner, since they are shared
by that project's backup policies and outlive any one stack.

The signing key is also written to the `temp-url-key` or `temp-url-key-2`
metadata of the customer project's Swift account (`AUTH_<project>`), so
the project's Swift owners can read it. It signs URLs for that account
only.

### Operator-provided references

The operator creates these secrets in the service project and gives
Coast the reference. Coast reads them and creates none of them.

| Secret | Where the reference is set | Read by |
|---|---|---|
| S3 export credentials, JSON `{access_key_id, secret_access_key}` | `[export] s3_credentials_ref` | `coast/conductor/exports.py` |
| Engine API token for an adopted stack | `coast-manage adopt-stack --token-ref` (`coast/cmd/manage.py`) | Every engine call. Stack delete deletes it like a Coast-created token |
| Swift export temp-URL key | `Coast-Temp-URL-Key-Ref` metadata on the service project's Swift account, plus the matching account `Temp-URL-Key` | `coast/conductor/exports.py`, `SwiftStorage` |

## Secrets outside Barbican

| Item | Where | Notes |
|---|---|---|
| `[service_auth]` password, database and RabbitMQ URLs | `coast.conf` (Kolla `passwords.yml`) | Rendered by the Kolla role with mode 0600 |
| Webhook key | `[webhooks] secret_key` in `coast.conf` | Each webhook token is an HMAC of this key, the service ID and `services.webhook_nonce` (`coast/api/webhooks.py`). Changing the key invalidates every webhook URL |
| Coast server CA and client CA keys | Files under `/etc/kolla/config/coast/certs` on the deploy host, copied to `coast-conductor` only (`tools/kolla/ansible/roles/coast/tasks/certificates.yml`) | The client CA private key stays on the deploy host |
| Agent certificate and private key, heartbeat key | Guest config drive, `/etc/coast/coast.conf` on the guest (`coast/conductor/controller.py`, `config_drive`) | Generated per boot; the certificate is signed by the Coast server CA |
| Replacement engine token during rotation | Private 0600 file under `[agent] state_path/rotation-<uuid>` on the controller data volume | Retained for replay until revocation is recorded, then deleted |
| Engine `.env` (root user password, `APP_KEY`, database passwords) | Engine installation directory on the controller data volume | Generated on the guest; retained on the controller data volume and in its volume snapshots only |
| Env var values, compose, file mounts, registry and SSH credentials after deploy | App engine database on the controller data volume, and running containers | Coast pushes them to the engine; the engine keeps its own copy |
| Console tokens | `console_tokens.token_hash` | Hash only |
| Customer DNS trust | `stacks.dns_trust_id` | Keystone trust ID, not a bearer secret (`coast/network/dns_credentials.py`) |
| Operator SSH access | Nova keypair `[controller_worker] admin_ssh_key_name` in the service project | Public key only; the operator keeps the private key |

## Ownership and access control

- Every Coast-created secret belongs to the service project and was
  created by the `coast` user. The Kolla role gives that user the `admin`
  role by default, configurable with `coast_keystone_user_roles`,
  in `coast_service_auth_project` (default `service`,
  `tools/kolla/ansible/roles/coast/defaults/main.yml`). See
  [Why the Coast service user has admin](service-permissions.md#why-the-coast-service-user-has-admin).
- Coast disables project-wide read access with a per-secret ACL granting
  read to the Coast service user only. Project admins and Barbican admins
  can still read according to Barbican policy. Operators must review their
  deployed [Barbican policy](https://docs.openstack.org/barbican/latest/configuration/policy.html).
- Tenants never get the references. Access to secret values goes through
  the Coast API, which filters by the request's project and applies the
  masking above.

## Lifecycle

### Create

Stack credentials are created during stack create, before the controller
boots. Each reference is saved to the database immediately; if the save
fails, the new secret is deleted. App secrets are created by the API
request that sets them. Each secret is named at creation, restricted by ACL,
and attributed with metadata before Coast receives its reference.

### Rotation

Engine API tokens rotate after `[secrets] engine_token_rotation_days`
(default 30 days; 0 disables automatic rotation). The conductor checks every
60 seconds for ACTIVE managed stacks with an ACTIVE controller whose mTLS
health endpoint succeeds. Adopted stacks are excluded. Any stack or child
task in flight excludes a new rotation. `[secrets] rotation_max_concurrent`
(default 2) limits executing rotations across conductors and operator commands.

Operators can start or retry the same flow with:

```console
$ coast-manage --config-file /etc/coast/coast.conf rotate-engine-token <stack>
```

Rotation uses the exclusive stack `UPGRADING` task lock without changing
stack status. The durable rotation UUID identifies retries; an execution
owner and timestamp refuse concurrent attempts with a conflict. A killed
worker's claim expires after three controller build timeouts plus 60 seconds.
Every external phase renews the claim; checkpoints fence out replaced owners.
A failed attempt releases its execution claim but keeps the stack task until
an operator retry or automatic sweep completes the same rotation.

1. The agent creates a uniquely named replacement without revoking the current
   token. Private persistent staging on the controller data volume lets the
   same rotation UUID return the same token after a lost response or restart.
2. The conductor stores the replacement in Barbican, then atomically switches
   `engine_token_ref`, retaining the old reference in
   `engine_token_previous_ref`, together with both token names. A crash between
   secret creation and the checkpoint can leave a named orphan for the janitor;
   the old stored token remains valid.
3. The agent revokes only the previous named Coast token and records a durable
   receipt before removing its staged plaintext. Repeated revocation succeeds.
4. Only after that confirmation does the conductor delete the previous Barbican
   secret, clear the rotation fields and release the stack task. Deletion and
   completion can be retried after a crash. The stack always retains a stored
   working token. Unnamed development tokens are not backfilled or adopted.

The SSH key, heartbeat key and registry password keep their values for the life
of the stack. Agent mTLS certificates rotate separately
(`/v1/certificates/rotate`); they are not stored in Barbican.

Env vars, compose and file mounts are versioned, not rotated: each change
writes a new secret. The current row and every retained deployment spec
pin their references (`env.*.value_ref`, `compose_ref` and
`file_mounts[].content_ref`). Superseded references are pruned only after
the database transaction commits, including when a deployment is retired.
All retained deployments pin their secrets, regardless of status. Coast
does not currently trim deployment history automatically. The file mount
reference inventory is reduced to content still referenced by current
rows or retained deployments.

A rollback or failed database commit never deletes the old secret. If
Coast crashes after commit, or Barbican deletion fails during pruning, an
unreferenced secret can remain for janitor cleanup; a committed reference
is never pruned. Pruning failures log a generic warning without backend
errors or secret references.

### Failover and engine upgrade

Replacement VMs reuse the stack's existing secrets. Worker and controller
failover (`server_failover.py`, `controller_failover.py`) and engine
upgrade (`engine_upgrade.py`) read the heartbeat key into the new config
drive and read the engine token to reach the engine. Failover reads the
SSH key to re-register a worker and the env var values recorded in the
last deployment to redeploy apps. The engine data volume moves to the
replacement, so the engine's own `.env` is kept. No secret is created or
changed. Required engine endpoint, heartbeat and worker SSH credentials are
read before failover admission and retained only for that attempt. A secret-store
outage defers admission without fencing or claiming an automatic failover.
Engine upgrade admission also treats secret-store errors as transient.

### Delete

- **App delete** (`manager.py`, `_delete_service`) collects current env,
  compose and file mount references plus all versions in retained deployment
  specs, including references held only by those specs. It deletes every
  collected secret before retiring deployment history or destroying rows.
  Backup keys are deleted by `backups.drop_service`.
- **Stack delete** (`stack_teardown.py`, `_secrets` step) deletes the four
  primary stack secrets and any previous engine token retained during rotation,
  every app's current and deployment-held env, compose and file mount references,
  and source credentials. Backup keys are deleted
  with each app's backups.
- Both paths keep reference-bearing rows until all secret deletes succeed.
  Deletes ignore missing secrets, so retry after partial failure repeats
  the collection and finishes cleanup.
- **Project:** the backup signing key is kept after all of the project's
  stacks are deleted.

## Runtime access

Coast fetches a payload only when it needs it and keeps it in memory for
that call:

- Every engine request reads `engine_token_ref` (`manager.py`, `_endpoint`).
- Each deploy reads the compose content and every env var value, then
  sends them to the engine over the agent mTLS proxy.
- The health manager's `StackKeyLookup` reads the heartbeat key and caches
  it for 30 seconds (`coast/health_manager/keys.py`). On secret-store errors
  the last good key remains usable for up to `[health_manager]
  key_cache_max_stale` seconds after expiry (default 3600). Changed references
  and deleted stacks invalidate the cached key immediately.
- Each boot reads the heartbeat key into the config drive.

## Kolla configuration

The Coast Kolla role sets `[secrets] backend = barbican` when
`enable_barbican | bool` is true and `disabled` otherwise, in every Coast
service configuration. Operators can override `coast_secrets_backend`.
The role also renders `[barbican] region_name`, `valid_interfaces` and
`ca_certificates_file` from the cloud settings.

`coast_max_secret_bytes` renders `[secrets] max_secret_bytes` (default
20000). The store sends
plain text without base64 encoding. Before storing any submitted values,
the API rejects oversized content with HTTP 400 and a message naming the
limit in UTF-8 bytes (`coast/common/secret_limits.py`). Payloads exactly at
the byte limit pass size validation; multibyte characters count as multiple bytes:

- Each env var value: `max_secret_bytes`.
- File mount content: the smaller of 64 KiB and `max_secret_bytes`.
- Compose YAML: the smaller of `[api] max_compose_bytes` (default 10000)
  and `max_secret_bytes`.

Deploy Barbican in the same region and ensure the Coast service user can
create secrets in the service project. Match `coast_max_secret_bytes` to
Barbican's configured payload cap. To allow full 64 KiB file mounts, raise
both Coast's cap and Barbican's `max_allowed_secret_in_bytes` to at least
65536, and raise Barbican's `max_allowed_request_size_in_bytes` enough for
JSON escaping and metadata. Escaped text can require substantially more
space than its UTF-8 payload.

See [Kolla deployment](kolla.md#configuration-overrides-logs-and-health-checks)
for override file order.

## When Barbican is unavailable

Every `put`, `get` and `delete` raises `Secret storage operation failed.`
Effects:

| Operation | Result |
|---|---|
| Stack create | Fails at the credentials step; the flow reverts |
| Env var, compose or file mount write | Requests requiring storage fail; cleanup of newly created secrets is attempted and can also fail during an outage |
| `GET .../env` | Fails if a non-secret value must be fetched; secret-only listings return masked metadata without reading payloads |
| Deploy, rollback, engine calls | Fail before contacting the engine, because the engine token cannot be read |
| Stack or app delete | The secrets step fails and the delete can be retried |
| Running apps | Unaffected; containers run on the guests |
| Heartbeats | Use the last good key on secret-store errors, within the configured maximum stale age |
| Automatic worker/build and controller failover | Defer before admission when required secrets cannot be read; keep server and stack status and leave the VM running |
| Engine upgrade or rollback admission | Restore the stack's pre-admission status and release tasks without changing controller status or faults; retry after recovery |

The heartbeat cache normally refreshes every 30 seconds. On a secret-store
error, `[health_manager] key_cache_max_stale` allows the last good key for
another 3600 seconds by default. Failed refreshes never extend that deadline.
Set the option to zero to disable stale key reuse. The cached reference must
still match the live stack; a changed reference never reuses the old key.

Secret refreshes run outside the receiver thread, with at most four in
flight and no queued requests. A newly started refresh waits at most 50 ms;
subsequent heartbeats never wait on that request and use the last good key
within its stale limit. A failed refresh is retried after 30 seconds, rather
than on every packet. Requests arriving during that interval do not extend
either the retry deadline or the maximum stale age. Successful outstanding
refreshes are retained for their stack until the next lookup, so refreshing
another stack does not discard them. Retention is bounded; evicting an old
completed result permits a fresh fetch without a retry delay. An uncached
or over-age key still defers stale checks while refresh is pending or secret
storage is unavailable.

When no usable key exists, including after a health-manager restart or expiry
of the maximum stale age, heartbeats cannot be authenticated and are dropped.
The stale checker retries key lookup outside database locks and defers health
changes and failover dispatch for that stack while secret storage is failing.
A WARNING names the stack at most once per minute and includes no backend
exception, secret reference or payload. Invalid keys and missing references
are not treated as store outages. With a usable key, genuinely missing
heartbeats still follow the normal timeout and grace rules.

Failover prefetches the engine endpoint credentials, heartbeat key and, for
workers/build servers, SSH key before admission. A secret-store error leaves
automatic candidates UNHEALTHY, without turning a running server into ERROR.
Attempts back off for 10, 20, 40 seconds and so on, capped at 300 seconds per
server. This bounded, process-local retry state resets on conductor restart;
no resource claim is held during the delay. An already accepted explicit
failover releases its API claim and restores its pre-admission status without
changing its fault. Single-server resize, including the app engine server,
and rolling worker resize follow the same rule before fencing. A deferred
resize keeps the original VM and flavor; retry resize after storage recovers.
UNHEALTHY and ERROR servers are not reported as recovered
until heartbeat observation or a successful replacement verifies recovery.
After storage recovers, the next eligible attempt proceeds normally; fresh
heartbeats can instead recover UNHEALTHY servers without rebuilding them.
Secret errors after fencing retain the existing taskflow revert and operator
recovery rules. Existing guests and running apps keep running during an outage.

## What customers can see

- Customers cannot see Coast secrets in Barbican. They are in the service
  project, so `openstack secret list` in a customer project does not show
  them.
- The Coast API shows env var names, version and flags, and values of
  variables not marked secret. It does not return secret values, file
  mount content, registry or SSH credentials, or the engine token.
- A customer's Swift owners can read the backup signing key from their
  own account metadata (see [Database backups](#database-backups)).

## Auditing

Use credentials for the service project:

```console
$ openstack secret list --limit 100 --name 'coast:%'
$ openstack secret list --limit 100 --name 'coast:<stack_id>:%'
$ openstack secret get <secret-href>
$ coast-manage --config-file /etc/coast/coast.conf janitor-report
```

`--limit 100` bounds each page; `--name` filters secret names.
`--config-file` selects the operator configuration file.
The name filter uses Barbican's SQL `LIKE` wildcard `%`. Follow pagination
to inspect all secrets. `openstack secret get` without `--payload` shows metadata only.
Names identify ownership; match the exact reference against the columns
above to distinguish versions.

The read-only janitor lists service-project Barbican metadata without
fetching payloads. A `coast:` secret is an orphan candidate when its stack
or owner does not exist, or no DB row holds its reference. Project signing
keys use the project's backup account as their owner and scope. The scan
includes current and historical env/deployment refs, compose, file mounts,
source credentials and backup keys. Live stack ownership alone does not
protect an unreferenced secret. `missing_secrets` contains the UUIDs of DB
refs absent from the complete Barbican listing. Either kind of finding
makes the command exit with status 1. A failed inventory returns no partial
report and exits with status 2. The janitor never deletes.

DB observations before and after inventory reduce concurrent-create false
positives, but this is not an atomic snapshot. Repeat after active flows
finish and investigate candidates before taking operator action. Unnamed
secrets lack Coast attribution and cannot be matched by this name-based scan.

## Service-user permission audit

The [service permission inventory](service-permissions.md) records each
service-session call, ownership, upstream 2026.1 policy and minimum role.
See the [admin rationale](service-permissions.md#why-the-coast-service-user-has-admin)
for the 0.1.0 default and role alternatives. Swift tenant backup
account operations separately require reseller rights; `admin` alone does not provide them.
The read-only checker in that guide validates representative reads without
accessing secret payloads or changing their lifecycle. Secret creation,
reference storage, payload retrieval and deletion stay in the service
project; this audit does not change secret ACLs, rotation or cleanup.

## Database passwords

Coast generates database passwords. Callers cannot supply them. Engines
that need a separate root/admin password receive one too. Passwords go
through `coast/common/secrets.py` `SecretStore` into Barbican, with a
checkpointed reference on the database row. The Coast DB stores only
references. Creation and cleanup use the same secret storage protections
as other Coast secrets.

Ordinary database responses omit passwords from connection URLs and never
return secret references. `GET /v1/databases/{id}/credentials` is the
explicit retrieval path. It requires project member access under
`coast:database:get_credentials`; project reader access is insufficient.
It returns passwords and full connection URLs with `Cache-Control:
no-store`. No credential value is logged or added to notifications.

Treat `openstack coast database credentials show` output as a secret.
Copy the internal URL into a runtime secret app environment variable
through a hidden prompt. Do not store it in shell history, build settings,
source control or support tickets. See the
[database connection guide](portal-guide.md#create-and-connect).
