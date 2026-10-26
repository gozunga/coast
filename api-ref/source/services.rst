========
Services
========

Names must be unique among live rows in the stack. A name in use returns
409. Names remain reserved while deletion is in progress and can be reused
after deletion completes.

Services are project-scoped. A read requires ``coast:service:get_all`` or
``coast:service:get_one``. A write requires the matching member policy.

Sources include public HTTPS Git, SSH Git with deploy keys from 1.30,
a tagged image, or a catalog template.
From 1.31, pasted Dockerfile and Compose sources are also available.
Before API 1.30 private Git credentials are rejected with 400. The Git response source
has ``type``,
``repo_url``, ``branch``, ``build_pack``, ``dockerfile_path``,
``compose_path``, and ``base_directory``. It does not include source
IDs, private credentials, or secret references. From 1.30, SSH Git sources
also include the deploy public key and fingerprint described below.

``default_url`` and ``dns_records`` are computed. ``deployment_id`` is
returned only by create when ``deploy`` is true. Engine identifiers
are not returned.

Create stores status ``PENDING``. Start requires ``STOPPED``. Stop and
restart require ``ACTIVE``. Those actions return 202 and leave the
status unchanged until completion. Start and restart then report
``STARTING`` until the engine reports the service running (healthy or
without a healthcheck) for ``[conductor] stable_seconds`` (20 by
default), then ``ACTIVE``; after ``[conductor]
start_timeout`` the status becomes ``ERROR``. While a deployment runs the
service reports ``DEPLOYING``. Delete returns 202 with status
``DELETING``.

Responses include ``appgroup_id``, ``environment_id`` and read-only
``template_id`` (null for Git and image). The app group ID is derived from the
stored environment parent. Services without a parent return null
for both parent IDs. A list can filter by ``appgroup_id``,
``environment_id`` or other public service columns, including ``stack_id``.
You cannot sort on ``appgroup_id``, or sort/filter on ``source``,
``default_url`` or ``dns_records``.

From microversion 1.24, service responses include read-only nullable
``task_owner`` and ``task_started_at`` (UTC ISO 8601). They identify the
current operation and when it acquired the service task. Both are null
when idle and omitted from older microversions. They also support ``fields``
selection. Busy 409 responses can say, for example, ``Domain check in
progress since 22:58 UTC. Try again in a moment.`` Existing deployment,
rollback and deletion messages are preserved.

List services
=============

.. rest_method:: GET /v1/services

List services in the token project.

Each service has a stored ``kind``: ``app`` or ``service``. Filter with
``?kind=app`` or ``?kind=service`` (Q148); other values return 400.
The create microversion and explicit kind determine the stored value;
listing at a newer microversion does not reclassify existing rows.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404
   - 406
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - all_projects: all_projects
   - fields: fields
   - limit: limit
   - marker: marker
   - project_id: project_id_query
   - sort_dir: sort_dir
   - sort_key: sort_key
   - stack_id: stack_id_query
   - appgroup_id: appgroup_id_query
   - environment_id: environment_id_query

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - task_owner: task_owner
   - task_started_at: task_started_at
   - services: services
   - id: id
   - project_id: project_id
   - stack_id: stack_id
   - appgroup_id: appgroup_id
   - environment_id: environment_id
   - template_id: service_template_id
   - name: name
   - description: description
   - server_id: server_id
   - ports: ports
   - replicas: replicas
   - cpu_limit: cpu_limit
   - memory_limit_mb: memory_limit_mb
   - healthcheck_path: healthcheck_path
   - health_check: health_check
   - health_check_port: health_check_port
   - health_check_path: health_check_path
   - auto_deploy: auto_deploy
   - status: status
   - current_deployment_id: current_deployment_id
   - created_at: created_at
   - updated_at: updated_at
   - repo_url: source_repo_url
   - branch: source_branch
   - build_pack: source_build_pack
   - dockerfile_path: source_dockerfile_path
   - compose_path: source_compose_path
   - base_directory: source_base_directory
   - default_url: default_url
   - primary_url: primary_url
   - default_domain_enabled: default_domain_enabled
   - dns_records: dns_records
   - type: dns_type
   - name: dns_name
   - value: dns_value
   - purpose: dns_purpose
   - links: links
   - rel: rel
   - href: href

Create service
==============

.. rest_method:: POST /v1/services

Create a service.

