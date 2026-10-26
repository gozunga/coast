# Building a UI for Coast

This guide is for developers of a tenant portal that drives Coast on
behalf of a cloud user. It describes the implemented Coast API. Use Coast
as the source of truth. Do not call the app engine, Nova, Cinder,
Barbican, or a guest agent with operator credentials from a tenant portal.

The API serves microversions **1.0** to **1.31**
(`coast/api/microversions.py`). The worked examples below were captured
at microversion **1.28** and show the core create, deploy and delete flows.
For additional endpoints, including storage, file mounts, backups, metrics,
ingress rules and resize, see the API reference (`api-ref/source/`) and
[api.md](api.md). Send
`OpenStack-API-Version: app-hosting 1.28` and read the version
discovery document for the range the cloud supports.

## Read this first

A response that accepts work (202) is not proof that the work finished.
Poll the resource until it reaches ACTIVE or ERROR.

| Feature | Implemented boundary | Portal behavior |
|---|---|---|
| App group > environment > service | Service create requires both parent IDs. Responses expose both; lists filter by either ID. | Select an ACTIVE app group and environment before create. |
| Image source | The public schema accepts `{"type":"image","image":"<ref, no digest>","tag":"<tag>"}`. The tag is required. A digest reference or a credential field returns 400. Public registries only. | Offer image create with a separate reference and a required tag. Do not send a digest or registry credentials. |
| Git and template service create | The API validates ACTIVE app group/environment parents and requires a worker `server_id` (there is no automatic placement). | Ask the user to pick a worker. Poll the service and deployment after 202. |
| App group/environment delete | DELETE returns 202 when no child remains. A live child returns 409 naming the conflict. A deleted name can be reused. | Poll GET until 404. Do not offer delete while a child remains. |
| Stack delete | Refused with 409 while app groups remain; the fault names them. Requires the project `manager` role or admin. | Delete apps, services, environments and app groups first. |
| Servers | Add or remove workers and build servers (project `manager` or admin). The controller cannot be removed; a server with workloads returns 409. Resize to a strictly larger flavor (1.26); list allowed flavors (1.27). | Offer only larger flavors for resize. To shrink, add a smaller worker and move apps. |
| Engine upgrade | `upgrade` with a published image ID; `upgrade_rollback` from ACTIVE or ERROR when a rollback point exists (409 otherwise). Nothing rolls back automatically. | Show the stack's `engine_version`. Rollback references are not returned, so treat 409 on rollback as "no rollback point". |
| Worker ingress rules | 1.25: IPv4 TCP, UDP or ICMP rules per stack. Managed HTTP, HTTPS and SSH rules are listed with `managed: true` and cannot be deleted. | Show managed rules read-only. |
| Domains and HTTPS | Verify checks TXT synchronously and can return VERIFIED. Conductor checks continue through PENDING_DNS to ACTIVE after DNS and TLS checks. A timeout sets ERROR. | Do not show HTTPS as ready until the domain reaches ACTIVE. Offer verify from PENDING_VERIFICATION or ERROR. |
| Stack offboarding | Snapshot-on-delete and `export_before_delete` are implemented. | A 202 does not prove that every cloud resource is gone. |
| Replicas and auto-deploy | `replicas` must be 1 and `auto_deploy` must be false. | Hide these controls. |
| Private Git | API 1.30 accepts SSH repositories with generated or uploaded deploy keys. | Show only the public key; see [App sources](app-sources.md). |
| Deploy and push webhooks | 1.3: `GET /v1/services/{id}/webhooks` returns per-app deploy and Git host URLs when the operator enables webhooks; `POST .../webhooks/regenerate` revokes them. | Show the URLs only to project members; treat them as secrets. |
| Quota usage | Project quota show returns `usage.in_use` and `usage.reserved` for each quota key. | Show those counts with the limits. |

### Where the examples came from

Every JSON block below is captured at API **1.28** by
`tools/check_doc_examples.py`. Run `tox -e doc-examples` to check them or
`tox -e doc-examples -- --write` to recapture them. Requests pass through
the real in-process WSGI app, controllers, schemas, objects, policy and
version middleware. The checker compares every complete JSON body,
including nested keys, and rejects stale explicit version headers.

The script uses the same isolated SQLite and in-memory secret fixtures as
`coast.tests.functional`. Asynchronous RPC handoff is suppressed; explicit
fixture completions supply later observed states. It never contacts a
cloud, app engine, Barbican or object store. This checks the API contract,
not provisioning or cleanup. IDs, timestamps, generated host suffixes and
DNS challenges are normalized to stable example values. Reuse the IDs
and challenge values from your own responses.

Template reads use the packaged metadata selected by `TEMPLATE_VERSION`
in `coast/common/engine_versions.py`, with policy allowing only
`wordpress-with-mariadb`. Archive metadata and signed URL results are
fixtures, not uploaded objects. Bearer tokens and signatures are replaced
with `REDACTED`. Env write values are synthetic examples; marked secret
values remain null in captured reads. No real secret payload is used.

State and policy tables below describe the code separately from these
captured request and response examples.

## 1. Resource model and ownership

The approved hierarchy is:

```text
Keystone project
  +-- stack (infrastructure and isolation boundary)
       +-- app group
       |    +-- environment (a user-supplied name)
       |         +-- app or service (needs appgroup_id and environment_id)
       +-- servers: one control server, workers, optional build servers
       +-- volumes: data, docker, scratch, controller
       +-- exports: selected data/docker volumes -> archive artifacts
       +-- services (flat /v1/services collection)
       |    +-- deployments (numbered history and frozen specifications)
       |    +-- domains (custom FQDN, DNS challenge, TLS state)
       |    +-- env vars (key -> value, secret flag, version)
       +-- events (project-scoped activity)

Operator plan catalog ----> stack/server create plan names
Packaged template catalog -> template source, selected per stack/engine
```

A stack is not a stage. Names such as `production` are environment names,
not plans, flavors, or fixed API enums. App groups and environments do
not own servers or networks. Service create requires both `appgroup_id`
and `environment_id`; responses expose both parent IDs.

| Resource | Identity and relationship | Public route |
|---|---|---|
| Stack | Coast UUID, project_id | `/v1/stacks/{stack_id}` |
| App group | Coast UUID, stack_id | `/v1/appgroups/{appgroup_id}` |
| Environment | Coast UUID, appgroup_id | `/v1/environments/{environment_id}` |
| Service | Coast UUID, appgroup_id, environment_id, stack_id, nullable server_id | `/v1/services/{service_id}` |
| Server | Coast UUID, stack_id; role control/worker/build; kind vm/baremetal | `/v1/stacks/{stack_id}/servers/{server_id}` |
| Volume | Coast UUID, stack_id, nullable server_id; purpose and size_gb | `/v1/stacks/{stack_id}/volumes/{volume_id}` |
| Deployment | Coast UUID, service_id, per-service number, optional rollback_of | `/v1/services/{service_id}/deployments/{dep_id}` |
| Domain | Coast UUID, service_id; globally unique fqdn | `/v1/services/{service_id}/domains/{domain_id}` |
| Env var | Key within a service; response has version, not a secret ID | `/v1/services/{service_id}/env` |
| Export | Coast UUID; selected volume_ids; one artifact per archive | `/v1/stacks/{stack_id}/exports/{export_id}` |
| Event | Coast UUID; resource_type/resource_id and optional stack_id | `/v1/events?id={event_id}` (list filter, not an item route) |
| Plan | Operator-defined name, not a UUID | `/v1/plans` (collection only) |
| Template | Catalog string ID, not a Coast UUID | `/v1/templates/{template_id}?stack_id={stack_id}` |
| Quota | Keystone project ID, not a separate Coast UUID | `/v1/quotas/{project_id}` |

Use IDs as opaque strings. Store `project_id` from the authenticated
session; do not send it in resource create bodies. The API stores the
project from the token. A foreign resource ID returns 404, including on
most admin item routes. Admin cross-project access is an explicit list
option or quota operation, not a general item-lookup bypass.

The tenant owns Coast records. The service project owns the underlying
Nova, Neutron, Cinder, and Barbican resources. A tenant portal must not
use a Coast server ID as a Nova ID or a Coast volume ID as a Cinder ID.
Cloud IDs, engine references, and secret references are not tenant
identifiers. Admin detail policy adds some infrastructure fields only.

Stack, app group, environment, server, service, and deployment records
support soft deletion in the data model. Normal reads exclude deleted
rows. Other resources use hard deletion. Do not invent a deleted-items
query parameter. Volume mounts exist internally but have no public CRUD
route or service-create mount field in this version.

See [the data model](data-model.md), [security](security.md), and
[network ownership](networking.md).

## 2. Authentication, discovery, and headers

Obtain a **project-scoped Keystone token** through the portal's normal
OpenStack login. Send it as `X-Auth-Token` on resource requests. Never
send test-only `X-User-Id`, `X-Project-Id`, or `X-Roles` headers as a
production authentication method. Keystone middleware validates identity
and replaces client identity claims.

Find service type **app-hosting** in the token's service catalog. The
registered service name is `coast`, but select by type. Select the region
and interface from portal/operator configuration. Use the returned URL,
including any path prefix. Do not hard-code a hostname, region, or port.
Do not append `/v1` twice if the catalog endpoint already has it.
The catalog can return no match or more than one match; ask the operator
to supply the intended region/interface instead of guessing.
The [Keystone catalog reference](https://docs.openstack.org/keystoneauth/latest/api/keystoneauth1.access.service_catalog.html)
describes endpoint selection.

For JSON bodies send `Content-Type: application/json`. Use these headers:

```text
X-Auth-Token: <project-scoped-token>
OpenStack-API-Version: app-hosting 1.28
Accept: application/json
Content-Type: application/json
```

Angle-bracket text above is a substitution, not a literal token.
The response echoes the selected version, for example
`OpenStack-API-Version: app-hosting 1.28`, and sets
`Vary: OpenStack-API-Version`. An absent version header selects 1.0;
`app-hosting latest` selects the highest version the server supports
(`max_version` in discovery). Pin the highest version your portal was
tested against rather than send `latest`, so a server upgrade cannot
change response bodies under you. The parser requires exactly
`app-hosting` plus a space and a version. A malformed header returns
400. A well-formed version outside the supported range, such as
`app-hosting 1.99`, returns 406. No alternate
Coast version header is implemented.

`GET /`, `GET /v1`, and `GET /v1/` provide public version discovery.
`GET /healthcheck` is also public; it bypasses microversion handling and
does not return the microversion header. Resource routes require identity.
Keep the public endpoint's path prefix when constructing resource URLs.

**Discover the version**

```text
GET /
```

Request body: none.

Response: **200**.

```json
{
  "versions": [
    {
      "id": "v1",
      "status": "CURRENT",
      "min_version": "1.0",
      "max_version": "1.31",
      "links": [
        {
          "rel": "self",
          "href": "https://api.example.com/v1/"
        }
      ]
    }
  ]
}
```

**Read the selected version**

```text
GET /v1
```

Request body: none.

Response: **200**.

```json
{
  "version": {
    "id": "v1",
    "status": "CURRENT",
    "min_version": "1.0",
    "max_version": "1.31",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/"
      }
    ]
  }
}
```

### Request IDs and browser access

Every request has `X-OpenStack-Request-Id` in the response. Save that ID
with the method, resource ID, HTTP status, and time in a support report.
Do not log tokens, env values, log bodies, or signed URLs. The events
collection can filter by `request_id`. The pipeline also supports an
OpenStack global request ID; do not assume that the response ID is your
incoming correlation ID.

For direct browser access, the operator must permit the portal origin
in CORS configuration and expose the response headers the portal uses.
A portal backend can keep tokens out of browser storage. Do not disable
TLS verification or substitute service-project credentials to avoid a
CORS or policy error.

## 3. Asynchronous work and progress

A 202 means **accepted**, not successful. There is no operation/job URL
and no guaranteed Location or Retry-After header. Use the returned
resource ID and self link. On service create with `deploy` omitted or
true, also save the create-only `deployment_id`. Follow both the service
and that deployment. `current_deployment_id` is the completed current
revision, not necessarily the newly accepted deployment.

Recommended portal polling, not an API requirement:

1. Show an immediate local progress indicator after acceptance.
2. Poll after 2 seconds, then 5 seconds. Back off to 10, 20, then 30
   seconds for long infrastructure operations. Add small random delays
   so many browser sessions do not poll together.
3. Poll deployment progress every 5 seconds while its page is open;
   use up to 30 seconds on a background page. Refresh lists less often.
4. Stop rapid polling on success or failure. Resume on a user refresh.
   Do not keep an inactive tab polling each row independently.
5. A portal timeout means “Still working; check again.” It must not
   change the resource to ERROR or resend the write.
6. On 401, renew authentication once. On 409, refresh the resource and
   explain the current conflict. On transient server/transport failure,
   back off. Do not automatically repeat a POST after an uncertain reply.

There is no idempotency-key API. After a lost create reply, reconcile
through a project-scoped list and the intended name before another
create. A duplicate name can return 409. Do not treat that as proof that
the duplicate is the resource from the lost request.

### Status versus task state

Status is a lifecycle value. `task_state` is a separate operation lock:
`NONE`, `PROVISIONING`, `ADDING_SERVER`, `DEPLOYING`, `ROLLING_BACK`,
`UPGRADING`, `FAILING_OVER`, `DELETING`, or `RECONCILING`.
A non-NONE task can cause 409 even when status is ACTIVE.

Stack and server `task_state` are admin detail fields. App group
and environment responses do not expose their task state. Service
responses include `task_owner`, `task_started_at` and `reconcile_fault`.
Volumes, deployments, domains, and exports have no task-state column.
Keep a local “request accepted” indicator for actions such as restart,
whose response status does not change. Do not infer that an unchanged
status means the work is complete. A restart has no public completion
receipt field; use a later activity event when available, or show
“Restart requested” rather than claim verified completion.

For successful service, server, volume, domain, and export removal, a
subsequent GET normally returns 404. A successful stack removal is also
hidden from normal reads, even though its history status is DELETED.
A 404 before acceptance is “not found,” not successful deletion.
App group/environment deletion also completes when GET returns 404.

### Activity and notifications

`GET /v1/events` is a polled project-scoped activity list. It is not an
SSE, WebSocket, or broker subscription. There is no public event-item
route. Its self link is an equality-filtered collection URL.
The conductor records outcomes `start`, `success`, and `error`.
Some rows are internal operation receipts; do not turn every event
message into an end-user alert.

Control-plane oslo.messaging notifications have the form
`coast.<resource>.<action>.<phase>`, with phase `start`, `end`, or `error`.
Notification resource names use **environment for stack** and **app for service**.
Examples of implemented lifecycle names are
`coast.environment.create.start`, `coast.environment.create.end`,
`coast.server.delete.end`, `coast.volume.delete.error`,
`coast.app.create.end`, `coast.deployment.create.end`,
`coast.domain.verify.end`, and `coast.export.create.end`.
Rollback maps to deployment **create**, not rollback. Cancel and volume
extend map to **update**. Env changes use `coast.env_var.create`,
`coast.env_var.update`, or `coast.env_var.delete`, with start/end suffixes.
Synchronous stack/service updates emit start and end in the API process.
Do not assume that app group/environment operations emit those pairs;
their current handlers do not use the standard notification helpers.

Lifecycle payloads allow only scalar `resource_id`, `project_id`,
`user_id`, `status`, `stack_id`, `service_id`, `server_id`, and
`operation_id` when set. Volume cleanup notifications use a stable
operation_id; terminal receipt replay retries delivery without cloud work.
They do not contain names, secret values, secret references, or full
resource JSON. Delivery failure is logged and does not fail the API.
More than one start can be emitted. These are not exactly-once or
complete change-history guarantees.

Periodic usage snapshots emit `coast.environment.exists`,
`coast.server.exists`, `coast.volume.exists`, and `coast.app.exists`.
They describe **current undeleted rows**, labelled with the previous
complete UTC audit period, not all resources that existed during that
period. Extra allow-listed fields include plan, flavor_id, size_gb,
replicas, cpu_limit, memory_limit_mb, created_at,
audit_period_beginning, audit_period_ending, and the configured service_id.
They exclude user_id. There is no export-byte usage snapshot in this code.
Deduplicate snapshots by resource and audit bounds; a restart or replica
can repeat them. Deleted-resource/full-period usage history is future work.

A browser must never connect to RabbitMQ. An operator-controlled portal
backend can consume notifications only with a separate approved broker
integration. Filter by authenticated project, deduplicate, and use the
notification as a reason to GET the current Coast resource. Polling
remains necessary if events are lost. There is no tenant subscription
endpoint to invent.

**Read recent service activity**

```text
GET /v1/events?resource_id=bbf16995-64cc-430a-bfbc-86717d8dc7b0&limit=1&sort_key=created_at&sort_dir=desc
```

Request body: none.

Response: **200**.

```json
{
  "events": [
    {
      "id": "d6362a86-403c-5fb3-a49e-6d317c7403cb",
      "project_id": "project",
      "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
      "resource_type": "app",
      "resource_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
      "action": "create",
      "outcome": "accepted",
      "message": "Create accepted.",
      "request_id": null,
      "created_at": "2026-10-22T12:00:00Z",
      "updated_at": null,
      "links": [
        {
          "rel": "self",
          "href": "https://api.example.com/v1/events?id=d6362a86-403c-5fb3-a49e-6d317c7403cb"
        }
      ]
    }
  ],
  "links": []
}
```

## 4. User workflows

The first examples form a single noop workflow. The application/template
and Swift fixture examples use separate stacks, as stated in each section.
All GET requests have no body. A DELETE with no body is preferred unless
stack delete options are required. Action bodies are unwrapped objects
such as `{"stop": {}}`, not a service envelope.

### 4.1 Choose plans and create a stack

Read plans first. Show the returned descriptions, kinds, and allowed roles.
Plan names below are example operator configuration, not default plans.
The catalog does not expose vCPUs, RAM, prices, or disk sizes. Only
microversion 1.1 adds each plan's Nova `flavor` name (an interim field
for the dashboard); 1.0 and 1.2 onward omit it, as in the example below.
From 1.27, `GET /v1/flavors` lists the flavors allowed for
server create and resize. Do not query the
service project's Nova catalog from the portal to fill those gaps.
If the plan list is empty, show an operator configuration message.

