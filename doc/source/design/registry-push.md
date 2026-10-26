---
orphan: true
---

# Customer registry push access (Q69) - specification

Status: SPEC ONLY, not implemented. Needs James's approval of the open
choices below (Q143) before any code.

## Goal

A cloud user can `docker push` images straight to their environment's
private registry, the one that already runs on the Coolify VM for build
servers (Q16), and then deploy them as image services. Each environment
stays isolated, usage is under quota, and transport uses TLS signed by
the Coast CA.

## Current state

- One `registry:3` container per environment on the Coolify VM. It
  listens on `[controller_worker] registry_port` (default 5000) and is
  reachable only from the environment subnet (networking.md SG
  `coast-env-<id>-ctrl`).
- Auth uses a bcrypt htpasswd file holding one generated credential, kept
  in Barbican (Q125/Q126). TLS uses a leaf signed by the Coast server CA.
- The agent runs hourly garbage collection (`/v1/registry/gc`), which is a
  no-op for an empty registry (!204).

## Proposed design

### Authentication (choose one, Q143-A)

1. **Per-environment push tokens (recommended first step).** A new API
   resource `POST /v1/environments/{id}/registry-credentials` returns a
   username and a one-time-shown token. Coast stores only a bcrypt hash in
   the env's htpasswd (pushed by the agent) and the secret ref in Barbican.
   List/revoke work like application credentials. It is simple and works
   with plain `docker login`.
2. **Keystone token auth.** Run the registry in `token` auth mode against a
   Coast token endpoint (`/v1/registry/token`) that validates Keystone
   credentials or application credentials and issues JWTs scoped to
   `repository:<env>/*:push,pull`. It is more native, but adds a token
   service, signing keys and their rotation.

Both options are policy-gated: `coast:registry:credential:create` defaults
to project member.

### Endpoint exposure (Q143-B)

The registry is not internet-facing today. Options:
- (a) Floating IP on the Coolify VM env port, with SG ingress on the
  registry port from operator-configured CIDRs (default: none).
- (b) Through the stack's existing ingress or HAProxy with SNI
  `registry-<env>.<stack domain>`, which reuses public TLS.
- (c) Only within the tenant network (VPN/bastion). Nothing changes, but
  the user experience is weaker.
TLS: (a)/(c) use the Coast CA, so users must trust the Coast CA bundle
(published at `/v1/ca`). (b) can use a public certificate.

### Quota (Q143-C)

New quota dimensions, nullable limit = unlimited, matching Q19:
`registry_gb` per project (enforced from agent-reported blob usage, with
push rejected through registry `readonly` mode at the limit) and
`registry_credentials` per project. Storage lives on the env's
`/var/lib/registry` volume, so its size also caps usage.

### Isolation

Every credential is scoped to one environment's registry. Registries
never share storage. Revoking a credential or deleting the environment
removes its htpasswd entry or the whole registry. No cross-project pulls.

### API / CLI sketch

The future interface would manage registry credentials and expose the
endpoint, CA bundle URL and usage. No registry CLI commands are available
in 0.1.0.

## Out of scope

Vulnerability scanning (Trivy, Later), pulling from other environments,
public registries, and replication.

## Verification plan (when approved)

Unit tests for policy, quota and credential lifecycle. A live run on
devstack: create a credential, `docker login`, push an image, deploy it as
an image service, revoke, then confirm the push fails. Also check the
quota limit and that GC keeps pushed blobs.
