# Database backups

Database backups require a Swift-compatible object-storage endpoint in the
cloud (Swift or RGW with its Swift API enabled). They are not verified end
to end in 0.1.0; full verification is tracked for 0.1.1 in issue #124.
API 1.20 provides the control-plane half of the
[agent contract](database-backup-agent.md). The client provides
`openstack coast backup` and `openstack coast backup policy` commands.

## API and access

Use `OpenStack-API-Version: app-hosting 1.20`. Earlier versions return 404.
Project readers can read policies and backup metadata. Project members can
change policies, create/delete backups, download dumps and restore them.
Administrators can operate on any project. Each operation uses the recorded
owner project for DB and storage calls. Other projects receive 404.

| Method | Path | Result |
| --- | --- | --- |
| GET/PUT | `/v1/services/{id}/backup_policy` | Read/change policy, 200 |
| GET | `/v1/services/{id}/backups` | List records, 200 |
| POST | `/v1/services/{id}/backups` | Queue manual backup, 202 |
| GET | `/v1/backups/{id}` | Record and restore jobs, 200 |
| DELETE | `/v1/backups/{id}` | Queue object deletion, 202 |
| GET | `/v1/backups/{id}/download` | Authenticated decrypted gzip stream |
| POST | `/v1/backups/{id}/restore` | Queue restore, 202 |

Policy PUT requires a database template service and uses the
`backup_policy` envelope. The first write requires
`engine` (`postgres`, `mysql`, `mariadb`, `mongodb`) and `container` (the
compose container name). Explicit selection avoids guessing which database
a template contains. Later writes merge the supplied fields. Other fields:
`enabled` (default false), `hour` (2), `minute` (0), `keep_daily` (14),
`keep_weekly` (8). Time is UTC. Hours are 0-23; minutes 0-59; daily retention
is 0-365 and weekly retention 0-104. Zero removes that retention category.
An absent policy GET returns these defaults with null engine/container.
Policy fields and engine/container are the only writable fields.

Backup POST accepts an empty body or `{}` and requires a saved policy.
Records contain `id`, `service_id`, `kind` (`manual`, `scheduled`, `safety`),
`status`, `size_bytes`, `started_at`, `finished_at`, `duration_seconds`,
`error`, `engine`, and `engine_version`. Sizes measure encrypted stored
bytes. Timestamps are UTC. GET on an individual backup also returns
`restores`: job id, destination `service_id`, `operation`, `status`, times,
safe error and `safety_backup_id`. Keys, references, segments, object names
and temporary links never enter public JSON. Unknown query parameters and
unknown body fields return 400. Missing authentication returns 401.

Restore defaults to `new_copy` and requires `name`. Coast creates a fresh
database service through the normal template creation path in the same
stack, environment and worker. Normal service/storage quotas apply. It uses
the same template identifier, fresh template defaults and fresh storage;
it does not copy custom environment settings, Compose edits or host binds.
The 1.20 agent backup inventory selects template services. The new service stays
visible on failure so the tenant can inspect or delete it. A bounded
provisioning wait precedes restore. GET on the source backup reports progress.

Overwrite uses `{"mode":"overwrite","confirmation":"service-name"}`.
The UI must ask the user to type the exact service name; the API checks it
again. `name` is invalid in this mode. Coast first makes a safety backup.
Any safety-backup failure prevents restore. The safety backup remains after
success or failure and needs explicit deletion. The agent rejects an older
major destination version before loading. A failed restore can leave partial
data; Coast does not attempt an automatic second destructive restore.

New backups, policy writes and overwrite restores require an idle service;
operations hold the existing service task lock. A source backup cannot be
deleted while a restore uses it. Jobs are durable and claimed with a DB CAS,
so a second conductor cannot execute the same job. Interrupted commands fail
closed after the command deadline plus a recovery margin. Error text is
fixed and contains no tool output, credentials, key or temporary URL.

## Storage and encryption

Coast creates `coast-backups-{service UUID}` in the customer's own Swift/RGW
account. This is separate from volume exports. No external S3 backend or
existing-container option is included in v1. A missing regional object-store
endpoint returns 409, `Object storage not available.` HTTPS is required.

Coast currently authenticates cloud calls with `[service_auth]`, in the
service project; it does not retain tenant tokens. Backup storage uses that
same service session and the catalog endpoint's standard `AUTH_` account
prefix, replacing only the project suffix with the recorded customer project
UUID. The operator must grant the Coast service user Swift reseller-admin
rights (or the equivalent RGW cross-account permission). Ordinary service
roles alone do not grant access to customer accounts. Coast does not send
this credential to a worker. Nonstandard account naming is unsupported and
fails closed. `[backup] region_name` and `interface` select the catalog
endpoint; the region defaults to the configured Nova region. That endpoint
must also be reachable from the workers.