Select a plan with `control` for the stack. Select a matching `worker`
plan for each initial worker. `build_server: true` requests one initial
build VM.
At microversion 1.1 only, `build_server_plan` selects the build plan and
`build_server_image` selects its published worker image. Omitting the plan
requires exactly one configured build plan. Later server create selects
its own plan. Bare-metal requests require the operator to enable
`[controller_worker] enable_baremetal`, but the provisioning flow accepts
only VM workers and build servers. Offer VM plans in the portal.

```text
GET /v1/plans
```

Request body: none.

Response: **200**.

```json
{
  "plans": [
    {
      "name": "example-control",
      "kind": "vm",
      "roles": [
        "control"
      ],
      "description": "Example controller plan"
    },
    {
      "name": "example-worker",
      "kind": "vm",
      "roles": [
        "worker",
        "build"
      ],
      "description": "Example worker plan"
    }
  ]
}
```

**Create with one initial worker**

```text
POST /v1/stacks
```

Request body:

```json
{
  "stack": {
    "name": "website-stack",
    "plan": "example-control",
    "workers": [
      {
        "kind": "vm",
        "plan": "example-worker"
      }
    ],
    "build_server": false,
    "data_volume_gb": 10
  }
}
```

Response: **202**.

```json
{
  "stack": {
    "id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "project_id": "project",
    "name": "website-stack",
    "description": null,
    "plan": "example-control",
    "engine_version": "<engine version>",
    "status": "PENDING",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "fault": null,
    "default_domain": null,
    "dns_zone_id": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969"
      }
    ]
  }
}
```

The response does not echo workers, build_server, or data_volume_gb.
Poll the stack self link; then list its servers and volumes.

**Read completed stack state**

```text
GET /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969
```

Request body: none.

Response: **200**.

```json
{
  "stack": {
    "id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "project_id": "project",
    "name": "website-stack",
    "description": null,
    "plan": "example-control",
    "engine_version": "<engine version>",
    "status": "ACTIVE",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "fault": null,
    "default_domain": null,
    "dns_zone_id": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969"
      }
    ]
  }
}
```

**Read the allocated servers**

```text
GET /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers
```

Request body: none.

Response: **200**.

```json
{
  "servers": [
    {
      "id": "149b064d-5436-5282-a998-23d731f5b797",
      "project_id": "project",
      "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
      "name": "control-0",
      "hostname": null,
      "kind": "vm",
      "role": "control",
      "status": "ACTIVE",
      "floating_ip": null,
      "created_at": "2026-10-22T12:00:00Z",
      "updated_at": null,
      "links": [
        {
          "rel": "self",
          "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers/149b064d-5436-5282-a998-23d731f5b797"
        }
      ],
      "flavor": null
    },
    {
      "id": "43828f3a-dd3f-5308-b8eb-51f304689211",
      "project_id": "project",
      "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
      "name": "worker-1",
      "hostname": null,
      "kind": "vm",
      "role": "worker",
      "status": "ACTIVE",
      "floating_ip": null,
      "created_at": "2026-10-22T12:00:00Z",
      "updated_at": null,
      "links": [
        {
          "rel": "self",
          "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers/43828f3a-dd3f-5308-b8eb-51f304689211"
        }
      ],
      "flavor": null
    }
  ],
  "links": []
}
```

### 4.2 Add and remove a worker

Require the server create/delete policy. Choose an existing ACTIVE stack.
Use `role: worker` for deployment capacity or `role: build` for a build
server. A build server is not valid service placement. Only Coast creates
control servers. The public response does not store or return the plan.
A worker floating_ip can be null until allocation; build/control servers
have no public floating IP in the real VM flow.

```text
POST /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers
```

Request body:

```json
{
  "server": {
    "name": "worker-two",
    "kind": "vm",
    "role": "worker",
    "plan": "example-worker"
  }
}
```

Response: **202**.

```json
{
  "server": {
    "id": "045dff79-d669-4c4d-b353-0ae1b080ac2c",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "name": "worker-two",
    "hostname": "coast-website-stack-84b93c-worker-two",
    "kind": "vm",
    "role": "worker",
    "status": "PENDING",
    "floating_ip": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers/045dff79-d669-4c4d-b353-0ae1b080ac2c"
      }
    ]
  }
}
```

**Poll the new worker**

```text
GET /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers/045dff79-d669-4c4d-b353-0ae1b080ac2c
```

Request body: none.

Response: **200**.

```json
{
  "server": {
    "id": "045dff79-d669-4c4d-b353-0ae1b080ac2c",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "name": "worker-two",
    "hostname": "coast-website-stack-84b93c-worker-two",
    "kind": "vm",
    "role": "worker",
    "status": "ACTIVE",
    "floating_ip": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers/045dff79-d669-4c4d-b353-0ae1b080ac2c"
      }
    ],
    "flavor": null
  }
}
```

Remove placed services and volume mounts before worker removal.
The API rejects a visible service placement with 409. There is no drain,
force, retention, or migration option on this DELETE. The real VM remove
flow keeps non-Docker data volumes and their volume quota; it removes
Docker volumes. This differs from the simpler noop fixture. List volumes
after real removal to find retained rows with server_id null.

**Remove an empty worker**

```text
DELETE /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers/045dff79-d669-4c4d-b353-0ae1b080ac2c
```

Request body: none.

Response: **202**.

```json
{
  "server": {
    "id": "045dff79-d669-4c4d-b353-0ae1b080ac2c",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "name": "worker-two",
    "hostname": "coast-website-stack-84b93c-worker-two",
    "kind": "vm",
    "role": "worker",
    "status": "DELETING",
    "floating_ip": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/servers/045dff79-d669-4c4d-b353-0ae1b080ac2c"
      }
    ]
  }
}
```

### 4.3 Create an app group and an environment

These examples use a separate, pre-existing ACTIVE fixture stack.
Create an app group in that stack, then create an environment under
that app group's ID. Manager permission is required for these creates,
not just member permission. Name/description changes use member policy.
The item routes are top-level; do not append an item ID to the nested
create URL. No top-level app group or environment collection is exposed. App group
creation supplies a `production` environment; this example adds `staging`.

```text
POST /v1/stacks/5a814ad0-d4a5-4480-97cd-aec1e3f8b847/appgroups
```

Request body:

```json
{
  "appgroup": {
    "name": "website",
    "description": "Website workloads"
  }
}
```

Response: **202**.

```json
{
  "appgroup": {
    "id": "0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe",
    "project_id": "project",
    "stack_id": "5a814ad0-d4a5-4480-97cd-aec1e3f8b847",
    "name": "website",
    "description": "Website workloads",
    "status": "PENDING",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/appgroups/0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe"
      }
    ]
  }
}
```

```text
POST /v1/appgroups/0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe/environments
```

Request body:

```json
{
  "environment": {
    "name": "staging"
  }
}
```

Response: **202**.

```json
{
  "environment": {
    "id": "b532f5c6-028a-4233-9541-061cd5857a75",
    "project_id": "project",
    "appgroup_id": "0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe",
    "name": "staging",
    "description": null,
    "status": "PENDING",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/environments/b532f5c6-028a-4233-9541-061cd5857a75"
      }
    ]
  }
}
```

Poll `/v1/appgroups/{id}` and `/v1/environments/{id}` for ACTIVE
or ERROR. This fixture has no conductor, so its GETs remain PENDING.
The production conductor has create activation for these records.
Do not infer an app group/environment ID from a stack or server ID.
Service create uses both IDs. A missing required field returns 400. A
missing or foreign parent returns 404. A same-project hierarchy mismatch
returns 400. Both parents must be ACTIVE and have engine references, or
create returns 409 (Q98). Do not infer readiness from a 202 response.

### 4.4 Deploy a service from public Git

Choose an app group and environment in the intended stack. Send
appgroup_id, environment_id, name, server_id (even if null), and source.
The environment must belong to the app group. A non-null server must be
a worker in the app group's stack and project. The app group selects
the stack; do not send stack_id in the create body. Both parents must be
ACTIVE with engine references. This example uses public HTTPS Git. For SSH repositories with deploy keys,
see [App sources](app-sources.md). Do not
include credentials, userinfo, query strings, or deploy-key fields in this
public HTTPS example.

A name is lower-case letters/digits with optional internal hyphens,
at most 63 characters. Build packs are nixpacks, dockerfile, static, or
compose. Dedicated workers permit host paths, Docker sockets, privileged
containers, host namespaces, devices and capabilities on every supported
microversion. The Git branch defaults to main. Ports are a string, not an
integer array. replicas must be 1; auto_deploy must be false.
The optional CPU/memory limits must be positive when set.

The example uses an ACTIVE app group and environment in the main
workflow stack. Create those parents as in section 4.3, but use this stack
and the IDs returned by your own requests. The API supplies the parent
placement needed by the app engine. Noop completion is still not proof
of real-cloud deployment. Poll service and deployment status after 202.

Create starts a deployment by default. `deploy: false` omits the
create-only deployment_id and the noop completion leaves the service
STOPPED. It does not mean that image/template validation is skipped.

```text
POST /v1/services
```

Request body:

```json
{
  "service": {
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "name": "web",
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "source": {
      "type": "git",
      "repo_url": "https://example.com/web.git",
      "branch": "main",
      "build_pack": "dockerfile",
      "dockerfile_path": "Dockerfile"
    },
    "ports": "8080",
    "env": {
      "MODE": {
        "value": "production"
      },
      "PASSWORD": {
        "value": "example-only",
        "secret": true
      }
    }
  }
}
```