The caller needs ``coast:service:create``. The response code is 202.
``name``, ``appgroup_id``, ``environment_id``, ``server_id`` and
``source`` are required. ``server_id`` must be a worker server; Coast has
no automatic placement, so a null or missing ``server_id`` returns 400
(``server_id is required``) before any quota, secret or database write
(Q144). The app group selects
the stack; ``stack_id`` is not accepted in the create body. The environment
must belong to that app group. A supplied server must be a worker in the
app group's stack and Keystone project.

Q98: a missing or foreign-project app group, environment or server
returns 404 without an existence leak. A same-project hierarchy mismatch
or non-worker server returns 400. Both app group and environment must be
``ACTIVE`` with an engine reference; otherwise create returns 409.
Validation runs before quota, secret, DB or RPC writes.

The call reserves one service. ``deploy`` defaults to true. When true,
the response includes ``deployment_id``. The service stays ``PENDING``
until completion. Parent IDs and top-level ``template_id`` cannot be
changed by PATCH; top-level ``template_id`` is read-only.
The source is a Git or image object, either template syntax below, or
pasted Dockerfile/Compose content from 1.31.
From 1.28, omitted ``kind`` defaults to ``app`` for image and Git sources,
including Dockerfile and compose-from-repository build packs. One-click
templates default to ``service``. Explicit ``kind=app|service`` wins.
At 1.11–1.27, images default to ``service`` and accept either explicit kind;
Git sources require ``app`` and templates require ``service`` (conflicting
explicit kinds return 400). At 1.0–1.10, images and Git default to ``app``,
templates default to ``service``, and explicit ``kind`` returns 400.
At API 1.7, template creates without ports prefer ``http_port`` over the
first catalog port. If ``health_check`` is omitted and ``http_port`` is set,
health defaults to ``http`` on that port with path ``/``. Explicit client
settings take precedence. Otherwise health defaults to ``none``.
Earlier template creates without ports use the first catalog port, when available,
before HTTP health validation. Without catalog ports, HTTP checks require
explicit service ports. PATCH validates the resulting health and port
settings on every update.
``replicas`` must be 1 and ``auto_deploy`` must be false.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 202

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403: over_quota
   - 404
   - 406
   - 409
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - Content-Type: content_type
   - service: service_request
   - appgroup_id: appgroup_id
   - environment_id: environment_id
   - kind: service_kind_request
   - name: service_name_request
   - server_id: service_server_request
   - repo_url: source_repo_url
   - branch: source_branch_request
   - build_pack: source_build_pack_request
   - dockerfile_path: source_dockerfile_request
   - compose_path: source_compose_request
   - base_directory: source_base_request
   - description: service_description_request
   - ports: ports_request
   - replicas: replicas_request
   - auto_deploy: auto_deploy_request
   - cpu_limit: cpu_limit_request
   - memory_limit_mb: memory_limit_request
   - healthcheck_path: healthcheck_request
   - health_check: health_check
   - health_check_port: health_check_port
   - health_check_path: health_check_path
   - env: env_request
   - deploy: deploy

The source is required. For Git, send an object with ``type=git`` and
``repo_url``; the Git-specific parameter rows above apply only to that
source. For a template, send ``template:<id>`` or an object with
``type=template`` and ``template_id``. Do not send Git fields in a template
source. Git and template responses project the seven source fields listed
at the start of this page; Git-only fields can be null for a template.

Q99: for an image, send ``{"type":"image","image":"registry.example.com/web",
"tag":"release"}``. The image is a Docker reference name without a scheme,
credentials, tag or ``@``. ``tag`` is required: no implicit latest. Tags
must match ``[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}``. Digest references, supplied
registry credentials and Git-only fields return 400 (Q53). Private
registries require preconfigured per-environment engine credentials.
Image responses contain only ``type``, ``image`` and ``tag``. PATCH can
change ``source.tag``; from microversion 1.14 it can also change
``source.image`` for an image source. Deployment
``source_ref`` overrides the tag and uses the same grammar. Rollback is
unchanged.

The following examples include Q94 parent IDs and the Q98 response fields.
They show API acceptance, not a live deployment. Use the IDs from
your own app group and environment create responses.

Request Example
---------------

.. code-block:: json

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

Response
--------

The response uses the list service fields inside a ``service`` envelope, plus
``deployment_id`` when ``deploy`` is true. ``status`` is ``PENDING``.

