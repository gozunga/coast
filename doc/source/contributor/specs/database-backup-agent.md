# Database backup agent

Approved for 0.1.0. This document specifies the agent half only.
The conductor owns policies, schedules, safety backups, typed overwrite
confirmation, retention, keys in Barbican, and the Swift static large object
(SLO) manifest. The default destination is a per-service container in the
customer project. The worker receives only temporary links and an in-memory
32-byte key. It never receives OpenStack credentials. No public API,
microversion, database migration, or client command is added here.

## Commands

Use the existing management mTLS connection, worker role only:

* `POST /v1/backup_database`: `service_id`, `container`, `engine`,
  `upload_urls` (ordered HTTPS temporary PUT links), `segment_bytes`, `key`.
* `POST /v1/restore_database`: `service_id`, `container`, `engine`,
  `download_urls` (ordered HTTPS temporary GET links), `key`, `target`.
  `target` must equal `container`; the conductor selects an existing
  destination container, including a newly created copy.

Both commands accept `resource_type` (`service` or `database`, default
`service`). For standalone databases it selects the database ownership
labels; `service_id` and `container` are the destination database engine
UUID. Inspection rechecks the labels before running any tool. A new-copy
restore derives these values from the destination rather than the backup.

`service_id` is the service's engine UUID used by the existing container
inventory, not the Coast database UUID. `container` is the compose service
name, or its `c-` inventory identifier. Exact compose names take precedence.
The agent verifies managed service labels and running state before exec.
No controller database or engine-management container is selected.

The key is base64 encoding of exactly 32 bytes. Supported engines are
`postgres`, `mysql`, `mariadb`, and `mongodb`. Redis is deferred.
Backup returns `size_bytes`, `sha256`, `duration_seconds`, `engine_version`,
and ordered `segments`, each with `size_bytes` and `sha256`. Sizes and hashes
cover the stored encrypted bytes, including the format header. Array position
maps each segment to the corresponding PUT URL; unused links are ignored.
The conductor uses these sizes and SHA-256 checksums for accounting and
integrity, and obtains Swift ETags separately to publish the SLO manifest.
Restore returns `duration_seconds` only.

Invalid requests return 400; missing worker routes return 404; unsupported
methods return 405. Tool, transfer, integrity, version, and busy failures
return 412 with a safe `error` message. No subprocess output, key, database
credential, or URL appears in the response or logs. Each worker permits one
backup or restore at a time. Existing `[export] archive_timeout` bounds the
command connection and database subprocess; transfer calls have 10-second
connect and 60-second read timeouts. Container runtime/tool lookup failures
are errors, never successful empty backups.

## Database tools and credentials

Tools run through Docker exec in the selected customer container. Shell
expansion reads credentials inside that container; the worker does not
inspect or copy its environment. File-based secrets and custom image variable
mappings are not supported in v1. Tool discovery fails closed if absent.

| Engine | Dump / load | Official-image environment and defaults |
| --- | --- | --- |
| PostgreSQL | `pg_dump --format=custom --compress=0`; `pg_restore --exit-on-error --clean --if-exists --no-owner` | `POSTGRES_USER` (postgres), `POSTGRES_DB` (user), `POSTGRES_PASSWORD`; local socket |
| MySQL | `mysqldump --single-transaction`; `mysql` | `MYSQL_ROOT_PASSWORD` (root), otherwise `MYSQL_USER` (root) and `MYSQL_PASSWORD`; `MYSQL_DATABASE` (mysql); local socket |
| MariaDB | `mariadb-dump`, fallback `mysqldump`, `--single-transaction`; `mariadb`, fallback `mysql` | `MARIADB_*` first, `MYSQL_*` fallback; same root/user/database defaults as MySQL |
| MongoDB | `mongodump --archive`; `mongorestore --archive --drop` | `MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD`, admin auth DB; local loopback; no auth when username absent |

SQL tools use environment password variables inside the container. MongoDB
credentials become tool arguments inside that customer container only.
SQL dumps cover the selected database; MongoDB archives cover all databases.
MySQL/MariaDB single-transaction consistency applies to transactional tables;
DDL and nontransactional writes require customer coordination. MongoDB writes
also require customer coordination for a consistent multi-collection backup.
Restore replaces matching objects; it does not remove unrelated objects.
The conductor must use a fresh target for a clean copy.

The agent queries the PostgreSQL/MySQL/MariaDB server version. MongoDB uses
`mongod --version` from the server image, not the unrelated tools version.
Restore rejects a different engine or an older major server version before
starting the loader. MariaDB and PostgreSQL before 10 compare major/minor;
other releases compare the first component. This is a minimum gate, not a
promise of compatibility across newer server versions or SQL extensions.

## Stream format: COASTDB v1

All integers are unsigned, big endian. No backup file or key file is written.
Dump bytes pass through gzip compression, then AES-256-GCM frames.

| Header field | Bytes | Meaning |
| --- | --- | --- |
| Magic and format version | 8 | ASCII `COASTDB` followed by byte `0x01` |
| Chunk size | 4 | 1048576 compressed plaintext bytes maximum per frame |
| Metadata length | 4 | JSON length, at most 4096 bytes |
| Nonce prefix | 8 | Fresh cryptographically random bytes for this backup |
| Metadata | variable | UTF-8 JSON: `engine`, `engine_version` |

Each frame contains a 4-byte ciphertext length followed by AES-GCM ciphertext
and its 16-byte tag. The nonce is the 8-byte prefix followed by the 4-byte
frame counter, starting at zero. Associated data is the complete header
(including metadata) followed by that counter. Counters must never wrap.
An authenticated empty plaintext frame ends the stream. It is mandatory;
truncation and trailing bytes fail. Metadata is trusted only after successful
authentication of the first frame. The format contains no credentials or key.
A new backup uses a new nonce prefix even with the same service key.

The encrypted stream is split at exact `segment_bytes` boundaries (1 byte to
4 GiB); the final segment can be shorter. The conductor should use modest
segments (for example 64 MiB) and provision enough temporary links. Every
segment is held in memory until PUT succeeds, then discarded. This permits
up to three PUT attempts for network faults, HTTP 408/429 or server errors,
with 1- and 2-second backoff. Other errors fail immediately. Redirects are
refused; netrc credentials and environment proxies are disabled. Memory use
is proportional to one segment plus bounded encryption/compression buffers.
Large segment settings therefore require sufficient worker memory.

Restore streams ordered GETs, verifies each frame before decompression,
and writes bounded blocks to the loader. It does not retry partially read
GETs. Integrity failure late in the stream can leave a partially restored
target; there is no transactional rollback across a streamed restore.
The control plane must keep its automatic safety backup on overwrite failure
and report the restore error. Failed PUTs can leave uploaded segments; the
conductor owns their cleanup and must not publish an incomplete manifest.

Public tool references:
[Docker exec](https://docs.docker.com/reference/cli/docker/container/exec/),
[AESGCM](https://cryptography.io/en/latest/hazmat/primitives/aead/),
[pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html),
[MariaDB dump](https://mariadb.com/kb/en/mariadb-dump/), and
[mongodump](https://www.mongodb.com/docs/database-tools/mongodump/).