Response: **202**.

```json
{
  "service": {
    "id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "template_id": null,
    "name": "web",
    "description": null,
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "ports": "8080",
    "replicas": 1,
    "cpu_limit": null,
    "memory_limit_mb": null,
    "healthcheck_path": null,
    "health_check": "none",
    "health_check_port": 8080,
    "auto_deploy": false,
    "status": "PENDING",
    "current_deployment_id": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "task_owner": "PROVISIONING",
    "task_started_at": "2026-10-22T12:00:00Z",
    "reconcile_fault": null,
    "health_check_path": "/",
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "kind": "app",
    "pending_changes": false,
    "source": {
      "type": "git",
      "repo_url": "https://example.com/web.git",
      "branch": "main",
      "build_pack": "dockerfile",
      "dockerfile_path": "Dockerfile",
      "compose_path": null,
      "base_directory": null
    },
    "default_url": "https://web-bbf1.apps.example.com",
    "dns_records": [],
    "default_domain_enabled": true,
    "primary_url": "https://web-bbf1.apps.example.com",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0"
      }
    ],
    "deployment_id": "89591377-8bc9-42e4-8496-e28bce24661c"
  }
}
```

**Poll the completed service**

```text
GET /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0
```

Request body: none.

Response: **200**.

```json
{
  "service": {
    "id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "template_id": null,
    "name": "web",
    "description": null,
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "ports": "8080",
    "replicas": 1,
    "cpu_limit": null,
    "memory_limit_mb": null,
    "healthcheck_path": null,
    "health_check": "none",
    "health_check_port": 8080,
    "auto_deploy": false,
    "status": "ACTIVE",
    "current_deployment_id": "89591377-8bc9-42e4-8496-e28bce24661c",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "task_owner": null,
    "task_started_at": null,
    "reconcile_fault": null,
    "health_check_path": "/",
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "kind": "app",
    "pending_changes": false,
    "source": {
      "type": "git",
      "repo_url": "https://example.com/web.git",
      "branch": "main",
      "build_pack": "dockerfile",
      "dockerfile_path": "Dockerfile",
      "compose_path": null,
      "base_directory": null
    },
    "default_url": "https://web-bbf1.apps.example.com",
    "dns_records": [],
    "default_domain_enabled": true,
    "primary_url": "https://web-bbf1.apps.example.com",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0"
      }
    ]
  }
}
```

**Poll the initial deployment**

```text
GET /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments/89591377-8bc9-42e4-8496-e28bce24661c
```

Request body: none.

Response: **200**.

```json
{
  "deployment": {
    "id": "89591377-8bc9-42e4-8496-e28bce24661c",
    "project_id": "project",
    "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "number": 1,
    "source_ref": "main",
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "trigger": "api",
    "rollback_of": null,
    "status": "ACTIVE",
    "fault": null,
    "started_at": null,
    "finished_at": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "resolved_ref": "0000000000000000000000000000000000000000",
    "runtime_log": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments/89591377-8bc9-42e4-8496-e28bce24661c"
      }
    ]
  }
}
```

Use resolved_ref to show the exact commit/digest when the driver returns
it. Do not fabricate it from source_ref. The zeros in this noop result
are the noop driver's fixed test commit, not an actual repository commit.
PATCH service settings does not deploy them. It accepts name,
description, ports, cpu_limit, memory_limit_mb, healthcheck_path, and the
Git source settings branch/build_pack/dockerfile_path/compose_path/
base_directory. It cannot change placement, source type, or repo URL.
Submit a new deployment after a settings or env change.

### 4.5 Deploy a service from a public image

API gap 2 is closed (Q99). The public API accepts an image source.
Send the image reference and the tag as separate fields. Do not copy an
app engine payload into the Coast API.

`image` is a Docker reference name. It has no scheme, no credentials, no
tag, and no digest. `tag` is required. There is no implicit `latest`.
A digest reference returns 400. Credential fields return 400. This create
path is for public registries only. A private registry uses credentials
that are already configured on the environment engine. Do not send those
credentials in the request.

Use the same parent fields as section 4.4. Both parents must be ACTIVE
and must have engine references.

```text
POST /v1/services
```

Request body:

```json
{
  "service": {
    "name": "image-web",
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "source": {
      "type": "image",
      "image": "registry.example.com/web",
      "tag": "release"
    }
  }
}
```

A valid request returns **202**. The service `source` in the response is
the same object: `type`, `image`, and `tag`. Example values are
`image` `registry.example.com/web` and `tag` `release`.

These requests return **400** and do not create a service:

* `image` set to `registry.example.com/web:release` (the tag is inside the reference)
* `image` set to `registry.example.com/web@sha256:abc` (a digest reference)
* a source field `credentials` or `credentials_ref`
* a missing `tag`

An image-source PATCH accepts only `source.tag`. A deployment
`source_ref` can override the tag for that deployment. The override uses
the same tag grammar. Rollback is unchanged.

### 4.6 Read templates, then request a template service

These examples use the app group and environment from section 4.3,
after both reach ACTIVE with engine references. Always pass
stack_id on both catalog reads. No list pagination or fields selection
is available here. The engine version must match the packaged catalog;
there is no fallback to a nearby version. All catalog templates are
selectable and creatable subject to project authorization. The template
index supplies named-volume accounting metadata only.

```text
GET /v1/templates?stack_id=5a814ad0-d4a5-4480-97cd-aec1e3f8b847
```

Request body: none.

Response: **200**.

```json
{
  "templates": [
    {
      "category": "cms",
      "description": "WordPress is open source software you can use to create a beautiful website, blog, or app.",
      "http_port": 80,
      "id": "wordpress-with-mariadb",
      "logo": "svgs/wordpress.svg",
      "name": "wordpress-with-mariadb",
      "ports": [
        80
      ],
      "required_env": [
        "SERVICE_PASSWORD_ROOT",
        "SERVICE_PASSWORD_WORDPRESS",
        "SERVICE_USER_WORDPRESS"
      ],
      "tags": [
        "blog",
        "cms",
        "content",
        "management",
        "mariadb"
      ]
    }
  ]
}
```

```text
GET /v1/templates/wordpress-with-mariadb?stack_id=5a814ad0-d4a5-4480-97cd-aec1e3f8b847
```

Request body: none.

Response: **200**.

```json
{
  "template": {
    "category": "cms",
    "description": "WordPress is open source software you can use to create a beautiful website, blog, or app.",
    "http_port": 80,
    "id": "wordpress-with-mariadb",
    "logo": "svgs/wordpress.svg",
    "name": "wordpress-with-mariadb",
    "ports": [
      80
    ],
    "required_env": [
      "SERVICE_PASSWORD_ROOT",
      "SERVICE_PASSWORD_WORDPRESS",
      "SERVICE_USER_WORDPRESS"
    ],
    "tags": [
      "blog",
      "cms",
      "content",
      "management",
      "mariadb"
    ]
  }
}
```

Use the exact returned template ID. required_env contains names, not
values; SERVICE_* entries can be generated by the app engine. Do not invent
credentials for those names. Both source object syntax below and the
string `template:wordpress-with-mariadb` are accepted.

```text
POST /v1/services
```

Request body:

```json
{
  "service": {
    "appgroup_id": "0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe",
    "environment_id": "b532f5c6-028a-4233-9541-061cd5857a75",
    "name": "blog",
    "server_id": "2b3f2ad6-8d5d-4116-91ac-090a26a0be3c",
    "source": {
      "type": "template",
      "template_id": "wordpress-with-mariadb"
    },
    "deploy": false
  }
}
```

Response: **202**.

```json
{
  "service": {
    "id": "3d957ab7-6de2-4f86-be1d-41137ebc680f",
    "project_id": "project",
    "stack_id": "5a814ad0-d4a5-4480-97cd-aec1e3f8b847",
    "environment_id": "b532f5c6-028a-4233-9541-061cd5857a75",
    "template_id": "wordpress-with-mariadb",
    "name": "blog",
    "description": null,
    "server_id": "2b3f2ad6-8d5d-4116-91ac-090a26a0be3c",
    "ports": "80",
    "replicas": 1,
    "cpu_limit": null,
    "memory_limit_mb": null,
    "healthcheck_path": "/",
    "health_check": "http",
    "health_check_port": 80,
    "auto_deploy": false,
    "status": "PENDING",
    "current_deployment_id": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "task_owner": "PROVISIONING",
    "task_started_at": "2026-10-22T12:00:00Z",
    "reconcile_fault": null,
    "health_check_path": "/",
    "appgroup_id": "0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe",
    "kind": "service",
    "pending_changes": false,
    "source": {
      "type": "template",
      "repo_url": null,
      "branch": "main",
      "build_pack": null,
      "dockerfile_path": null,
      "compose_path": null,
      "base_directory": null
    },
    "default_url": "https://blog-3d95.203-0-113-25.apps.example.com",
    "dns_records": [
      {
        "type": "A",
        "name": "blog-3d95.203-0-113-25.apps.example.com",
        "value": "203.0.113.25",
        "purpose": "routing"
      }
    ],
    "default_domain_enabled": true,
    "primary_url": "https://blog-3d95.203-0-113-25.apps.example.com",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/3d957ab7-6de2-4f86-be1d-41137ebc680f"
      }
    ]
  }
}
```

This 202 proves catalog and parent validation plus persistence. The
response includes read-only template_id and both parent IDs. The source
projection contains type and Git-shaped nullable fields. The conductor
uses the recorded parent engine references; poll the service for completion.
This example sets deploy to false, so completed create leaves it STOPPED.
Template env values are runtime-only. Template named volumes are accounted
as storage on the selected worker; the worker needs a Docker disk with
enough free capacity.

### 4.7 Custom domains and HTTPS

Read default_url and dns_records on the service. A configured default
URL can be returned before any DNS, floating IP, or TLS work succeeds.
A domain FQDN must be lower-case DNS syntax; ports, schemes, wildcards,
and underscores are not accepted. A custom FQDN is unique across projects.
An optional auto_dns true selects designate; false selects manual.
The optional `tls` mode is `letsencrypt` (the default) or `none`, available
since 1.2. The `primary` flag selects the primary custom domain since 1.23.

```text
POST /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/domains
```

Request body:

```json
{
  "domain": {
    "fqdn": "www.example.com",
    "auto_dns": false
  }
}
```

Response: **201**.

```json
{
  "domain": {
    "id": "8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e",
    "project_id": "project",
    "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "fqdn": "www.example.com",
    "verified_at": null,
    "tls": "letsencrypt",
    "auto_dns": false,
    "dns_mode": "manual",
    "status": "PENDING_VERIFICATION",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": null,
    "primary": false,
    "dns_records": [
      {
        "type": "TXT",
        "name": "_coast-challenge.www.example.com",
        "value": "example-dns-challenge",
        "purpose": "verification"
      },
      {
        "type": "CNAME",
        "name": "www.example.com",
        "value": "web-bbf1.apps.example.com",
        "purpose": "routing-alternative"
      }
    ],
    "zone_id": null,
    "managed_records": [],
    "record_type": "A",
    "status_reason": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/domains/8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e"
      }
    ]
  }
}
```

Show the returned TXT verification record exactly. It is the only public
location of the challenge value; there is no verification_token field.
Show the A/AAAA routing record when a server floating IP is known.
A CNAME is a routing alternative to the default service host, not an
extra record to add alongside an A at the same name. The noop worker has
no floating IP, so this example contains only TXT and CNAME.
After the user has added the records, submit verify and poll the domain.

```text
POST /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/domains/8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e/action
```

Request body:

```json
{
  "verify": {}
}
```

Response: **202**.

```json
{
  "domain": {
    "id": "8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e",
    "project_id": "project",
    "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "fqdn": "www.example.com",
    "verified_at": null,
    "tls": "letsencrypt",
    "auto_dns": false,
    "dns_mode": "manual",
    "status": "PENDING_VERIFICATION",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": null,
    "primary": false,
    "dns_records": [
      {
        "type": "TXT",
        "name": "_coast-challenge.www.example.com",
        "value": "example-dns-challenge",
        "purpose": "verification"
      },
      {
        "type": "CNAME",
        "name": "www.example.com",
        "value": "web-bbf1.apps.example.com",
        "purpose": "routing-alternative"
      }
    ],
    "zone_id": null,
    "managed_records": [],
    "record_type": "A",
    "status_reason": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/domains/8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e"
      }
    ]
  }
}
```

```text
GET /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/domains/8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e
```

Request body: none.

Response: **200**.

```json
{
  "domain": {
    "id": "8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e",
    "project_id": "project",
    "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "fqdn": "www.example.com",
    "verified_at": "2026-10-22T12:00:00Z",
    "tls": "letsencrypt",
    "auto_dns": false,
    "dns_mode": "manual",
    "status": "VERIFIED",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "primary": false,
    "dns_records": [
      {
        "type": "TXT",
        "name": "_coast-challenge.www.example.com",
        "value": "example-dns-challenge",
        "purpose": "verification"
      },
      {
        "type": "CNAME",
        "name": "www.example.com",
        "value": "web-bbf1.apps.example.com",
        "purpose": "routing-alternative"
      }
    ],
    "zone_id": null,
    "managed_records": [],
    "record_type": "A",
    "status_reason": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/domains/8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e"
      }
    ]
  }
}
```

The verify response can be VERIFIED if the synchronous TXT check succeeds;
otherwise it is PENDING_VERIFICATION. Poll the domain. The
conductor moves it through this path:

PENDING_VERIFICATION -> VERIFIED -> PENDING_DNS -> ACTIVE

Show PENDING_VERIFICATION as "Waiting for the TXT record."
Show VERIFIED as "Publishing the domain." The sample above is this poll.
Show PENDING_DNS as "Waiting for DNS and HTTPS."
Show ACTIVE as "HTTPS ready."
Show ERROR as "Needs attention."