Swift/RGW temporary links require an account signing key. Coast creates one
random signer per customer project and stores its Barbican reference in
`backup_accounts`. This DB row is the authority; customer-editable account
metadata can never select a control-plane secret. Coast uses a free
Temp-URL-Key-2 or Temp-URL-Key slot and reuses its known key on later calls.
It never replaces an occupied customer key. If both slots are occupied by
other keys, the operator must reserve a slot before backups can run.
Initialization serializes the DB checkpoint with the project lock, then
makes the idempotent Swift metadata call outside the transaction. The
account key and its DB reference remain after service deletion so new
services can reuse it. Workers never receive this key. RGW signing uses
the existing Swift HMAC-SHA1 temporary URL protocol.

Each service has a random 32-byte AES key in Barbican in the service project.
Only opaque references enter Coast's DB. The key is passed over management
mTLS and is held in worker memory. The agent's COASTDB v1 stream applies gzip
and chunked AES-256-GCM. Credentials remain inside the database container.
No dump or key file is written to the worker or the control plane.

The conductor supplies ordered temporary PUT links. The default segment size
is 64 MiB, with at most 1024 segments (64 GiB of encrypted data). Operators
can change both limits; the worker holds one segment in memory. Each segment
is below Swift's single-object size limit. Coast validates the receipt,
HEADs each used segment for size and ETag and publishes a static large object
manifest only when all segments exist. SHA-256 receipts cover encrypted
bytes; SLO uses Swift's separate ETags. Uncertain and failed PUTs have
deterministic prefixes and are removed on error/recovery scans. Account
permission errors and other storage failures produce a safe ERROR record.

Download passes through the authenticated Coast API. It decrypts and checks
GCM frames in bounded memory and returns the compressed dump as
`{backup UUID}.dump.gz`, with `Cache-Control: no-store`. This choice keeps the
AES key private and checks authorization for each download. It consumes API
bandwidth. The header and first frame are authenticated before HTTP 200;
a later integrity or network failure terminates the stream. Clients must
reject an incomplete HTTP transfer. There is no range/resume support in v1.
Upload-to-restore is deferred.

## Scheduling, retention and deletion

The existing conductor service runs a separate `[backup] poll_interval`
timer, default 60 seconds. Scheduling is off until policy `enabled=true`.
The first pass after the selected UTC time queues one scheduled backup per
service per UTC date. A durable date checkpoint and project/service locks
prevent duplicate scheduling across conductors. Missed dates are not
replayed. Manual backups work while scheduling is off.

Retention keeps the newest successful scheduled backup for each of N UTC
dates and each of N ISO weeks. The union is retained; missing days/weeks do
not reduce protection. Failed backups never displace good restore points.
Manual and safety backups are excluded. Retention continues when scheduling
is off and queues at most one deletion per service per pass. Failed partial
uploads are swept by deterministic prefix. Service or stack deletion removes
its backup objects, container, AES secret and DB records. Active backup or
restore jobs block that cleanup. Customer account signing keys are retained.

## Schema and configuration

Migration `0008_database_backups` follows `0007_storage_types`. It adds:

* `backup_accounts`: one project-owned Barbican signing-key reference, retained
  for safe reuse after the last service is deleted.

* `backup_policies`: one project-owned policy per service, selection, schedule,
  retention, Barbican key references and last scheduled time.
* `backups`: project/service ownership, public record fields, container,
  encrypted checksums, segment receipt and encryption-key reference.
* `backup_jobs`: durable operation, source backup and destination service,
  status, timestamps, safe error and safety-backup checkpoint.

No DB plaintext secret or new dependency is added. Backup and job states use
`coast/common/states.py`. State changes emit identifier-only notifications.

`[backup]` options: `region_name`, `interface` (public), `poll_interval` (60),
`segment_bytes` (67108864; 1 MiB-4 GiB), `max_segments` (1024; 1-4096),
`url_ttl` (7200 seconds; at least 60). Temporary link lifetime must exceed
`[export] archive_timeout` (the existing agent dump/restore timeout), including
transfer time. Recovery permits two command timeouts plus five minutes for
an overwrite's safety backup followed by restore. Enable `[secrets]
backend=barbican` and configure management mTLS before use.

Public references:
[Swift temporary URLs](https://docs.openstack.org/swift/latest/api/temporary_url_middleware.html),
[Swift SLO](https://docs.openstack.org/swift/latest/overview_large_objects.html),
[SDK object storage](https://docs.openstack.org/openstacksdk/latest/user/proxies/object_store.html),
and [Keystone Swift access](https://docs.openstack.org/swift/latest/overview_auth.html).

## Standalone databases (1.29, issue #124)

The same policies, encrypted storage, scheduling, retention, download and
cleanup support `kind=database` services. Database nested routes and shared
backup routes for their records require 1.29. The catalog's `backups` flag
controls availability; unsupported engines return 409. Policy engine and
container are derived, and are rejected as request fields. The agent selects
containers using the database ownership labels and checks ownership again
on inspection. Its existing official-image credential environment names
also apply to standalone databases.

New-copy restore uses normal database creation with the original engine,
version, placement, database name and username, fresh generated credentials
and public access disabled. It retains normal quota and task admission.
The destination container is derived from the new database, never from the
source backup. Standalone document-database dumps contain only the configured
database, excluding administrative users so fresh credentials survive restore.
Template-service backup selection and restore behavior remain unchanged.
