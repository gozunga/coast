=========
Templates
=========

Template reads use the packaged metadata catalog for the stack driver and
exact engine version. They do not contact the controller. Available at
microversion 1.0. No quota is consumed.

List templates
==============

.. rest_method:: GET /v1/templates

``stack_id`` is a required query parameter. Policy ``coast:template:list``
is required; each result also requires ``coast:template:get``. Readers,
members, managers and project-scoped admins can read by default. Stack
lookups are project-scoped by default.
GET collection and show requests accept ``all_projects=true`` or
``project_id`` for cross-project reads when the caller passes the admin
policy as well as the endpoint policy. Non-admin use returns 403.
Without either parameter, foreign resources return 404 even for admins.
These parameters do not extend write access across projects.


Response: ``{"templates": [...]}``.

The list response includes a weak ``ETag`` for the visible JSON value and
``Cache-Control: private, no-cache``. Send ``If-None-Match`` with that ETag
to receive 304 with no body when the visible list is unchanged. Weak and
strong validators, comma-separated validators, and ``*`` are supported.
The 304 response includes the current ETag. Stack ownership, policies,
are checked before the validator on every request.

The server caches only raw catalogs, keyed by driver and engine version.
``[templates] cache_ttl`` defaults to 3600 seconds; 0 disables caching.
The cache uses a thread lock and is local to each API worker process.
These are static, versioned packaged files. Visible responses are computed
on each request.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200
   - 304

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404
   - 405
   - 406
   - 409
   - 500

A missing/empty stack ID or unknown query parameter returns 400. An
unknown/foreign stack or unknown template returns 404. An unavailable
catalog for the exact engine version returns 409.

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - stack_id: template_stack_id_query
   - all_projects: all_projects
   - project_id: project_id_query

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - templates: templates
   - id: template_id
   - name: name
   - description: description
   - logo: template_logo
   - category: template_category
   - tags: template_tags
   - required_env: template_required_env
   - ports: template_ports
   - http_port: template_http_port

Response example
----------------

.. literalinclude:: samples/templates-list-response.json
   :language: javascript

Show template
=============

.. rest_method:: GET /v1/templates/{template_id}

``stack_id`` is a required query parameter. Policy ``coast:template:get``
is required. Response: ``{"template": {...}}``.

Fields are ``id``, ``name``, ``description``, ``logo``, ``category``,
``tags``, ``required_env`` (variable names, never values), and ``ports``
(integer ports). API 1.7 also returns read-only ``http_port`` (integer or
null). Earlier versions omit it. Names in ``required_env`` include engine-generated
``SERVICE_*`` names; callers do not supply those generated values.

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
   - 409
   - 500

A missing/empty stack ID or unknown query parameter returns 400. An
unknown/foreign stack or unknown template returns 404. An unavailable
catalog for the exact engine version returns 409.

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - stack_id: template_stack_id_query
   - all_projects: all_projects
   - project_id: project_id_query
   - template_id: template_id_path

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - template: template
   - id: template_id
   - name: name
   - description: description
   - logo: template_logo
   - category: template_category
   - tags: template_tags
   - required_env: template_required_env
   - ports: template_ports
   - http_port: template_http_port

Response example
----------------

.. literalinclude:: samples/template-show-response.json
   :language: javascript

Template create boundary
========================

Service create validates ``source: "template:<id>"`` or
``source: {"type": "template", "template_id": "<id>"}`` against the stack
catalog and get policy. A valid request stores
``template_id`` and returns 202. Unknown templates return
404 before any quota, secret, DB or RPC write. Git and template create
both require ``appgroup_id`` and ``environment_id`` with the Q98
validation described in :doc:`services`.

Implementation limit
====================

Create acceptance is not proof of a completed template deployment. The
conductor uses the selected app group and environment engine references.
The service response exposes read-only ``template_id`` and both parent IDs.
Template env values are runtime-only. Domains require an unambiguous
sub-app group target. Caller mounts are not sent to template services.

HTTP port selection (1.7)
=========================

The generator scans decoded compose text in order for ``SERVICE_URL_`` and
``SERVICE_FQDN_`` magic variables. The first variable with a numeric port
suffix selects ``http_port``, even when metadata lists another port.
With only bare variables, it uses the first catalog port, or null when
there are no ports. Without these variables, ``http_port`` is null.

At API 1.7, omitted service ports default to ``http_port`` when set, else
the first catalog port. If ``health_check`` is omitted and ``http_port``
is set, the default is ``http`` on that port with path ``/``. Explicit
client settings take precedence. Without ``http_port``, health defaults
to ``none``. Earlier versions keep their existing defaults.

Dedicated worker workloads
==========================

All catalog templates are selectable and creatable, including Beszel and
Beszel Agent. Project authorization remains enforced. The named-volume
index supplies accounting metadata only, with no safety verdict. This
behavior applies on every supported microversion.
Templates and customer Compose may use host paths, Docker sockets,
privilege, host namespaces, devices and capabilities on dedicated workers.
There is no workload safety check on create, deploy, edit or reconcile.
Control-plane networking and credentials remain protected.