A TXT timeout or a routing and TLS timeout sets ERROR. Read the activity
message. It is a fixed sentence and does not include the challenge value.
Offer verify from PENDING_VERIFICATION or ERROR. From ERROR, the API
re-enters PENDING_VERIFICATION before checking TXT synchronously; a match
returns VERIFIED. The conductor continues the checks asynchronously.
Do not show HTTPS as ready before ACTIVE. An ACTIVE service is not proof
that a custom domain is ready. Verify from ACTIVE or PENDING_DNS returns
409. Do not offer verify in those states. The poll interval is
`[domains] poll_interval` (30 seconds). TXT waits up to 24 hours.
Routing and TLS wait up to 1 hour.

**Remove a custom domain**

```text
DELETE /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/domains/8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e
```

Request body: none.

Response: **202**.

```json
{
  "domain": {
    "id": "8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e",
    "project_id": "project",
    "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "fqdn": "www.example.com",
    "verified_at": "2026-10-22T12:00:00Z",
    "tls": "letsencrypt",
    "auto_dns": false,
    "dns_mode": "manual",
    "status": "VERIFIED",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "primary": false,
    "dns_records": [
      {
        "type": "TXT",
        "name": "_coast-challenge.www.example.com",
        "value": "example-dns-challenge",
        "purpose": "verification"
      },
      {
        "type": "CNAME",
        "name": "www.example.com",
        "value": "web-bbf1.apps.example.com",
        "purpose": "routing-alternative"
      }
    ],
    "zone_id": null,
    "managed_records": [],
    "record_type": "A",
    "status_reason": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/domains/8f8c53e0-8735-47e5-b5b9-77d8df1a3d0e"
      }
    ]
  }
}
```

Delete returns the current domain status, not DELETING. Poll for 404.
Removing a domain is not proof that user-managed external DNS was removed.

### 4.8 Environment variables and secret masking

Use the service env endpoint, not the app group environment endpoint.
The response maps each key to `value`, `secret`, `version` and `build_time`.
Secret values are null for every role, including admin. Null in a read
means “stored but hidden,” not “delete this key.” Never send that masked
read value back as a new write value.

All env values, including non-secret ones, are stored in Barbican in the
service project. The Coast database and frozen deployment specifications
hold references and versions. The conductor resolves the selected versions
in memory and sends them to the app engine over management mTLS. For apps,
`build_time` controls build exposure; runtime exposure remains enabled.
Template variables are runtime-only. The Secret flag makes values write-only
through Coast reads and masks them in returned observations and logs; it
does not prevent the workload or a build given that value from reading it.
App env changes require a deployment to change the running workload.
For template services with a deployed engine reference, env changes also
queue runtime synchronization with the app engine.

```text
GET /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/env
```

Request body: none.

Response: **200**.

```json
{
  "env": {
    "MODE": {
      "value": "production",
      "secret": false,
      "version": 1,
      "build_time": true
    },
    "PASSWORD": {
      "value": null,
      "secret": true,
      "version": 1,
      "build_time": true
    }
  }
}
```

PATCH merges keys. A null key deletes it. An omitted secret/build_time
flag on an existing key keeps that flag. New keys default `secret` to
false. Since 1.6, app env vars default `build_time` to true; template env
vars are runtime-only. The value field is required and must be a string.

```text
PATCH /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/env
```

Request body:

```json
{
  "env": {
    "PASSWORD": {
      "value": "example-replacement"
    },
    "MODE": null
  }
}
```

Response: **200**.

```json
{
  "env": {
    "PASSWORD": {
      "value": null,
      "secret": true,
      "version": 2,
      "build_time": true
    }
  }
}
```

PUT replaces the entire map: omitted keys are deleted. Omitted flags
reset to their defaults (`secret: false`, and `build_time: true` for apps
at 1.6+, and false for templates). Use PATCH for an editor that cannot reload masked secrets.
Both writes return 200; changes set `X-Coast-Redeploy-Required: true`.
Neither starts a deployment. Template env changes also queue runtime
synchronization when a deployed engine reference exists. The response returns `build_time`; preserve it when
editing existing variables.

```text
PUT /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/env
```

Request body:

```json
{
  "env": {
    "MODE": {
      "value": "production"
    },
    "PASSWORD": {
      "value": "example-only",
      "secret": true,
      "build_time": true
    }
  }
}
```

Response: **200**.

```json
{
  "env": {
    "MODE": {
      "value": "production",
      "secret": false,
      "version": 1,
      "build_time": true
    },
    "PASSWORD": {
      "value": null,
      "secret": true,
      "version": 3,
      "build_time": true
    }
  }
}
```

Show “Saved; deployment required.” Start a new deployment to apply changes.
Env variable names match `[A-Za-z_][A-Za-z0-9_]*` and are at most 255
characters. Never put secret entry values into analytics or support logs.

### 4.9 Service logs and deployment logs

Logs are JSON polling endpoints, not streams. Service logs accept tail
(integer >= 0) and since (ISO 8601 time). Use a UTC time. No other query
parameter is accepted. tail defaults to 200 in the real log handler;
tail 0 returns no lines. Deployment log accepts no query parameters.
Noop returns empty line lists, even after successful deployment.
A live read returns 503 when the engine is unreachable or the engine
rejects the Coast credential. `faultstring` is `Service Unavailable`.
Do not show the response body as a diagnostic. Retry the read.
Another engine fault returns 500.

```text
GET /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/logs?tail=200&since=2026-10-09T14:06:01Z
```

Request body: none.

Response: **200**.

```json
{
  "logs": {
    "lines": []
  }
}
```

```text
GET /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments/89591377-8bc9-42e4-8496-e28bce24661c/log
```

Request body: none.

Response: **200**.

```json
{
  "log": {
    "deployment_id": "89591377-8bc9-42e4-8496-e28bce24661c",
    "complete": false,
    "lines": []
  }
}
```

Live line objects are produced from the driver LogLine fields ts, stream,
and line. Render line text as text, never HTML. A deployment log
includes build output that the engine marks hidden. The line fields
stay ts, stream, and line. A hidden build entry with no output text
is the line "Build step in progress." Logs can contain values
printed by tenant code; treat them as sensitive data. The log complete
flag is not a substitute for the deployment status. Use the deployment
resource for success/failure and cancel eligibility.

### 4.10 Deploy again, list history, and roll back

Create a deployment with a deployment envelope. The object can be empty;
source_ref then uses the source branch. Only one deployment can hold the
service task. A second create while busy returns 409.

```text
POST /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments
```

Request body:

```json
{
  "deployment": {
    "source_ref": "release"
  }
}
```

Response: **202**.

```json
{
  "deployment": {
    "id": "f22f6fc0-6d38-455e-a460-05c79a56a14b",
    "project_id": "project",
    "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "number": 2,
    "source_ref": "release",
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "trigger": "api",
    "rollback_of": null,
    "status": "PENDING",
    "fault": null,
    "started_at": null,
    "finished_at": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": null,
    "resolved_ref": null,
    "runtime_log": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments/f22f6fc0-6d38-455e-a460-05c79a56a14b"
      }
    ]
  }
}
```

```text
GET /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments?sort_key=number&sort_dir=desc
```

Request body: none.

Response: **200**.

```json
{
  "deployments": [
    {
      "id": "f22f6fc0-6d38-455e-a460-05c79a56a14b",
      "project_id": "project",
      "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
      "number": 2,
      "source_ref": "release",
      "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
      "trigger": "api",
      "rollback_of": null,
      "status": "ACTIVE",
      "fault": null,
      "started_at": null,
      "finished_at": null,
      "created_at": "2026-10-22T12:00:00Z",
      "updated_at": "2026-10-22T12:00:00Z",
      "resolved_ref": null,
      "runtime_log": null,
      "links": [
        {
          "rel": "self",
          "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments/f22f6fc0-6d38-455e-a460-05c79a56a14b"
        }
      ]
    },
    {
      "id": "89591377-8bc9-42e4-8496-e28bce24661c",
      "project_id": "project",
      "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
      "number": 1,
      "source_ref": "main",
      "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
      "trigger": "api",
      "rollback_of": null,
      "status": "SUPERSEDED",
      "fault": null,
      "started_at": null,
      "finished_at": null,
      "created_at": "2026-10-22T12:00:00Z",
      "updated_at": "2026-10-22T12:00:00Z",
      "resolved_ref": "0000000000000000000000000000000000000000",
      "runtime_log": null,
      "links": [
        {
          "rel": "self",
          "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments/89591377-8bc9-42e4-8496-e28bce24661c"
        }
      ]
    }
  ],
  "links": []
}
```

To roll back, select an older deployment with status ACTIVE, SUPERSEDED,
or ROLLED_BACK and number less than the current deployment's number.
A current or failed deployment is not a valid rollback target.
The response is a **new** deployment row with a new ID and number,
trigger rollback, and rollback_of equal to the selected target ID.
It copies the frozen target specification and secret versions, and uses
resolved_ref when available, otherwise source_ref. Do not replace the
old row in the UI or assume the selected deployment itself becomes current.

```text
POST /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments/89591377-8bc9-42e4-8496-e28bce24661c/action
```

Request body:

```json
{
  "rollback": {}
}
```

Response: **202**.

```json
{
  "deployment": {
    "id": "42b61340-af30-4fa9-82dd-90726dc9f11d",
    "project_id": "project",
    "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "number": 3,
    "source_ref": "0000000000000000000000000000000000000000",
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "trigger": "rollback",
    "rollback_of": "89591377-8bc9-42e4-8496-e28bce24661c",
    "status": "PENDING",
    "fault": null,
    "started_at": null,
    "finished_at": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": null,
    "resolved_ref": null,
    "runtime_log": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/deployments/42b61340-af30-4fa9-82dd-90726dc9f11d"
      }
    ]
  }
}
```

Cancel uses POST to the latest in-flight deployment's action URL with
one cancel key and an empty object. The API sets that deployment to
CANCELLED immediately but keeps the service task until conductor cleanup.
Only PENDING, BUILDING, or DEPLOYING can transition to CANCELLED; a
second cancel returns 409. Do not delete deployment history.

**Cancel the latest deployment (controller fixture)**

```text
POST /v1/services/ba9a7b9a-9987-418a-af57-2c50d58454fb/deployments/72ce1873-4e1d-4a7d-a419-bd5dc593bbdf/action
```

Request body:

```json
{
  "cancel": {}
}
```

Response: **202**.

```json
{
  "deployment": {
    "id": "72ce1873-4e1d-4a7d-a419-bd5dc593bbdf",
    "project_id": "project",
    "service_id": "ba9a7b9a-9987-418a-af57-2c50d58454fb",
    "number": 2,
    "source_ref": null,
    "server_id": "2b3f2ad6-8d5d-4116-91ac-090a26a0be3c",
    "trigger": "api",
    "rollback_of": null,
    "status": "CANCELLED",
    "fault": null,
    "started_at": null,
    "finished_at": null,
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "resolved_ref": null,
    "runtime_log": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/ba9a7b9a-9987-418a-af57-2c50d58454fb/deployments/72ce1873-4e1d-4a7d-a419-bd5dc593bbdf"
      }
    ]
  }
}
```

### 4.11 Start, stop, restart, and scale

Start requires STOPPED. Stop and restart require ACTIVE. Each action
accepts exactly one key with an empty object. A busy service returns 409.
The 202 response keeps its old status. Poll before showing the desired
state; restart needs the progress treatment in section 3.

```text
POST /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/action
```

Request body:

```json
{
  "stop": {}
}
```

Response: **202**.

```json
{
  "service": {
    "id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "template_id": null,
    "name": "web",
    "description": null,
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "ports": "8080",
    "replicas": 1,
    "cpu_limit": null,
    "memory_limit_mb": null,
    "healthcheck_path": null,
    "health_check": "none",
    "health_check_port": 8080,
    "auto_deploy": false,
    "status": "ACTIVE",
    "current_deployment_id": "f22f6fc0-6d38-455e-a460-05c79a56a14b",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "task_owner": "stop",
    "task_started_at": "2026-10-22T12:00:00Z",
    "reconcile_fault": null,
    "health_check_path": "/",
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "kind": "app",
    "pending_changes": false,
    "source": {
      "type": "git",
      "repo_url": "https://example.com/web.git",
      "branch": "main",
      "build_pack": "dockerfile",
      "dockerfile_path": "Dockerfile",
      "compose_path": null,
      "base_directory": null
    },
    "default_url": "https://web-bbf1.apps.example.com",
    "dns_records": [
      {
        "type": "TXT",
        "name": "_coast-challenge.www.example.com",
        "value": "example-dns-challenge",
        "purpose": "verification"
      },
      {
        "type": "CNAME",
        "name": "www.example.com",
        "value": "web-bbf1.apps.example.com",
        "purpose": "routing-alternative"
      }
    ],
    "default_domain_enabled": true,
    "primary_url": "https://web-bbf1.apps.example.com",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0"
      }
    ]
  }
}
```

```text
POST /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/action
```

Request body:

```json
{
  "start": {}
}
```

Response: **202**.

```json
{
  "service": {
    "id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "template_id": null,
    "name": "web",
    "description": null,
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "ports": "8080",
    "replicas": 1,
    "cpu_limit": null,
    "memory_limit_mb": null,
    "healthcheck_path": null,
    "health_check": "none",
    "health_check_port": 8080,
    "auto_deploy": false,
    "status": "STOPPED",
    "current_deployment_id": "f22f6fc0-6d38-455e-a460-05c79a56a14b",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "task_owner": "start",
    "task_started_at": "2026-10-22T12:00:00Z",
    "reconcile_fault": null,
    "health_check_path": "/",
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "kind": "app",
    "pending_changes": false,
    "source": {
      "type": "git",
      "repo_url": "https://example.com/web.git",
      "branch": "main",
      "build_pack": "dockerfile",
      "dockerfile_path": "Dockerfile",
      "compose_path": null,
      "base_directory": null
    },
    "default_url": "https://web-bbf1.apps.example.com",
    "dns_records": [
      {
        "type": "TXT",
        "name": "_coast-challenge.www.example.com",
        "value": "example-dns-challenge",
        "purpose": "verification"
      },
      {
        "type": "CNAME",
        "name": "www.example.com",
        "value": "web-bbf1.apps.example.com",
        "purpose": "routing-alternative"
      }
    ],
    "default_domain_enabled": true,
    "primary_url": "https://web-bbf1.apps.example.com",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0"
      }
    ]
  }
}
```