.. rest_parameters:: parameters.yaml

   - deployment_id: deployment_id
   - task_owner: task_owner
   - task_started_at: task_started_at

Response Example
----------------

.. code-block:: json

   {
     "service": {
       "appgroup_id": "1422cb40-ae97-4440-b05b-d0b0ea4a63fd",
       "environment_id": "548c975a-4805-494f-b555-a54984ea408e",
       "id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
       "project_id": "project",
       "template_id": null,
       "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
       "name": "web",
       "description": null,
       "server_id": "43828f3a-dd3f-5308-b8eb-51f304689211",
       "ports": "8080",
       "replicas": 1,
       "cpu_limit": null,
       "memory_limit_mb": null,
       "healthcheck_path": null,
       "auto_deploy": false,
       "status": "PENDING",
       "current_deployment_id": null,
       "created_at": "2026-10-11T16:32:16.607929Z",
       "updated_at": "2026-10-11T16:32:16.613574Z",
       "source": {
         "type": "git",
         "repo_url": "https://example.com/web.git",
         "branch": "main",
         "build_pack": "dockerfile",
         "dockerfile_path": "Dockerfile",
         "compose_path": null,
         "base_directory": null
       },
       "default_url": "https://web-bbf1.203-0-113-10.sslip.io",
       "dns_records": [],
       "links": [
         {
           "rel": "self",
           "href": "https://api.example.com/v1/services/bbf16995-64cc-430a-bfbc-86717d8dc7b0"
         }
       ],
       "deployment_id": "89591377-8bc9-42e4-8496-e28bce24661c"
     }
   }

Show service
============

.. rest_method:: GET /v1/services/{service_id}

Show one service.

The caller needs ``coast:service:get_one``. Another project returns 404.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404
   - 406
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - service_id: service_id
   - fields: fields

Response
--------

The response uses the list service fields inside a ``service`` envelope.
Create-only ``deployment_id`` is not included.

Update service
==============

.. rest_method:: PATCH /v1/services/{service_id}

Update service settings without a new deployment.

Settings changes do not wait for, or block, a running deployment; only a
deleting service returns 409. ``name`` and ``description`` are metadata.
When the service is idle and known, those fields are pushed immediately
and do not set ``pending_changes``. ``pending_changes`` compares the
frozen spec when that spec has ``ports`` and ``source``. An older
incomplete spec uses the settings timestamp. A revert of a
deployment-affecting field clears the flag.

The caller needs ``coast:service:update``. The response code is 200.
A deleting service returns 409. The call does not change
environment variables.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404
   - 406
   - 409
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - Content-Type: content_type
   - service_id: service_id
   - service: service_update
   - name: service_name_request
   - default_domain_enabled: default_domain_enabled
   - description: service_description_request
   - ports: ports_request
   - cpu_limit: cpu_limit_request
   - memory_limit_mb: memory_limit_request
   - healthcheck_path: healthcheck_request
   - health_check: health_check
   - health_check_port: health_check_port
   - health_check_path: health_check_path
   - source: source_update
   - branch: source_branch_request
   - build_pack: source_build_pack_request
   - dockerfile_path: source_dockerfile_request
   - compose_path: source_compose_request
   - base_directory: source_base_request

Response
--------

The response uses the show service fields.

.. rest_parameters:: parameters.yaml

   - task_owner: task_owner
   - task_started_at: task_started_at

Delete service
==============

.. rest_method:: DELETE /v1/services/{service_id}

Delete a service.

The caller needs ``coast:service:delete``. The status becomes ``DELETING``.
The response code is 202. A body must be empty. An empty JSON object
is accepted. Cleanup is deferred. Delete always wins over an in-flight
deployment: if the service is provisioning, deploying or rolling back, the
latest ``PENDING``, ``BUILDING`` or ``DEPLOYING`` deployment becomes
``CANCELLED``, the engine deployment is cancelled, and the delete proceeds.

An idle DEGRADED stack permits app deletion. An exclusive stack task still
returns 409; health degradation alone does not indicate a running operation.

An ERROR parent stack skips engine cleanup with a warning. On an ACTIVE or
DEGRADED stack, an unreachable engine also permits record deletion. Other
engine failures on ACTIVE or DEGRADED stacks still fail deletion. DNS and
backup cleanup must still succeed. Poll GET until 404, then delete the
remaining parent resources and the stack to remove its cloud resources. These
rules apply to all microversions.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 202

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404
   - 406
   - 409
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - service_id: service_id

