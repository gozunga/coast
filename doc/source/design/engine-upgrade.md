---
orphan: true
---

# M4 engine upgrade and controller failover

Engine releases and defaults come from `coast/common/engine_versions.py`.
Upstream source paths below refer to that release’s tag; dated evidence
refers to the recorded supported release, not the new-stack default.

Implementation contract. Two M4 roadmap items:
the customer/admin engine-upgrade action (no canary rollout), and controller
replacement that preserves every service. Worker failover, the
five-minute worker acceptance, reconcile, and Q105 stay out. Q105 stays
open. The upgrade action uses the controller rebuild primitive. Controller
failover (M4 g4cf) uses that primitive on the recorded image. The real
driver path does not call `register_server`. Noop admin completion is
unchanged.

## Decision (James 2026-10-13, RESOLVED)

Coolify engine upgrades rebuild the Coolify VM from the new golden image
and reattach all three volumes, the same ports and the floating IP, reusing the
failover rebuild path. Coolify's own updater stays off (`AUTOUPDATE=false`,
no `upgrade.sh`). Rollback rebuilds on the previous image, with the
pre-upgrade snapshots. Q120, Q122 and Q123 below are derived from this
decision.

James 2026-10-13 (8:22 CT, RESOLVED): upgrades are customer-initiated
(admins can also trigger them) by picking a newer published golden image
for the Coolify VM role; Coast rebuilds onto it. There is no staged canary
rollout, so Q121 needs no canary marking.

## Issue 24 correction (James, 2026-10-19)

This section supersedes controller-only upgrade wording below. Upgrade is
one stack-wide rolling replacement: controller first, then worker and build
VMs in stable role/UUID order. Each server must finish before the next
starts. Stop at the first failure, release operation tasks, retain recovery
references and set the stack ERROR after a partial rollout. Do not undo a
completed controller replacement when a later worker fails.