```text
POST /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0/action
```

Request body:

```json
{
  "restart": {}
}
```

Response: **202**.

```json
{
  "service": {
    "id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "template_id": null,
    "name": "web",
    "description": null,
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "ports": "8080",
    "replicas": 1,
    "cpu_limit": null,
    "memory_limit_mb": null,
    "healthcheck_path": null,
    "health_check": "none",
    "health_check_port": 8080,
    "auto_deploy": false,
    "status": "ACTIVE",
    "current_deployment_id": "f22f6fc0-6d38-455e-a460-05c79a56a14b",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "task_owner": "restart",
    "task_started_at": "2026-10-22T12:00:00Z",
    "reconcile_fault": null,
    "health_check_path": "/",
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "kind": "app",
    "pending_changes": false,
    "source": {
      "type": "git",
      "repo_url": "https://example.com/web.git",
      "branch": "main",
      "build_pack": "dockerfile",
      "dockerfile_path": "Dockerfile",
      "compose_path": null,
      "base_directory": null
    },
    "default_url": "https://web-bbf1.apps.example.com",
    "dns_records": [
      {
        "type": "TXT",
        "name": "_coast-challenge.www.example.com",
        "value": "example-dns-challenge",
        "purpose": "verification"
      },
      {
        "type": "CNAME",
        "name": "www.example.com",
        "value": "web-bbf1.apps.example.com",
        "purpose": "routing-alternative"
      }
    ],
    "default_domain_enabled": true,
    "primary_url": "https://web-bbf1.apps.example.com",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0"
      }
    ]
  }
}
```

The following request does not scale a service. The schema rejects
replicas in PATCH, and create restricts replicas to 1. There is no public
scale endpoint. Resource-limit changes are settings, not replica scaling.

```text
PATCH /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0
```

Request body:

```json
{
  "service": {
    "replicas": 2
  }
}
```

Response: **400**.

```json
{
  "faultstring": "Invalid request.",
  "faultcode": "Client",
  "debuginfo": null
}
```

### 4.12 Volumes, export, download, and renewal

An explicit data volume can be created under a stack. Tenant create only
allows purpose data; mount_path defaults to /data. This is a volume record,
not a service mount API. The example has no server association.

```text
POST /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/volumes
```

Request body:

```json
{
  "volume": {
    "name": "saved-data",
    "size_gb": 2
  }
}
```

Response: **202**.

```json
{
  "volume": {
    "id": "6d54fbf5-505a-4790-a978-3ae831d8d2f7",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "server_id": null,
    "name": "saved-data",
    "size_gb": 2,
    "volume_type": null,
    "mount_path": "/data",
    "purpose": "data",
    "status": "CREATING",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/volumes/6d54fbf5-505a-4790-a978-3ae831d8d2f7"
      }
    ]
  }
}
```

```text
GET /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/volumes/6d54fbf5-505a-4790-a978-3ae831d8d2f7
```

Request body: none.

Response: **200**.

```json
{
  "volume": {
    "id": "6d54fbf5-505a-4790-a978-3ae831d8d2f7",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "server_id": null,
    "name": "saved-data",
    "size_gb": 2,
    "volume_type": null,
    "mount_path": "/data",
    "purpose": "data",
    "status": "AVAILABLE",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/volumes/6d54fbf5-505a-4790-a978-3ae831d8d2f7"
      }
    ]
  }
}
```

To export, require an ACTIVE stack and the export-create policy.
Omit volume_ids or set it to null to select all data/docker volumes.
An explicit list must be nonempty and unique. Volumes must belong to
that stack and be AVAILABLE or IN_USE. An empty selection is 400.
Create holds the stack RECONCILING task and leaves its status ACTIVE.

```text
POST /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/exports
```

Request body:

```json
{
  "export": {
    "volume_ids": [
      "6d54fbf5-505a-4790-a978-3ae831d8d2f7"
    ]
  }
}
```

Response: **202**.

```json
{
  "export": {
    "id": "15bfbb7b-d479-4625-b933-3a5c268f2a58",
    "status": "PENDING",
    "volume_ids": [
      "6d54fbf5-505a-4790-a978-3ae831d8d2f7"
    ],
    "artifacts": [],
    "object_expires_at": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/exports/15bfbb7b-d479-4625-b933-3a5c268f2a58"
      }
    ]
  }
}
```

```text
GET /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/exports/15bfbb7b-d479-4625-b933-3a5c268f2a58
```

Request body: none.

Response: **200**.

```json
{
  "export": {
    "id": "15bfbb7b-d479-4625-b933-3a5c268f2a58",
    "status": "ACTIVE",
    "volume_ids": [
      "6d54fbf5-505a-4790-a978-3ae831d8d2f7"
    ],
    "artifacts": [],
    "object_expires_at": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/exports/15bfbb7b-d479-4625-b933-3a5c268f2a58"
      }
    ]
  }
}
```

This noop completion does not create an archive, object expiry, or URL.
Renewal therefore returns 409, not a successful download URL.

```text
POST /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/exports/15bfbb7b-d479-4625-b933-3a5c268f2a58/action
```

Request body:

```json
{
  "renew_url": {}
}
```

Response: **409**.

```json
{
  "faultstring": "Resource conflict.",
  "faultcode": "Client",
  "debuginfo": null
}
```

#### Swift download response (functional storage fixture)

This separate example used the Swift export functional test described
above. The object and URL values are fixtures. In a real supported Swift
or RGW deployment, render the archive only after ACTIVE and temporary
snapshot/clone cleanup. The S3 backend uses the same `download_url`
field with a SigV4 presigned GET. Conductor-relay upload is not implemented.

**Accept a Swift export**

```text
POST /v1/stacks/af53612d-fc4a-48a2-a18e-8292d8cf1987/exports
```

Request body:

```json
{
  "export": {}
}
```

Response: **202**.

```json
{
  "export": {
    "id": "bd36c581-3ec8-497f-aee6-412936e68bc6",
    "status": "PENDING",
    "volume_ids": [
      "c8e24b2b-7a60-48e5-8afb-882131f46efd"
    ],
    "artifacts": [],
    "object_expires_at": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/af53612d-fc4a-48a2-a18e-8292d8cf1987/exports/bd36c581-3ec8-497f-aee6-412936e68bc6"
      }
    ]
  }
}
```

**Read an archive as a manager**

```text
GET /v1/stacks/af53612d-fc4a-48a2-a18e-8292d8cf1987/exports/bd36c581-3ec8-497f-aee6-412936e68bc6
```

Request body: none.

Response: **200**.

```json
{
  "export": {
    "id": "bd36c581-3ec8-497f-aee6-412936e68bc6",
    "status": "ACTIVE",
    "volume_ids": [
      "c8e24b2b-7a60-48e5-8afb-882131f46efd"
    ],
    "artifacts": [
      {
        "volume_id": "c8e24b2b-7a60-48e5-8afb-882131f46efd",
        "name": "data.tar.gz",
        "size_bytes": 42,
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "download_url": "https://objects.example.com/archive?temp_url_sig=REDACTED",
        "expires_at": "2026-10-22T13:00:00Z"
      }
    ],
    "object_expires_at": "2026-10-25T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/af53612d-fc4a-48a2-a18e-8292d8cf1987/exports/bd36c581-3ec8-497f-aee6-412936e68bc6"
      }
    ]
  }
}
```

The manager/admin renew policy also controls URL visibility on GET.
A member can read metadata but does not receive download_url or
expires_at. Reader alone cannot read exports. Compare the same resource
under member permission:

```text
GET /v1/stacks/af53612d-fc4a-48a2-a18e-8292d8cf1987/exports/bd36c581-3ec8-497f-aee6-412936e68bc6
```

Request body: none.

Response: **200**.

```json
{
  "export": {
    "id": "bd36c581-3ec8-497f-aee6-412936e68bc6",
    "status": "ACTIVE",
    "volume_ids": [
      "c8e24b2b-7a60-48e5-8afb-882131f46efd"
    ],
    "artifacts": [
      {
        "volume_id": "c8e24b2b-7a60-48e5-8afb-882131f46efd",
        "name": "data.tar.gz",
        "size_bytes": 42,
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    ],
    "object_expires_at": "2026-10-25T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/af53612d-fc4a-48a2-a18e-8292d8cf1987/exports/bd36c581-3ec8-497f-aee6-412936e68bc6"
      }
    ]
  }
}
```

Treat download_url as a bearer capability. Do not store it in local
storage, logs, analytics, or public page URLs. Use it for the download
only; do not send the Keystone token to the object-storage host.
Show name, size_bytes, sha256, URL expires_at, and object_expires_at.
URL expiry and object expiry are separate. Verify the downloaded archive
checksum when the client can do so. The UI cannot validate this fixture's
checksum against a real archive.

Renew with exactly renew_url and an empty object. Renewal is synchronous
200 and returns the same export shape. It cannot extend object retention.
The export must be ACTIVE, with future object_expires_at, and usable
signed URLs must be available for a non-noop stack. Otherwise expect 409.
An engine connection or credential fault returns 503. `faultstring` is
`Service Unavailable`. Retry the renewal. Do not show the body as a
diagnostic. Another fault returns 500.

**Renew while the retained object exists**

```text
POST /v1/stacks/af53612d-fc4a-48a2-a18e-8292d8cf1987/exports/bd36c581-3ec8-497f-aee6-412936e68bc6/action
```

Request body:

```json
{
  "renew_url": {}
}
```

Response: **200**.

```json
{
  "export": {
    "id": "bd36c581-3ec8-497f-aee6-412936e68bc6",
    "status": "ACTIVE",
    "volume_ids": [
      "c8e24b2b-7a60-48e5-8afb-882131f46efd"
    ],
    "artifacts": [
      {
        "volume_id": "c8e24b2b-7a60-48e5-8afb-882131f46efd",
        "name": "data.tar.gz",
        "size_bytes": 42,
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "download_url": "https://objects.example.com/archive?temp_url_sig=REDACTED",
        "expires_at": "2026-10-22T13:00:00Z"
      }
    ],
    "object_expires_at": "2026-10-25T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/af53612d-fc4a-48a2-a18e-8292d8cf1987/exports/bd36c581-3ec8-497f-aee6-412936e68bc6"
      }
    ]
  }
}
```

Offer renewal if the link has expired but the object has not. Offer a new
export, not renewal, after object expiry. Missing signed URL fields mean
no usable download capability; do not construct a storage URL yourself.

**Delete the noop export**

```text
DELETE /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/exports/15bfbb7b-d479-4625-b933-3a5c268f2a58
```

Request body: none.

Response: **202**.

```json
{
  "export": {
    "id": "15bfbb7b-d479-4625-b933-3a5c268f2a58",
    "status": "DELETING",
    "volume_ids": [
      "6d54fbf5-505a-4790-a978-3ae831d8d2f7"
    ],
    "artifacts": [],
    "object_expires_at": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969/exports/15bfbb7b-d479-4625-b933-3a5c268f2a58"
      }
    ]
  }
}
```

### 4.13 Delete and offboard

Use explicit confirmation for destructive actions. Remove custom domains
and services first. The approved order is service, environment,
app group, then stack. A parent delete does not cascade. A live child
returns 409. There is no force option.

```text
DELETE /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0
```

Request body: none.

Response: **202**.

```json
{
  "service": {
    "id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
    "project_id": "project",
    "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "template_id": null,
    "name": "web",
    "description": null,
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "ports": "8080",
    "replicas": 1,
    "cpu_limit": null,
    "memory_limit_mb": null,
    "healthcheck_path": null,
    "health_check": "none",
    "health_check_port": 8080,
    "auto_deploy": false,
    "status": "DELETING",
    "current_deployment_id": "f22f6fc0-6d38-455e-a460-05c79a56a14b",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "task_owner": "DELETING",
    "task_started_at": "2026-10-22T12:00:00Z",
    "reconcile_fault": null,
    "health_check_path": "/",
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "kind": "app",
    "pending_changes": false,
    "source": {
      "type": "git",
      "repo_url": "https://example.com/web.git",
      "branch": "main",
      "build_pack": "dockerfile",
      "dockerfile_path": "Dockerfile",
      "compose_path": null,
      "base_directory": null
    },
    "default_url": "https://web-bbf1.apps.example.com",
    "dns_records": [
      {
        "type": "TXT",
        "name": "_coast-challenge.www.example.com",
        "value": "example-dns-challenge",
        "purpose": "verification"
      },
      {
        "type": "CNAME",
        "name": "www.example.com",
        "value": "web-bbf1.apps.example.com",
        "purpose": "routing-alternative"
      }
    ],
    "default_domain_enabled": true,
    "primary_url": "https://web-bbf1.apps.example.com",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0"
      }
    ]
  }
}
```

**Check service removal**

```text
GET /v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0
```

Request body: none.

Response: **404**.

```json
{
  "faultstring": "Resource not found.",
  "faultcode": "Client",
  "debuginfo": null
}
```

DELETE returns 202 with status DELETING. The conductor then removes the
engine object, stores status DELETED, and soft-deletes the row. Poll GET
until the response is 404. A live child returns 409. A stack that is not
ACTIVE returns 409. PENDING and DELETING also return 409. There is no
cascade and no force option. ERROR is the retry when no child remains.

