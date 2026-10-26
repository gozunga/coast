# Compose editor (M39, API 1.12)

Engine releases and defaults come from `coast/common/engine_versions.py`.
Upstream source paths below refer to that release’s tag; dated evidence
refers to the recorded supported release, not the new-stack default.

## Desired state and storage

Coast owns desired compose after the first read or save. Store the entire
UTF-8 document in Barbican and only `services.compose_ref` in the DB.
The YAML can contain credentials. Do not put its text in RPC, events,
exceptions, source responses, or deployment JSON. A deployment snapshots
only the immutable reference. Retain earlier references as for env versions.

Before this feature, Coast stored only `sources.compose_path`. Templates
were created from their catalog ID; Coolify alone held generated raw YAML.
The first editor GET imports a template's `docker_compose_raw` once through
the conductor. Later reads use Coast, including when the engine has drifted.
An imported document may need repair; GET can show it, but PUT and deploy
validate it. A busy service or exclusive stack task blocks import or save.

## API and authorization

Both routes require `OpenStack-API-Version: app-hosting 1.12` and return
404 at earlier versions. Both use project member/admin policies because
the content can contain secrets. Other-project and unknown IDs return 404;
readers return 403. Responses use `Cache-Control: no-store`.

- `GET /v1/services/{service_id}/compose` returns
  `{"compose": {"yaml": "services: ..."}}` (200).
- `PUT /v1/services/{service_id}/compose` takes the same document shape.
  It replaces the whole saved document and returns that shape (200), with
  `X-Coast-Redeploy-Required: true`. It does not change running containers.
- Save and redeploy uses that PUT followed by the existing
  `POST /v1/services/{service_id}/deployments` with `{"deployment": {}}`.
  The latter returns 202 and snapshots the saved reference. Changes are
  visible through `pending_changes`. A failed save does not enqueue work.

Unknown body fields and query parameters return 400. Malformed YAML and
isolation violations return 400 with the service and key, never the value.
Unavailable compose returns 409. Engine connectivity/authentication faults
on import return 503. Secret store failures return a generic 500.

The route covers templates and compose build-pack workloads for which Coast
already holds the content and the driver supports updates. It does not import
Git repository compose. The supported engine application update contract
has no compose-content field; its Git-backed compose applications return 409,
even if a reference is present. Do not PATCH undocumented application fields,
change engine resource types, fetch Git content, or add a compatibility shim.
This is a conservative implementation limit, not permission to use internals.

## Parsing and dedicated workers

`[api] max_compose_bytes` defaults to 10000 UTF-8 bytes. The API applies
the smaller of this cap and `[secrets] max_secret_bytes` (default 20000),
returning 400 naming the limit before secret writes. Keep these settings
within Barbican and HTTP limits. The SafeLoader parser rejects malformed YAML,
duplicate keys, aliases, custom tags and missing/empty service mappings.
These checks protect the control-plane parser and storage, not the worker.

API 1.17 removes workload restrictions on all microversions. Customers own
their dedicated stack workers. Templates and customer Compose can use host
binds, Docker sockets, privilege, host namespaces, devices, capabilities,
external volumes/networks, file imports and any other engine-supported
Compose options. Coast does not recheck workload safety at deploy or
reconcile. The engine remains responsible for Compose execution semantics.
Control-plane network isolation, project policies and agent authentication
remain enforced. No customer Keystone or service credential is added to a
guest. See security.md and networking.md.

New named volumes receive the existing `template_storage_gb` account and
worker capacity/quota checks. Repeated saves do not charge twice. Removed
volume declarations retain their account because Docker retains the bytes.
Service deletion uses the existing template storage cleanup.

## Driver and deploy

`get_compose` reads raw text and `set_compose` replaces it without deployment.
The Coolify driver uses only GET/PATCH `/services/{uuid}`; PATCH sends base64
`docker_compose_raw` and `instant_deploy=false`. Immediately before enqueue,
the conductor reads the frozen reference and applies it before
env sync and the existing start/restart flow. A compose sync failure prevents
enqueue. The noop driver supports the same interface for functional tests.

Public contract sources:

- [Barbican size limits](https://docs.openstack.org/barbican/latest/configuration/config.html#DEFAULT.max_allowed_secret_in_bytes)
- [PyYAML SafeLoader](https://pyyaml.org/wiki/PyYAMLDocumentation)
- `openapi.json` at the selected upstream tag
- [Service update](https://coolify.io/docs/api-reference/api/operations/update-service-by-uuid)
- [Application update](https://coolify.io/docs/api-reference/api/operations/update-application-by-uuid)

## Per-container observations (M39, API 1.13)

Project readers and admins can use these read-only routes:

- `GET /v1/services/{service_id}/containers` returns `containers` summaries.
- `GET /v1/services/{service_id}/containers/{container_id}` returns `container`.
- `GET /v1/services/{service_id}/containers/{container_id}/logs` returns `logs`
  with `lines` containing `ts`, `stream`, and `line`.

A summary contains `id`, `name`, `image`, `state`, `health`, and
`restart_count`. The name is the compose service key. The ID is that key with a `c-`
prefix to avoid reserved route names. Neither field uses the Docker name or ID. Detail adds `environment` (a key/value mapping) and
`limits` (`cpu` in cores, `memory_bytes` in bytes). Missing health/restart
information and unlimited resources are null. Observations include stopped
containers (at most 100 per read). An undeployed service with no worker/reference returns 503;
a deployed service with no containers returns an empty list. These routes
cover templates and Git-backed compose workloads, but not single-image apps.
Multiple observed containers for one compose key are ambiguous and fail closed.

The worker agent uses Docker Engine API 1.44 list, inspect, and logs over its
local Unix socket. The conductor uses its existing management mTLS client,
GET timeout, response size bound, and safe RPC fault mapping. The agent's
request deadline still applies. No engine API, database, or shell is used.
Both Docker list and inspect verify workload ownership labels. The worker
must belong to the service's stack and the request to the configured project.
Only the selected workload's containers are read. Reserved `coolify.*` and
`com.docker.compose.*` labels cannot be set through the compose editor.
Label keys must be literal, so variable expansion cannot bypass that rule.
Public responses contain no labels, host paths, mounts, Docker IDs, or host
configuration. Engine-injected `COOLIFY_` and `COAST_` environment keys are
omitted. Values for Coast Secret-flagged keys and occurrences of their values
are masked with `***`; plain values remain text, including password-like keys.
Logs mask those same Secret-flagged values, including the observed value
when the running container still has an earlier secret. No name-based secret guessing is
used. Responses use `Cache-Control: no-store`; observations are not persisted.

Logs accept only `tail` (0..10000, default 200) and ISO timestamp `since`.
As for service logs, `since` is exclusive and naive timestamps mean UTC.
`tail=0` returns no lines; no follow or historical failure fallback is used.
Output is bounded to 1 MiB per Docker response and the configured agent
response limit. Oversized responses fail instead of returning partial JSON
or log frames. Unknown query fields and invalid values return 400.
Older microversions and other-project or unknown services/containers return
404. Unauthorized roles return 403; unauthenticated requests return 401.
These reads change no desired state and require no quota reservation.
Connection failures return generic 503 faults; invalid responses return a
generic 500. Workers must run an agent that supports this route.

Docker contract: [Engine API 1.44](https://docs.docker.com/reference/api/engine/version/v1.44/).

## Repository-free sources (card #137, API 1.31)

Create accepts `source: {"type": "dockerfile", "dockerfile": "<text>"}`
or `source: {"type": "compose", "compose": "<text>"}`. Content must be
nonempty UTF-8, bounded by the smaller of `max_compose_bytes` and the
secret limit. Compose uses the existing parser with unrestricted worker
options. Source responses contain only `type`. Content never enters RPC,
events, exports, database plaintext or deployment JSON.

Dockerfile workloads require `kind=app`. Compose defaults to `kind=service`
and also accepts `kind=app`; kind does not change the engine resource type.
Pasted Compose uses engine services, the compose editor, container views,
named-volume accounting, explicit database backup selection and cleanup.

`GET /v1/services/{id}/dockerfile` returns
`{"dockerfile": {"text": "FROM ..."}}`. PUT accepts and returns that same
shape, with `X-Coast-Redeploy-Required: true`. Both require the new
microversion and project member/admin authorization, return 404 for other
projects, and set `Cache-Control: no-store`. Unknown fields or query
parameters return 400. Wrong source types return 409. Saves serialize with
service work and stack teardown, and never deploy. These semantics match
the compose editor.

Dockerfile desired state lives in `services.dockerfile_ref`, with immutable
references in deployment snapshots, retained for rollback and removed by
service/stack teardown. Creation uses documented POST
`/applications/dockerfile` with base64 text, no instant deploy and no
generated domain. The Dockerfile is fixed at app creation, including when
immediate deployment is disabled. Editor saves still store text, but the
driver skips unchanged content and rejects changed content before deployment
starts. To change it, create a new app from the new Dockerfile, or use a Git
repository source, whose Dockerfile changes deploy normally.
Compose creation uses POST `/services` with base64 `docker_compose_raw`;
it never uses the removed application compose create route. Deploy and
rollback apply frozen content before starting a fresh deployment. Ports
and domain handling match existing Git Dockerfile applications.
