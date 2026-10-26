=================
Version discovery
=================

``GET /`` and ``GET /v1`` are public. A token is not required.
Both calls return the microversion response headers.

List versions
=============

.. rest_method:: GET /

List the API versions that this deployment provides. The maximum
microversion is 1.31 (repository-free app sources); the minimum is 1.0.

The service returns one version object. ``links`` points at ``/v1/``.
The service builds that URL from ``[api] public_endpoint`` when the
option is set. Otherwise it uses the request URL.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 405
   - 406
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token_public

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - Vary: vary
   - versions: versions
   - id: version_id
   - status: version_status
   - min_version: min_version
   - max_version: max_version
   - links: links
   - rel: rel
   - href: href

Response Example
----------------

.. literalinclude:: samples/versions-list-response.json
   :language: javascript

Show v1
=======

.. rest_method:: GET /v1

Show the v1 version object.

``GET /v1/`` returns the same body. The object matches one entry from
``GET /``. The envelope key is ``version``.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 405
   - 406
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token_public

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - Vary: vary
   - version: version
   - id: version_id
   - status: version_status
   - min_version: min_version
   - max_version: max_version
   - links: links
   - rel: rel
   - href: href

Response Example
----------------

.. literalinclude:: samples/version-show-response.json
   :language: javascript

Microversion 1.8: Stack event timeline
======================================

Adds ``GET /v1/stacks/{stack_id}/events`` with selected infrastructure
facts, descending time/ID order, marker paging and resource type filtering.

Microversion 1.9: Shell sessions
================================

Adds ``POST /v1/apps/{app_id}/console`` and
``POST /v1/services/{service_id}/console``. A project member receives a
one-time shell URL. The stack timeline can also return ``app`` and
``service`` rows for shell open and close events.

Microversion 1.10: Named storage
================================

Adds ``/v1/stacks/{stack_id}/storages``. A storage is a named volume on
the worker Docker disk. Older microversions return 404 for these routes.
The Cinder ``/volumes`` collection is unchanged.

Microversion 1.11: Workload kind
================================

Service creation accepts explicit ``kind=app`` or ``kind=service`` for
images, which default to ``service`` through 1.27. Git sources require
``app`` and templates require ``service`` through 1.27. Before 1.11,
explicit kind is rejected and images default to ``app``.

Microversion 1.12: Compose editor
=================================

Adds GET and PUT ``/v1/services/{service_id}/compose`` for project members.
Content can contain secrets. Earlier versions return 404.


Microversion 1.13: Service container views
==========================================

Adds read-only service container list, detail (environment and CPU/memory
limits), and recent logs. Project readers and admins can read observations.
Coast Secret-flagged values are masked. Worker agent support is required.

Microversion 1.14: In-place image source updates
================================================

Adds partial image source updates followed by the existing deployment action.

Microversion 1.15: Container config files
=========================================

Apps and services can create, list, show, update, and delete file mounts.
Content is write-only and changes require the next deploy. See
:doc:`file-mounts`.

Microversion 1.16: Observed service named volumes
=================================================

Storage lists include named volumes from the public engine storage API.
Responses add ``source=coast`` or ``source=template``. Template and Compose
volumes are read-only, including engine-only volumes without Coast rows.

Microversion 1.17: Unrestricted dedicated worker workloads
==========================================================

Dedicated workers permit host paths, Docker sockets, privileged containers,
host namespaces, devices and capabilities for templates, customer Compose
and image workloads. The template index supplies named-volume accounting
metadata only. All catalog templates are selectable and creatable. This
behavior applies to every supported microversion, including versions before
1.17; it is not gated by 1.17. Control-plane isolation, least-privilege
credentials and project/stack authorization apply on every version.

Microversion 1.18: Admin stack inventory
========================================

Adds stack network IDs, the service project ID when different from the
owner, and live server/workload counts under the existing stack detail
policy (admin-only by default). Enables explicit admin cross-project stack
show and nested server, volume and event reads with ``all_projects=true``.
Create and replacement history includes Nova IDs. See :doc:`stacks`.


Microversion 1.19: Typed service storage
========================================

Storage supports bind mounts and named volumes, create-and-attach,
read-only mode, PATCH and attached delete. Service-filtered listings include
all template mounts with null IDs and explicit type/source/origin.


Microversion 1.20: Database backup policies, backups, downloads and restore
===========================================================================

Adds database backup policies, manual and scheduled backups, authenticated
compressed downloads and durable new-copy or overwrite restores. See
:doc:`backups` for schemas, project access and safety-backup requirements.

Microversion 1.21: Per-container CPU and memory metrics
=======================================================

``GET /v1/services/{service_id}/metrics`` returns retained container CPU and
memory history with project isolation and administrator access.


Microversion 1.22: Customer Designate zones and automatic DNS
=============================================================

Adds project zone discovery, optional stack default-domain links and
ownership-safe automatic custom-domain records. See :doc:`dns`.


Microversion 1.23: Primary custom domains and optional default address
======================================================================

Domains accept and return ``primary``. Setting true clears the other primary
flags on this service. Services return ``primary_url`` and accept and return
``default_domain_enabled`` (default true). Turning off the default requires
a primary custom domain. Reconciliation removes the default route and DNS
records. Removing or un-primarying the main domain while the default is off
returns 409. Older requests reject the new fields with 406 and older
responses omit them.


Microversion 1.24: Read-only task holder and start time
=======================================================

Service responses include read-only nullable ``task_owner`` and
``task_started_at`` (UTC ISO 8601). They are null when idle and omitted
before 1.24. Known busy operations include a plain label and UTC start
time in their 409 response. See :doc:`services`.


Microversion 1.25: Customer worker ingress rules
================================================

Adds project-scoped worker ingress list, create and delete. Required
rules are marked managed and cannot be deleted. See :doc:`ingress-rules`.


Microversion 1.26: Customer server resize and rolling worker resize
===================================================================

Adds project-member ``resize`` server actions for worker, build and App
engine servers, and serial ``resize_workers`` stack actions. Only strictly
larger operator flavors are accepted. Preserve volumes and network identity;
write flavor/plan only after healthy replacement. See server and stack
action sections for payloads, responses, policies and errors.

Microversion 1.27: Customer server flavors and allowed flavor catalog
=====================================================================

Server GET list/show adds project-visible ``flavor`` (Nova ID, name,
vCPUs, RAM in MiB and disk in GiB). ``GET /v1/flavors`` lists operator-mapped
VM sizes with plan names and compatible roles under project reader policy.
Older server representations and admin detail policies are unchanged.


Microversion 1.28: App sources default to app kind on create
============================================================

Service creation without ``kind`` defaults to ``app`` for image and Git
sources, including Dockerfile and compose-from-repository build packs.
One-click templates still default to ``service``. An explicit
``kind=app|service`` takes precedence. Earlier microversions keep their
existing behavior, and existing rows retain their stored kind.

Microversion 1.29: Databases
============================

Adds the database engine catalog, database create, list, show, update,
delete, and member-only credentials. Public access must remain disabled.

Microversion 1.30: Private Git deploy keys
------------------------------------------

Generated or uploaded deploy keys for SSH Git sources. Generated keys
skip the first deploy by default. Responses expose only public key material.

Microversion 1.31: Repository-free app sources
----------------------------------------------

Create services from pasted Dockerfile or Compose text. Dockerfile content
is editable through GET/PUT ``/v1/services/{service_id}/dockerfile``.
Compose content uses the existing compose editor. Saves do not deploy.
