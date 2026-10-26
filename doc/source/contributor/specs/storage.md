# Persistent storage (0.1)

Engine releases and defaults come from `coast/common/engine_versions.py`.
Upstream source paths below refer to that release’s tag; dated evidence
refers to the recorded supported release, not the new-stack default.

Customer app data is a Coast **storage**. A storage is a named volume
on the worker Docker disk. That disk is the existing per-worker Cinder
volume mounted at `/var/lib/docker`.

This is API microversion **1.10**. Older versions return 404 for these
routes. The Cinder collection `/v1/stacks/{stack_id}/volumes` is
unchanged. Guest disks stay there. Customer volumes use `/storages`.

## Where the bytes live

Three placements were compared.

1. Named volumes on the worker root disk. A replacement worker is a
   new disk. The data is lost. The root disk also fills, and the
   Cinder export does not include it.
2. One Cinder volume per worker, mounted as the Docker volume root.
   Workers already have `purpose=docker` at `/var/lib/docker`. Named
   volumes live under `volumes/` on that disk. Redeploy keeps the
   volume when the engine record is kept. Worker replacement
   reattaches this Cinder volume, so the bytes move with the worker.
3. One Cinder volume per customer storage. That needs a host bind or
   a new volume plugin, plus extra attach and failover work.

0.1 uses placement 2. Coast does not create a Cinder volume per
storage. The named-storage API manages named volumes. Customers can use
host binds directly through Compose on their dedicated workers.

`size_gb` is an account of capacity. The Docker volume has no project
quota inside the filesystem. The sum of storage sizes on a worker,
including detached rows, must fit in that worker's Docker Cinder
volume. A missing Docker volume, or more than one that is not
`DELETING`, returns 409.

## Survival

Redeploy and a new deployment call the engine again. A persistent
record whose mount path matches, and whose name equals the Coast name
or ends with `-{name}`, is kept. Other persistent records and directory
bind records are removed. Content-file records belong to the file-mount
flow and are preserved. Matching data stays on the Docker disk.

Worker replacement already moves the Docker Cinder volume. No extra
failover step is added for 0.1.

Delete of a Coast row does not remove the Docker volume. The engine
prefixes the volume name with the application id. A later attach of
the same Coast name on the same app can reuse that Docker volume when
the engine still has it. A detached row keeps `service_id` and
`server_id` so the next attach cannot move the bytes to another app.

## Quota

`storage_gb` is a project quota. It is separate from `volume_gb`.
`volume_gb` already counts the whole Docker Cinder disk.

The default is 50 GiB (`[quotas] storage_gb`, `-1` means unlimited).
Create reserves and commits `size_gb` in the same database
transaction as the row. Over-quota returns 403.

Template volumes have no size in the catalog. Each one uses
`[controller_worker] template_storage_gb` (default 1 GiB, minimum 1).

Service delete releases template rows and `IN_USE` rows. It leaves
detached `api` rows. Those rows still hold quota. Stack delete
releases and removes every storage on the stack.

## Export

`coast/agent/export.py` quiesces containers. It does not create
volumes. An export of the worker Docker Cinder volume already
archives the `volumes/` subtree. That archive includes customer named
volumes. There is no per-storage export in 0.1.

## Template services

A one-click service declares volumes in its compose file. Coast reads
a generated index, not the public catalog document. The catalog file
stays metadata only.

The generated index records named-volume accounting only. It does not
contain a safety verdict and never hides or refuses a template. Host binds,
anonymous mounts and read-only mounts are omitted from capacity accounts.
Other named mounts are accounted even when the template also uses privilege,
host networking or any other worker feature.

An accounted named template volume gets one Coast row:

- `origin` is `template`
- `status` is `IN_USE`
- `size_gb` is `template_storage_gb`
- `name` is `{service}-{volume}` when that fits the name rules,
  otherwise `{first 8 hex chars of the service id}-{volume}`
- the container path is the compose target

Those rows stay for the life of the service. Detach, delete, and an
extra attach return 409. The compose file owns the engine mounts.
Coast does not push mounts for a template service.

Bind checks run before the disk check and before quota.

## Customer API

Policies follow volumes, without snapshot or detail fields.

