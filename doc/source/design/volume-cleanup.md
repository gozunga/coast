# SERVER-8: retained data-volume cleanup

## Status and scope

James approved the real-cloud cleanup flow and the safety checks below
on 2026-10-11 at 10:19 CT. The document status is **APPROVED**, except
decision 5, which stays **DEFERRED**. The flow is implemented in
`coast/conductor/volume_delete.py` (roadmap SERVER-8). Decision 5, export
of detached retained data, is not implemented. The existing API is the
contract.

A tenant uses Coast to delete a retained `purpose=data` volume after
worker or build server removal. Coast owns its Cinder volume in the
service project. The tenant must not need service-project credentials.
Deletion is permanent; the tenant must save any required data first.

Non-Docker does not mean tenant-deletable: `scratch` and `controller`
remain Coast-managed. This design does not add transfer, force-delete,
attachment changes, a retention option, or a new API route.

## Existing removal behavior

The merged `coast/conductor/server_remove.py` (`RemoveServer.storage`),
introduced in commit `5580c32` ("Add M3 stack server provisioning and
removal flows"), is the starting point. See also
[server add/remove](../lifecycle.md#server-add-worker--build).

- Removal holds the stack task and refuses placed services or volume mounts.
- It deregisters the server and confirms Nova deletion before storage work.
- For each Docker volume, it deletes Cinder storage, releases accounted
  `volume_gb`, removes the Coast row, and records completion.
- For each other volume with a Cinder reference, it waits for availability.
  It clears `server_id`; `IN_USE` becomes `AVAILABLE`. Other statuses remain
  unchanged. It keeps the volume row, Cinder reference, and volume quota.
- Server completion releases server quota and soft-deletes the server row.
  Failed steps keep unfinished references. Uncertain interrupted work needs
  operator inspection; there is no automatic takeover.

Thus server removal is not consent to delete retained data. A cleared
`server_id` is not proof that Cinder has no attachment. Cleanup must check
both Coast and Cinder again.

## API and policy

Use the existing [volume API](../api.md#volumes-decisions-q24-q25-q26):

```text
GET /v1/stacks/{stack_id}/volumes
DELETE /v1/stacks/{stack_id}/volumes/{volume_id}
202 {"volume": {"id": "...", "status": "DELETING", ...}}
```

The DELETE has no force or retention options. A tenant lists the stack's
volumes, selects the retained data volume, and submits DELETE. The tenant
can GET the volume while cleanup runs. Successful row removal makes GET
return 404. Do not introduce a `DELETED` volume status.

Existing rules and responses remain:

| Condition | Rule or response |
|---|---|
| List/show | `coast:volume:get_all` / `get_one`: project reader or admin |
| Delete | `coast:volume:delete`: project manager or admin |
| Cinder reference disclosure | `coast:volume:get_detail`: admin only |
| Missing authentication | 401 |
| Visible resource, insufficient role | 403 |
| Missing or foreign-project resource | 404, not a cross-project 403 |
| Docker, scratch, or controller purpose | 400, including for admins |
| Repeated DELETE while `DELETING` | 409; no second operation |
| Invalid forward status edge | 409 |

Policies have `scope_types=['project']`; keep secure RBAC defaults.
Resolve the stack and volume with the request context's `project_id`, and
verify the volume belongs to that stack. Repeat ownership checks in the
conductor. Admin policy exceptions do not permit deletion by an arbitrary
Cinder ID. No new policy rule is proposed.

**APPROVED safety gate:** refuse with 409 if `server_id` is set, mounts
exist, or a competing stack task, volume extend, snapshot, export, or
attachment operation exists. Reject before status acceptance where known.
This narrows currently allowed delete edges. James approved this gate
(decision 1).
A late Cinder attachment found after acceptance fails cleanup safely.

## States and operation ownership

Existing volume states are `CREATING`, `AVAILABLE`, `IN_USE`,
`SNAPSHOTTING`, `ERROR`, and `DELETING`. Volumes have no `task_state`.
The API uses `check_transition` and status compare-and-swap (CAS), not a
same-state no-op. Existing edges permit deletion from each non-DELETING
state. Do not silently change that state table.

The approved retained-data path normally accepts `AVAILABLE -> DELETING`.
`ERROR -> DELETING` permits cleanup retry after checks. `IN_USE` with a
cleared server association still needs Cinder verification; do not trust
that status as proof of attachment. The approved path blocks active creation
and snapshots despite their existing delete edges. Success hard-deletes
the row. Failure uses `DELETING -> ERROR` through
`coast/common/states.py`, preserves references and quota, and emits error.

**APPROVED:** acquire the stack's `RECONCILING` task together with volume
status CAS in the acceptance transaction. Use the project quota sentinel
before stack and volume locks, as required by [quotas](../api.md#quotas).
All competing paths must use the same lock order and check operation
ownership. A volume status lock alone cannot serialize PATCH: current
extend does not use it. Do not hold DB transactions across cloud calls.

## Approved conductor flow and checkpoints

Store a durable operation receipt before RPC dispatch. It must bind the
project, stack, volume, accepted revision, Cinder reference, quota amount,
and completed steps. Reuse the existing receipt machinery if it survives
row deletion (decision 3). If it does not survive, bring a small schema
design back to James before you add any table. Reject duplicate or stale
receipts. Use resumable
Taskflow steps; a checkpoint follows confirmed results, not just requests.

| Step | Action and durable checkpoint |
|---|---|
| 1. Accept | Check policy, ownership, purpose and competing work. Acquire task and CAS to `DELETING`. Save receipt and start event with dispatch intent atomically. |
| 2. Verify detached | Reload volume and mounts. GET the exact Cinder ID; verify Coast ownership metadata, no attachments, and deletable state. Record detached verification. |
| 3. Check dependencies | Scan snapshots, clone references and export receipts, including failed cleanup. Reject unresolved dependencies. Save verified dependency set. |
| 4. Delete Cinder volume | Save delete intent first. Request ordinary Cinder deletion, with force and cascade disabled. Record acknowledgement; do not release quota yet. |
| 5. Wait gone | Poll the exact reference until an authoritative Cinder 404. Keep quota on timeout, backend error, or uncertain response. Save absence confirmation. |
| 6. Complete accounting | Under project and operation locks, CAS the receipt to completed, release accounted `volume_gb` once, hard-delete the volume row, and save end-event intent in one transaction. |
| 7. Publish | Publish `coast.volume.delete.end` and CADF audit through the existing notification mechanism. Release only this operation's stack task. Retain a completion receipt for replay. |

Cinder 404 is success only for a previously validated, owned reference.
If the DB reference is absent, use the existing tagged ownership lookup;
record the result before deletion. Do not infer absence from a timeout,
403, an incomplete list, or a name match. Multiple matches fail closed.
A truly never-created resource needs a known create receipt to distinguish
reserved quota from committed quota; use rollback rather than release.

The documented [SDK block-storage API](https://docs.openstack.org/openstacksdk/latest/user/proxies/block_storage_v3.html)
provides `delete_volume` with `force=False` and `cascade=False`, and
`wait_for_delete`. Use the project's constrained SDK and cloud helpers;
verify its pinned signatures during implementation. A DELETE response
alone does not prove storage is gone. Do not reset Cinder state or force
an attachment removal to make deletion succeed.

## Snapshot, clone, and export races

**APPROVED conservative rule:** block source deletion while any snapshot,
clone, or unresolved export cleanup depends on it. Never cascade-delete
snapshots or delete an export archive as a side effect of volume DELETE.

Export already holds the stack `RECONCILING` task. The cleanup flow must
hold the same task from acceptance through accounting completion. The
snapshot action, extend, attachment, and server paths must also check it
before accepting work. Check all pages of project/stack-scoped rows and
owned Cinder dependencies. Cinder's own conflict response remains a final
safety check against a cloud-side race; Coast locking is not a substitute.

An ACTIVE export archive is independent after its temporary clones and
snapshots are confirmed gone. Keep that export and its existing object
expiry/download policy. An ERROR export can still have temporary storage;
its status alone does not allow deletion. Interrupted exports currently
need operator inspection, as described in
[Swift export completion](../lifecycle.md#swift-export-completion-c3w).

The Q91 snapshot helper expects volume owners and quiesces their agents.
A removed server supplies no agent. Export of detached retained data is
**DEFERRED** (decision 5). It is recorded as a future requirement.
Do not manufacture an owner or bypass the Q91 contract.

## Failure, revert, and replay

Before Cinder deletion, revert only releases owned locks and records the
fault; it does not change attachments. After delete intent, never recreate
storage, restore an attachment, or silently select another volume.
Keep a sanitized fault, reference, and last checkpoint. Do not log tokens,
credentials, signed URLs, or cloud response bodies that can contain secrets.

On a known failure, retain the row and quota, move to ERROR, and release
only locks owned by this operation. A new authorized DELETE can resume
from verified facts. Recheck attachments and dependencies before another
Cinder request. If Cinder is already gone, proceed to accounting.

A crash retains operation ownership. Inspect the exact Cinder reference
before replaying an uncertain delete. Do not add automatic takeover or
lock expiry without approval. A crash after cloud deletion but before DB
completion must not leak quota permanently: replay reaches the same
single accounting transaction. A duplicate completion must not decrement
quota again, including after the row is gone.

Persist event intent before removing the row so completion can be delivered
without the live resource. Use a stable operation identity for duplicate
notification handling; do not promise exactly-once broker delivery.
The start, end, and error event types are
`coast.volume.delete.{start,end,error}`. Notification failure after committed
cleanup retries delivery, not Cinder deletion or quota release.

## Stack delete interplay

[Offboarding](../lifecycle.md#stack-delete--offboarding) owns all remaining
stack volumes, including retained rows with `server_id=null`. It must scan
by stack and project, not only surviving servers. Its snapshot/export
options remain the stack contract; individual volume DELETE adds none.

**APPROVED:** stack DELETE and individual cleanup cannot run concurrently.
The loser returns 409. Stack deletion must finish requested protection,
then use the same detached verification, non-cascade Cinder deletion,
absence wait, and single accounting completion for each volume. Managed
volumes use an internal path, not the tenant data-only purpose gate.

Retained offboarding snapshots must remain discoverable independently of
hard-deleted volume rows. If a backend cannot delete a source while
retaining its snapshots, fail safely and keep references/quota. Do not
remove protection or mark the stack DELETED to satisfy cleanup. Snapshot
retention storage must not be charged or released as source `volume_gb`
without a separate approved accounting rule.

## Decisions (James, 2026-10-11 10:19 CT)

James recorded these decisions on 2026-10-11 at 10:19 CT.
Decisions 1, 2, 3, 4, and 6 are approved. Decision 5 is deferred.
This document does not change the lifecycle contract or code.

| # | Decision | Status | Conservative proposal |
|---|---|---|---|
| 1 | Approve narrower DELETE eligibility despite existing state edges? | APPROVED as proposed | Reject active creation, attachments, mounts and competing snapshot/export work; no force option. |
| 2 | Approve shared stack serialization, including PATCH and snapshot? | APPROVED | Use existing RECONCILING with project-first lock order; no new volume task state. |
| 3 | Where do durable checkpoints, completion identity and event intent live? | APPROVED: reuse the existing receipt machinery if it survives row deletion; otherwise bring a small schema design back to James before adding any table | Reuse existing receipt machinery if it can survive row deletion; otherwise approve a separate schema design. |
| 4 | What is the retry/wait and interrupted-work recovery contract? | APPROVED | Bounded configured waits, retained quota, operator-verified replay; no automatic takeover. |
| 5 | Can users export detached retained data before deletion? | DEFERRED: export of detached retained data is a future requirement. Do not add it to the roadmap. Note it here. | Not through the current owner-based Q91 helper; specify a separate path if required. |
| 6 | How are retained snapshots/clones inventoried and later removed? | APPROVED | Block individual deletion until dependencies are resolved; never cascade. Define backend retention support and accounting before stack wiring. |