```text
DELETE /v1/environments/b532f5c6-028a-4233-9541-061cd5857a75
```

Request body: none.

Response: **202**.

```json
{
  "environment": {
    "id": "b532f5c6-028a-4233-9541-061cd5857a75",
    "project_id": "project",
    "appgroup_id": "0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe",
    "name": "staging",
    "description": null,
    "status": "DELETING",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/environments/b532f5c6-028a-4233-9541-061cd5857a75"
      }
    ]
  }
}
```

```text
DELETE /v1/appgroups/0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe
```

Request body: none.

Response: **202**.

```json
{
  "appgroup": {
    "id": "0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe",
    "project_id": "project",
    "stack_id": "5a814ad0-d4a5-4480-97cd-aec1e3f8b847",
    "name": "website",
    "description": "Website workloads",
    "status": "DELETING",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/appgroups/0eaf0eeb-c4e9-430a-b8d7-f1d85a21edfe"
      }
    ]
  }
}
```

#### Retained volume cleanup (SERVER-8)

After a real worker/build remove, list the stack volumes. Retained data
normally has server_id null and status AVAILABLE. Keep the storage quota
visible as an estimate until actual cleanup completes. Ask the user to
save required data before permanent deletion.

DELETE uses the exact stack volume ID, with no force, cascade, attachment,
or retention options. Project manager or admin policy is required. Only
`data` purpose is tenant-deletable. Docker, scratch, and controller volumes
return 400 even for admin. There is no DELETED volume status.

Coast returns 409 before acceptance if server_id is set, a mount exists,
or the volume is CREATING, SNAPSHOTTING, or DELETING. It also rejects
unsettled create/extend/snapshot receipts, busy servers, another stack
volume snapshot, an unsettled stack volume extension, and an export that
references this volume unless that export is ACTIVE or EXPIRED. The stack
must be ACTIVE, DEGRADED, or ERROR and have no competing task. A null
server_id alone does not prove that deletion is safe.

The following responses use a separate isolated fixture with a detached
two-GiB data volume. The first DELETE ran while the stack
held RECONCILING. No cloud deletion ran for that rejected request.

```text
DELETE /v1/stacks/be497d22-0933-4709-85db-2c5b7efbd0d9/volumes/a639618e-55c0-464e-88e6-5d11b94785fa
```

Request body: none. Response: **409**.

```json
{
  "faultstring": "Another operation is in progress on this stack. Try again in a moment.",
  "faultcode": "Client",
  "debuginfo": null
}
```

Show “Volume cleanup cannot run while this volume or stack is busy.”
Refresh the volume, stack, and activity. Do not retry the write in a loop.
The generic conflict body does not identify which safety check failed.

After the competing stack task completed, the same DELETE returned
**202** with the following body:

```json
{
  "volume": {
    "id": "a639618e-55c0-464e-88e6-5d11b94785fa",
    "project_id": "project",
    "stack_id": "be497d22-0933-4709-85db-2c5b7efbd0d9",
    "server_id": null,
    "name": "saved-data",
    "size_gb": 2,
    "volume_type": null,
    "mount_path": "/data",
    "purpose": "data",
    "status": "DELETING",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/be497d22-0933-4709-85db-2c5b7efbd0d9/volumes/a639618e-55c0-464e-88e6-5d11b94785fa"
      }
    ]
  }
}
```

Acceptance sets volume DELETING and acquires stack RECONCILING. Disable
repeat delete and competing stack actions. Poll the same volume GET at
the intervals in section 3. The production flow uses the service-project
Cinder SDK, not the noop cloud adapter. It checks the exact reference,
Coast ownership metadata, empty attachments, and Cinder state; it also
checks snapshots, clones, and unresolved export dependencies. It requests
ordinary deletion with force and cascade disabled and waits for confirmed
Cinder absence before removing the Coast row and releasing quota once.
This capture checks API acceptance and later reads after fixture completion;
it does not exercise RPC or Cinder deletion.

After completion, GET on the same path returned **404**:

```text
GET /v1/stacks/be497d22-0933-4709-85db-2c5b7efbd0d9/volumes/a639618e-55c0-464e-88e6-5d11b94785fa
```

```json
{
  "faultstring": "Resource not found.",
  "faultcode": "Client",
  "debuginfo": null
}
```

After an accepted delete, this 404 is the successful row-removal signal.
Refresh the volume list and quota estimate. Do not treat an initial 404
on an unknown ID as proof of cleanup. An initial Cinder 404 without prior
verified ownership does not release quota.

A late attachment, dependency, ownership mismatch, timeout, or uncertain
Cinder result sets ERROR, retains the row, reference, and quota, and
releases the stack task. Show “Volume cleanup failed; contact the operator
before retrying.” A manager can submit a new DELETE after the cause is
resolved; all safety checks run again. A crashed operation can remain
DELETING with the stack task held. Operator review and explicit replay
are required; there is no automatic takeover or task expiry.