| Method | Path | Policy | Code |
|---|---|---|---|
| GET | `/v1/stacks/{stack_id}/storages` | `coast:storage:get_all` (reader) | 200 |
| POST | `/v1/stacks/{stack_id}/storages` | `coast:storage:create` (member) | 201 |
| GET | `/v1/stacks/{stack_id}/storages/{storage_id}` | `coast:storage:get_one` (reader) | 200 |
| POST | `.../storages/{storage_id}/attach` | `coast:storage:attach` (member) | 200 |
| POST | `.../storages/{storage_id}/detach` | `coast:storage:detach` (member) | 200 |
| DELETE | `.../storages/{storage_id}` | `coast:storage:delete` (manager) | 204 |

Create:

```json
{"storage": {"name": "uploads", "size_gb": 10}}
```

Response envelope is `storage`. `status` is `AVAILABLE`.
`service_id`, `server_id`, and `container_path` are null.
`origin` is `api`.

`name` matches `^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$`. Names are unique
in the stack. A duplicate returns 409.

Attach:

```json
{"attach": {"service_id": "<uuid>", "container_path": "/var/data"}}
```

The path is one absolute container path. `.` and `..` segments are
rejected. The pattern is
`^/(?:[A-Za-z0-9._-]+)(?:/[A-Za-z0-9._-]+)*$` plus that segment check.
The first attach may use any non-template service in the same stack.
A later attach must use the same service. Another service returns 409.
The service worker must be `ACTIVE`. The stack task must be `NONE`.
The container path must be unique among `IN_USE` rows of that service.

Attach sets `status` to `IN_USE` and stores `service_id`,
`server_id`, and `container_path`. When the service already has an
engine record, Coast applies mounts before the response. A failed
apply restores the previous row and the previous
`settings_changed_at`.

Detach body is `{}`. It sets `status` to `AVAILABLE` and clears
`container_path`. It keeps `service_id` and `server_id`.

Delete is allowed only for `AVAILABLE`. `IN_USE` returns 409.
`origin=template` returns 409. The response has no body.

These fields return 400 on every storage body: `host_path`,
`fs_path`, `source`, `bind`, `type`, `read_only`. A read-only request
is rejected. Coast does not mount it read-write in secret.

List filters, `limit`, `marker`, `sort_key`, and `sort_dir` follow
the other stack collections. Public fields are `id`, `project_id`,
`stack_id`, `name`, `size_gb`, `status`, `origin`, `service_id`,
`server_id`, `container_path`, `created_at`, `updated_at`, and
`links`.

## Source paths

On every microversion, `compose_path`, `dockerfile_path`, and
`base_directory` must be relative to the repository. An absolute
path, a `..` segment, a backslash, or a leading `~` returns 400.
`./relative` is valid.

## States

`AVAILABLE` and `IN_USE` only. Create starts at `AVAILABLE`. There is
no `DELETING` status. Delete removes the row. A worker that still has
a storage row, including a detached row, cannot be removed (409).

## 0.1 limits

- No per-volume Cinder volume.
- No move of a storage to a different service.
- No resize.
- No filesystem quota inside the Docker disk.
- No `docker volume rm` when the Coast row is deleted.
- No extra attach on a template service.

CLI and the Horizon Storage tab are separate repositories. They
should call the shapes above.

## Config files (M39, API 1.15)

File mounts are separate from named storage. A tenant supplies one absolute
container path and UTF-8 text of at most the smaller of 64 KiB and
`[secrets] max_secret_bytes` (default 20000). Oversized content returns 400
naming the byte limit before secret writes. Empty text is valid. Paths
must be normalized absolute paths with ASCII segments; NUL and invalid
Unicode content are rejected. System and runtime socket paths are accepted
on dedicated stack workers, including image services. These syntax rules
also apply to named-volume attachment targets.

The collection is `/v1/services/{id}/file_mounts`. Apps can also use
`/v1/apps/{id}/file_mounts`; that route accepts only `kind=app`. The item
route appends `/{file_mount_id}`. GET lists or shows metadata, POST creates
(201), PATCH updates (200), and DELETE removes (204). Requests before 1.15
return 404. Requests use the `file_mount` envelope. List returns
`file_mounts`. These small settings collections have no list parameters;
unknown parameters return 400.

