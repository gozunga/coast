---
orphan: true
---

# M4 service reconcile design (c4rc)

Engine releases and defaults come from `coast/common/engine_versions.py`.
Upstream source paths below refer to that release’s tag; dated evidence
refers to the recorded supported release, not the new-stack default.

## Implementation status

The bounded settings pass is implemented in `coast/conductor/reconcile.py`
and scheduled by `coast/conductor/service.py`. Application env writes
support build-time flags and runtime values. Template defaults remain
engine-owned. Running-state drift produces events without automatic
restart; domain routing changes can restart a running workload through
`coast/drivers/coolify/v4.py`. The original staged implementation plan
below records the narrower initial scope.

## Original scope and size gate

This document designs the M4 roadmap item "Reconcile loop (env vars,
domains, service running state)". It does not change the lifecycle, driver,
API, data model or security contract beyond the approved Q110 comparison.
MR 1 implements the bounded pass described here.

MR 1 is the complete, bounded service pass: repair supported env and domain
settings when drift is known, then record running-state mismatch as an event.
It does not restart workloads. With simple per-service CAS locking, the
initial estimate is 500-600 changed lines including tests. Recheck this limit
before implementation; split driver support by workload type if necessary.

No crash recovery, owner/lease model, flow takeover, failover, engine upgrade,
cloud cleanup, resource discovery or new service/process is in scope.
Do not implement unconditional engine writes in place of drift detection.
Do not mark the roadmap item complete for this design.

## Existing contract and implementation

Read [lifecycle](../lifecycle.md) and
[driver interface](../driver-interface.md) together. The lifecycle specifies
a jittered ten-minute reconcile per environment, events, and restart of
stopped services that should run. It also forbids deletion of unknown
engine resources. The PM scope for this lane overrides restart for MR 1:
record mismatch without automatic restart. The broader lifecycle restart item remains
separate; this design does not implement it.

The implemented ABC still uses `get_app_status`, `start_app` and related
`app` names in `coast/drivers/base.py`. The spec uses service names after
Q94. Use the implemented names in code; do not rename methods in this lane.

| Finding | Location | Consequence and required action |
|---|---|---|
| Status observations have no env/domain drift fields | `base.ObservedStatus`, `CoolifyV4Driver.get_app_status` | Q110 approves compare_settings for settings reconciliation; do not encode drift in free-text detail. |
| Application env writes support managed keys and build-time flags | `CoolifyV4Driver.set_env` | Compare and repair Coast-managed values and flags; preserve unmarked keys. |
| Template env writes preserve unmarked defaults | `_set_template_env`, c3te | Compare and repair Coast-managed keys only; never delete generated/default keys. |
| Template domains can have ambiguous targets | `_set_template_domains`, c3te | Keep the existing target-selection guard; do not invent a selector. |
| Existing periodic recovery is deployment-specific | `conductor/service.py`, `Manager.reconcile_deployments` | Add a separate bounded task; do not broaden deployment recovery implicitly. |
| Generic row iteration can scan until exhaustion | `Manager.rows` | Use explicit page and pass limits, with a continuation cursor. |

The noop driver now models observed settings using in-memory fingerprints
and domain sets, without plaintext secrets or durable storage.

## Desired state and drift sources

**Coast DB is the source of truth.** Engine observations never overwrite
desired env vars, domains, source settings or expected running state.
Resolve secret references through Barbican only for an eligible comparison
or repair. Keep plaintext in memory, never in events, logs or DB rows.

| Item | Desired state | Possible drift | Which side wins |
|---|---|---|---|
| Env vars | Current service env rows, with values resolved by `Manager._variables` | Engine-side edits, removed keys, extra owned keys, value or flag changes | Coast's supported env set; template defaults remain engine-owned |
| Domains | `Manager._fqdns`, including default and published custom domains | Removed routes, extra routes, wrong target or hostname | Coast's full routable set, subject to existing template targeting |
| Running state | Eligible service status ACTIVE | STOPPED, ERROR, DEPLOYING or missing engine resource | Keep Coast intent; report mismatch, not a desired-state rewrite |

