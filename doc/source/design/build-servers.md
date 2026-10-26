# Multiple build servers per stack (Q13)

## Status

Q108 option 2 is implemented in M3 c4bq. Other gaps remain separate work.
Bare metal is out of scope / not planned (James 2026-10-13, Q6).

Q13 is decided. Build servers are optional, use a separate quota, and a
stack may have more than one. Coolify selects one usable build server at
random per build. Coast does not select the builder.
Source: [Build Servers](https://coolify.io/docs/core/infrastructure/servers/build-servers)
(checked 2026-10-13). The pinned contract notes record the same rule.

## What already exists

- `BuildServersTest` (`coast/tests/contract/coolify/test_live.py`) uses two
  or more Coolify servers with `server_role=build`, creates a public git
  app with `use_build_server=true`, and checks registry push and pull.
  See `coast/tests/contract/coolify/README.rst` and `driver-interface.md`.
- `register_server` maps `role=build` to `server_role=build` and
  `proxy_type=none`. A worker is `deployment` only when a usable build
  server already exists. Otherwise it is `both` (Q62). Do not change that.
- `create_app` sets `use_build_server=true` on a new Git (excluding Compose)
  or inline Dockerfile app when any server has `server_role=build`, plus
  `docker_registry_image_name` from the per-stack registry template.
  `[coolify] build_registry_image_template` is the fallback when that
  template is empty. Image services do not use a
  build server. A build server is not a placement target. Bootstrap sets
  `is_build_server_fallback_enabled` to false.
- `POST /v1/stacks` boolean `build_server` (default false) creates one VM.
  `POST /v1/stacks/{stack_id}/servers` with `role=build` adds one more.
  At API 1.1, `build_server_plan` selects a VM build plan; when omitted,
  exactly one configured VM build plan is required. `prepare_requests` runs
  that VM before workers through `AddServer`.
- Add steps: network, data volume, Docker volume, boot, active, health,
  floating IP, register, complete. A build server uses the build security
  group, has no floating IP, and syncs registry rules (`has_build_servers`).
- Remove keeps data volumes, deletes the Docker volume, and releases
  `build_servers` and `servers`. Add revert deletes the new guest in
  reverse. Uncertain register keeps the guest. The last build guest clears
  registry rules. Another build row with `compute_id`, including unhealthy,
  keeps those rules.
- Quota keys `build_servers` (default 1) and `servers` (default 2) exist.
  A build server consumes both. No new column is required.

## Gaps

| # | Gap | Impact |
|---|---|---|
| 1 | Stack create accepts only zero or one build server. | A second server needs a later POST and a higher `build_servers` limit. |
| 2 | RESOLVED: Q108 switches existing workers and git services. | First usable add and last removal update engine settings. |
| 3 | Unit tests do not run two `AddServer` build VMs. | A quota or security-group regression on the second VM can pass CI. |
| 4 | RESOLVED: Q125 creates the per-stack registry on the controller. | Stack create stores credentials, attaches the registry volume, and sets the image template. |

## API and data model

No new table and no new quota key. `servers.role=build` stays.
Do not store the Coolify builder choice.

**Q107 RESOLVED (James 2026-10-13 01:16 CT): option 1.** How does stack
create request more than one build server?

1. Keep the boolean. Add more with `POST /servers` and `role=build`.
2. Add an integer count on stack create.
3. Add a build-server list with the same shape as `workers`.

James chose option 1: keep the boolean (zero or one build server on
create); add or remove more with `POST`/`DELETE /servers`. Supported
create shapes are (a) Coolify VM + 1 worker and (b) Coolify VM + 1
worker + 1 build server, each role with its own customer-selectable
flavor (plan) and image. No all-in-one Coolify VM + worker.

**Q108 RESOLVED (James 2026-10-13 01:18 CT): option 2.** When the first
usable build server appears, or the last one is removed, does Coast update existing engine rows?

1. Leave existing workers and services unchanged.
2. Patch workers from `both` to `deployment`, set `use_build_server=true`
   on existing git services, and reverse both when the last build server
   is gone.
3. Patch services only. Do not change worker `server_role` after register.

James chose option 2. A failed patch keeps the guest and the previous
engine settings.

**PM-decided (2026-10-13):** add one driver method,
`set_build_server_mode(endpoint, stack, workers, services, enabled)`.
Implement it in noop and Coolify with public APIs and fixture tests.
First-add switches after successful registration and validation, before
completion. That call logs each registered worker into the per-stack
registry before the patches. A worker that registered with no build
server did not log in. Login failure does not change worker roles or
service settings. Removal does not log out. Last-remove switches
**before** engine deregistration and Nova deletion. If deregistration
fails, reverse the switch while the engine registration checkpoint
remains. After successful deregistration, keep local builds enabled
even if guest deletion or later cleanup fails.

The method reads all previous settings before writes. It uses absolute
PATCH values and confirms each write with GET. On failure, compensate all
attempted writes, including the patch whose reply failed. Compensation
also uses GET confirmation. If compensation cannot be confirmed, report
operator recovery through the existing sanitized failure path. There is
no public API transaction across engine rows; do not claim atomicity.
Keep the guest and its ports, volumes and engine reference on add patch
failure. Existing failed-create quota release remains unchanged.

Only registered Coast workers and Git or inline Dockerfile apps from the
same project and stack are targets. Image, Compose and template services
are not targets. If an app has no registry image name, use the per-stack
image template when the endpoint has one, otherwise
`[coolify] build_registry_image_template`. Fail before writes if that
value is missing or invalid. Compensation
clears a previously unset registry name with the public API's empty string.
Disabling dedicated builds leaves an existing registry image name intact.
Coast roles and source rows do not change; there is no new DB column.

**PM-decided (2026-10-13; FYI James):** add exactly
`PATCH /api/v1/servers/{uuid}` to the guest agent proxy allow-list, with
the existing UUID path pattern and full path match. No other method or
path is added. mTLS, header filtering and response handling are unchanged.
Route tests check denied sibling paths and methods.

The controller agent proxy permits this PATCH through its documented
allow-list. Mode switching and compensation use that same route.

Public API sources checked 2026-10-13:
[server update](https://coolify.io/docs/api/endpoints/servers/update-server-by-uuid)
and [application update](https://coolify.io/docs/api/endpoints/applications/update-application-by-uuid).
The pinned fixtures observe application `use_build_server` through
`settings.is_build_server_enabled`, not a top-level response field.

## Conductor, quota, and revert

Repeated add uses `AddServer`. The stack holds one task. Wait until
`is_reachable` and `is_usable` are true. Do not set a proxy or a floating
IP on a build server. Order stays: controller bootstrap, build VMs, then
workers, so worker registration can see a usable build server (Q62).

One add reserves `servers=1` and `build_servers=1`, plus data and Docker
`volume_gb` before Cinder create. Success commits. Failed add releases
those holds. Remove releases the server keys after Nova delete and storage
cleanup. Default `build_servers` is 1, so the second server returns 403
until an admin raises the limit. Do not change that default.
`servers` default 2 counts workers and build servers, not the controller.

Revert is per step, in reverse. Do not recreate a removed guest. Keep the
guest if deregister fails. Keep ports and volumes if Nova delete fails.
Registry rules follow `other_builds()`.

## Tests

- Second `role=build` add: build security group, no floating IP, registry
  rules stay on, `in_use_build_servers=2`.
- Limit 1 rejects the second reservation with 403 and creates no row.
- Remove one of two build servers and keep the rules. Remove the last and
  clear them.
- Second `register_server` sends `server_role=build`. A worker registered
  after a usable build server sends `deployment`.
- Q108: first usable add, second usable add no-op, last remove before
  deregistration, and non-last remove no-op (including an unhealthy peer).
- Q108: patch failure keeps references and guest; last-remove retry is
  safe; deregistration failure reverses mode; Nova failure does not reverse
  after deregistration. Fixtures cover compensation after lost replies.

## Implementation MRs

1. Unit tests for a second build VM, quota rejection, and security-group
   revert. No API change.
2. After Q107 only: stack-create count or list, quota delta, and serial
   `AddServer` children before workers. Skipped: Q107 keeps the boolean. Per-role flavor/image
   selection on create is tracked separately.
3. Q108 implemented (M3 c4bq): one helper patches worker role and
   `use_build_server` after build add validation and before last removal.
   An uncertain patch fails closed. See the PM-decided order above.
4. Update `api.md` and the api-ref only if Q107 adds a field. Leave the
   M3 roadmap item open until those MRs merge.