Create requires `container_path` and `content`. PATCH accepts either field.
Optional `resource_name` selects a template container by its engine name.
It is invalid on non-template apps. When omitted, a template must have
exactly one application/database container. An ambiguous or missing target
fails the deployment before file storage writes or enqueue. Paths are
unique across the whole Coast workload, including its template containers.
A file path cannot equal, contain, or be contained by another file path or
an attached named-volume path. The reverse check runs on volume attach.
These checks use the project write lock. File settings writes require an
idle workload and a stack without an exclusive task; busy requests return
409. Readers can list/show; members create/update; managers delete.

Content is write-only. Every version is stored in Barbican. The Coast DB
stores opaque references only. Responses contain `id`, `project_id`,
`service_id`, `container_path`, `resource_name`, `created_at`, `updated_at`.
Content and references are absent from responses, notifications, and error
logs. The workload's internal `file_mount_secret_refs` list retains content
used by current file rows or any retained deployment spec for log masking
and cleanup. Unreferenced versions are pruned only after commit, including
when deployment history is retired. Failed DB writes delete newly made
secrets. Workload/stack deletion deletes all retained file secrets and rows.

A successful write sets `settings_changed_at` and
`X-Coast-Redeploy-Required: true`. It makes no engine call. The conductor
replaces Coast-managed file storage before the next deploy or rollback
enqueue, using current desired file settings. Removal also needs a deploy.
A failed apply prevents enqueue. Retry removes engine records under the
Coast-owned `coast-files/` resource directory and recreates desired files.
Other template-owned storage is preserved. Named-volume edits preserve
Coast-managed files until that next deploy.

Coolify's documented public storage API creates `type=file` with `content`
and `is_directory=false`. Coast generates the relative `fs_path` as
`coast-files/{file_mount_id}`. This content API generates its source path;
customers can select worker host paths directly through Compose.
Template calls supply the selected child `resource_uuid`. The agent permits
only service storage GET/POST and item DELETE, in addition to the existing
application routes. No container exec or engine internals are used.

The pinned public content API has no file permission-mode control. `mode`,
directory flags, and all other unspecified keys return 400. Coast
uses engine file permissions; it does not accept a mode and silently ignore
it. Public schema source:
`openapi.json` at the selected upstream tag.

Tenant-selected Compose build packs are accepted on create and update.
Neither the catalog index nor the editor restricts runtime host access.
Template named-volume sources can be implicit; the index extracts those
sources without changing the upstream Compose document.

File/volume conflicts are also checked against Coast-owned Compose content.
Template Compose PUT rejects volume paths that overlap active file mounts;
file create/update rejects paths that overlap saved Compose volume targets.
Both writes use the project lock before the workload lock.

## Observed named volumes (M40, API 1.16)

The stack storage list also reads each deployed service's named volumes
through the documented public engine storage endpoint. The existing
`service_id` filter selects one service for the Storage tab. Responses add
`source=coast` for API-created accounts and `source=template` for template
and Compose volumes. Earlier microversions retain their accounting view.

Coast-created accounts, including detached accounts, remain visible.
For deployed services, template accounts are shown only when their mount
is observed. Removed declarations keep their quota account but do not
appear as live mounts. Undeployed services retain their accounting view.
Engine-only named volumes have stable service-scoped UUIDs, null `size_gb`,
and no DB row. Their timestamps come from the service. GET makes no writes
and reserves no quota. Unknown volumes use the existing template origin;
attach, detach, PATCH and delete return 409 with a read-only message.
Host paths, file contents and engine record IDs are never returned.
Engine connectivity, authentication and conductor read failures return 503
rather than a partial list. The response says "Storage details are
unavailable right now. Try again in a moment." The API logs one sanitized
cause with operation, stack, service and request IDs. List filtering, field
selection, sorting and marker paging include observed volumes.

## Dedicated worker workloads (API 1.17, issue 19)

James approved unrestricted customer workloads on dedicated stack workers
on 2026-10-19 at 11:10 PM CT. This supersedes the template-only Docker socket
and host-network exceptions. Templates, customer Compose and image services
have the same worker trust boundary. Host paths, sockets, privilege, host
network/pid/ipc/uts/userns, devices and capabilities are permitted. Coast
removes the workload safety checker and all runtime rechecks.

The template index is accounting metadata only, with no `ok` field or
refusal gate. Beszel, Beszel Agent and all other catalog templates are
selectable and creatable, subject to project authorization and capacity.
Template allow/deny config is removed. API 1.17 records this behavior;
the removed restrictions stay removed on older microversions as well.

