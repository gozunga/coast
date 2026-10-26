# REST API v1

- Catalog: service type **`app-hosting`**, service name `coast`.
  Public endpoint (proposed): `https://cloud.example.com:9600`
  (port TBD). Internal/admin via kolla internal VIP.
- Base path `/v1`. JSON only. All resources are **project-scoped**;
  `project_id` always comes from the token, never from the body.
  Admins may pass `?all_projects=true` / `?project_id=` on list calls.
- Auth: `X-Auth-Token` (keystonemiddleware). Request ID: `X-OpenStack-Request-Id`.
- Async writes return **202** with the resource in its transitional
  status. Clients poll `GET` or subscribe to events. These writes are
  synchronous: stack PATCH returns **200**, appgroup PATCH returns
  **200**, environment PATCH returns **200**, service PATCH returns
  **200**, domain POST returns **201**, env PUT and env PATCH return
  **200**, quota PUT returns **200**, quota DELETE returns **204**,
  export `renew_url` returns **200**, storage create returns **201**,
  storage show, attach, and detach return **200**, and storage delete
  returns **204**.
- Timestamps are UTC ISO 8601. The service replaces an offset of
  `+00:00` with `Z`. A naive UTC value has no offset, so that value can
  omit `Z`. Examples use `Z` for the offset form.
- Errors: OpenStack style `{"faultstring": "...", "faultcode": "Client", "debuginfo": null}`
  (WSME). Other-project resources return **404** (no existence leak).
- Pagination: `limit`, `marker`, `sort_key`, `sort_dir`; response has
  `links: [{"rel":"next","href":...}]` (Octavia style).
- Filtering: equality on simple fields (`?status=ACTIVE&name=web`).
- Fields filtering: `?fields=id,name,status`.
- Tags: not in v1.

## Microversions

- Header: `OpenStack-API-Version: app-hosting 1.N` (response echoes it,
  plus `Vary: OpenStack-API-Version`).