A target with the same engine version is eligible when its UUID differs
and its Glance `created_at` is later than the recorded controller image.
Reject missing or invalid creation times and engine downgrades. Keep the
published owner, tag, role and supported-version checks. Glance creation
time is the publication order; no new build metadata is required.
See [Image.created_at](https://docs.openstack.org/openstacksdk/2026.1/user/resources/image/v2/image.html#openstack.image.v2.image.Image.created_at).

Workers and build servers use the configured published worker image,
whose engine version must match the controller target. Refuse an ambiguous
worker catalog before snapshots or cloud changes. Retain their data and
Docker volumes, ports, engine references and workload state. The existing
snapshot rollback action restores the controller only. It does not undo
completed worker replacements; the operator must inspect a partial rollout.
This is a correction to the unreleased action, without a new microversion.

Admin worker/build failover accepts the API's FAILOVER/FAILING_OVER claim
without a health grace wait. Automatic failover still requires the expired
health episode. Both use the real server replacement flow. Controller
admission and its separate replacement flow stay in place.

## Contract

Sources: [lifecycle](../lifecycle.md) (Health and failover, version pinning, guest replace, Coolify version upgrade) and [driver interface](../driver-interface.md).

| Item | Rule |
|---|---|
| Upgrade API | `POST /v1/stacks/{id}/action` `{"upgrade":{"image_id":"<published Coolify VM image>"}}`, `coast:stack:upgrade` (stack owner or admin), 202. Stack ACTIVE, task `NONE`, no PENDING, BUILDING, or DEPLOYING deployment. |
| Failover API | `POST /v1/stacks/{id}/servers/{id}/action` `{"failover":{}}`, `coast:server:failover`, 202. Server ACTIVE or UNHEALTHY. |
| Pin | `SUPPORTED_ENGINE_VERSIONS` is an exact list. `[coolify] pinned_version` is one version. `AUTOUPDATE=false`. |
| Lock | `coolify-images.lock`, or `COAST_COOLIFY_IMAGES_LOCK` for another reviewed lock. First boot loads digests with `--pull never`. |
| Offline | Do not run `upgrade.sh`. It fetches CDN files and pulls images (Q78). |
| Replace | Fence the old VM, then move all three Cinder volumes and the same ports. Do not bootstrap again. Never delete the only data copy. |

An image-only replace does not change `stacks.engine_version`. A version
change snapshots first, starts N+1 so it can migrate, checks
`get_engine_version`, then writes the version. Notifications use the
Q47 names `coast.environment.upgrade.start`, `.end`, and `.error`.

Before publishing an N+1 image, land N+1 with the existing release steps (OpenAPI diff,
contract tests, allow-list, controller Glance image). Keep PostgreSQL
15. Stock `upgrade.sh` downloads from the CDN
([script](https://github.com/coollabsio/coolify/blob/v4.x/scripts/upgrade.sh),
checked 2026-10-13). The pinned release copy is staged and not executed.

## N to N+1, services stay up

Replace only the controller. Worker and build containers stay up. A short control-plane gap is expected. Services that were running are running afterwards.

Pre-checks fail closed before any cloud change:

1. Stack owner or admin caller. The body names the target image, which
   must be in the published set for the Coolify VM role and newer than the
   stack's current image (Q120).
2. Conductor repeats the API rules above.
3. Driver `coolify_v4`, `engine_version` is N, and N+1 is on the allow-list.
4. One live `role=control`, `kind=vm` server is ACTIVE.
5. The catalog has one active `control` image for N+1. Store that id only after success.
6. That server has `purpose=data` (`/data`) and `purpose=docker` (`/var/lib/docker`).

The API sets UPGRADING before the cast. If a conductor check fails
before a snapshot or boot, return ACTIVE and task NONE, store
`Engine upgrade refused.`, and send `upgrade.error`.

### Coolify DB snapshot

PostgreSQL data is Docker volume `coolify-db` on the controller docker
disk. `.env`, `APP_KEY`, and SSH keys are on `/data`. Snapshot data, Docker, and registry
controller volumes. Snapshot no worker volume.

1. Stop only controller containers `coolify` and `coolify-db`.
2. Snapshot all three volumes as `coast-upgrade-<stack-id>-data` and
   `coast-upgrade-<stack-id>-docker`. Add `coast-upgrade-<stack-id>-registry`. Wait until all are available.
   Log ids only. No new table or public field; private stack columns hold references. Duplicate names are an error.
   Use the service project and the
   [Block Storage v3 proxy](https://docs.openstack.org/openstacksdk/2026.1/user/proxies/block_storage_v3.html).
3. Leave those containers stopped. The new controller starts them.
4. On snapshot failure, start the old containers, return ACTIVE, and
   stop. Do not delete a partial snapshot until an operator says it is unused.

### Apply, verify, roll back

Boot the N+1 image. Fence the old controller before either guest mounts
the volumes. Move all three volumes and the same ports, ids, addresses, and
floating IP. Put agent identity on the config drive, not credentials or
the token. Load the locked archive with `--pull never` and start
Compose. The new process migrates its database. Keep `AUTOUPDATE=false`.

Wait up to `[conductor] deploy_timeout` (default 30 minutes) for agent
health and `get_engine_version` == N+1. Read each worker with
`get_server_status`. Redeploy `current_deployment_id` only when that
container is missing. Then set `engine_version` to N+1, ACTIVE, and
task NONE.

If the new engine never starts, stop the new VM and move the volumes
back when the old VM can boot. Do not restore snapshots on that path.
If the new engine has started, do not move volumes back. Set ERROR and
`Engine upgrade failed.` Wait for owner/admin rollback (Q123/Q133): an owner/admin API stack action that rebuilds
on the previous image through the same rebuild path. Rollback
restores all three recorded snapshots, boots the N image, and writes version N
only after `get_engine_version` is N.

### No canary rollout

James 2026-10-13: no staged canary rollout and no `coast-manage upgrade`
batch script. Each upgrade is one stack, started by its owner or an admin
through the API action above. Operators publish the N+1 golden image when it
is ready; customers choose when to move.

## Controller failover

Use the current image, not N+1. Do not migrate, do not run `upgrade.sh`,
and do not change `engine_version`. Preserve Coolify projects, services,
domains, and env rows.

When `[health_manager] enable_failover` is true (the default), cast
`failover_server` after `failover_grace` (default 120 seconds) past the
stale controller. Skip a stack task other than NONE, a server that is
not `kind=vm` and `role=control`, and a disabled flag. Bare metal: set
ERROR and do not rebuild. The admin action uses this flow with no grace
wait.

Boot `servers.image_id` when that image is active and is version N
(Q122: use the stack's recorded image; refuse failover if it is missing). Fence the old VM, then move all three volumes and the same ports.
The image starts `docker.socket` after the Cinder mounts and does not
mask it. `docker.service` requires the socket. The config-drive bootcmd
does not stop Docker. Coolify opens the existing database and the stored
API token. Do not bootstrap and do not register the
controller as a deployment server. Remove the stub `register_server`
call. After agent health,
`get_engine_version` must equal the stored version and worker rows must
match `get_server_status`. Redeploy a service only when its container
is missing. Set the server ACTIVE. Set the stack ACTIVE only when every
live server is ACTIVE. Keep DEGRADED when another server is down.

On failure, return resources to the old VM when it can boot. Otherwise keep the volumes and set ERROR. Never delete the only data copy. Acceptance: each service that was running is running, with the same engine ref, and the tenant does nothing.

M4 g4cf implements this section. Automatic failover waits for
`failover_grace`. The admin action does not. A missing or wrong-version
image leaves an automatic attempt UNHEALTHY and records one operator
event. The admin action returns 409 before it changes the server. After
the API has set FAILOVER, a pre-fence refusal returns ACTIVE so the
health manager can mark the server UNHEALTHY again. A failure after the
fence sets ERROR. Worker, build and controller claims share
`failover_max_concurrent`. A missing template container fails closed.
`get_server_status` must equal the stored worker or build status. The
stack returns to ACTIVE only through the heartbeat rollup.

## Ordered MRs

| MR | Change | Done when |
|---|---|---|
| 1 | Conductor upgrade checks. Do not write `engine_version`. | A refusal returns ACTIVE and does not call Nova. |
| 2 | Quiesce the controller database and snapshot all three controller volumes. | A failed snapshot starts the old containers and returns ACTIVE. |
| 3 | N+1 replace, version and service checks, then write `engine_version`. | A test writes N+1 only after the version check. No snapshot restore after the new engine starts. |
| 4 | Owner/admin `image_id` selection from the published Coolify VM images and rollback action. | Only a published, newer Coolify VM image is accepted; rollback rebuilds on the previous image. |
| 5 | Controller failover for the health-manager cast and the admin action. | Services and `engine_version` stay. One controller owns the volumes. |

## Open questions

Q120. RESOLVED (James 2026-10-13, revised 8:22 CT). The target is the image the customer or admin picks from the published golden images for the Coolify VM role, not the automatic newest.
Q121. RESOLVED (James 2026-10-13). No staged canary rollout, so no canary marking is needed.
Q122. RESOLVED (derived from James 2026-10-13). Failover rebuilds on the stack's recorded image; if that image is missing, refuse and keep the server UNHEALTHY with an operator event. Implemented by M4 g4cf.
Q123. RESOLVED (derived from James 2026-10-13). Rollback is a rebuild on the previous image with the pre-upgrade snapshots. Q133 extends the action to owners and admins.

## Q130-Q133 implementation contract (James, 2026-10-13 11:27 CT)

* Q130: snapshot and restore `data`, `docker`, and `registry`. Snapshot
  no worker volume. Rollback creates new Cinder volumes from the snapshots;
  it retains the displaced volumes for operator recovery. It never
  overwrites the only copy. Retained recovery resources are billable.
* Q131: controller-only mTLS `POST /v1/engine/quiesce` accepts
  `{"export_id":"<operation UUID>","volume_ids":["<Coast volume UUID>"]}`.
  `POST /v1/engine/resume` accepts `{"export_id":"<operation UUID>"}`.
  These use the export lease format, watchdog, and
  `[agent] export_auto_resume_timeout` (default 900 seconds), but stop only
  running containers named `coolify` and `coolify-db`. Registry and proxy
  remain running. No engine database access or `docker exec` is permitted.
  Snapshot waits must finish before the lease expires. Fence the old VM
  immediately afterwards. A failed snapshot resumes the old containers.
* Q132: private nullable stack columns `previous_image_id` (String(36)),
  `previous_engine_version` (String(64)), `upgrade_snapshots` (JSON), and
  `upgrade_checkpoint` (JSON). Snapshots map purpose to Cinder UUID.
  The checkpoint stores the operation UUID, selected image, previous image
  and version, pending snapshots, and replacement/restoration references.
  Write snapshot and clone references before waiting. Preserve the previous
  successful rollback point until the new upgrade succeeds. A failed
  upgrade uses its checkpoint for rollback. Delete superseded snapshots
  only after success; cleanup failures retain them for operator inspection.
  Use operation-suffixed snapshot names when a previous rollback point
  exists. Do not expire the current rollback point automatically.
* Q133: `POST /v1/stacks/{id}/action` accepts exactly one action:
  `{"upgrade":{"image_id":"<published controller image UUID>"}}` or
  `{"upgrade_rollback":{}}`. Both return 202 with the stack representation,
  use project-scoped member-or-admin policy, and require task NONE.
  Upgrade requires ACTIVE. Rollback requires ACTIVE or ERROR and a complete
  recorded rollback point. Rollback uses status/task ROLLING_BACK and
  finishes ACTIVE or ERROR. It uses the recorded previous image, not an
  image from the request. No new microversion is required.

Admission and conductor checks bound the stack to 100 services and reject
any busy service or pending/building/deploying deployment. Project then
stack locks serialize admission. A receipt and conductor ownership CAS
prevent duplicate casts from starting a second rebuild. Each phase uses
short DB transactions; cloud calls never hold DB locks. On conductor restart,
fail closed, release child locks, retain checkpoint and storage references,
and emit the operation error event. Operator inspection precedes rollback
when a cloud create reply was lost.

Once a replacement boot is attempted, it may already have migrated the
retained Docker database. An uncertain boot outcome therefore fails closed
without attaching those disks to the old image. Before boot, compensated
failures can restore the old guest. Rollback always restores snapshots.
The replacement config drive contains the existing agent/registry identity
material required by provisioning, not an engine token or bootstrap request.
The first-boot image contract preserves `.env` and starts locked Compose
with `--pull never`. No new engine version is allow-listed by this change.

Events are `coast.environment.upgrade.{start,end,error}` and
`coast.environment.upgrade_rollback.{start,end,error}`. Controller status
changes also emit `coast.server.update.{start,end,error}` after commit. Write the image and
version only after agent health, exact engine version, worker status, and
service checks succeed. Controller failover (M4 g4cf) reuses this rebuild
primitive and the worker fence, move and revert path. It does not change
`engine_version`.

Rollback clone names use the resource-name helper and operation UUID, so
repeated restores cannot exceed the cloud name length limit. Upgrade
snapshots are consistent for the stopped engine/database; registry storage
is crash-consistent because the registry keeps running during the lease.
Stack teardown removes all snapshots and displaced rollback volumes whose
metadata verifies Coast ownership, project and stack, after fencing the
stack guests. Explicit delete-retention snapshots keep their retention
policy. Unowned snapshots still block volume deletion. No cleanup may remove a
rollback snapshot merely because a VM operation failed.

A successful rollback does not replace an existing last-successful-upgrade
rollback point. If no point exists yet, it promotes the restored pending
point. Additional snapshots from a failed upgrade remain referenced in
`upgrade_checkpoint.retained_snapshot_ids`; admission preserves that list.
Only a later successful upgrade removes these superseded references and
starts snapshot cleanup. Partial or uncertain snapshot creates still require
operator inspection; success never authorizes deleting an unknown data copy.

On controller first boot, preserve every retained identity value, but refresh
`LATEST_IMAGE` to the golden image's compiled engine version and enforce
`AUTOUPDATE=false`. The unchanged stock
`docker-compose.prod.yml` at the selected upstream tag
uses `LATEST_IMAGE` to select the engine tag (checked 2026-10-13). A retained
N value must not select N after booting an N+1 image. No installer or updater
script is executed, and no engine database is read or modified by Coast.

## Issue 34 Part B: resize

API 1.26 reuses worker/build and controller failover with a target flavor,
without changing the image or engine version. Admission and rolling behavior
are specified in [Server resize](../api.md#server-resize-126-issue-34-part-b).
Store flavor (and the controller stack plan) only in successful completion.
Use existing taskflow reverts to restore the old VM and its retained disks
and ports on failure. A rolling worker resize stops on its first failure
and preserves already completed replacements. No snapshot, disk resizing,
new table, client or dashboard change is part of this operation.

The approved driver interface has no drain or workload-move method. Planned
resize uses the existing Nova stop-and-wait fence before detaching storage,
then recovers workloads from the retained disks. It does not move applications
to another worker or introduce an engine-private drain API.