Response
--------

The response uses the show service fields. ``status`` is ``DELETING``.

Start, stop, or restart service
===============================

.. rest_method:: POST /v1/services/{service_id}/action

Start, stop, or restart a service.

Send exactly one of ``start``, ``stop``, or ``restart``. The value is
an empty object. The caller needs the matching policy. Start requires
status ``STOPPED``. Stop and restart require status ``ACTIVE``. The
response code is 202. The status does not change in this response.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 202

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404
   - 405
   - 406
   - 409
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - Content-Type: content_type
   - service_id: service_id
   - start: service_action
   - stop: service_action
   - restart: service_action

Response
--------

The response uses the show service fields. ``status`` is unchanged.

Show service logs
=================

.. rest_method:: GET /v1/services/{service_id}/logs

Show service log lines.

The caller needs ``coast:service:get_logs``. Noop returns an empty line
list. A provisioned service with an engine reference reads live driver
logs through the conductor. ``since`` is an ISO 8601 UTC time. ``tail`` is an
integer of 0 or more. The live handler defaults to 200 lines; 0 returns
no lines. Another query parameter returns 400. The call returns 503
when the engine is unreachable or the engine rejects the Coast
credential. ``faultstring`` is ``Service Unavailable``. The body does
not include the engine error. Missing engine log resources return empty
lines with ``complete: true`` without retries. HTTP 403 from the template
service logs route, including an older controller agent's rejection, has the
same empty, complete result. HTTP 401 and failures on service metadata or
env reads retain their existing error handling. Another engine fault
returns 500.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404
   - 405
   - 406
   - 500
   - 503

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - service_id: service_id
   - since: since
   - tail: tail

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - logs: logs
   - lines: service_log_lines
   - ts: log_ts
   - stream: log_stream
   - line: log_line

Response Example
----------------

.. literalinclude:: samples/service-logs-response.json
   :language: javascript

Q95 template source boundary
============================

Template source syntax is parsed and validated against the stack catalog,
``coast:template:get`` and operator allow/deny lists. Both
``"template:<id>"`` and ``{"type": "template", "template_id": "<id>"}``
are recognized. A valid request stores ``template_id`` and returns 202.
The nested response source does not return ``template_id``; the top-level
service does. The conductor uses the selected app group and environment
engine references for Git and template placement. A 202 response is not
proof of engine completion. See :doc:`templates`.


Optional HTTP checks (1.4)
==========================

From microversion 1.4, create and PATCH accept ``health_check`` as ``none``
or ``http``. Show and list return the mode and HTTP port and path. Earlier
versions reject these request fields with 406 and omit them in responses.
New apps default to ``none``, except for template HTTP defaults at API 1.7
described above. Existing apps retain ``http`` after upgrade.

Every app requires a successful rollout and a stable running container.
HTTP adds an extra readiness check. Its port defaults to the first app
port and its path defaults to ``/``. ``health_check_path`` uses the same
storage as ``healthcheck_path``; send only one name per request.

Ports are required for ``http``. With ``none``, an app can have no ports.
In that case ``default_url`` is null and no default domain route is set.
Domain add returns 400 with ``App has no ports.`` Remove custom domains
before removing ports. Catalog services keep their template routes.


Stored failure output (1.5)
---------------------------

From microversion 1.5, deployment show and list include nullable
``runtime_log``: sanitized output capped at 65535 UTF-8 bytes. Output
is stored as JSON lines; each line records the engine timestamp (when
present), stream, text, and capture time. Service show
and list include nullable ``reconcile_fault``. Stable runtime recovery
clears this fault; unrelated deployment errors are preserved.

Log endpoints prefer live output. Empty or failed engine reads return the
stored deployment output when available. Service logs use the newest
deployment with stored output. Unfinished empty reads and driver errors are
retried up to ten times, except missing resources, which return complete
output without a retry. Retries are 0.5 seconds apart, within a five-second retry window. These responses include ``captured_at_failure: true``, ``captured_at`` (the latest capture time), ``deployment_id``, and a note that identifies output captured at failure
time. Line timestamps retain the engine timestamp when present. Untimed
lines share one capture time per block; consecutive identical untimed lines
become one ``(repeated N times)`` entry. Service ``tail`` still applies; stored failure tails are returned
regardless of ``since``. Without stored output, existing errors apply.