- `1.0` = initial API. Min `1.0`, max `1.31`, advertised in `GET /`.
- `1.1` = stack-create role plans and images, with one or more VM workers.
- `1.2` = per-domain TLS mode `letsencrypt` or `none`.
- `1.6` = application env variables default to build-time exposure.
- `1.7` = template HTTP port metadata and HTTP health defaults.
- `1.8` = stack event timeline.
- `1.9` = shell sessions on an app or a service.
- `1.10` = persistent named storage for apps and services.
- `1.11` = explicit workload kind.
- `1.12` = protected compose editor.
- `1.17` = unrestricted workloads on dedicated stack workers.
- `1.18` = admin stack inventory and cross-project infrastructure reads.
- `1.19` = typed service storage: bind mounts and named volumes.
- `1.20` = database backup policies, backups, downloads and restore.
- `1.21` = per-container CPU and memory history.
- `1.22` = customer Designate DNS.
- `1.23` = primary custom domains and optional default address.
- `1.26` = server resize and rolling worker resize.
- `1.27` = project-visible server flavors and allowed VM flavor catalog.
- `1.28` = app sources default to `kind=app` on create.
- `1.29` = databases (#121).
- `1.30` = private Git deploy keys (#136).
- `1.31` = repository-free Dockerfile and Compose app sources (#137).
- Bump rules: any added field/param/endpoint or behaviour change = new minor;
  documented in `coast/api/microversions.py` history + reno note.
- Future versions: byo servers, service replicas > 1, `auto_deploy`,
  and shared-edge ingress options. No version numbers are assigned yet.

## Version discovery

```text
GET /
200 OK
{"versions": [{"id": "v1", "status": "CURRENT", "min_version": "1.0",
  "max_version": "1.31", "links": [{"rel": "self", "href": "https://cloud.example.com:9600/v1/"}]}]}
```

Both discovery endpoints are public (no token required) and return the
microversion response headers described above. `GET /v1` returns the same
v1 object as `GET /`, in a single-version envelope:

```text
GET /v1
200 OK
{"version": {"id": "v1", "status": "CURRENT", "min_version": "1.0",
  "max_version": "1.31", "links": [{"rel": "self", "href": "https://cloud.example.com:9600/v1/"}]}}
```

## Endpoint summary

| Method | Path | Policy | Async |
|---|---|---|---|
| GET | `/v1/stacks` | `coast:stack:get_all` | |
| POST | `/v1/stacks` | `coast:stack:create` | 202 |
| GET | `/v1/stacks/{stack_id}` | `coast:stack:get_one` | |
| PATCH | `/v1/stacks/{stack_id}` | `coast:stack:update` (name/description) | |
| DELETE | `/v1/stacks/{stack_id}` | `coast:stack:delete` | 202 |
| POST | `/v1/stacks/{stack_id}/action` `{"upgrade":{"image_id":"<UUID>"}}` | `coast:stack:upgrade` (member/admin) | 202 |
| POST | `/v1/stacks/{stack_id}/exports` | `coast:export:create` | 202 |
| GET | `/v1/stacks/{stack_id}/exports`, `/v1/stacks/{stack_id}/exports/{export_id}` | `coast:export:get_all` / `get_one` | |
| POST | `/v1/stacks/{stack_id}/exports/{export_id}/action` `{"renew_url":{}}` | `coast:export:renew_url` | |
| DELETE | `/v1/stacks/{stack_id}/exports/{export_id}` | `coast:export:delete` | 202 |
| GET | `/v1/stacks/{stack_id}/servers` | `coast:server:get_all` | |
| POST | `/v1/stacks/{stack_id}/servers` | `coast:server:create` | 202 |
| GET | `/v1/stacks/{stack_id}/servers/{server_id}` | `coast:server:get_one` | |
| DELETE | `/v1/stacks/{stack_id}/servers/{server_id}` | `coast:server:delete` | 202 |
| POST | `/v1/stacks/{stack_id}/servers/{server_id}/action` `{"failover":{}}` | `coast:server:failover` (admin) | 202 |
| GET/POST | `/v1/stacks/{stack_id}/volumes` | `coast:volume:get_all` / `create` | 202 |
| GET/PATCH/DELETE | `/v1/stacks/{stack_id}/volumes/{volume_id}` | `coast:volume:get_one` / `update` (extend) / `delete` | 202 |
| POST | `/v1/stacks/{stack_id}/volumes/{volume_id}/action` `{"snapshot":{}}` | `coast:volume:snapshot` | 202 |
| GET/POST | `/v1/stacks/{stack_id}/storages` | `coast:storage:get_all` / `create` | POST 201 |
| GET/DELETE | `/v1/stacks/{stack_id}/storages/{storage_id}` | `coast:storage:get_one` / `delete` | DELETE 204 |
| POST | `.../storages/{storage_id}/attach` `{"attach":{"service_id","container_path"}}` | `coast:storage:attach` | 200 |
| POST | `.../storages/{storage_id}/detach` `{}` | `coast:storage:detach` | 200 |
| GET | `/v1/services` (`?appgroup_id=&environment_id=`) | `coast:service:get_all` | |
| POST | `/v1/services` | `coast:service:create` | 202 |
| GET/PATCH/DELETE | `/v1/services/{service_id}` | `coast:service:get_one` / `update` / `delete` | PATCH sync (no redeploy), DELETE 202 |
| POST | `/v1/services/{service_id}/action` `{"start":{}}`, `{"stop":{}}`, `{"restart":{}}` | `coast:service:start` / `stop` / `restart` | 202 |
| POST | `/v1/apps/{app_id}/console` | `coast:app:console` (member/admin) | 201 |
| POST | `/v1/services/{service_id}/console` | `coast:service:console` (member/admin) | 201 |
| GET | `/v1/services/{service_id}/deployments` | `coast:deployment:get_all` | |
| POST | `/v1/services/{service_id}/deployments` | `coast:deployment:create` | 202 |
| GET | `/v1/services/{service_id}/deployments/{dep_id}` | `coast:deployment:get_one` | |
| POST | `/v1/services/{service_id}/deployments/{dep_id}/action` `{"rollback":{}}` / `{"cancel":{}}` | `coast:deployment:rollback` / `cancel` | 202 |
| GET | `/v1/services/{service_id}/deployments/{dep_id}/log` | `coast:deployment:get_log` | |
| GET | `/v1/services/{service_id}/logs?since=&tail=` | `coast:service:get_logs` | |
| GET/POST | `/v1/services/{service_id}/domains` | `coast:domain:get_all` / `create` | |
| GET/PATCH/DELETE | `/v1/services/{service_id}/domains/{domain_id}` | `coast:domain:get_one` / `update` / `delete` | PATCH 202, DELETE 202 |
| POST | `/v1/services/{service_id}/domains/{domain_id}/action` `{"verify":{}}` | `coast:domain:verify` | 202 |
| GET | `/v1/services/{service_id}/env` | `coast:secret:get_all` (secret values masked) | |
| PUT | `/v1/services/{service_id}/env` (bulk replace) | `coast:secret:update` | |
| PATCH | `/v1/services/{service_id}/env` (merge; `null` deletes key) | `coast:secret:update` | |
| GET | `/v1/events?resource_id=&stack_id=&environment_id=` | `coast:event:get_all` | |
| GET | `/v1/quotas/{project_id}` | `coast:quota:get_one` (own project or admin) | |
| PUT/DELETE | `/v1/quotas/{project_id}` | `coast:quota:update` / `delete` (admin) | |
| GET | `/v1/quotas` / `/v1/quotas/defaults` | `coast:quota:get_all` (admin) / `get_defaults` | |
| GET | `/v1/plans` | `coast:plan:get_all` (any authenticated) | |
| GET | `/v1/stacks/{stack_id}/appgroups` | `coast:appgroup:get_all` | |
| POST | `/v1/stacks/{stack_id}/appgroups` | `coast:appgroup:create` | 202 |
| GET | `/v1/appgroups/{appgroup_id}` | `coast:appgroup:get_one` | |
| PATCH | `/v1/appgroups/{appgroup_id}` | `coast:appgroup:update` (name/description) | |
| DELETE | `/v1/appgroups/{appgroup_id}` | `coast:appgroup:delete` | 202 |
| GET | `/v1/appgroups/{appgroup_id}/environments` | `coast:environment:get_all` | |
| POST | `/v1/appgroups/{appgroup_id}/environments` | `coast:environment:create` | 202 |
| GET | `/v1/environments/{environment_id}` | `coast:environment:get_one` | |
| PATCH | `/v1/environments/{environment_id}` | `coast:environment:update` (name/description) | |
| DELETE | `/v1/environments/{environment_id}` | `coast:environment:delete` | 202 |
| GET | `/v1/templates?stack_id=` | `coast:template:list` | 1.0 |
| GET | `/v1/templates/{template_id}?stack_id=` | `coast:template:get` | 1.0 |

DELETE an appgroup or environment only from ACTIVE or ERROR. The
stack must be ACTIVE, DEGRADED or ERROR, with task NONE or RECONCILING.
DEGRADED describes health, not an operation in progress. An exclusive stack
task still returns 409. A non-deleted child returns 409: an environment
blocks its appgroup, and a service blocks its environment. PENDING,
DELETING, and any other status also return 409. The checks and the
DELETING write share one transaction. There is no cascade and no
request body. The conductor removes the engine object, stores status
DELETED, then soft-deletes the row. A missing engine reference skips
the driver. Driver NotFound completes the delete. Driver failure stores
ERROR. Poll GET until 404. Q101.

Create bodies use the envelopes `appgroup` and `environment` with
`name` and optional `description` (Q94). A service requires
`appgroup_id` and `environment_id`. That environment must belong to
that appgroup. The service's server must belong to the same stack.
Deployments, domains and secrets stay under `/v1/services/{service_id}`.
Service list and create are flat. See Q94 for why this is not a nested
`/v1/environments/{id}/services` collection.

### Templates (Q95)

`GET /v1/templates?stack_id=` and `GET /v1/templates/{template_id}?stack_id=`
are read endpoints at microversion 1.0. `stack_id` is required. The stack
driver and `engine_version` select the catalog. The body fields are `id`, `name`,
`description`, `logo`, `category`, `tags`, `required_env`, and `ports`.
At API 1.7, list and show also return
read-only `http_port` (integer or null). Earlier versions omit this field.
The generator scans decoded compose text in order for `SERVICE_URL_` and
`SERVICE_FQDN_` magic variables. The first variable with a numeric port
suffix selects `http_port`. If only bare variables exist, the first catalog
port is used, or null when there are no ports. Without these magic variables,
`http_port` is null. Metadata ports do not override a suffix port.
`coast:template:list` and `coast:template:get` authorize catalog access. Readers, members,
managers and project-scoped admins can read by default. Every stack lookup
is project-scoped, including admin requests. Foreign stacks return 404.
A missing/empty `stack_id` or an unknown query parameter returns 400.
An unknown template returns 404. An unavailable engine
version catalog returns 409; Coast never falls back to another version.
The collection body is `{"templates": [...]}`; detail is `{"template": {...}}`.
These reads have no quota cost and do not contact the controller.

The list response includes a weak `ETag` for the visible JSON value and
`Cache-Control: private, no-cache`. A matching `If-None-Match` (including
weak validators, lists of validators, or `*`) returns 304 with no body.
Stack ownership and policies are checked on every
request before comparing the validator. A change to the visible list
changes the ETag. No new microversion is required.

`[templates] cache_ttl` defaults to 3600 seconds; 0 disables the raw catalog
cache. Each API worker has its own in-memory cache, keyed by driver and
engine version and protected by a thread lock. This is derived metadata
from static, versioned packaged files. Expiry uses a monotonic clock;
workers refill independently. Only raw catalogs are cached. Request
visibility and responses are never cached on the server.

Catalogs have no workload safety or operator allow/deny filter.
Project authorization still applies to list, detail and create.

Service create parses `source: "template:<id>"` or the source object below
and validates the stack catalog and policy. A valid
request stores `services.template_id`. The response is 202.
`coast:template:get` applies before any quota, secret, DB or
RPC write. Coast sends caller env values and verified domains to template
services through the app engine server API. Template env defaults and
generated
`SERVICE_*` values stay in the engine unless the caller overrides them.
Coast removes only env keys that it marked as managed. Template variables
are runtime-only; `build_time=true` is not supported. Env PUT/PATCH on an
existing template service sends a reference-only conductor update. Values
stay in Barbican and are read only in the conductor.

Domains target the only sub-appgroup with an existing `fqdn`, or the only
sub-appgroup if none has an `fqdn`. A non-empty domain set with no unique
target fails closed. An empty set clears all exposed sub-appgroups.
Coast does not yet accept a sub-appgroup selector. Template mounts stay
unchanged in this task; caller mounts are not sent to the engine.

Env changes are stored immediately but take effect on the **next
deployment** (response header `X-Coast-Redeploy-Required: true`).

## Examples

### Create stack
```text
POST /v1/stacks
OpenStack-API-Version: app-hosting 1.0
{"stack": {
  "name": "acme",
  "description": "Acme marketing sites",
  "plan": "standard-2",
  "workers": [{"kind": "vm", "plan": "worker-4-8"}],
  "build_server": false,
  "data_volume_gb": 100
}}

202 Accepted
{"stack": {
  "id": "6f0f5d0e-2a1b-4c8f-9a51-0b9f1f2d7c11",
  "project_id": "c1a2...",
  "name": "acme",
  "description": "Acme marketing sites",
  "plan": "standard-2",
  "engine_version": "<engine version>",
  "status": "PENDING",
  "created_at": "2026-10-09T14:05:00Z",
  "updated_at": null,
  "links": [{"rel": "self", "href": ".../v1/stacks/6f0f..."}]
}}
```
Create stores status `PENDING` and task `PROVISIONING`.
The response
code is 202. Policy `coast:stack:create` defaults to the project member
role (Q96). Manager and admin stay required for elevated operations.
The public body omits `task_state`. Admin-only fields on
GET (`coast:stack:get_detail`): `driver`, `network_id`, `engine_ref`,
`task_state`. The existing `fault` field is public on stack reads.

If conductor handoff fails before a conductor claims the stack, the API
marks it ERROR, releases its task and accepted quota reservation, and
returns 500. It can then be deleted. DELETE also accepts a PENDING stack
with a PROVISIONING task and no owning conductor, cancelling the create
before teardown; a delayed create message cannot provision it.
For a stack without an acceptance receipt, quota recovery uses the held
project counters only when no other stack/server create or volume operation
could own them. Otherwise DELETE returns 409 for retry after that work
finishes. Other projects and unrelated quota resources are preserved.

### Admin stack inventory at microversion 1.18

Stack list and show add `subnet_id`, `router_id`, `sg_ingress_id`,
`sg_mgmt_id`, `server_count`, and `workload_count` under
`coast:stack:get_detail` (admin-only by default). `network_id` already
uses that policy. Fields are read-only, support `fields`, and are omitted
before 1.18. They are not filter or sort keys. Counts use two grouped
queries per stack page, including all server roles and both workload
kinds in `services`, excluding soft-deleted rows. Empty counts are zero.

`project_id` is the stack owner. Nova servers and Cinder volumes are
created with `[service_auth]` in the service project, not the owner's
project. Admin inventory adds `service_project_id`, resolved from that
credential, only when it differs from `project_id`. It does not disclose
credentials. An unresolved service project fails the read with 500.

At 1.18, `all_projects=true` on stack show, nested server/volume list and
show, and nested stack events permits cross-project reads. It requires
`ADMIN` as well as the endpoint policy. Without it, foreign resources
return 404, including for admins. Non-admin use returns 403. Event next
links preserve the flag and markers must belong to the selected stack.
`/v1/stacks`, `/v1/services?stack_id=`, and `/v1/events?stack_id=` already
support admin `all_projects=true`; their existing behaviour is unchanged.
Nested appgroup, environment, storage, export and ingress rule GET reads,
and template list/show reads, also allow `all_projects=true` or `project_id`
after the `ADMIN` check, before resolving the parent. The endpoint policy
still applies. Writes do not use these parameters to cross project boundaries.

Server create/delete timeline detail records `compute_id`. Server
failover already records `old_compute_id` and `new_compute_id`; a failed
replacement that never boots has a null new ID. Stack engine upgrade and
rollback replacement events also record `server_id`, `old_compute_id`,
and `new_compute_id`, including interrupted operations. Existing events
are not backfilled. No history table is added. Infrastructure IDs already
available in the timeline remain visible under the existing event policy.

### Stack create at microversion 1.1 (Q111)

Image selections are accepted from microversion 1.1 onward. Later versions
retain optional workers and accept VM or bare-metal workers; bare metal
requires `[controller_worker] enable_baremetal`.

```text
POST /v1/stacks
OpenStack-API-Version: app-hosting 1.1
{"stack": {
  "name": "example-stack",
  "plan": "controller-plan",
  "image": "published-controller-image",
  "workers": [{"kind": "vm", "plan": "worker-plan",
               "image": "published-worker-image"}],
  "build_server": true,
  "build_server_plan": "build-plan",
  "build_server_image": "published-build-image"
}}
```

At microversion 1.1, `workers` is required and has one or more entries,
each with `kind=vm` (Q151).
At 1.1, bare metal is rejected even when the configuration flag is enabled.
The controller is separate from the worker. `build_server` remains an
optional boolean (default false). The project quota limits the worker count.
Add more workers or build servers later with `POST /servers`.

`plan` and each worker plan must name an operator VM plan that allows the
matching `control` or `worker` role. `build_server_plan` must allow `build`.
With `build_server=true`, omission uses the unique build-compatible VM plan;
zero or multiple candidates return 400. Build plan/image fields without
`build_server=true` return 400. Unknown, wrong-kind, or wrong-role plans
return 400 before quota reservation.

`image`, worker `image`, and `build_server_image` are optional image IDs or
names. Each selection must match exactly one allowed active Glance image,
with the configured owner, role tag, and `coast_role` property. Missing,
ambiguous, or disallowed selections return 400. Controller images require
`coast_role=controller`; workers and build servers use the existing worker
catalog with `coast_role=worker`. Build and worker selections are independent.
A selected image overrides the configured exact ID but not catalog checks.
Omission retains the current operator-configured image. Names resolve to
IDs before RPC; the conductor checks the catalog again before boot.

These are request-only fields; public responses do not gain fields.
Quota semantics stay unchanged: the controller does not consume `servers`,
a worker consumes one `servers`, and a build server consumes both one
`servers` and one `build_servers`. Existing storage accounting stays.
Version 1.0 rejects these new fields and retains its worker-count rules.

### Add a server
```text
POST /v1/stacks/{stack_id}/servers
{"server": {"name": "worker-2", "kind": "baremetal", "role": "worker", "plan": "bm-16-64"}}
202 {"server": {"id": "...", "kind": "baremetal", "role": "worker", "status": "PENDING",
  "floating_ip": null, ...}}
```
The request may include `plan`. The service accepts `plan`. It does not
store `plan` and it does not return `plan`. Create stores status
`PENDING`.

`role=control` cannot be created/deleted via API (one per stack, managed by Coast).
`kind=byo` remains unsupported and returns 400.

### Volumes (decisions Q24, Q25, Q26)

```text
POST /v1/stacks/{stack_id}/volumes
{"volume": {"name": "app-data", "size_gb": 100, "server_id": null,
            "volume_type": null, "mount_path": "/data", "purpose": "data"}}
202 {"volume": {"id": "...", "name": "app-data", "size_gb": 100,
                "status": "CREATING", ...}}

PATCH /v1/stacks/{stack_id}/volumes/{volume_id}
{"volume": {"size_gb": 200}}
202 {"volume": {"id": "...", "size_gb": 200, ...}}

POST /v1/stacks/{stack_id}/volumes/{volume_id}/action
{"snapshot": {}}
202 {"volume": {"id": "...", "status": "SNAPSHOTTING", ...}}
```

Create requires envelope `volume`, `name`, and a positive integer
`size_gb`. `server_id` and `volume_type` are optional and nullable.
When `server_id` is set, that server must belong to this stack.
A server that is not visible in the project returns 404. A server in
the same project but a different stack returns 400. `mount_path`
defaults to `/data`. `purpose` defaults to `data`.

Users can create only `purpose=data`. `docker`, `scratch`, and
`controller` are Coast-managed and return 400 on create. The model
enum remains `data|docker|scratch|controller`. Coast creates one
`docker` volume per server at `/var/lib/docker`. DELETE and PATCH
return 400 for `docker`, `scratch`, and `controller`, including for
admins. Snapshot is allowed for `data` and `docker` only. Snapshot of
`scratch` or `controller` returns 400. Export requests for `scratch`
or `controller` stay rejected. Export or snapshot of `docker` includes
only the `volumes/` subtree. Images, build cache, overlay layers, and
containers are excluded. The VM layout is in data-model.md.

PATCH accepts only `volume.size_gb`. It does not change `status`, name,
attachment, type, mount path, or purpose. The new size must be a
positive integer strictly larger than the stored size. An equal or
smaller size returns 400. Extend does not use the status lock.

Delete and snapshot use `check_transition`, then compare-and-swap
`status`. Volumes have no `task_state` column. These actions require a
forward edge. The same-state no-op is not used. A disallowed edge, a
repeated snapshot, or a repeated delete returns 409 and does not change
the row. SERVER-8 adds a stricter DELETE eligibility gate: active
creation, snapshots, server associations, mounts, unsettled extensions
and competing stack/server work return 409. Acceptance atomically takes
the stack `RECONCILING` task and changes the volume to `DELETING`.
Extend and snapshot check the stack task under project-first locks.
The status transition table itself is unchanged. Snapshot from `AVAILABLE` or
`IN_USE` stores `SNAPSHOTTING`. Completion later restores the prior
attachment status. That restore is conductor work.

`cinder_volume_id` is returned only when the caller passes
`coast:volume:get_detail`. The rule is project-scoped and defaults to
admin. Other callers do not receive the field. A `fields` request for
that name without the rule returns 400.

### Named storage

Microversion 1.10. Older versions return 404. These routes are not the
Cinder `/volumes` collection.

```text
POST /v1/stacks/{stack_id}/storages
{"storage": {"name": "uploads", "size_gb": 10}}
```

The response envelope is `storage`. `status` is `AVAILABLE`. `origin` is
`api`. `service_id`, `server_id`, and `container_path` are null.

```text
POST /v1/stacks/{stack_id}/storages/{storage_id}/attach
{"attach": {"service_id": "<uuid>", "container_path": "/var/data"}}
```

Attach sets `IN_USE` and stores the service, the worker, and the
container path. Detach body is `{}`. Detach sets `AVAILABLE`, clears
`container_path`, and keeps `service_id` and `server_id`. Delete is
allowed only while `AVAILABLE` and returns 204 with no body.

`host_path`, `fs_path`, `source`, `bind`, `type`, and `read_only` return
400. An absolute `compose_path`, `dockerfile_path`, or `base_directory`
also returns 400 on every microversion. `./relative` is valid.

Project quota key is `storage_gb` (default 50). It is separate from
`volume_gb`. The sum of storage sizes on a worker must fit that
worker's Docker disk. A missing disk, a second live Docker disk, an
attached delete, a template row change, or a move to another service
returns 409. See `contributor/specs/storage`.

### Create service

The stored `kind` controls `kind=app|service` list filtering. At 1.28 and
later, omitted `kind` defaults to `app` for image and Git sources (including
Dockerfile and compose-from-repository build packs), and `service` for
one-click templates. Explicit `kind=app|service` takes precedence.
At 1.11–1.27, images default to `service` and accept either explicit kind;
Git sources must be `app` and templates must be `service` (conflicting
explicit kinds return 400). At 1.0–1.10, images and Git default to `app`,
templates default to `service`, and explicit `kind` returns 400. Existing
rows keep their stored kind regardless of the version used to list them.

```text
POST /v1/services
{"service": {
  "appgroup_id": "p100...",
  "environment_id": "e200...",
  "name": "web",
  "source": {"type": "git", "repo_url": "https://github.com/acme/web.git",
             "branch": "main", "build_pack": "nixpacks"},
  "ports": "3000",
  "server_id": "w300...",
  "memory_limit_mb": 1024,
  "healthcheck_path": "/healthz",
  "env": {"NODE_ENV": {"value": "production"}, "DATABASE_URL": {"value": "postgres://...", "secret": true}},
  "deploy": true
}}

202 {"service": {"id": "a9c3...", "name": "web", "status": "PENDING",
  "default_url": "https://web-a9c3.apps.example.com",
  "current_deployment_id": null, "deployment_id": "d001...", ...}}
```
M1 schema clarification (Q27, PM-decided, FYI James): the workload envelope
requires `name`, `server_id` (a worker, Q144), and `source`. Q94 names that
envelope `service`. `name` is a lowercase DNS label, at most 63 characters.
Q27 let the caller omit `environment_id` when `server_id` supplied the old
infra parent. Q94 requires both `appgroup_id` and `environment_id`.
The app engine server environment is not derived from the server. A supplied
server must be a worker on the stack that owns that appgroup, in the same
Keystone project.

Q98 (PM-decided): missing or other-project appgroup, environment or
server IDs return 404 without an existence leak. A same-project environment
outside the supplied appgroup, or a server outside that appgroup's
stack, returns 400. A non-worker server also returns 400. Both the
appgroup and environment must be ACTIVE and have an `engine_ref`;
otherwise create returns 409. These checks run before quota, secret, DB
or RPC writes. The appgroup selects the stack. `stack_id` is not a
create body field.

Q144 (James 2026-10-14): `server_id` must name a worker. Coast has no
automatic placement and the app engine server needs a server, so a null
`server_id` returns 400 with `server_id is required: ...` before any
lookup, quota, secret, DB or RPC write.

Responses expose `appgroup_id`, `environment_id` and read-only
`template_id` (null for Git and image). The appgroup ID is derived from the
stored environment parent. Existing pre-Q94 services without that parent
return null for both parent IDs. PATCH cannot change these fields.

Q95: service create validates `source=template:<id>` at the API boundary. In the
existing source object that is `{"type": "template", "template_id": "<id>"}`.
`coast:template:get` applies before
create. A valid template request stores `template_id` and returns 202.
Git source rules below stay in force for `type: git`.

Before API 1.30, a Git `source` requires `type: git` and a public HTTPS
`repo_url`
without embedded credentials, query or fragment. Optional fields:
`branch` (default `main`),
`build_pack` (`nixpacks|dockerfile|static|compose`, default `nixpacks` on create),
`dockerfile_path`,
`compose_path`, `base_directory`. Other source fields are rejected. Private Git support is described below.

At API 1.30, SSH Git sources accept ``git@host:path`` and
``ssh://git@host[:port]/path`` with ``deploy_key`` set to either
``{"generate": true}`` or ``{"private_key": "<OpenSSH/PEM text>"}``.
HTTPS sources cannot use a deploy key; embedded HTTPS credentials remain
invalid. Earlier microversions accept public HTTPS Git only.

Coast generates an ed25519 key or validates an uploaded, unencrypted
ed25519, ECDSA or RSA key (RSA must have at least 2048 bits). Private keys
are normalized to OpenSSH format and stored in Barbican, subject to the
configured secret size limit. Create, show and list expose only
``source.deploy_key.public_key`` and ``source.deploy_key.fingerprint``
(the OpenSSH SHA256 fingerprint). Private keys never appear in responses,
notifications or exports. Invalid uploads, including keys above the secret
size limit, return 400 with
``deploy_key.private_key: not a supported unencrypted private key`` on both
create and PATCH.

Generated keys default to ``deploy: false``. The create response includes
``deploy_required: true`` when no first deployment was requested. Add the
public key to the repository, then request a deployment. An explicit
``deploy: true`` is honored. Uploaded keys retain the usual deploy default.

PATCH ``source.deploy_key`` replaces the app's deploy key without deploying.
It returns 200 on success and 409 if replacement fails or the app has no
engine application yet. Failed replacements release the service operation
claim so DELETE remains available while recovery retries in the background.
The existing engine key UUID and application binding stay unchanged. Coast
updates the key material through the documented key API and commits the new
Barbican reference only after success. A failure restores the previous key
material and retains the old source credentials. Uncertain delivery or
rollback is recovered from a durable checkpoint. Obsolete Barbican secrets
are removed after commit; failed cleanup is retried without deleting the
live engine key. App and stack deletion remove keys and owned secrets,
including pending cleanup, with retries after partial failure.



Q99 (RESOLVED, PM-decided, FYI James): an image source is
`{"type":"image","image":"registry.example.com/web","tag":"release"}`.
`image` must be a Docker reference name without scheme, credentials, tag
or `@`. `tag` is required (no implicit latest), with grammar
`[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`. Digest references and supplied registry
credentials return 400 (Q53). Private registries require preconfigured
per-environment engine credentials; the API does not accept them.
Git-only fields are rejected. For image sources, PATCH can change
`source.tag`; from the image-edit microversion it can also change
`source.image`. Deployment `source_ref` overrides the tag
and uses the same grammar. Rollback is unchanged.

Optional service fields: `description`, `ports` (default empty: no exposed ports),
`replicas` (1 only), `auto_deploy` (false only), `cpu_limit` (positive or null),
`memory_limit_mb` (positive integer or null), `healthcheck_path`, `env`, and
`deploy` (boolean, default true). Nullable description/limits/healthcheck
fields default null; env defaults empty. Unknown fields are rejected.

PATCH requires the `service` envelope and accepts only `name`, `description`,
`ports`, `cpu_limit`, `memory_limit_mb`, `healthcheck_path`, and a partial
`source` containing branch/build settings for Git, or image/tag for images
(image updates require the new microversion; type/repo_url/credentials are
immutable).
It cannot change placement, appgroup, environment, identity IDs, or env vars. PATCH
returns 200 and does not redeploy. Only a deleting service returns 409.
Name and description are metadata. When the service is idle and known,
those fields are pushed immediately and do not set `pending_changes`.
Ports, source, CPU limit, memory limit, replicas, health check path, and
env values are deployment-affecting. `pending_changes` compares those
fields with the frozen spec when that spec has `ports` and `source`.
An older incomplete spec uses `settings_changed_at`. A revert of a
deployment-affecting field clears the flag.

Public service fields are the service columns except `source_id`, `engine_ref`,
`task_state`, and `deleted_at`, plus a nested public `source` (without IDs,
secret refs or credentials), computed `default_url`, `dns_records` and links.
The default URL is `https://` plus the stored default hostname. A row
without that value uses `<name>-<id4>.<dashed-ip>.<suffix>` when a
floating IP is present, otherwise `<name>-<id4>.<suffix>`. The suffix
is `[domains] default_suffix`. An empty suffix makes the URL null.
DNS records
include the default host routing record when placement has a floating IP,
and records from custom domain rows. Unknown routing IPs are not invented.
`deployment_id` is returned only by create when deploy is true (Q28).

Create remains PENDING with a PROVISIONING lock until handler completion.
Start (STOPPED only), stop/restart (ACTIVE only) return 202, retain status
and acquire RECONCILING until completion (Q31). Start/restart then show
STARTING until the engine reports running, polled by the deployment
reconcile loop without the project lock; after `[conductor] start_timeout`
(default 600 s) the service is ERROR. A user deployment shows DEPLOYING on
the service until it is terminal. On failure it becomes ACTIVE only if a
previous release is verified running; otherwise it becomes ERROR. A known
crash loop always produces ERROR. Cancellation restores ACTIVE when a
previous release exists, otherwise STOPPED. With `[conductor] http_check`
(default on), ACTIVE also requires the default URL to answer below HTTP 500 at the worker
floating IP. DELETE returns 202 with
DELETING and the DELETING lock; cleanup later soft-deletes the service.

### Deploy
```text
POST /v1/services/{service_id}/deployments
{"deployment": {"source_ref": "main"}}            # or a commit SHA / image tag
202 {"deployment": {"id": "d042...", "number": 42, "status": "PENDING", "trigger": "api",
  "source_ref": "main", "resolved_ref": null, "server_id": "...", "created_at": "..."}}
```

Deployment create requires the `deployment` envelope; optional `source_ref`
defaults to the source branch. It acquires the service DEPLOYING lock and returns
409 when busy. Public fields: id, project_id, service_id, number, source_ref,
resolved_ref (read-only exact commit SHA or image digest; null until reported
by the driver, Q33), server_id, trigger, rollback_of, status,
started_at, finished_at, created_at, updated_at, links. Frozen spec, secret
refs, image_digest, engine_ref, log_ref and fault are not returned.

### Rollback
```text
POST /v1/services/{service_id}/deployments/{dep_id}/action
{"rollback": {}}
202 {"deployment": {"id": "d043...", "number": 43, "trigger": "rollback",
  "rollback_of": "d040...", "status": "PENDING"}}
```
Rollback creates a **new** deployment whose spec copies `dep_id`'s spec and
image digest. Its source_ref uses the target resolved_ref when present,
otherwise the target original source_ref. The new resolved_ref remains null
until the driver reports it. Rollback does not change the old spec or source
reference. Runtime failure output can still be captured on a failed row.
The target must be a prior successful deployment of the same service (ACTIVE,
SUPERSEDED or ROLLED_BACK; the enum has no SUCCEEDED state), with a lower
number than the current deployment; otherwise 409. Rollback acquires the
service ROLLING_BACK lock. Cancel accepts `{"cancel":{}}` for the latest
PENDING/BUILDING/DEPLOYING deployment while its existing service task is
PROVISIONING, DEPLOYING or ROLLING_BACK; otherwise 409. It returns 202 with
CANCELLED, retaining that lock until handler completion. It cannot cancel
an older or completed release. No second task lock is acquired.

### Logs
```text
GET /v1/services/{service_id}/deployments/{dep_id}/log
200 {"log": {"deployment_id": "d042...", "complete": false,
  "lines": [{"ts": "2026-10-09T14:06:01Z", "stream": "build", "line": "#5 [2/6] RUN npm ci"}]}}
GET /v1/services/{service_id}/logs?tail=200
200 {"logs": {"lines": [...]}}
```
Logs are read live through the driver (`call` with timeout). M18 also stores
a sanitized runtime failure tail on the deployment; see API 1.5 below.
In M1 the synchronous no-op handler returns empty lines, and deployment
logs return complete=false (Q32). Service log `since` accepts an ISO UTC
timestamp and `tail` a non-negative integer; other parameters return 400.
Without stored failure output, a live read returns **503** when the
conductor cannot reach the engine
or the engine rejects the Coast credential (`EngineUnreachable`,
`EngineAuthError`). `faultstring` is `Service Unavailable`. The body
does not include the engine error. Missing engine log resources return
empty lines with `complete: true`, rather than an error. Template deployment
logs contain a completed snapshot of service container output when a single
log target is available; otherwise they are empty and complete. HTTP 403
from the template service logs route, including an older agent's rejection,
also returns empty, complete output without retries. HTTP 401 and failures
on service metadata or env reads retain their existing error handling.
Another engine fault stays **500**.
Export list, show, and `renew_url` return **503** for the same faults.
The conductor keeps `EngineUnreachable` and `EngineAuthError` and drops
their text. Another fault stays **500**. This correction applies to
microversions 1.0 and later. M18 adds the stored-output fallback described
below.
An app engine server build entry with hidden set to true is included in line.
Masking is unchanged and the response has no new fields. A hidden
build entry with no output text is the line "Build step in progress."

### Domain
```text
POST /v1/services/{service_id}/domains
{"domain": {"fqdn": "www.acme.com", "auto_dns": false}}
201 {"domain": {"id": "...", "fqdn": "www.acme.com", "status": "PENDING_VERIFICATION",
  "dns_mode": "manual",
  "dns_records": [
    {"type": "TXT", "name": "_coast-challenge.www.acme.com", "value": "hbr-7Yc...", "purpose": "verification"},
    {"type": "A", "name": "www.acme.com", "value": "203.0.113.25", "purpose": "routing"},
    {"type": "CNAME", "name": "www.acme.com", "value": "web-a9c3.203-0-113-10.sslip.io", "purpose": "routing-alternative"}]}}
```
`dns_records` is always returned (decision Q5) so any client can show users
exactly what to create. With `auto_dns: true` and the tenant's zone in
Designate (same project) Coast creates them itself (`dns_mode: designate`).
After `verify` matches, Coast publishes the domain. `tls` `letsencrypt`
uses HTTPS. `tls` `none` uses HTTP on port 80. `GET
/v1/services/{service_id}` also returns `dns_records` for the default URL when
Designate is not in use.

M1 domain schema: the `domain` envelope requires a lowercase valid `fqdn`
(up to 253 characters), with optional boolean `auto_dns` (default false).
Create returns 201, PENDING_VERIFICATION and computed DNS records without
DNS/backend calls. From microversion 1.2, create and PATCH accept `tls`
as `letsencrypt` (default) or `none`. A lower version returns 406.
Authorization is checked first, so an unauthorized caller receives 403.
`[domains] skip_verification` marks the new domain ACTIVE and publishes
it. Public fields are domain columns except
verification_token, plus dns_records and links; the TXT record carries the
verification token. FQDN conflicts return 409 without exposing ownership.
Verify and delete return 202 under the service RECONCILING lock. Verify
checks TXT in the request and returns the updated domain: VERIFIED on a
match, otherwise PENDING_VERIFICATION. ERROR moves to PENDING_VERIFICATION
first. Delete keeps the status unchanged and retains the row until
completion. PATCH `tls` returns 202 and publishes the domain list again.

### Plans (decision Q34, revisable contract)

Plan descriptions returned to non-admin callers replace engine product names
with "App engine server". Operators should use neutral catalog descriptions.

Plans come from operator config, not the DB. `GET /v1/plans` requires
`coast:plan:get_all` (any authenticated project user) and returns 200:

```json
{"plans": [{"name": "example-plan", "kind": "vm",
            "roles": ["control", "worker", "build"], "description": null}]}
```

The response is sorted by name, has no pagination or links, and includes
only name, kind (`vm|baremetal`), roles (list of `control|worker|build`),
and nullable description. There are no plan show/write endpoints. Query
parameters return 400; writes return 405. All projects see the same catalog.

`[plans] definitions` is a JSON array in the operator configuration, empty
by default. Each entry requires name (non-empty, at most 64 characters),
kind, roles (distinct supported roles), and internal flavor (Nova flavor
name/ID, non-empty, at most 255 characters). Description is optional and
defaults to null. Names must be unique; unknown config fields are rejected.
Malformed operator definitions return a sanitized 500, not tenant 400.
Internal flavor is never returned, including to admins. Empty config returns
`{"plans": []}`. The existing plans_file option is not read by this endpoint.

Q34 is **PM-decided, FYI James** and is a **revisable contract**. Version 1.0 does not
validate plan references on version 1.0 stack create or server create.
Version 1.1 stack create validates plans by VM kind and role (Q111).
The conductor maps plans to backend flavors; no plan DB table is introduced.

### Export (decision Q10)
```text
POST /v1/stacks/{stack_id}/exports
{"export": {"volume_ids": null}}          # null = all data and Docker volumes
202 {"export": {"id": "e7...", "status": "PENDING", "volume_ids": ["..."]}}

GET /v1/stacks/{stack_id}/exports/e7...
200 {"export": {"id": "e7...", "status": "ACTIVE",
  "artifacts": [{"volume_id": "...", "name": "acme-prod-data-20261009T1500Z.tar.gz",
                 "size_bytes": 7340032000, "sha256": "9f2c...",
                 "download_url": "https://objects.example.com/v1/AUTH_.../coast-exports/...?temp_url_sig=...&temp_url_expires=...",
                 "expires_at": "2026-10-10T15:00:00Z"}],
  "object_expires_at": "2026-10-12T15:00:00Z"}}
```
Exports include `/data` and only the `volumes/` subtree of Docker volumes.
The export tool must quiesce DB containers (stop or app-level dump) before
snapshot and tar; lifecycle.md defines consistency and manifest rules.
The driver should configure app engine server app persistent storage under `/data`
where possible. Guest replacement moves both Cinder volumes.

`download_url` is returned only to project managers/admins and only while
valid; `renew_url` mints a new one while the object exists. The S3 backend
returns that same field. The value is a path-style SigV4 presigned GET.
It is not stored.

M1 export schema clarification (Q35, PM-decided, FYI James): create requires
an `export` envelope. `volume_ids` is optional or null for all data/Docker
volumes; an explicit list must contain distinct, non-empty IDs and cannot
be empty. Selection scans all pages. A missing or foreign-project volume
returns 404; a same-project volume in another stack or with purpose
`scratch`/`controller` returns 400. No selected volumes returns 400.
Selected volumes must be AVAILABLE or IN_USE; otherwise 409.

Create requires an ACTIVE stack and acquires its RECONCILING task
lock by CAS, without changing stack status. A busy or non-ACTIVE
stack returns 409. The new export is PENDING. Noop completion moves
it through SNAPSHOTTING and UPLOADING to ACTIVE, then releases the
stack lock. The noop driver writes no archive, expiry, or download URL.
The real flow in `coast/conductor/exports.py` creates archives and signs
download URLs through the configured Swift or S3 backend.
Delete compare-and-swaps export
status to DELETING and returns 202. Noop delete removes the row. If that
export's create has not finished, delete releases the stack
RECONCILING lock. It does not release a lock held for a different export.
A repeated delete returns 409. Exports have no task_state column; delete
does not acquire another stack lock.

`DELETE /v1/stacks/{stack_id}` accepts `export_before_delete`. Real
teardown runs this export flow before it deletes cloud resources and
keeps the export after the stack is deleted. You can set that flag
with `snapshot_volumes`. `snapshot_retention_days` without
`snapshot_volumes` still returns 400. The noop fake-cloud delete
still fails closed after the API accepts the flag.

Public export fields are the fields in the examples: id, status, volume_ids,
artifacts, object_expires_at, plus links. Backend and fault are not exposed.
List supports the common pagination/filter/fields parameters; default order
is created_at ascending. Explicit sort/filter of JSON fields is rejected.
There is no separate download endpoint.

Q36 (PM-decided, FYI James): use the existing `coast:export:renew_url`
policy for URL visibility on list/show as well as renewal; do not add a
policy default. Return only documented artifact metadata (volume_id, name,
size_bytes, sha256), plus ephemeral download_url/expires_at when permitted.
Mint URLs only for ACTIVE exports whose object_expires_at is in the future.
Expired URLs, or URLs beyond object retention, are omitted. Renewal returns
200 with the export envelope; non-ACTIVE or expired objects return 409.
An engine connection or credential fault on list, show, or renewal returns
503 with faultstring `Service Unavailable`. The body does not include the
engine error. Another fault returns 500.
The synchronous injectable M1 handler returns no URLs; it does not claim
that an archive or signed URL exists. URLs are never stored in artifacts.
Archive creation, DB quiescing, path selection and URL signing follow in M3.
Export concurrency and size limits use the project quota lock (Q40).
Pending exports (PENDING, SNAPSHOTTING, UPLOADING) count toward
`[export] max_concurrent_per_project`. Selected volume capacity must not
exceed `[export] max_size_gb`. These are separate operator limits, not
additional fields in the seven-resource quota API. Violations return 403.

### Env vars
```text
GET /v1/services/{service_id}/env
200 {"env": {"NODE_ENV": {"value": "production", "secret": false,
                         "version": 1, "build_time": false},
             "DATABASE_URL": {"value": null, "secret": true,
                              "version": 3, "build_time": false}}}
PATCH /v1/services/{service_id}/env
{"env": {"FEATURE_X": {"value": "on"}, "OLD_KEY": null}}
200 (+ X-Coast-Redeploy-Required: true)
```

Each env value is limited to `[secrets] max_secret_bytes` (default 20000
UTF-8 bytes). Oversized values return 400 naming the limit before any
submitted values are stored.

Env PUT replaces all keys; PATCH merges keys, with null deleting a key.
Each non-null entry requires string `value` and accepts boolean `secret`
and `build_time`. `secret` defaults false. At microversion 1.6,
`build_time` defaults true for applications and false for template services.
At 1.0 through 1.5 it defaults false for both. Explicit flags work at all
versions; template services reject `build_time=true` with HTTP 400.
Env names use `[A-Za-z_][A-Za-z0-9_]*`
and are at most 255 characters. Each update stores a new immutable opaque
secret ref and increments the existing row version; omitted PATCH flags
retain their previous values. Replacing a key with PUT resets omitted flags
to the create defaults above. Historical refs remain available for
deployment rollback.
GET, PUT and PATCH responses use the GET shape above, include the stored
`build_time` flag and never expose secret refs. Changes return 200 with
`X-Coast-Redeploy-Required: true`; a busy service returns 409.
All values require secret storage, including non-secret values. The M1
injectable interface fails closed when not configured; its in-memory fake
is for tests only. Real Barbican integration is a follow-up (Q30).

Events are list-only (Q29), with the public events table fields and links.
Equality filters include resource_id, stack_id and environment_id; standard
pagination and fields filtering apply. No event get/write endpoint exists.
An event's self link selects it through the list's id filter.

## Policies

Base check strings (`coast/policies/base.py`):

| Name | Check string |
|---|---|
| `PROJECT_READER` | `role:reader and project_id:%(project_id)s` |
| `PROJECT_MEMBER` | `role:member and project_id:%(project_id)s` |
| `PROJECT_MANAGER` | `role:manager and project_id:%(project_id)s` |
| `ADMIN` | `role:admin` |
| `PROJECT_READER_OR_ADMIN` | `rule:PROJECT_READER or rule:ADMIN` (same for member/manager) |

| Rule | Default |
|---|---|
| `coast:stack:get_all`, `get_one` | PROJECT_READER_OR_ADMIN |
| `coast:stack:create` | PROJECT_MEMBER_OR_ADMIN |
| `coast:stack:delete` | PROJECT_MANAGER_OR_ADMIN |
| `coast:stack:update` | PROJECT_MEMBER_OR_ADMIN |
| `coast:export:get_all`, `get_one` | PROJECT_MEMBER_OR_ADMIN |
| `coast:export:create`, `renew_url`, `delete` | PROJECT_MANAGER_OR_ADMIN |
| `coast:stack:get_detail` | ADMIN |
| `coast:stack:upgrade`, `upgrade_rollback` | PROJECT_MEMBER_OR_ADMIN |
| `coast:appgroup:get_all`, `get_one` | PROJECT_READER_OR_ADMIN |
| `coast:appgroup:create`, `delete` | PROJECT_MANAGER_OR_ADMIN |
| `coast:appgroup:update` | PROJECT_MEMBER_OR_ADMIN |
| `coast:environment:get_all`, `get_one` | PROJECT_READER_OR_ADMIN |
| `coast:environment:create`, `delete` | PROJECT_MANAGER_OR_ADMIN |
| `coast:environment:update` | PROJECT_MEMBER_OR_ADMIN |
| `coast:server:get_all`, `get_one` | PROJECT_READER_OR_ADMIN |
| `coast:server:create`, `delete` | PROJECT_MANAGER_OR_ADMIN |
| `coast:server:failover`, `get_detail` | ADMIN |
| `coast:volume:get_all`, `get_one` | PROJECT_READER_OR_ADMIN |
| `coast:volume:create`, `update`, `snapshot` | PROJECT_MEMBER_OR_ADMIN |
| `coast:volume:delete` | PROJECT_MANAGER_OR_ADMIN |
| `coast:volume:get_detail` | ADMIN |
| `coast:service:get_all`, `get_one`, `get_logs` | PROJECT_READER_OR_ADMIN |
| `coast:service:create`, `update`, `delete`, `start`, `stop`, `restart` | PROJECT_MEMBER_OR_ADMIN |
| `coast:template:list`, `get` | PROJECT_READER_OR_ADMIN or PROJECT_MEMBER_OR_ADMIN or PROJECT_MANAGER_OR_ADMIN |
| `coast:deployment:get_all`, `get_one`, `get_log` | PROJECT_READER_OR_ADMIN |
| `coast:deployment:create`, `rollback`, `cancel` | PROJECT_MEMBER_OR_ADMIN |
| `coast:domain:*` (read) / (write) | READER / MEMBER (OR_ADMIN) |
| `coast:secret:get_all` | PROJECT_MEMBER_OR_ADMIN (values of `secret=true` never returned) |
| `coast:secret:update` | PROJECT_MEMBER_OR_ADMIN |
| `coast:event:get_all` | PROJECT_READER_OR_ADMIN |
| `coast:quota:get_one` | PROJECT_READER_OR_ADMIN |
| `coast:quota:get_defaults` | PROJECT_READER_OR_ADMIN |
| `coast:quota:get_all`, `update`, `delete` | ADMIN |
| `coast:plan:get_all` | `@` (any authenticated) |

`scope_types=['project']` on all rules. The API layer *also* checks
`resource.project_id == context.project_id` before policy (defence in depth).

## Quotas

| Resource | Default (proposed) | Counted when |
|---|---|---|
| `environments` | 1 | stack row created (not DELETED). Q94 keeps this key name. |
| `servers` (worker+build, excl. control) | 2 | server row created |
| `baremetal_servers` | 0 | kind=baremetal |
| `build_servers` | 1 | role=build |
| `services` | 10 | service row created. The implemented quota key is still `apps`. |
| `domains` | 20 | custom domains (default domains free) |
| `volume_gb` | 200 | sum of volumes.size_gb |

Defaults in `[quotas]` config; per-project overrides via `/v1/quotas`.
Over-quota -> **403** `{"faultstring": "Quota exceeded for resources: ['apps']"}`
(Octavia behaviour). Reservations are made in the API transaction and
committed/rolled back by the conductor.

### Quota payloads (Q37)

Q37 is **PM-decided, FYI James**, approved by the project lead. James
approved a public `usage` field on show (2026-10-11, gap 8). Show
returns `{"quota": {<seven limits>}, "usage": {"in_use": {...}, "reserved": {...}}}`.
Defaults and update return `{"quota": {<seven limits>}}`. The admin list
returns `{"quotas": [{"project_id": "...", <seven limits>}]}`. Only the
seven limit names are accepted on update. Show usage counts are the
quota engine counters. Empty counters are zero. A missing
project override uses config defaults.

`GET /v1/quotas/{project_id}` returns 200, the effective limits, and the
usage counters. `GET /v1/quotas/defaults` returns 200 and the effective
limits only. `GET /v1/quotas` returns 200 with stored override rows,
sorted by project_id; null limits mean config defaults. No overrides
returns an empty list. The list does not include projects with only a
usage sentinel. Quota endpoints do not support query parameters (400).

`PUT /v1/quotas/{project_id}` accepts the `quota` envelope and any subset
of the seven limits. Omitted limits keep their previous values. Integers
must be at least -1; -1 means unlimited, 0 disables new allocations,
and null resets that limit to its config default. Booleans, unknown fields,
usage counters and project_id in the body return 400. PUT returns 200
with effective limits. An admin can lower limits below current usage;
existing resources remain, but new reservations must fit the new limit.

`DELETE /v1/quotas/{project_id}` synchronously resets all overrides and
returns 204 without a body. Reset is idempotent, even without overrides;
it preserves the project lock sentinel and all usage counters. A request
body returns 400. These synchronous status codes follow Coast's endpoint
summary; the limit semantics follow the
[Octavia quota API](https://docs.openstack.org/api-ref/load-balancer/v2/).
Show is available to the owning project's reader or admin; list, update
and delete require the existing admin policies. Foreign project paths
return 404 to non-admins before any override lookup. Defaults use the
existing get_defaults policy. Missing authentication returns 401; wrong
roles return 403. Unsupported methods return 405, microversions above
1.0 return 406. This completes the existing 1.0 quota contract.

### Reservation lifecycle (Q38, Q39)

Reserve holds the existing quota_usages project sentinel before limits,
usage, or resource locks. Reservations and the resource write share one
API transaction; a failed write rolls both back. Reservations remain held
until conductor completion. The noop conductor commits or rolls back
reservations for environments, servers, volumes, services (implemented
counter `apps`) and domains.
Exports do not reserve quota. A completed export no longer counts toward
the concurrent export limit.

Stack create reserves one `environments` quota unit, each requested worker, the
optional VM build server, and requested data_volume_gb (or the configured
controller_worker data_volume_size_gb). Control servers do not use server
quotas. Baremetal and build servers also consume the general servers limit.
Plan-dependent managed volume sizes are not invented; later provisioning
must reserve any additional known capacity before creating its rows.
Volume extend reserves only the positive increase, under project and volume
locks, not the full new size. DELETING rows still consume capacity until
cleanup; environment DELETED and soft-deleted rows do not.

The internal reservation token is a mapping of resource names to amounts,
not a new DB/API field. The M1 handler receives the exact amounts through
context.quota_reservation. This transient attribute is not included in
context serialization; the M1 RPC handoff explicitly carries and retains
the mapping. The caller must complete it exactly once under its operation
state lock: commit moves reserved to in_use; rollback releases reserved
capacity in the same transaction as failed-resource cleanup. Delete
completion releases in_use capacity in the same cleanup transaction.
Aggregate counters cannot identify duplicate
completion calls while other reservations exist, so completion must use
CAS before changing counters. Underflow returns 409 without partial changes.
No reservation expiry or automatic background cleanup is added in M1.

Existing zero counters are initialized from project-scoped live resource
rows at first reservation of that resource. Counters with in_use or reserved
capacity are authoritative and are not counted again. This supports the
sentinels created by the earlier DB API without a new schema marker.
All later resource creation and cleanup must use the quota lifecycle.
Failed-create completion receipts exclude rolled-back rows from reseeding.
Volume start receipts hold only the reserved GiB amount in the existing
activity message. Completion clears that amount and records the outcome;
coalescing pending changes makes quota completion safe for unordered casts.

## Notifications

Lifecycle events use an oslo.messaging `Notifier`. The topic is
`notifications` unless `[oslo_messaging_notifications] topics` overrides
it. The message format is `messagingv2`. Coast selects `messagingv2`
when `driver` is unset. Set `driver = noop` to disable delivery. `retry`
defaults to 0 so a broker outage does not block the API or the
conductor. A delivery failure is logged without the payload and does not
change the API result.

Publishers are `coast-api.<hostname>` and `coast-conductor.<hostname>`.
Event types are `coast.<resource>.<action>.<phase>` with priority
`info`. Resources are `environment`, `server`, `volume`, `app`,
`domain`, `deployment`, `export`, `env_var`, and `appgroup`.
`environment` denotes an infrastructure stack and `app` denotes a service. Appgroup delete uses `coast.appgroup.delete`. App engine server
environment delete uses the public name `environment`, so those events
are `coast.environment.delete`. Stack delete already uses that same
event name. The payload `resource_id` selects the row.

| Phase | Publisher | When |
|---|---|---|
| `start` | `coast-api` | The API accepted create, update, or delete |
| `start` | `coast-conductor` | The conductor began an attempt |
| `end` | `coast-api` | A synchronous change finished in the API |
| `end` | `coast-conductor` | The conductor committed success |
| `error` | `coast-conductor` | The conductor committed a failure |

Volume extend uses action `update`. Deployment cancel uses action
`update`. Rollback emits `coast.deployment.create.start` for the new
row. Environment PATCH (the stack) and service PATCH (Q47 resource
`app`), plus env-var writes, finish in the API, so the API emits `start`
and `end`. The noop conductor emits `end` and `error` for the `app`
resource, domain, deployment, and export completion. There is no
server update API, no domain update API, and no deployment delete API.
A failed cast does not emit `error`; the accepted state remains for
recovery.

The payload allow-list is `resource_id`, `project_id`, `user_id`,
`status`, `environment_id`, `service_id`, and `server_id`. Notifications
omit names, tokens, environment values, verification tokens, Barbican
references, and nested documents. The notification context omits
Keystone tokens. The pycadf paste audit filter stays later (Q46). Usage
notifications use the separate allow-list below (Q75/Q76).

### Usage notifications (decision Q11: Trove model)

The conductor emits `coast.environment.exists`, `coast.server.exists`,
`coast.volume.exists`, and `coast.app.exists` with priority `info`. These
use the existing conductor publisher, topic, transport, and token-free
context serializer. Lifecycle events keep their existing envelope and
allow-list; this task does not add separate lifecycle usage aliases.
The planned lifecycle usage aliases (`coast.environment.create/delete`,
`coast.server.create/delete/modify_plan`, and
`coast.volume.create/delete/modify_size`) and `coast.deployment.build`
with build duration remain follow-up work.

Q75/Q76 are PM-decided, FYI James. Each event is a snapshot of a resource
currently present in the Coast DB, excluding soft-deleted rows. This is
not historical usage: resources created after the audit bounds can appear,
and sizes reflect the current row, not the size during the period. Deleted
resources and complete period history are deferred follow-up work. Volumes
are hard-deleted, so this MR cannot recover their historical sizes.

| Resource | Resource-specific payload fields |
|---|---|
| environment | `plan` |
| server | `environment_id`, `flavor_id` when stored |
| volume | `environment_id`, `server_id` when stored, `size_gb` (GiB) |
| app | `environment_id`, `server_id` when stored, `replicas`, `cpu_limit`, `memory_limit_mb` when stored |

All events include `resource_id`, `project_id`, `status`, `created_at`,
`audit_period_beginning`, and `audit_period_ending`. `service_id` is included
only when configured. Timestamps are UTC ISO 8601 strings ending in `Z`.
The `app` event sizes are the service's configured limits, not measured consumption. Unset fields
are omitted. No names, user identity, tokens, Barbican references, nested
documents, or unavailable cloud sizing fields are sent.

The sweep runs at startup and every `[notifications]
exists_notification_interval` seconds (default 3600).
`[notifications] usage_audit_period` selects `hour` (default), `day`, or
`month`. Audit bounds label the previous complete UTC calendar period,
following [Nova's usage audit convention](https://docs.openstack.org/nova/latest/configuration/config.html#DEFAULT.instance_usage_audit_period).
Months use calendar boundaries, not a fixed number of seconds. As in Nova,
at an exact boundary the selected period ends strictly before the sweep
time (see the [Nova implementation](https://opendev.org/openstack/nova/src/branch/master/nova/utils.py)).
Offset and yearly periods are not supported in this MR.

Each process suppresses repeated sweeps for the same period. After downtime,
only the latest complete period is emitted; there is no catch-up history.
Restarts and conductor replicas can repeat events. Consumers must deduplicate
by event type, `project_id`, `resource_id`, and audit bounds. Delivery remains
best-effort: a broker failure does not change resource state, and individual
failed deliveries are not retried by this task. No durable audit checkpoint
or election is added. Guests never publish usage notifications.

## Engine rebuild actions (M4)

See [the engine upgrade contract](design/engine-upgrade.md). Upgrade accepts
only a published, active, operator-owned App engine server controller image
with a newer numeric engine version or a later Glance creation time for
the same engine version. The same image and engine downgrades are refused.
The conductor replaces the controller first, then each worker and build VM
on the configured published worker image for that version. It waits for
one server to complete before starting the next and stops on failure.
Unsupported versions are refused. Rollback
uses `{"upgrade_rollback":{}}`, requires a recorded rollback point, and is
available to project members and admins from ACTIVE or ERROR. Both actions
return 202 with the stack, 400 for invalid bodies/images, 403 for readers,
404 for other-project stacks, and 409 for busy or invalid states. Internal
rollback metadata is never returned, including in admin detail responses.

## Deploy and Git push webhooks (microversion 1.3)

Project members get the app URLs from `GET /v1/services/<id>/webhooks`
(`OpenStack-API-Version: app-hosting 1.3`):

* `deploy`: `POST /v1/apps/<id>/deploy?token=<token>` starts a deployment.
* `github`, `gitlab`, `bitbucket`, `gitea`:
  `POST /v1/hooks/<id>/<provider>?token=<token>`, for that host's push
  webhook. Use the token as the webhook secret so Coast verifies
  `X-Hub-Signature-256`, `X-Gitea-Signature`, Bitbucket `X-Hub-Signature`
  or GitLab `X-Gitlab-Token`. Non-push events are acknowledged and ignored;
  pushes to other branches and tags are ignored.

`POST /v1/services/<id>/webhooks/regenerate` issues a new per-app token and
revokes the old URLs. Each app deploys one at a time: a webhook while a
deployment runs gets `409`. Other responses: `202 queued` with
`deployment_id`, `200 pong`, `401` bad signature, `404` unknown app or
token, `413` over `[webhooks] max_body_size`.

## App health checks (microversion 1.4)

At microversion 1.4, service create and PATCH accept `health_check`:
`none` or `http`. Show and list return this field at 1.4 or later.
Earlier versions reject these new request fields with 406 and omit them
from responses. The authorization rules do not change.

New services use `none`, except for template defaults at API 1.7 below.
The `health_check` column defaults to `none`. Every app must have a
successful engine deployment and a running container for at least
`[conductor] stable_seconds` (default 20) before it becomes ACTIVE.
A poll that reports a stopped or restarting container resets this window.
The `none` mode requires no HTTP listener.

At API 1.7, template creates without ports use `http_port` when set, else
the first catalog port. If `health_check` is omitted and `http_port` is set,
Coast defaults to `http`, that HTTP port, and path `/`. Explicit client
settings take precedence. Without `http_port`, the default remains `none`.
Earlier versions default ports to the first catalog port and health to `none`.
These defaults are applied before health validation. Templates without catalog
ports keep empty ports. PATCH validates the resulting health and port settings
against the stored service on every update, including metadata updates.

With `http`, ports are required. `health_check_port` is an optional integer
from 1 to 65535; the default is the first app port. `health_check_path`
defaults to `/`. It uses the existing `healthcheck_path` storage field.
Do not send both path names in one request. The engine checks this port
and path. The conductor also checks the existing default URL route when
`[conductor] http_check` is enabled. A response below HTTP 500 passes.
No answer, 502, or 503 for `deploy_unreachable_timeout` fails the deployment
with the existing port message. HTTP readiness is an extra check after
the base stability window. Deployment checks use the saved deployment
spec; settings edits apply to the next deployment.

With `none`, ports can be null, blank, or absent. An app with no ports has a null
`default_url` and no default proxy route. Domain add returns 400 with
`App has no ports.` Remove custom domains before removing the app ports.
Show and list return no default routing `dns_records` for these apps,
even if a default hostname is still stored.
Catalog services keep the routes defined by their template.

## Durable runtime output (microversion 1.5, M18)

Deployment show and list add nullable `runtime_log` at 1.5. This field
contains sanitized runtime output captured during failure detection, capped
at 65535 UTF-8 bytes. Captures use JSON lines with engine timestamps,
streams, text, and capture times. Earlier versions omit the field. Service show and
list add nullable `reconcile_fault` at 1.5. It explains an observed engine
failure or confirmed restart loop and clears after stable runtime recovery.

Deployment and service log endpoints prefer live engine output. If the
engine returns no output or raises a driver error, deployment logs fall
back to that deployment's stored output. Service logs use the newest
deployment with non-empty stored output, regardless of deployment status.
Live reads retry unfinished empty output and driver errors up to ten times,
except missing resources, which return an empty, complete result. Retries are
0.5 seconds
apart, with a five-second retry deadline. No more retries start after the
deadline; individual engine calls still use their transport timeout. Fallback responses contain
`captured_at_failure: true`, `captured_at` (the latest stored capture time),
`deployment_id`, and a `note` that identifies
stored output. Line timestamps use the engine timestamp when available.
Untimed lines share one capture time per block. Consecutive identical
untimed lines become one `(repeated N times)` entry;
`since` does not discard a stored failure tail. `tail` limits service
output, including stored output; `tail=0` returns no lines. Without stored
output, existing error responses apply. Tenant authorization is unchanged.

Create and PATCH accept null or blank `ports` at all microversions. HTTP
health checks still require an app port. Custom domains must be removed
before the port can be cleared. A service without a port has no default
route; the driver sends an empty exposed-port list and empty domains.

If no output has ever been captured and the engine reports a restart or
failure, the logs response explains that capture continues in the
background and asks the user to refresh. A stored tail takes priority over
this message. See the Q105 TODO in lifecycle.md for the future worker
endpoint needed to read exited-container output reliably.

### Stack event timeline (1.8)

`GET /v1/stacks/{stack_id}/events` requires microversion 1.8. Earlier
versions return 404. The policy `coast:stack:events:get` defaults to
`rule:coast:stack:get_one`. Unknown and other-project stacks return 404.
Below microversion 1.9, only events for this stack with resource_type
`stack` or `server` appear. The optional `resource_type` filter accepts
those two values. At 1.9 the same call also returns `app` and `service`
rows. `volume` stays invalid.

Rows sort by created_at descending, then id descending. `limit` defaults
to 50 and is capped at 200; it must be positive. `marker` is the event ID
from a previous page, in the same stack and selected resource type.
Responses contain `events` and `links`; a next link has rel `next`.
Each event contains id, stack_id, resource_type, resource_id, action,
outcome, message, detail, request_id and created_at. Rows without
infrastructure facts have null detail.
Detail contains selected infrastructure facts only, never credentials or
environment variable values. Existing actions, messages and operation
receipts are unchanged. Delete detail counts removed servers, volumes and
stack networks when cleanup succeeds.

### Shell sessions (1.9)

`POST /v1/apps/{app_id}/console` and
`POST /v1/services/{service_id}/console` require microversion 1.9.
Earlier versions return 404. The policies are `coast:app:console` and
`coast:service:console`. Both require a project member or admin. A
reader receives 403. A manager-only role receives 403.

The app route accepts a workload with no template id. The service route
accepts a workload with a template id. The other route returns 404.
Another project's resource returns 404.

The body is optional. Omit it, or send `{"command": null}`, for an
interactive shell. Otherwise `command` is an array of 1 to 32 non-empty
strings. Each string is at most 256 characters. A bad body returns 400
and stores no token.

The workload must be `ACTIVE` and placed on a worker that has a
management address on the same stack. Otherwise the response is 409 and
`faultstring` is `The app is not available.` No token is stored.

The response is 201:

```text
{"url": "wss://console.example.com:9610/?token=REDACTED",
 "expires_at": "2026-10-18T12:00:00"}
```

`url` is a single-use websocket address. `expires_at` is a UTC
timestamp. The token is valid for `[console] token_ttl` seconds
(default 60). The service stores only a hash of the token. When
`[console] tls` is false, the scheme is `ws`. When
`[console] public_endpoint` is empty or contains a scheme or a path,
the response is 503 and `faultstring` is `Service Unavailable`.

An idle session closes after `[console] idle_timeout` seconds (default
900). Every session closes after `[console] max_duration` seconds
(default 14400). The stack timeline records `console.opened` and
`console.closed` at microversion 1.9. Close detail includes `duration`
in whole seconds, `user_id`, `resource_type` and `resource_id`. The
timeline does not record the command, keystrokes, or output.

## Compose editor (1.12)

Project members can GET and PUT `/v1/services/{service_id}/compose`.
The body is `{"compose": {"yaml": "services: ..."}}`. Compose is limited to
the smaller of `[api] max_compose_bytes` (default 10000) and
`[secrets] max_secret_bytes` (default 20000), in UTF-8 bytes. Oversized
content returns 400 naming the limit before secret writes. Save returns 200;
redeploy uses the existing deployments POST. See the
[compose contract](contributor/specs/compose-editor.md) for validation,
policy, faults, limits, and the pinned-engine application restriction.

## Service containers (microversion 1.13)

Read-only compose container list, detail, and logs are documented in
[the M39 contract](contributor/specs/compose-editor.md).
Policies are `coast:service:get_containers`, `coast:service:get_container`,
and `coast:service:get_container_logs`, all PROJECT_READER_OR_ADMIN.

## In-place image changes

PATCH saves a partial image source, for example
`{"service":{"source":{"image":"nginx","tag":"1.29-alpine"}}}`.
An omitted image or tag keeps its saved value. The same reference grammar
as create applies. Image/tag fields on Git, Git-compose or template sources
return 400. Existing member/admin `coast:service:update` policy applies;
other-project resources return 404. Unknown image-source fields return 400;
the existing source schema is unchanged.

Save does not contact the engine or deploy. Then use
`POST /v1/services/{service_id}/deployments` with `{"deployment":{}}`.
The driver PATCHes the existing engine application using the frozen source
before enqueue. It does not delete or recreate it. The public engine API
is documented at
documented application update API.
Coast and engine IDs, env references, named storage, file mounts, domains and deployment history
remain in place. Deploy failure keeps saved desired state for a later attempt.
Rollback uses its original frozen source. No schema migration is needed.

## Container config files (API 1.15)

Apps and services expose `GET/POST /v1/services/{id}/file_mounts` and
`GET/PATCH/DELETE /v1/services/{id}/file_mounts/{file_mount_id}`. Apps also
expose the same operations under `/v1/apps/{id}/file_mounts`. Policy rules
are `coast:file_mount:{get_all,get_one,create,update,delete}`. Reads require
reader, writes require member, and delete requires manager (or admin).
Content is write-only, UTF-8, at most the smaller of 64 KiB and
`[secrets] max_secret_bytes` (default 20000), and stored in Barbican.
Oversized content returns 400 naming the limit before secret writes.
Create and update use `{"file_mount": {"container_path": "/etc/site.conf",
"content": "server {}"}}`; PATCH may omit either field. Optional
`resource_name` selects one container in a template service. File and named
volume paths cannot overlap. Changes, including removal, require the next
deploy; rollback also uses current file settings. See
[Config files](contributor/specs/storage.md#config-files-m39-api-115).

Compose build packs are accepted. Worker workload settings are unrestricted
as described under API 1.17 below.

## Observed service storage (API 1.16)

`GET /v1/stacks/{stack_id}/storages?service_id={service_id}` includes
engine-observed named volumes. Storage responses add `source=coast` or
`source=template`. Template and Compose volumes are read-only (409 on
attach, detach, PATCH or delete). Engine-only volumes have stable IDs and
null `size_gb`; reads do not persist accounts or reserve quota. Engine
read failure returns 503 for connectivity/authentication faults, otherwise
500. See contributor/specs/storage for account retention and paging.

## Unrestricted dedicated workers (API 1.17)

Customers may use host paths, Docker sockets, privileged containers, host
network/pid/ipc/uts/userns, devices and added capabilities on their dedicated
stack workers. Templates and customer Compose have no workload safety
checker or create/deploy/edit/reconcile recheck. Beszel and Beszel Agent
are selectable and creatable on every supported microversion. The template
index supplies named-volume accounting metadata only; it does not gate
workload settings. Storage response shapes are unchanged.

The Compose editor retains bounded safe YAML parsing and secret storage.
File-mount and named-storage targets may use worker system/socket paths.
Image PATCH retains its image/tag schema; runtime settings use Compose or
the worker. Control-plane security groups, address groups, agent mTLS,
least-privilege service credentials and project/stack ownership are kept.

## Service metrics (API 1.21)

`GET /v1/services/{service_id}/metrics?range=1h` uses
`coast:service:get_one`. Project readers can read their own service;
administrators can read any service. No extra policy is added. Earlier
microversions return 404. Invalid or repeated ranges and unknown query
parameters return 400. See service-metrics.md for windows and aggregation,
and the API reference for the complete response. There is no write API.

## Typed service storage (API 1.19)

Storage create accepts `type=bind` with `host_path`, or `type=volume` with
`name` and `size_gb`. Both require `service_id` and `container_path` and
accept `read_only` (default false). PATCH updates the path, source or mode;
DELETE also removes attached mounts. Writes take effect on redeploy.
List with `service_id` returns user and template mounts with `id`, `type`,
`source`, `container_path`, `read_only` and `origin=user|template`.
Template entries have null IDs and remain immutable. See
[the storage contract](contributor/specs/storage.md#typed-service-storage-m45-api-119-issue-22)
for schemas, validation, accounting and placement rules.

## Database backup API (1.20)

Database backups require Swift-compatible object storage in the cloud.
They are not verified end to end in 0.1.0; full verification is tracked
for 0.1.1 in issue #124.

See [Database backups](contributor/specs/database-backups.md) for the
API contract, durable operations, storage permissions, secrets and limits.

### API 1.22: Customer DNS zones and automatic records

`GET /v1/dns/zones` returns `{"zones": [{"id": "...", "name":
"example.com.", "project_id": "..."}]}`. The project reader/member/manager
policy permits only zones in the caller project, including for admins.
The optional `trust_id` query selects a Keystone trust for discovery.
Unsupported microversions return 404; unsupported query fields return 400.
At 1.22, no DNS endpoint in the service catalog returns 404 with
`DNS zones are not available on this cloud.` Clients should hide zone and
host fields for this response. An empty zone list still returns 200.
Designate failures return 409 with a safe reason (for example,
`DNS service unavailable.`), never backend exception details. This is a
bug fix within 1.22; an older-version 404 is not an availability signal.

Stack create and PATCH accept nullable `default_domain`, `dns_zone_id`, and
`dns_trust_id`. Set a domain alone to select the longest matching project
zone, or set a zone alone to use its name. Clear both domain and zone with
null to return new apps to the configured fallback. Stack responses add
`default_domain` and `dns_zone_id`. Trust references are not returned.

Domain create and PATCH accept `fqdn`, `auto_dns`, `zone_id`, `dns_trust_id`,
`record_type` (`A`, `AAAA`, `CNAME`) and the existing `tls`. Alternatively,
`host` plus `zone_id` forms the FQDN; an empty host selects the zone apex.
Host and zone require `auto_dns=true`. A missing zone for an automatic FQDN
uses the longest project-zone match. An explicit zone must contain the name.
CNAME at the zone apex is invalid. Foreign or missing zones return 400,
without disclosing the zone's project. Domain PATCH returns 202; a name or
routing change resets verification and rotates the TXT challenge. Existing
manual DNS and TLS checks remain in effect.

Domain responses add nullable `zone_id`, `record_type`, `status_reason`, and
`managed_records` (each entry has `id`, `zone_id`, `name`, `type`, `records`).
`dns_mode` is `designate` for automatic records and `manual` otherwise.
The observation list can be empty while the conductor creates records.
Quota, credential, missing-zone, service and conflict failures have safe
status reasons. Reconcile retries without overwriting a customer record.
Earlier microversions retain their request and response fields and their
existing no-op `auto_dns` behavior for unlinked domains.

Server responses include read-only `hostname`, the saved DNS label used by
Nova, the guest and its App engine server entry. Stack/server renames and
failover preserve it. Requests cannot set it. See lifecycle.md for naming.

### Primary address preferences (API 1.23, issue 43)

Domain create and PATCH accept boolean `primary` (default false). Responses
include it from 1.23. Setting true clears the other primary flags on the same
service in the same transaction, serialized by its service row lock. A domain
must belong to that service and the caller's project. Verification and TLS
readiness are unchanged; choosing a primary domain does not verify it.

Service PATCH accepts boolean `default_domain_enabled` (default true).
Service responses add this setting and `primary_url` from 1.23. `primary_url`
is the primary domain URL (`http` for TLS `none`, otherwise `https`), falling
back to `default_url` when no primary is selected.

Disabling the default address without a primary custom domain returns 409:
`Pick a main domain before turning off the default address.` While disabled,
`default_url` is null, its routing records and CNAME routing alternative are
omitted, and reconciliation removes the default engine route and owned DNS
records. The stored default hostname remains available for re-enabling.
Custom routes and verification records remain. A managed custom CNAME that
would target the disabled address is reconciled to a direct A/AAAA record;
re-enabling restores its requested CNAME preference.

Deleting or un-primarying the primary domain while the default is disabled
returns 409: `Turn the default address back on or pick another main domain
first.` These guards also apply to older clients. Requests using the new
fields below 1.23 return 406; older responses omit them. Existing rows retain
their default address and have no primary custom domain. Quotas are unchanged.

### Service task holder (microversion 1.24)

Service responses include read-only nullable `task_owner` and
`task_started_at` (UTC ISO 8601). Both are null when no task is held and
are omitted before 1.24. A 409 for a known operation can include its plain
label and start time, for example: `Domain check in progress since 22:58
UTC. Try again in a moment.` Deployment, rollback and deletion messages
retain their existing text. No backend product names are exposed.

## Worker ingress rules (issue 50, API 1.25)

The owner project's readers can list `GET /v1/stacks/{id}/ingress_rules`.
Members can `POST` to that collection and `DELETE` a custom rule at
`/v1/stacks/{id}/ingress_rules/{rule_id}`. Other-project stacks return 404.
The request envelope is `ingress_rule`; list returns `ingress_rules`.
Creation returns 201, deletion 204. Older microversions return 404.

A rule contains `protocol` (`tcp`, `udp`, `icmp`), `port_range_min`,
`port_range_max`, `source` (IPv4 CIDR, default `0.0.0.0/0`) and optional
`description` (up to 255 characters). TCP/UDP require a minimum port in
1..65535; the maximum defaults to the minimum and cannot be smaller.
ICMP forbids ports. Sources are normalized to network prefixes.
Unknown fields, egress, IPv6, remote groups and address groups return 400.
Exact duplicates, including required rules, return 409; descriptions do
not affect duplicate detection. Overlapping ranges are allowed.
The per-stack customer limit is `[controller_worker] ingress_rules_per_stack`
(default 50); exceeding it returns 403 `OverQuota`. Writes during stack
operations return 409. Project and stack locks serialize checks and writes.

Responses include `id` and `managed`. Required TCP 80/443 from anywhere
and TCP 22 from the app engine server are listed with `managed: true` and
stable IDs `managed-http`, `managed-https`, `managed-ssh`. The SSH entry is
available once the controller private address is recorded. Deleting these
IDs returns 409. The API exposes ingress only; required egress stays under
Coast control. Security group IDs remain admin-only.

Customer desired state is stored in Coast. Stack create, server add and
periodic network repair merge it into the worker allow list. API changes
apply at the next enabled repair sweep; there is no inline Neutron call.
Disabling periodic repair also disables automatic application of changes.
The default sweep interval is 600 seconds; busy stacks and a spent time
budget defer repair. Unknown Neutron rules are still removed.

Repair also restores the agent management group's two required rules:
TCP 9443 ingress and UDP 5565 egress to the configured control group.
It repairs this stack's reciprocal control-side peer rules and removes
unexpected rules targeting this stack's peer group, preserving other
stacks and operator rules with other remotes. Management SSH ingress is
not permitted.
The CLI provides `openstack coast stack ingress rule list|create|delete`.
The dashboard exposes these rules on the stack Firewall tab; managed
rules are read-only. Live post-deploy acceptance is tracked separately.

## Server resize (1.26, issue 34 Part B)

Project members may grow their own VM workers, build servers and App engine
server with `POST /v1/stacks/{stack_id}/servers/{server_id}/action`:

```json
{"resize": {"flavor": "<operator flavor name or ID>"}}
```

Send `OpenStack-API-Version: app-hosting 1.26`. The policy is
`coast:server:resize` (project member or admin). The response is **202** with
`{"server": <show-server representation>}`, status `FAILOVER` and task
`FAILING_OVER` where visible. Infrastructure detail remains admin-only.
Poll the existing server show endpoint until `ACTIVE` with task `NONE`,
or `ERROR`. The server ID stays the same. Its stored `flavor_id` changes
only after replacement Nova ACTIVE, agent health and workload checks pass.
The App engine server also updates the stack `plan` in that success
transaction. Worker/build plans remain derived from their stored flavor;
no server plan column or new response field is introduced.

Flavor selection uses the create-time `flavor_to_plan` mapping and requires
one operator VM plan compatible with the server role. Plan names are also
accepted, as on create. A larger flavor must have no smaller vCPU count,
RAM, root disk or ephemeral disk and at least one strictly larger dimension.
Coast refuses incomparable sizes, equal sizes, shrinking and bare metal.
The error directs users to add a new, smaller worker and move applications.
No change to retained Cinder disk sizes is implied by flavor growth.

Both stack and selected servers must be `ACTIVE`, with no outstanding
stack/server/service/deployment work. Admission serializes with other
stack work. The accepted stack stays locked through completion, including
between workers in a rolling operation. Current Coast quotas count servers
and storage, not vCPU or RAM: resize has zero increase to reserve and keeps
those counts unchanged. Existing usage above operator quota limits returns
`403 OverQuota`; additional resource dimensions would require a separate
quota contract. Nova can still refuse replacement capacity asynchronously;
the operation then fails and attempts compensation.

Stack action `POST /v1/stacks/{stack_id}/action` accepts:

```json
{"resize_workers": {"flavor": "<operator flavor name or ID>"}}
```

It uses the same microversion and `coast:stack:resize_workers` policy
(project member or admin), returning **202** with
`{"stack": <show-stack representation>}`. Stack status stays `ACTIVE`;
its task is `FAILING_OVER` where visible. All workers must pass size and
state validation before admission. An empty worker set returns 400.
Workers run serially in UUID order. The next starts only after the previous
worker's guest, agent, engine registration and workloads are healthy.
Stop on the first failure; completed workers retain their new flavors and
unstarted workers stay unchanged. The stack activity event records
`completed_server_ids` and `failed_server_id`. It reports failure even when
some workers completed. There is no automatic retry of a partial rollout.

Both actions record start/success/failure in the existing stack timeline
and emit `coast.server.resize.{start,end,error}` or
`coast.environment.resize_workers.{start,end,error}` notifications.
The existing failover events also describe each replacement. The old VM
is fenced before volumes and retained ports move. Preserve all data/Docker
volumes (and registry volume for the App engine server), fixed addresses,
ports and floating IP. On failure delete the replacement and reattach to
and restart the old VM when safe. Never delete retained volumes or revert
completed workers. If compensation is uncertain, keep the old VM fenced
and report ERROR for operator recovery; never attach writable disks to two
running VMs. Flavor and plan remain unchanged on failed replacement.
A compensated failure still reports the server as `ERROR` with a fault and
releases its task, even when the original VM has restarted. Worker resize
leaves the stack `ACTIVE`; controller resize health rollup marks it
`DEGRADED` with a fault. These states allow operator recovery through the
existing admin server failover action; they do not imply lost retained data.

Errors (the existing JSON `faultstring`, `faultcode`, `debuginfo` envelope):

| HTTP | Meaning |
|---|---|
| 400 | Invalid/missing flavor or schema, unavailable/wrong-role plan, non-growing size, bare metal, empty worker set, or microversion below 1.26. |
| 401 | Missing authentication. |
| 403 | Policy refusal or `OverQuota`. Readers cannot resize. |
| 404 | Stack/server belongs to another project, is absent, or server is not in the requested stack. |
| 406 | Requested microversion is unsupported. |
| 409 | Stack/server not ACTIVE, outstanding task/deployment, or selection changed during admission. |
| 500 | Catalog/service failure or conductor handoff failure. |

Asynchronous failures are reported through the server status/fault and
stack activity, rather than changing an already returned 202 response.

## Customer server flavors (1.27, issue 34 Part A)

From 1.27, server GET list/show responses include `flavor` for project
readers and admins. It is an object with `id`, `name`, `vcpus`, `ram`
(MiB), and `disk` (GiB), resolved from the saved Nova flavor reference.
It is null for bare metal or a VM without a saved flavor. If Nova no
longer has that flavor, return `{"id": "<saved reference>", "name": null}`.
Nova failures return a sanitized 503. List resolution is cached per request
by flavor reference. `fields=flavor` is supported; flavor is not a filter
or sort key. Versions through 1.26 and all write responses are unchanged.
Admin infrastructure detail, including `flavor_id`, keeps its existing policy.

`GET /v1/flavors` requires 1.27 and `coast:flavor:get_all` (project reader
or admin). It returns 200 with `{"flavors": [...]}`. Each entry has `id`,
`name`, `vcpus`, `ram` (MiB), `disk` (GiB), `plan` (operator plan name),
and `roles` (a nonempty subset of `control`, `worker`, `build`). Only VM
flavors in the operator plan catalog are returned. Missing Nova flavors
are omitted. Roles with more than one plan for the same resolved flavor
are omitted; entries with no remaining roles are omitted. Entries are
sorted by plan name. An empty catalog returns an empty array. This is the
operator allowlist, not the service project's full Nova flavor inventory.

Choose an entry whose roles contain the intended server role. Use its
`plan` as `server.plan` on add-server, or as `resize.flavor` /
`resize_workers.flavor` on resize. Flavor names and IDs retain the existing
mapping rules. Resize still validates strictly larger dimensions, current
state and quota; this catalog does not guarantee Nova capacity.

No query parameters are accepted (400); other methods return 405.
Missing identity returns 401, wrong role or scope returns 403, versions
below 1.27 return 404, unsupported versions return 406, invalid operator
catalogs return sanitized 500, and Nova failures return sanitized 503.

### Stack deletion recovery (issue 53)

Stack DELETE returns 409 when any non-deleted app group remains. The
message includes the stack name, count and app group names; delete the
services, environments and app groups first. This applies to admins too,
without a force option, at every supported microversion. No RPC is cast
for this refusal. App group creation checks the stack under the same
lock, preventing a new group after deletion is claimed. The conductor
repeats the check before cloud teardown.

Failed teardown records ERROR, releases the DELETING task and retains a
masked failure reason in the existing `fault` field. Stack list and show
include `fault` for project readers. Service, environment and app group
DELETE accept an ERROR parent stack so cleanup can proceed. Other service
operations retain their ACTIVE parent requirement. Stack DELETE can then
be retried; already missing cloud resources are treated as removed.

Child deletion skips engine cleanup with a warning when the stack is ERROR.
On ACTIVE stacks, an unreachable engine also allows record deletion; other
engine failures still fail deletion. This includes app routing, application
and registry cleanup. DNS and backup cleanup still must succeed. Delete apps
before environments, then app groups, then the stack; cloud resources remain
until stack teardown. These recovery rules apply at every microversion.

Public stack faults and deletion failure events replace engine and routing
product names with generic wording and use plain hyphens. Callers authorized
by `coast:stack:get_detail` retain the masked diagnostic fault; operator logs
retain the masked exception message and traceback.

## Databases

Databases require API microversion 1.29 or later. Older microversions
return 404 for these routes. Unknown body
fields and query parameters return 400. Missing resources and resources
owned by another project return 404.

### Catalog and routes

`GET /v1/database_engines` returns `database_engines`. Each entry has
`engine`, `display_name`, `versions`, `default_version`, `port`, `backups`,
`has_username` and `has_database`. Read available engines and versions
there or with `openstack coast database engine list`. The catalog comes
only from `DATABASES` in `coast/common/engine_versions.py`. Reviewed image
tags remain internal.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/v1/databases` | List databases |
| POST | `/v1/databases` | Create; 202 |
| GET | `/v1/databases/{id}` | Show |
| PATCH | `/v1/databases/{id}` | Update mutable fields |
| DELETE | `/v1/databases/{id}` | Delete; 202, then 404 after cleanup |
| GET | `/v1/databases/{id}/credentials` | Read credentials; member only |
| POST | `/v1/databases/{id}/action` | Start, stop or restart; 202 |
| GET | `/v1/databases/{id}/logs` | Runtime logs |
| GET/PUT | `/v1/databases/{id}/backup_policy` | Read or set backup policy |
| GET/POST | `/v1/databases/{id}/backups` | List or create backups |

### Create, update and response

Create uses a `database` envelope. Required fields are `name`, `engine`,
`appgroup_id`, `environment_id` and `server_id`. The server must be a
worker in the app group's stack, as for service creation. Optional fields
are `version`, `description`, `database`, `username`, `cpu_limit`,
`memory_limit_mb` and `public`.

The version defaults to the catalog default. The database name and
username default to `app` where supported. Supplying either field when
its catalog capability is false returns 400. Coast generates passwords;
caller-supplied passwords are rejected. Some engines need a separate
root/admin password. Engine-specific user semantics come from the catalog
and the engine's documented authentication model.

`public` has `enabled` (default false), `port` (default null) and `source`
(default unrestricted IPv4). Use a restricted source for public access.
CPU and memory limits default to null. Description defaults to empty.
A database counts as one service for quota; exhaustion returns 403
`OverQuota`. A live service or database name conflict in the stack returns
409. Creation moves from `PENDING` through `CREATING` or `STARTING` to
`ACTIVE`, or to `ERROR`.

PATCH accepts only `name`, `description`, `cpu_limit`, `memory_limit_mb`
and `public`. Engine, version, placement and parents are read-only;
changes return 400. Limit changes are validated against worker capacity
and set `restart_required` until a successful start or restart. Limit-only
PATCH requests do not interrupt the running database. Public access changes
restart a running database to apply its published port.

The list, show and create database object contains `id`, `project_id`,
`stack_id`, `appgroup_id`, `environment_id`, `server_id`, `name`,
`description`, `engine`, `version`, `status`, `fault`, `cpu_limit`,
`memory_limit_mb`, `port`, `internal_host`, `internal_url`, `public`,
`public_url`, `task_owner`, `task_started_at`, `created_at`, `updated_at`
and `links`, plus `restart_required`. The response `public` object contains `enabled`, `port`,
`source`, `host` and `private_host`. Ordinary URLs contain no password.
Internal engine resource identifiers and secret references are never
returned. `internal_host` names the database on its worker's container
network.

List filters are `stack_id`, `appgroup_id`, `environment_id`, `server_id`,
`engine` and `status`. Common controls are `fields`, `limit`, `marker`,
`sort_key` and `sort_dir`. Admins may use `all_projects` and `project_id`.

### Credentials, actions and policy

Credentials use a `credentials` envelope with `username`, `password`,
`database`, `internal_url` and `public_url`. `root_password` is present
only for engines that require a separate root/admin password. These URLs
include passwords. The response sets `Cache-Control: no-store`.
Credentials never enter logs or notifications.

Actions accept exactly `{"start": {}}`, `{"stop": {}}` or
`{"restart": {}}`. Start requires `STOPPED`; stop and restart require
`ACTIVE`; other states and busy tasks return 409. Busy errors say:
"Wait for the current operation on this database to finish, then try again."
Conflict messages describe what the user can do without exposing internal
task names or owners. Start/restart report `STARTING` until stable and
healthy, then `ACTIVE`, or `ERROR` after the configured timeout.
Logs accept `tail` and `since` and use the service log shape.

Policies are `coast:database:get_all`, `get_one`, `create`, `update`,
`delete`, `start`, `stop`, `restart`, `get_logs` and `get_credentials`
with the same prefix. Project readers may read metadata and logs.
`coast:database:get_credentials` requires project member access, as do
writes and actions.

Backup routes require catalog `backups: true`. Policy requests need no
`engine` or `container`; Coast derives both. Existing `/v1/backups/{id}`
show, delete, download and restore routes remain unchanged. A `new_copy`
restore creates a database with the same engine, version and placement.
See [backup semantics](contributor/specs/database-backups.md).

Databases use their own routes. `/v1/services` excludes database rows;
`?kind=database` there returns 400. A database ID passed to
`/v1/services/{id}` returns 404.

Database responses at microversion 1.29 include read-only `fault` (string or
null). Database tasks
that end in ERROR store a fixed, customer-safe reason for unsupported stack
endpoints, an unreachable control server, worker capacity conflicts, or the
failed operation. Exception details remain in conductor logs. The fault
clears when a new action starts or the database reaches ACTIVE or STOPPED.
POST and PATCH reject caller-supplied `fault` as an unknown field (400).

### Public access

Public access (#123) defaults off. Create/PATCH `public` accepts `enabled`,
`port` (1024-65535, null allocates from `[databases] public_port_min/max`,
default 15432-15999), and IPv4 `source` (default `0.0.0.0/0`). PATCH merges
omitted members. Reserved agent/app engine ports return 400; collisions
with another enabled database in the stack or custom/required ingress
return 409. Disabled settings retain ports without reserving them.
Allocation and quota admission serialize under project and stack locks.
Database managed rules count toward the per-stack ingress limit (403).
They have stable IDs `managed-database-<uuid>` and description `Database
<name>`. Direct DELETE returns 409 naming the database. Rules derive from
live desired state; disabling, deleting and ERROR remove desired exposure.
The current shared worker firewall applies the rule stack-wide. The
conductor applies firewall changes synchronously. Failed updates retain
durable cleanup work retried independently of optional network repair. Public host is the worker floating IP,
private_host is its fixed IP; public_url prefers the floating IP, falling
back to the fixed IP. Choose the stack subnet source for stack-only access.
Public setting changes use the existing restart and STARTING readiness flow.

Database backup policy GET/PUT and backup list/create use
`/v1/databases/{id}/backup_policy` and `/v1/databases/{id}/backups` at 1.29.
Only catalog engines with `backups=true` accept these operations; others
return 409, `Backups are not available for this engine.` Policy engine and
container are derived and cannot be supplied. Scheduling, retention and
manual backup behavior match service backups. Existing backup show, delete,
download and restore routes also require 1.29 for database backups.
New-copy restore creates a database with the original engine, version and
placement, fresh credentials and public access disabled. Overwrite retains
typed confirmation and safety-backup admission. See the
[backup specification](contributor/specs/database-backups.md).

## Repository-free app sources (1.31)

POST services accepts `source: {"type": "dockerfile", "dockerfile": "<text>"}`
(app kind only) and `source: {"type": "compose", "compose": "<text>"}`
(service kind by default, app accepted). Responses expose only source type.
Compose uses the existing compose editor and container routes. Dockerfiles
use GET/PUT `/v1/services/{id}/dockerfile`, with the envelope
`{"dockerfile": {"text": "FROM ..."}}`. Both routes require project member
or admin access, set `Cache-Control: no-store`, and return 404 for other
projects or older microversions. PUT returns 200 with
`X-Coast-Redeploy-Required: true` and never deploys. The Dockerfile is fixed
when the app is created (including with `--no-deploy`): PUT saves text, but
on redeploy unchanged Dockerfile text is skipped and the image is reused;
a deploy with changed text fails before it starts. Create a new app from
the new Dockerfile, or use a Git repository source, whose Dockerfile
changes deploy normally. Content is nonempty
UTF-8, capped by the smaller of the compose and secret limits, and stored
only in Barbican. Deployments and rollbacks use frozen content references.