Use the existing published-domain selection, not all domain rows. Unverified
and deleting domains must not become routable through reconciliation.
Do not adopt unknown engine resources. Report an absent known engine ref;
do not recreate it or search by a broad name match.

## Per-item driver calls

Q110 approves driver-local settings comparison. It returns only drift
flags, not engine secret values. The conductor uses the public driver method,
not private engine HTTP calls. MR 1 adds no DB or API field.

| Item | Observation | Repair after confirmed drift | Verification |
|---|---|---|---|
| Env vars | Approved driver comparison of desired variables against documented env GETs | `set_env(ep, stack, service, variables)` | Repeat approved observation; unreadable values remain unknown |
| Domains | Approved driver comparison against documented application or template GET | `set_domains(ep, stack, service, fqdns)` | Repeat approved observation; preserve targeting guards |
| Running state | `get_app_status(ep, stack, service)` | First MR records mismatch only; no start/restart/deploy call | Report normalized state, not engine detail |

Unknown is not drift. Hidden or missing values must not cause endless writes
or false convergence. Unsupported observations remain unknown or return sanitized errors. Domain comparison must normalize
hostname case and ordering, without accepting a different scheme, port or
path as equivalent to a Coast HTTPS route. Invalid or ambiguous observations
fail closed.

Use only the documented pinned public API over the existing endpoint and
mTLS transport. No Coolify DB access, UI scraping or container execution.

References for follow-up validation:

- `openapi.json` at the selected upstream tag
- `app/Http/Controllers/Api/ServicesController.php` at the selected upstream tag
- [oslo.service periodic tasks](https://docs.openstack.org/oslo.service/latest/reference/periodic_task.html)

The repository's c3te contract supplies the template endpoint details.
Public API shapes and the OpenStack 2026.1 periodic-task interface were
checked before MR 1. See the driver comparison and operator documentation.

## Eligibility, task locking and bounding

PM-decided scope, recorded in Q110:

1. Select only ACTIVE services whose parent stack is ACTIVE and whose
   parent stack has `task_state=NONE`. Require a known engine ref. Check
   project links and supported driver/version before engine access.
2. An admin context may discover candidates. Reload and operate with each
   recorded project's context; verify service, environment and stack links.
3. Acquire the existing service RECONCILING task with CAS from NONE for
   the duration of one pass for that service. Skip every already locked
   service. Use short DB transactions and existing lock ordering.
4. Reload desired state after acquisition and recheck parent eligibility.
   Do not hold DB transactions across engine or Barbican calls. Release
   the service task in `finally` after this pass, using the existing CAS
   release helper. Do not change task ownership or flow takeover.
5. Start with at most 100 candidates and 30 seconds of scheduling per pass,
   one service at a time. Do not start another service after either limit.
   A current request uses the existing finite HTTP timeout/retry limits;
   the scheduling budget is not a hard cancellation deadline.
6. Keep a continuation cursor with wrap-around, so early UUIDs do not starve
   later services. Keep scheduling and retry maps bounded to the candidate
   window. Apply the ten-minute, jittered due time per scan round (Q112); large
   environments continue across passes rather than running unbounded scans.
7. Use a separate oslo.service timer and stop it before endpoint cleanup.
   Multiple conductors contend through existing DB CAS. Process-local due
   times are not a global rate guarantee.

No owner, lease, durable reconcile receipt, recovery sweep, stale-task
clearance or flow takeover is required or added. Existing behavior after a
process crash remains unchanged. Do not expand this lane to address it.

## Back-off and partial repair

Proposed PM choice: retry a failed item no sooner than its next ten-minute
cycle, then use exponential delay with jitter up to one hour. Reset after
confirmed success. Do not sleep in the sweep. Keep retry bookkeeping
in bounded memory; a conductor restart can reset retry timing.
Document this limit rather than promise durable back-off.

Auth failures, unsupported versions and unknown observations produce fixed
errors and defer repair. A failure for one service must not stop the pass.
No repair retries a start, restart or deploy POST. Existing driver HTTP retry
rules remain unchanged.

Env and domains are separate idempotent assignments. If env repair succeeds
and domain repair fails, keep desired DB state unchanged and record partial
failure. Next attempt observes both again and repairs only remaining drift.
Do not restore stale engine settings as a revert. Revert releases only the acquired
service task and records a sanitized error. Notification failure must not
trigger another engine write within the pass.

## Running state and notifications

For the first MR, retain ACTIVE as expected running intent and use an
existing service activity event for mismatch. Do not set STOPPED from an
observation: that would lose the intent used for the next comparison.
ERROR, transient DEPLOYING, unreachable and missing resource outcomes are
separate fixed classifications; none proves a stopped workload.

Propose `coast.service.reconcile.{start,end,error}` through existing
oslo.messaging/CADF helpers. Use recorded project/resource IDs, drift item
names, normalized states and fixed reason codes only. No env values, secret
refs, engine response bodies, URLs or exception text. Persist activity/event
intent through existing machinery; do not promise exactly-once delivery.
Emit start only after service CAS acquisition, end for convergence or a completed
observation, and error for mismatch or failed/unknown repair. Quiet, unchanged
healthy checks do not need events. Suppress unchanged mismatch reports using
existing durable events if safe; otherwise bound them to the retry schedule.
All actual state changes still use `coast/common/states.py` and notifications.

The existing lifecycle restart requirement remains for a later MR. Its
eligibility and retry safety need explicit review: normalized STOPPED only,
no transient or unknown state, no repeated POST after an uncertain outcome.
Do not ship automatic restart as a side effect of settings repair.

## Questions and judgement calls

Q110 records the PM scope and engineering choices.
Q110 approves settings comparison and Q112 records MR 1 scheduling.
Use only public driver calls; do not add task ownership or takeover.

| ID | Status | Decision or question |
|---|---|---|
| D1 | RESOLVED, PM-decided | MR 1 repairs supported settings drift and records running mismatch, without auto-restart. |
| D2 | RESOLVED, PM-decided | ACTIVE service, ACTIVE parent stack with task NONE; existing per-service CAS for one pass; skip locked services. |
| D3 | RESOLVED, PM-decided | No ownership, lease, crash recovery or takeover work. |
| D4 | RESOLVED, PM-decided | Bounded scans, fair continuation, unknown is not drift, bounded back-off and sanitized events. |
| A1 | RESOLVED, PM-approved | Approve `compare_settings(ep, stack, service, variables, fqdns)` returning env/domain drift flags (`True`, `False`, or `None` for unknown). Approved in Q110. |

## Ordered small MRs and verification

| Order | Scope | Required proof and gate |
|---|---|---|
| 1 | Approved settings comparison, noop support, supported Coolify observations, bounded conductor pass, env/domain repair and running mismatch events | Q110 approved. Stay under about 600 total changed lines; test clean/drift/unknown settings, Q52 rejection, template defaults/ambiguity, wrong project, busy service/stack, parent status, CAS release on error, bounds, back-off, secret masking and no restart. Add docs and release note. |
| 2 | Extend observation/repair coverage to additional supported workload cases if MR 1 needs splitting | Preserve hidden-value handling, generated keys and existing build/runtime semantics; recorded fixtures and pinned live contracts. |
| 3 | Operator documentation and contract evidence for the completed reconcile coverage | Confirm fair scans, event classification and partial retries under documented engine failures; no new takeover model. |

Keep each MR below about 500 non-test changed lines where possible. Each
runtime MR adds a release note and operator documentation. Run touched unit
tests plus `tox -e pep8,docs`; before pushing run
`tox -e py3,pep8,docs,genconfig,genpolicy`. Live driver contracts use only the
pinned disposable engine and existing mTLS endpoint. No live calls are needed
for this design. Tick the roadmap item only when the full contract is done.