Service create and PATCH accept null or blank ``ports``. HTTP health
checks require a port. Remove custom domains before clearing the port.
Services without ports have no exposed port or default domain route.

When no output has ever been captured, a restart/failure response explains
that capture continues in the background and asks the user to refresh.
Reading exited-container output needs a future worker endpoint and image
update (Q105); this change uses only the existing engine logs endpoint.

Read desired compose (1.12)
===========================

.. rest_method:: GET /v1/services/{service_id}/compose

Requires ``coast:service:get_compose`` (project member/admin). The response
is ``{"compose": {"yaml": "services: ..."}}`` with status 200 and
``Cache-Control: no-store``. First read imports template raw compose into
Barbican. Later reads use Coast's saved copy. Other-project IDs return 404;
readers return 403; earlier versions return 404. Engine faults return 503.

Replace desired compose (1.12)
==============================

.. rest_method:: PUT /v1/services/{service_id}/compose

Requires ``coast:service:update_compose`` (project member/admin). Request
and 200 response use ``{"compose": {"yaml": "services: ..."}}``. Unknown
fields and query parameters return 400. Responses include
``X-Coast-Redeploy-Required: true`` and ``Cache-Control: no-store``.
Save replaces the whole document without changing containers. To save and
redeploy, follow PUT with POST ``/v1/services/{service_id}/deployments``
and ``{"deployment": {}}`` (202). That deployment freezes the saved content.

The limit is the smaller of ``[api] max_compose_bytes`` (default 10000)
and ``[secrets] max_secret_bytes`` (default 20000), in UTF-8 bytes.
Oversized content returns 400 naming the byte limit before secret writes.
Invalid YAML, aliases, duplicate keys and missing/empty ``services``
mappings return 400. The fault names the service and key, never the value.
Named volumes receive existing storage quota accounting. Busy service/stack
operations return 409. On every supported microversion, host binds, sockets,
privilege, host namespaces, devices, capabilities, file imports and
engine-supported Compose options are allowed
on dedicated workers. Control-plane network and credential isolation remain.

Templates are supported. Compose build-pack workloads require Coast-held
content and driver support; Git repository content is not imported. When
the app engine has no supported way to update compose content for an
application, the request returns 409. Secret store faults return a generic 500.

List service containers (1.13)
==============================

.. rest_method:: GET /v1/services/{service_id}/containers

Requires ``coast:service:get_containers`` (project reader/admin).
Returns ``{"containers": [...]}`` and ``Cache-Control: no-store``.
Templates and Git-backed compose services are supported. Each summary has
``id``, ``name`` (compose service key), ``image``, ``state``, ``health`` and
``restart_count``. The ID is ``c-`` followed by the compose service key.
Docker names, IDs, host paths, mounts and labels are never returned.
Stopped containers are included. Missing health/restart data is null.
An empty deployed workload returns an empty list. Non-compose services return
409; an unavailable worker or missing engine reference returns 503.
Unknown query parameters return 400. Older versions and unknown or
other-project services return 404. Unauthenticated requests return 401;
unauthorized roles return 403. Invalid backend responses return 500.

Show service container (1.13)
=============================

.. rest_method:: GET /v1/services/{service_id}/containers/{container_id}

Requires ``coast:service:get_container`` (project reader/admin).
Returns ``{"container": {...}}`` with the summary fields plus
``environment`` (key/value mapping) and ``limits`` (``cpu`` in cores,
``memory_bytes`` in bytes). Unlimited resources are null. Only Coast
Secret-flagged keys and occurrences of their values are masked as ``***``.
Plain variables remain text, including keys that resemble passwords.
Keys injected by the app engine and ``COAST_`` keys are omitted.

For example, ``/containers/c-web`` can return:

.. code-block:: json

   {"container": {"id": "c-web", "name": "web", "image": "nginx:stable",
    "state": "running", "health": "healthy", "restart_count": 0,
    "environment": {"MODE": "production", "TOKEN": "***"},
    "limits": {"cpu": 0.5, "memory_bytes": 134217728}}}

Responses use ``Cache-Control: no-store``. Unknown containers return 404.
Other status codes and query validation match the list route.

Read service container logs (1.13)
==================================

.. rest_method:: GET /v1/services/{service_id}/containers/{container_id}/logs