The existing read-only Docker socket observation remains metadata, without
capacity accounting. Storage API shapes remain named-volume and content-file
operations. No new arbitrary runtime fields are added to image PATCH.
Customers use Compose or their own worker for engine-specific runtime
settings. Secret storage, quota accounting and file/volume overlap checks
remain. Overlap checks prevent conflicting Coast-managed desired state.

Control-plane protection stays in Neutron security groups/address groups
and the operator firewall. Guests cannot reach control-plane service ports;
management agent access uses the existing mTLS endpoints and heartbeat
routes. Guests receive no Keystone service password. The coast-agent role
has no tenant resource grant, and worker server certificates cannot be used
as control-plane client certificates. Project and stack ownership checks
remain. See security.md and networking.md.

## Typed service storage (M45, API 1.19, issue 22)

This section supersedes the named-only request and observation rules above
at microversion 1.19. The routes remain under
`/v1/stacks/{stack_id}/storages`. Create and PATCH use the `storage`
envelope. No new service or dependency is required.

Create a bind mount:

```json
{"storage": {"type": "bind", "host_path": "/srv/data",
 "container_path": "/data", "read_only": false, "service_id": "<uuid>"}}
```

Create a named volume:

```json
{"storage": {"type": "volume", "name": "pgdata", "size_gb": 10,
 "container_path": "/var/lib/postgresql/data", "read_only": false,
 "service_id": "<uuid>"}}
```

`type`, `service_id` and `container_path` are required. `read_only` defaults
to false. Volumes require `name` and `size_gb` and reject `host_path`.
Binds require `host_path` and reject `name` and `size_gb`. All unspecified
fields return 400. The host path must be absolute and normalized POSIX
syntax, with no dot segments, repeated or trailing separators (except
root), control characters, colon or backslash. Colon would change Compose
mount options. No host-path denylist is added. Dedicated workers are under
tenant control; control-plane isolation remains the security boundary.
The existing container-path syntax and file-mount conflict checks remain.

Both types are created as `IN_USE` on an active worker in the same stack.
Binds reuse the storage table with null name and size, but belong to one
service and worker. They are not reusable project storage resources.
Binds consume no `storage_gb` quota or Docker-disk capacity reservation.
Named volumes retain the existing accounting and unique stack name.
Host folders and volume bytes remain after a mount row is deleted.
Host folders outside the Docker disk do not gain its export or worker
replacement guarantees.

PATCH `/v1/stacks/{stack_id}/storages/{id}` accepts `container_path`,
`read_only`, and either `host_path` for binds or `name` for volumes.
Type, service, worker and size cannot change. Empty PATCH returns 400.
A source edit selects a different source; Coast does not move or rename data. Delete permits attached user mounts. Template-defined rows
remain immutable. Create uses the member create/attach policies, PATCH
uses attach, and delete uses the existing manager delete policy. Wrong
project resources return 404. Writes require an idle stack and workload.
The legacy attach/detach workflow remains for named volumes; binds use
create/PATCH/delete.

Writes set `settings_changed_at` and `X-Coast-Redeploy-Required: true`.
They do not call the engine. Deploy, rollback and the settings reconciler
apply current desired mounts to Compose before enqueue. Reconcile changes
stored configuration without restarting containers. Apply failures prevent
deployment enqueue. The engine creates a missing bind folder when it
applies the directory mount. Template services with added user mounts must
have exactly one container: the request has no container selector.
Ambiguous targets or template-owned target paths fail before Compose is
written. Existing template mounts remain unchanged.

GET with `?service_id=<uuid>` returns desired user mounts and all template
mounts, including binds such as a read-only Docker socket. Each entry has
`id`, `type`, `source`, `container_path`, `read_only` and `origin`.
`source` is the host path or volume source; anonymous template volumes
have a null source. User entries have a UUID and `origin=user`.
Template/Compose entries have `id=null`, `origin=template`, and no item
links. They cannot be edited through storage routes. Existing accounting
metadata remains available; bind name and size are null. Field selection,
filtering and paging support the new fields. GET makes no database writes.

The new migration adds `type`, `host_path` and `read_only` and permits null
name/size. It does not attempt to repair old development resources.