The activity receipt action is `delete.cleanup`, with outcome start,
success, or failure. Its internal checkpoint message is not returned to
tenants. Notifications use `coast.volume.delete.start`, `.end`, and
`.error`, with stable operation_id. They are refresh hints, not a substitute
for GET. Export of detached retained data remains deferred (SERVER-8a).
See the [SERVER-8 design](design/volume-cleanup.md) and
[implemented lifecycle](lifecycle.md#retained-data-volume-cleanup-server-8).
Do not allow a portal to call force-delete in Cinder.

#### Stack deletion

The stack DELETE body is **unwrapped**, unlike create bodies. It is
optional. Real stack teardown landed in !89. `snapshot_volumes` true
is supported. Before volume delete, Coast creates one Cinder snapshot
for each volume (`force` false), waits until that snapshot is
available, and stores the snapshot id. A retry uses the stored row
and does not create a second snapshot. The snapshots remain after
the volumes are deleted. The delete response does not list those
snapshot ids.

`export_before_delete` true is supported. Coast creates one export
for that delete, or reuses the export from an earlier attempt for
the same stack. Teardown continues only after that export is ACTIVE.
An export failure or timeout sets the stack to ERROR and deletes
nothing. The export row and its Swift objects remain. Normal export
retention still applies. You can set `snapshot_volumes` and
`export_before_delete` together. `snapshot_retention_days` without
`snapshot_volumes` true returns 400 before any write (Q102). The
stack row does not change. An absent `snapshot_retention_days` uses
the operator default inside an accepted request. A value above the
configured maximum returns 400. The following false-option example
is a noop cleanup, not proof of complete real-cloud offboarding.

```text
DELETE /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969
```

Request body:

```json
{
  "snapshot_volumes": false,
  "export_before_delete": false
}
```

Response: **202**.

```json
{
  "stack": {
    "id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
    "project_id": "project",
    "name": "website-stack",
    "description": null,
    "plan": "example-control",
    "engine_version": "<engine version>",
    "status": "DELETING",
    "created_at": "2026-10-22T12:00:00Z",
    "updated_at": "2026-10-22T12:00:00Z",
    "fault": null,
    "default_domain": null,
    "dns_zone_id": null,
    "links": [
      {
        "rel": "self",
        "href": "https://api.example.com/v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969"
      }
    ]
  }
}
```

**Check noop stack removal**

```text
GET /v1/stacks/84b93c26-f107-44cf-8a3d-dacd4e62c969
```

Request body: none.

Response: **404**.

```json
{
  "faultstring": "Resource not found.",
  "faultcode": "Client",
  "debuginfo": null
}
```

Do not report “All cloud resources deleted” from 404 alone. Current noop
stack cleanup can leave service history visible if services were not
removed first. Cloud teardown removes the cloud resources it
owns and keeps the delete snapshots. Export before delete keeps
the export after the stack is deleted. Secrets and network cleanup
still need the operator checks in
[lifecycle](lifecycle.md#stack-delete--offboarding).

Supported snapshot and export request:

```json
{
  "snapshot_volumes": true,
  "snapshot_retention_days": 14,
  "export_before_delete": true
}
```

### 4.14 Open a shell

At microversion 1.9, a project member or admin can open a shell on an
active app or service. A reader cannot. Omit `command` for an
interactive shell. A service uses the same call on
`/v1/services/{service_id}/console`.

```text
POST /v1/apps/{app_id}/console
OpenStack-API-Version: app-hosting 1.28
```

```json
{
  "command": [
    "sh"
  ]
}
```

Response: **201**.

```json
{
  "url": "wss://console.example.com:9610/?token=REDACTED",
  "expires_at": "2026-10-22T12:01:00Z"
}
```

The URL is single-use and expires in about one minute. Connect the
terminal to that websocket. By default, a session closes after 15 minutes
without terminal data in either direction, or after four hours total.
The operator configures these limits with `[console] idle_timeout` and
`[console] max_duration`. The activity log records that the
shell opened and closed. It does not record keystrokes or output. A
workload that is not active returns **409** with faultstring
`The app is not available.`

## 5. Status reference and allowed actions

The transition diagrams below were generated from
`coast/common/states.py:TRANSITIONS`. Each line is an outgoing edge set.
A terminal row has no outgoing edge. Known same-state no-ops are omitted.
The state helper validates edges only; it does not authorize or perform
an action. A state table is not a guarantee that every backend flow exists.

Public environment uses the `app_environment` state key; public service
uses the `service` state key.

### Stack transitions

```text
PENDING -> BUILDING | DELETING | ERROR
BUILDING -> ACTIVE | DELETING | ERROR
ACTIVE -> DEGRADED | DELETING | ERROR | UPGRADING
DEGRADED -> ACTIVE | DELETING | ERROR
UPGRADING -> ACTIVE | DELETING | ERROR
ERROR -> DELETING
DELETING -> DELETED | ERROR
DELETED -> (terminal)
```

### App group transitions

```text
PENDING -> ACTIVE | DELETING | ERROR
ACTIVE -> DELETING
ERROR -> DELETING
DELETING -> DELETED | ERROR
DELETED -> (terminal)
```

### Environment transitions

```text
PENDING -> ACTIVE | DELETING | ERROR
ACTIVE -> DELETING
ERROR -> DELETING
DELETING -> DELETED | ERROR
DELETED -> (terminal)
```

### Server transitions

```text
PENDING -> BUILDING | DELETING | ERROR
BUILDING -> ACTIVE | DELETING | ERROR
ACTIVE -> DELETING | ERROR | FAILOVER | UNHEALTHY
UNHEALTHY -> ACTIVE | DELETING | ERROR | FAILOVER
FAILOVER -> ACTIVE | DELETING | ERROR
ERROR -> DELETING
DELETING -> ERROR
```

### Volume transitions

```text
CREATING -> AVAILABLE | DELETING | ERROR
AVAILABLE -> DELETING | ERROR | IN_USE | SNAPSHOTTING
IN_USE -> AVAILABLE | DELETING | ERROR | SNAPSHOTTING
SNAPSHOTTING -> AVAILABLE | DELETING | ERROR | IN_USE
ERROR -> DELETING
DELETING -> ERROR
```

### Service transitions

```text
PENDING -> ACTIVE | DELETING | ERROR | STOPPED
ACTIVE -> DEGRADED | DELETING | ERROR | STOPPED
STOPPED -> ACTIVE | DELETING | ERROR
DEGRADED -> ACTIVE | DELETING | ERROR | STOPPED
ERROR -> ACTIVE | DELETING | STOPPED
DELETING -> ERROR
```

### Deployment transitions

```text
PENDING -> BUILDING | CANCELLED | DEPLOYING | FAILED
BUILDING -> CANCELLED | DEPLOYING | FAILED
DEPLOYING -> ACTIVE | CANCELLED | FAILED
ACTIVE -> ROLLED_BACK | SUPERSEDED
SUPERSEDED -> (terminal)
FAILED -> (terminal)
CANCELLED -> (terminal)
ROLLED_BACK -> (terminal)
```

### Domain transitions

```text
PENDING_VERIFICATION -> ERROR | VERIFIED
VERIFIED -> ACTIVE | ERROR | PENDING_DNS
PENDING_DNS -> ACTIVE | ERROR
ACTIVE -> ERROR | PENDING_DNS
ERROR -> PENDING_VERIFICATION
```

### Export transitions

```text
PENDING -> DELETING | ERROR | SNAPSHOTTING
SNAPSHOTTING -> DELETING | ERROR | UPLOADING
UPLOADING -> ACTIVE | DELETING | ERROR
ACTIVE -> DELETING | ERROR | EXPIRED
EXPIRED -> DELETING
ERROR -> DELETING
DELETING -> ERROR
```

### Badge and action rules

Text labels are required; do not use color alone. A spinner is a portal
recommendation, not a server field. All actions below also require policy,
a free operation task, valid parent state, and the workflow safety checks.
Read/refresh is available in every visible state. Hide unsupported actions
from the integration-limit table, even if a state edge exists.

| Resource | Status | Badge / spinner | Actions to offer |
|---|---|---|---|
| stack | `PENDING` | In progress / yes | Delete if task free; rename/description |
| stack | `BUILDING` | In progress / yes | Delete if task free; rename/description |
| stack | `ACTIVE` | Ready / no | Delete if task free; add server/export; admin upgrade; rename/description |
| stack | `DEGRADED` | Needs attention / no | Delete if task free; rename/description |
| stack | `UPGRADING` | In progress / yes | Delete if task free; rename/description |
| stack | `ERROR` | Needs attention / no | Delete if task free; rename/description |
| stack | `DELETING` | In progress / yes | Wait; no repeat delete |
| stack | `DELETED` | History / no | No actions; hidden after cleanup |
| app group | `PENDING` | In progress / yes | Rename/description; delete returns 409 |
| app group | `ACTIVE` | Ready / no | Rename/description; delete when no child remains |
| app group | `ERROR` | Needs attention / no | Rename/description; retry delete when no child remains |
| app group | `DELETING` | In progress / yes | Wait; a second delete returns 409 |
| app group | `DELETED` | History / no | No actions; hidden after cleanup |
| environment | `PENDING` | In progress / yes | Rename/description; delete returns 409 |
| environment | `ACTIVE` | Ready / no | Rename/description; delete when no child remains |
| environment | `ERROR` | Needs attention / no | Rename/description; retry delete when no child remains |
| environment | `DELETING` | In progress / yes | Wait; a second delete returns 409 |
| environment | `DELETED` | History / no | No actions; hidden after cleanup |
| server | `PENDING` | In progress / yes | Delete empty worker/build if task free |
| server | `BUILDING` | In progress / yes | Delete empty worker/build if task free |
| server | `ACTIVE` | Ready / no | Delete empty worker/build if task free; admin failover |
| server | `UNHEALTHY` | Needs attention / no | Delete empty worker/build if task free; admin failover |
| server | `FAILOVER` | In progress / yes | Delete empty worker/build if task free |
| server | `ERROR` | Needs attention / no | Delete empty worker/build if task free |
| server | `DELETING` | In progress / yes | Wait; no repeat delete |
| volume | `CREATING` | In progress / yes | Wait; delete returns 409 |
| volume | `AVAILABLE` | Ready / no | Delete detached data if safety gate passes; data extend; data/docker snapshot or export when safe |
| volume | `IN_USE` | Ready / no | Delete only detached data if safety gate passes; data extend; data/docker snapshot or export when safe |
| volume | `SNAPSHOTTING` | In progress / yes | Wait; delete returns 409 |
| volume | `ERROR` | Needs attention / no | Read activity; resolve cause with operator; retry detached data delete if safety gate passes |
| volume | `DELETING` | In progress / yes | Wait; no repeat delete |
| service | `PENDING` | In progress / yes | Settings/env; deploy; delete if task free |
| service | `ACTIVE` | Ready / no | Settings/env; deploy; delete if task free; stop/restart |
| service | `STOPPED` | Stopped / no | Settings/env; deploy; delete if task free; start |
| service | `DEGRADED` | Needs attention / no | Settings/env; deploy; delete if task free; no stop/restart action in current controller |
| service | `ERROR` | Needs attention / no | Settings/env; deploy; delete if task free |
| service | `DELETING` | In progress / yes | Wait; no repeat delete |
| deployment | `PENDING` | In progress / yes | Cancel only if latest and service owns deployment task |
| deployment | `BUILDING` | In progress / yes | Cancel only if latest and service owns deployment task |
| deployment | `DEPLOYING` | In progress / yes | Cancel only if latest and service owns deployment task |
| deployment | `ACTIVE` | Ready / no | Rollback only if older than current |
| deployment | `SUPERSEDED` | History / no | Rollback only if older than current |
| deployment | `FAILED` | Needs attention / no | Read history/log; deploy again through service |
| deployment | `CANCELLED` | History / no | Read history/log; deploy again through service |
| deployment | `ROLLED_BACK` | History / no | Rollback only if older than current |
| domain | `PENDING_VERIFICATION` | Waiting for the TXT record / no | Delete if service task free; verify |
| domain | `VERIFIED` | Publishing the domain / no | Delete if service task free; poll |
| domain | `PENDING_DNS` | Waiting for DNS and HTTPS / no | Delete if service task free; no verify action |
| domain | `ACTIVE` | HTTPS ready / no | Delete if service task free; no verify action |
| domain | `ERROR` | Needs attention / no | Delete if service task free; verify to retry |
| export | `PENDING` | In progress / yes | Delete |
| export | `SNAPSHOTTING` | In progress / yes | Delete |
| export | `UPLOADING` | In progress / yes | Delete |
| export | `ACTIVE` | Ready / no | Delete; download/renew if retained and policy allows |
| export | `EXPIRED` | History / no | Delete; create a new export |
| export | `ERROR` | Needs attention / no | Delete |
| export | `DELETING` | In progress / yes | Wait; no repeat delete |

Volumes have no public busy task indicator. Extend acceptance leaves
status unchanged, so status alone cannot prove resize completion.
Do not allow repeated destructive actions from a spinner. On ERROR/FAILED,
show a support action with the request ID and activity. Stack/server faults
are admin detail only; service/deployment/export faults are not returned.
Do not add a fault field to tenant response handling.

Volume delete acceptance is narrower than the transition diagram above.
The state table still permits delete edges from CREATING and SNAPSHOTTING,
but the API safety gate rejects them with 409. Volumes have no task_state.
A failed cleanup retains quota; successful cleanup removes the row.

Plans, templates, env vars, and events have no public status field.
An event's outcome is not a resource status. There is no public DELETED
status for servers, volumes, services, domains, deployments, or exports.

## 6. Errors and user-facing messages

The API uses HTTP status plus a three-field JSON object. There is no
machine-readable OverQuota type, error-code enum, retryable flag, or
field-level schema-error array. Never use faultcode as the HTTP status.

**Quota error captured from service create**

```text
POST /v1/services
```

Request body:

```json
{
  "service": {
    "name": "other",
    "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
    "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
    "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
    "source": {
      "type": "git",
      "repo_url": "https://example.com/web.git"
    }
  }
}
```

Response: **403**.

```json
{
  "faultstring": "Quota exceeded for resources: ['apps']",
  "faultcode": "Client",
  "debuginfo": null
}
```

| HTTP | Cause / body message | Recommended portal message and next step |
|---|---|---|
| 400 | Invalid request.; malformed version can use Bad Request | “Check the submitted values.” Validate the local form; do not expose secrets in a diagnostic. Unknown fields and wrong content type are rejected. |
| 401 | Unauthorized; Keystone can include WWW-Authenticate | “Your session has expired. Sign in again.” Preserve unsaved non-secret form state. |
| 403 | Not authorized. | “You do not have permission for this action.” Refresh session roles or contact the project manager. |
| 403 | Quota exceeded for resources: ['apps'] (or another resource list) | “The project has reached its quota.” Show limits and a request-more-quota action. Do not parse this as a stable error-type field. |
| 404 | Resource not found. or Not Found | “This resource is not available in the current project.” Refresh the parent/list; never imply ownership by another project. |
| 405 | Method Not Allowed | “This action is not supported.” Check the method and route in the client. Preserve Allow if returned. |
| 406 | Not Acceptable | “This Coast API version is not supported.” Read discovery and use a compatible portal version. |
| 409 | Resource conflict.; invalid state transition; unavailable template catalog; retained-volume safety gate | “This action cannot run in the current state.” Refresh status, history, and parent; explain a duplicate name or catalog issue when known. Do not retry a write in a loop. |
| 500 | Internal Server Error | “Coast could not complete the request. Contact the operator with the request ID.” The server hides exception text and tracebacks. Check state before any write retry. |
| 503 | Service Unavailable. A synchronous engine read or export URL renewal cannot reach the engine, or the engine rejected the Coast credential. | “This is temporarily unavailable. Try again.” Retry a log read or URL renewal. Do not show the response body as a diagnostic. |

For retained-volume DELETE, known attachment, mount, unsettled work,
export dependency, or stack conflicts return 409 before acceptance. The
exact captured body is in section 4.13. Cinder checks run after 202: their
failure is volume ERROR plus failure activity, not a later HTTP error
response to the original request. Preserve quota estimates until cleanup
succeeds. Do not offer force or cascade as a way around a conflict.

Coast exceptions such as InvalidStateTransition are still 409 Client
faults, not a separate error envelope. ServiceConfigError and
CertificateError are internal 500 errors, not tenant configuration forms.
The middleware uses faultcode Client for all 4xx responses, Server for
5xx responses, and debuginfo null. It masks password-like text.
Plain framework/auth errors can use standard HTTP titles; do not require
one exact faultstring for each HTTP status. App group faults do not
echo a failing field value. The generic boundary can wrap other HTTP
errors raised by middleware; reverse proxies can also return non-JSON
errors. Handle an unreadable body safely using status and request ID.
No controller promises a 429 response or Retry-After; honor those if an
operator's proxy supplies them, but do not document them as Coast fields.

## 7. Lists, filters, sorting, and fields

Database-backed lists return the plural envelope and a top-level links
array. The array is empty on the last page. Follow rel next only when it
exists. Keep the authenticated catalog origin/path in the client and do
not forward a token to an unexpected origin in a link.

* limit defaults to `[api] max_limit` (default 1000) and is capped there.
  Zero returns an empty list. Negative or non-integer values return 400.
* marker is a resource ID, not an offset. A missing/foreign marker is 404;
  a marker from another nested parent is also 404.
* sort_key defaults to created_at. sort_dir is asc or desc, default asc.
  The repository adds id as a tie-breaker. No offset or page-number API
  is implemented. Lists are not a transactional snapshot across pages.
* Filters are equality filters combined with AND. There is no substring,
  search, greater-than, repeated-value, or time-range operator.
* fields is a comma-separated projection. links remains present even if
  omitted from fields. Unknown names return 400. fields affects GET, not
  create/action response shapes.
* all_projects=true and explicit project_id filters require ADMIN policy.
  all_projects must be lower-case true/false. An admin nested list still
  resolves its parent in the authenticated project.

### Per-collection scalar filter and sort keys

Only the keys in this table are accepted for filter/sort. Computed
response fields in the next table are projection-only. Datetimes use
ISO 8601; service/deployment/domain/event numeric and boolean fields are
converted where implemented. Boolean filters use true/false.
Do not depend on database-specific implicit conversion for other fields.

| Collection | Scalar equality filter / sort keys |
|---|---|
| stacks | `id`, `project_id`, `name`, `description`, `plan`, `driver`, `engine_version`, `status`, `created_at`, `updated_at` |
| servers | `id`, `project_id`, `stack_id`, `name`, `kind`, `role`, `status`, `floating_ip`, `created_at`, `updated_at` |
| volumes | `id`, `project_id`, `stack_id`, `server_id`, `name`, `size_gb`, `volume_type`, `mount_path`, `purpose`, `status`, `created_at`, `updated_at` |
| appgroups | `id`, `project_id`, `stack_id`, `name`, `description`, `status`, `created_at`, `updated_at` |
| environments | `id`, `project_id`, `appgroup_id`, `name`, `description`, `status`, `created_at`, `updated_at` |
| services | `id`, `project_id`, `stack_id`, `name`, `description`, `server_id`, `ports`, `replicas`, `cpu_limit`, `memory_limit_mb`, `healthcheck_path`, `auto_deploy`, `status`, `current_deployment_id`, `created_at`, `updated_at` |
| deployments | `id`, `project_id`, `service_id`, `number`, `source_ref`, `server_id`, `trigger`, `rollback_of`, `status`, `started_at`, `finished_at`, `created_at`, `updated_at` |
| domains | `id`, `project_id`, `service_id`, `fqdn`, `verified_at`, `tls`, `auto_dns`, `dns_mode`, `status`, `created_at`, `updated_at` |
| events | `id`, `project_id`, `stack_id`, `resource_type`, `resource_id`, `action`, `outcome`, `message`, `request_id`, `created_at`, `updated_at` |
| exports | `id`, `status`, `object_expires_at` |

Exports also allow created_at as a sort key, but do not accept created_at
as a filter or return it. volume_ids and artifacts are JSON fields:
sort/filter on them returns 400. The stack/server/volume controller
accepts the names above but the repository still validates column types.
Nullable fields do not have a documented special null-filter syntax.

| Collection | Additional response/projection fields, not filter/sort keys |
|---|---|
| Stacks | network_id, engine_ref, task_state, fault under get_detail policy |
| Servers | flavor_id, image_id, compute_id, ironic_node_id, mgmt_ip, private_ip, floating_ip_id, agent_cert_serial, agent_cert_expires_at, last_heartbeat_at, engine_ref, task_state, fault under get_detail policy |
| Volumes | cinder_volume_id under get_detail policy |
| Services | source, default_url, dns_records; deployment_id only in create response |
| Deployments | resolved_ref |
| Domains | dns_records |
| Exports | volume_ids, artifacts; artifact download_url/expires_at under renew_url policy |

An unauthorized list fields request for admin detail names returns 400.
Do not use all_projects or fields to bypass detail policy.
Services accept appgroup_id and environment_id filters and fields
selection for both parent IDs and read-only template_id. Events do not
accept environment_id. Deployments have no resolved_ref sort/filter.

### Collections without common pagination

| Endpoint | Query and response behavior |
|---|---|
| `/v1/plans` | No query parameters. Sorted by name; plans envelope, no links. |
| `/v1/templates` | Exactly stack_id. templates envelope, no links/pagination. Do not assume name ordering. |
| `/v1/templates/{id}` | Exactly stack_id. template envelope. |
| `/v1/services/{id}/env` | No query parameters. Complete env map, no pagination. |
| `/v1/services/{id}/logs` | Only tail and since. logs envelope, no pagination link. |
| `/v1/services/{id}/deployments/{id}/log` | No query parameters. log envelope. |
| `/v1/quotas` | No query parameters. Admin only; stored overrides sorted by project_id. |
| `/v1/quotas/defaults` | No query parameters. Effective limits only. |
| `/v1/quotas/{project_id}` | No query parameters. Effective limits plus usage counts. |

## 8. Policy and role matrix

The following table was generated by evaluating every documented rule
from `coast/policies.list_rules()` through the actual enforcer. Each
column used one explicit role, a project-scoped context, a target in the
same project, and the default policies. Yes means policy allows the
operation, not that state or backend preconditions are satisfied.

**Roles are not inherited by this policy code.** A manager-only token
cannot perform reader/member operations unless Keystone also assigns
those roles or the operator overrides policy. The normal functional
fixture uses reader,member,manager together. Use the token's complete
role set. There is no capabilities endpoint. The operator can override
policies, so handle 403 even when a UI control is enabled locally.
Q96 permits **members to create stacks**; server add/remove and stack
delete still require manager. All rules have project scope.

| Rule | Reader | Member | Manager | Admin |
|---|---|---|---|---|
| `coast:stack:get_all` | Yes | No | No | Yes |
| `coast:stack:get_one` | Yes | No | No | Yes |
| `coast:stack:create` | No | Yes | No | Yes |
| `coast:stack:update` | No | Yes | No | Yes |
| `coast:stack:delete` | No | No | Yes | Yes |
| `coast:stack:upgrade` | No | No | No | Yes |
| `coast:stack:get_detail` | No | No | No | Yes |
| `coast:export:get_all` | No | Yes | No | Yes |
| `coast:export:get_one` | No | Yes | No | Yes |
| `coast:export:create` | No | No | Yes | Yes |
| `coast:export:renew_url` | No | No | Yes | Yes |
| `coast:export:delete` | No | No | Yes | Yes |
| `coast:appgroup:get_all` | Yes | No | No | Yes |
| `coast:appgroup:get_one` | Yes | No | No | Yes |
| `coast:appgroup:create` | No | No | Yes | Yes |
| `coast:appgroup:update` | No | Yes | No | Yes |
| `coast:appgroup:delete` | No | No | Yes | Yes |
| `coast:environment:get_all` | Yes | No | No | Yes |
| `coast:environment:get_one` | Yes | No | No | Yes |
| `coast:environment:create` | No | No | Yes | Yes |
| `coast:environment:update` | No | Yes | No | Yes |
| `coast:environment:delete` | No | No | Yes | Yes |
| `coast:server:get_all` | Yes | No | No | Yes |
| `coast:server:get_one` | Yes | No | No | Yes |
| `coast:server:create` | No | No | Yes | Yes |
| `coast:server:delete` | No | No | Yes | Yes |
| `coast:server:failover` | No | No | No | Yes |
| `coast:server:get_detail` | No | No | No | Yes |
| `coast:volume:get_all` | Yes | No | No | Yes |
| `coast:volume:get_one` | Yes | No | No | Yes |
| `coast:volume:create` | No | Yes | No | Yes |
| `coast:volume:update` | No | Yes | No | Yes |
| `coast:volume:delete` | No | No | Yes | Yes |
| `coast:volume:snapshot` | No | Yes | No | Yes |
| `coast:volume:get_detail` | No | No | No | Yes |
| `coast:service:get_all` | Yes | No | No | Yes |
| `coast:service:get_one` | Yes | No | No | Yes |
| `coast:service:create` | No | Yes | No | Yes |
| `coast:service:update` | No | Yes | No | Yes |
| `coast:service:delete` | No | Yes | No | Yes |
| `coast:service:start` | No | Yes | No | Yes |
| `coast:service:stop` | No | Yes | No | Yes |
| `coast:service:restart` | No | Yes | No | Yes |
| `coast:service:get_logs` | Yes | No | No | Yes |
| `coast:deployment:get_all` | Yes | No | No | Yes |
| `coast:deployment:get_one` | Yes | No | No | Yes |
| `coast:deployment:create` | No | Yes | No | Yes |
| `coast:deployment:rollback` | No | Yes | No | Yes |
| `coast:deployment:cancel` | No | Yes | No | Yes |
| `coast:deployment:get_log` | Yes | No | No | Yes |
| `coast:domain:get_all` | Yes | No | No | Yes |
| `coast:domain:get_one` | Yes | No | No | Yes |
| `coast:domain:create` | No | Yes | No | Yes |
| `coast:domain:delete` | No | Yes | No | Yes |
| `coast:domain:update` | No | Yes | No | Yes |
| `coast:domain:verify` | No | Yes | No | Yes |
| `coast:secret:get_all` | No | Yes | No | Yes |
| `coast:secret:update` | No | Yes | No | Yes |
| `coast:event:get_all` | Yes | No | No | Yes |
| `coast:quota:get_all` | No | No | No | Yes |
| `coast:quota:get_one` | Yes | No | No | Yes |
| `coast:quota:update` | No | No | No | Yes |
| `coast:quota:delete` | No | No | No | Yes |
| `coast:quota:get_defaults` | Yes | No | No | Yes |
| `coast:plan:get_all` | Yes | Yes | Yes | Yes |
| `coast:template:list` | Yes | Yes | Yes | Yes |
| `coast:template:get` | Yes | Yes | Yes | Yes |

The plan rule uses @ (any authenticated project identity). Template
read rules explicitly include reader, member, and manager. Export read
uses member, while signed URL visibility requires manager. Secret reads
are member operations, not reader operations. Admin is project-scoped
under these policy defaults; a system token is not a substitute.

## 9. Quotas and usage

Read the effective quota and usage for the token's project. The fixture
overrides some defaults to let the workflow run; do not copy its limits
into the UI.

```text
GET /v1/quotas/project
```

Request body: none.

Response: **200**.

```json
{
  "quota": {
    "environments": 5,
    "servers": 10,
    "baremetal_servers": 0,
    "build_servers": 1,
    "apps": 10,
    "domains": 20,
    "volume_gb": 1000,
    "storage_gb": 50
  },
  "usage": {
    "in_use": {
      "environments": 1,
      "servers": 1,
      "baremetal_servers": 0,
      "build_servers": 0,
      "apps": 1,
      "domains": 0,
      "volume_gb": 0,
      "storage_gb": 2
    },
    "reserved": {
      "environments": 1,
      "servers": 2,
      "baremetal_servers": 0,
      "build_servers": 0,
      "apps": 3,
      "domains": 1,
      "volume_gb": 12,
      "storage_gb": 0
    }
  }
}
```

```text
GET /v1/quotas/defaults
```

Request body: none.

Response: **200**.

```json
{
  "quota": {
    "environments": 5,
    "servers": 10,
    "baremetal_servers": 0,
    "build_servers": 1,
    "apps": 10,
    "domains": 20,
    "volume_gb": 1000,
    "storage_gb": 50
  }
}
```

| API quota key | What consumes it | Portal label |
|---|---|---|
| environments | Infrastructure stacks, not app group environments | Stacks |
| servers | Worker/build servers; controllers are excluded | Worker and build servers |
| baremetal_servers | Bare-metal worker/build servers, also counted in servers | Bare-metal servers |
| build_servers | Build-role servers, also counted in servers | Build servers |
| apps | Services, not app group records | Services |
| domains | Custom domain records; computed default URL is excluded | Custom domains |
| volume_gb | Allocated volume GiB, including retained data; extend reserves only the increase | Storage (GiB) |

There is no app group/environment count quota in this code.
Stack create reserves the stack, requested initial workers/build server,
and known initial data capacity. Real provisioning can reserve additional
configured data/Docker capacity later and fail after acceptance if that
capacity is unavailable. Do not treat a successful form quota check as
proof that all provisioning capacity is reserved.

- -1 means unlimited; display “Unlimited,” not a negative progress bar.
- 0 blocks new allocation. It does not delete existing resources.
- Effective show/defaults values are integers. Stored admin override rows
  can contain null (use configured default). PUT quota accepts a subset;
  omitted keys stay unchanged and null clears that override.
- Admin DELETE quota returns 204 with no body. It resets overrides, not
  usage. Lowering a limit does not remove resources or release capacity.

Project show returns `usage` next to `quota`. `usage.in_use` and
`usage.reserved` use the seven quota keys. The values are the quota
engine counters. Empty counters are zero for every key. Defaults,
the admin list, and PUT return limits only. There is
no usage query parameter. Show these counts with the limits. A count
of one list page is not the quota usage. Notifications remain an
operator report, not a second quota meter.

Export limits are separate operator options:
max_concurrent_per_project and max_size_gb. Export size is the sum of
selected volume sizes, not compressed archive bytes. Concurrent checks
count PENDING, SNAPSHOTTING, and UPLOADING exports. Over-limit errors name
exports or export_size_gb. These limits have no public read endpoint.
Ask the operator for limits to show in the portal; do not invent additional
keys under quota. The checked 403 example in section 6 shows the error
shape for allocation quota.

## 10. Integration checklist and further reading

Before you enable a write workflow, confirm its policy and backend support
with the operator. Keep controls outside the implemented boundaries in the first table disabled.
Use the following checks in a portal staging project:

1. Discover 1.0 through the catalog endpoint and record response request IDs.
2. Create with a member token where policy permits; verify reader cannot
   write. Test with a second project: foreign item reads must be 404.
3. Create a stack/worker and poll; do not confuse acceptance with readiness.
4. Create a Git service, view both service and deployment state, change an
   env secret with PATCH, and confirm the read returns null for its value.
5. Create a later deployment and roll back to an older revision. Keep both
   history rows. Test the 409 path while work is in progress.
6. Test logs as text; test DNS/HTTPS readiness without relying on a default
   URL. Never expose app engine credentials or the engine UI.
7. Test retained Swift download access with member versus manager roles;
   test expired URL/object behavior without logging signed URLs.
8. Delete only through supported paths. Check real cloud cleanup with the
   operator; the noop archive and removal results are not cloud proof.

Further reading:

* [Horizon UI mockups](ui-mockups.md): page structure and interaction intent.
  Use app groups, environments and services as the workload hierarchy.
* [App group API reference](api-apps.rst): location and build command for
  the separate `api-ref/source/` tree. Build with `tox -e api-ref` and open
  <a href="../../../api-ref/build/html/index.html">the built API reference</a>
  after both local documentation builds. The operator chooses the published
  API-reference URL.
* [API design](api.md) and [data model](data-model.md): approved contract
  and future work.
  Check the documented implementation boundaries before exposing actions
  in a portal.
* [Lifecycle](lifecycle.md), [SERVER-8 design](design/volume-cleanup.md),
  [operations](operations.md), and [security](security.md): operator-side
  safety and completion requirements.

## Databases

A Coast database is a standalone workload on a worker. It belongs to an
app group and an environment. It is separate from apps and template
services. Each database uses one service quota slot. Its name must be
unique among live services and databases in the stack.

In the dashboard, open Project > App Hosting > Databases to list databases
and filter by app group. Choose Create database to search the engine
catalog, see the Versions column and select the engine row's Create
database button. The app group detail page also has a Databases tab that
lists that app group's databases and offers Create database.

### Create and connect

Run `openstack coast database engine list` to see supported engines,
available versions, defaults, ports and backup support. Do not keep a
fixed engine list in a portal. Read `GET /v1/database_engines` instead.
Use its `has_username` and `has_database` fields to decide which inputs
to show.

Use `openstack coast database create` and supply a name, an engine, an
app group, an environment and a worker in that app group's stack. Read
`openstack coast database create --help` for the command options.
Omit the version to use the catalog default.

Name is the Coast name shown in Coast. Under Connection settings in the
Create database form, Initial database is the database created inside the
app engine server for your app (`--database`), and Username is the database
user for your app (`--username`). Both default to `app`; engines without
these settings do not show them. Coast generates a strong password and
stores it securely; there is no password input.

Creation is asynchronous. Use `openstack coast database list` and
`openstack coast database show` to check progress. The status moves from
`PENDING` through `CREATING` or `STARTING` to `ACTIVE`, or to `ERROR`.
Use `openstack coast database logs` to inspect runtime logs.

`internal_host` is the database hostname on the worker's container
network. Place the app on the same worker for this connection. Do not
assume that the hostname works from another worker or outside the stack.
The ordinary show response contains password-free URLs.
After creation, project members can reveal the password with Show password
on the Overview tab or
`openstack coast database credentials show NAME --show-password`.
Replace `NAME` with the Coast name. Without `--show-password`, secrets are
masked. Revealed output, including the full connection URL, is secret;
keep it private. Readers cannot retrieve credentials.

Put the full internal URL in the app's runtime secret environment
variable. Use the hidden prompt so the URL stays out of shell history:

```console
$ openstack coast env set --service web DATABASE_URL --secret --no-build-time
$ openstack coast deploy web
```

Enter the URL only at the prompt. Do not paste credentials into build
settings, logs, tickets or source files.

### Public access and changes

Public access is off by default. Use `openstack coast database set` to
change public access, the name, description or resource limits. Engine,
version, worker and parents cannot be changed.

For public access, select a port and restrict `public.source` to the
client's IPv4 CIDR. Coast manages the firewall rule for that port and
source. A source that allows every address exposes the listener to the
internet. IPv6 public access is not supported. Use the returned public
host and port; do not guess an address. Public access does not itself
provide database TLS. Disabling it removes the managed exposure.
See [database networking](networking.md#database-access).

### Backups, restore and lifecycle

Database backups require a Swift-compatible object-storage endpoint in the
cloud (for example, Swift or RGW with its Swift API enabled). They are not
verified end to end in 0.1.0; full verification is planned for 0.1.1.

Check the engine catalog's backup support first. Use
`openstack coast database backup policy show` and
`openstack coast database backup policy set` to read or change the
schedule and retention. Coast derives the engine and container; do not
supply them. Set a policy before making a backup. Schedules use UTC.
`openstack coast database backup create` makes a manual backup;
`openstack coast database backup list` lists restore points.

Use the existing `openstack coast backup show`, `download`, `delete` and
`restore` commands with a backup ID. A `new_copy` restore creates a new
database with the same engine, version and placement. Give it a new name
and allow for another service quota slot. An `overwrite` restore needs
the current database name as confirmation and takes a safety backup first.
A failed restore can leave partial data. Check the restore job before
connecting the app. See [backup details](contributor/specs/database-backups.md).

`openstack coast database stop` requires `ACTIVE` and stops the database
without deleting its data. `openstack coast database start` requires
`STOPPED`. `openstack coast database restart` requires `ACTIVE`.
These commands return before the operation finishes; poll show.

In the dashboard, the delete modal shows "To delete, type `<name>` below".
Type Name, the Coast name shown in Coast, to confirm deletion.

`openstack coast database delete` removes the database and its data
volumes. Existing backup cleanup rules also remove its backups. Download
any backup you need to keep before deletion. The status becomes `DELETING`;
show returns 404 after cleanup finishes. Stopping is the way to pause a
database while keeping its data.

See the [database support article draft](kb/databases.md).