Requires ``coast:service:get_container_logs`` (project reader/admin).
Returns ``{"logs": {"lines": [...]}}`` and ``Cache-Control: no-store``.
Each line has ``ts`` (ISO timestamp), ``stream`` (stdout/stderr), and ``line``.
Logs mask Coast Secret-flagged values. Reads include exited containers and
never substitute combined service logs or stored deployment output.

``tail`` accepts 0..10000 lines, default 200. ``tail=0`` returns no lines.
``since`` accepts an ISO timestamp, exclusive; a naive timestamp means UTC.
Invalid values or unknown query parameters (including ``limit``) return 400.
Unknown containers return 404. Other status codes match the list route.
Output is bounded to 1 MiB per Docker response and the configured agent
response size. Oversized reads fail with a generic fault. The worker agent
must include the container read route. No write operation is supported.

In-place image update (1.14)
============================

``PATCH /v1/services/{service_id}`` accepts
``{"service":{"source":{"image":"nginx","tag":"1.29-alpine"}}}``.
Either field can be omitted to keep its current value. Image names use the
create grammar without an embedded tag or digest. Git, compose and template
sources reject image/tag changes with 400. The existing
``coast:service:update`` member/admin policy applies. Other-project IDs
return 404; readers return 403. Earlier versions reject ``source.image``.

The response is 200. Save changes desired state and ``pending_changes``.
To apply it, POST ``{"deployment":{}}`` to
``/v1/services/{service_id}/deployments`` (202). The driver updates the
existing engine application before deployment. Service ID, environment
variables, storage, file mounts, domains and history are kept. No automatic
redeploy option is added. Unknown image-source fields return 400. The
image/tag schema is unchanged. Worker runtime settings use Compose or the
worker; Coast applies no workload safety checker to image services.


Primary address (API 1.23)
==========================

Service responses include ``primary_url`` and ``default_domain_enabled``.
The main custom domain supplies ``primary_url`` with its TLS scheme;
otherwise it equals ``default_url``. PATCH ``default_domain_enabled`` false
requires a primary domain or returns 409: ``Pick a main domain before turning
off the default address.`` The setting defaults to true for existing services.

While disabled, ``default_url`` is null. The next reconcile removes the
default route and its owned DNS records. Custom routing and verification
records remain; no CNAME routing alternative to the default URL is offered.
Managed custom CNAMEs use direct A/AAAA records while the default is off,
and return to CNAME when it is re-enabled. Manual DNS needs the same change
by the domain owner. New request fields require 1.23 (406 below it); older
responses omit them. Domain and service quotas are unchanged.

Private Git deploy keys (1.30)
------------------------------

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
notifications or exports.

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

Repository-free sources (1.31)
==============================

Create accepts ``source: {"type": "dockerfile", "dockerfile": "<text>"}``
or ``source: {"type": "compose", "compose": "<text>"}``. Dockerfile requires
``kind=app``; Compose defaults to ``service`` and accepts ``app``. Source
responses contain only ``type``. Text is nonempty UTF-8 and subject to the
smaller of the configured compose and secret byte limits. Errors never
include the pasted content.

Pasted Compose supports the existing compose editor, container views,
named storage and database backups with explicit container selection.

Read a Dockerfile
==================

.. rest_method:: GET /v1/services/{service_id}/dockerfile

Read the protected desired Dockerfile without contacting the app engine.

Replace a Dockerfile
====================

.. rest_method:: PUT /v1/services/{service_id}/dockerfile

Available from 1.31 for Dockerfile sources. GET returns and PUT accepts
``{"dockerfile": {"text": "FROM ..."}}``. Both return 200, require project
member or administrator access, and send ``Cache-Control: no-store``.
PUT returns the saved envelope and ``X-Coast-Redeploy-Required: true``;
it never deploys. Deploy separately through the existing deployments route.
Deployment and rollback use the frozen content stored for that release.
The Dockerfile is fixed at app creation, including when deployment is deferred.
On redeploy, unchanged Dockerfile text is skipped and the image is reused.
Changed text is refused before deployment starts with:

   This app can't use a changed Dockerfile. Create a new app from the new
   Dockerfile, or deploy from a Git repository.

Unknown fields, query parameters, empty text, invalid UTF-8 or oversized
content return 400. Other-project, unknown resources and earlier versions
return 404; insufficient roles return 403; missing authentication returns
401. Busy service/stack or a different source type returns 409. Secret store
failures return a generic 500. Content is stored in Barbican and removed
with service or stack deletion.
