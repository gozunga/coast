=======
Servers
=======

Servers belong to one stack. A user can create a ``worker`` or
``build`` server. Coast creates the ``control`` server. Create accepts
``plan`` and does not store it or return it. The new status is
``PENDING``. Responses include read-only ``hostname``, the saved DNS label
used by Nova, the guest and its App engine server entry. Renames and
failover preserve it.

A list can filter on the public server fields: ``id``, ``project_id``,
``stack_id``, ``name``, ``kind``, ``role``, ``status``,
``floating_ip``, ``created_at``, and ``updated_at``.

An administrator who passes ``coast:server:get_detail`` also receives
the detail fields in the response tables.

List servers
============

.. rest_method:: GET /v1/stacks/{stack_id}/servers

List servers in one stack.

The caller needs ``coast:server:get_all``. A hidden stack
returns 404.

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
   - stack_id: stack_id_path
   - all_projects: all_projects
   - fields: fields
   - limit: limit
   - marker: marker
   - project_id: project_id_query
   - sort_dir: sort_dir
   - sort_key: sort_key

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - servers: servers
   - id: id
   - project_id: project_id
   - stack_id: stack_id
   - name: name
   - hostname: server_hostname
   - kind: kind
   - role: role
   - status: status
   - floating_ip: floating_ip
   - created_at: created_at
   - updated_at: updated_at
   - flavor: server_flavor
   - flavor_id: flavor_id
   - image_id: image_id
   - compute_id: compute_id
   - ironic_node_id: ironic_node_id
   - mgmt_ip: mgmt_ip
   - private_ip: private_ip
   - floating_ip_id: floating_ip_id
   - agent_cert_serial: agent_cert_serial
   - agent_cert_expires_at: agent_cert_expires_at
   - last_heartbeat_at: last_heartbeat_at
   - engine_ref: engine_ref
   - task_state: task_state
   - fault: fault
   - links: links
   - rel: rel
   - href: href

Create server
=============

.. rest_method:: POST /v1/stacks/{stack_id}/servers

Add a worker or build server.

The caller needs ``coast:server:create``. The response code is 202.
Status is ``PENDING``. Responses include read-only ``hostname``, the saved DNS label
used by Nova, the guest and its App engine server entry. Renames and
failover preserve it. A bare-metal or build server also consumes the
matching quota. Over-quota returns 403.

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
   - stack_id: stack_id_path
   - server: server_request
   - name: server_name_request
   - kind: server_kind_request
   - role: server_role_request
   - plan: server_plan_request

Request Example
---------------

.. literalinclude:: samples/server-create-request.json
   :language: javascript

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - server: server
   - id: id
   - project_id: project_id
   - stack_id: stack_id
   - name: name
   - hostname: server_hostname
   - kind: kind
   - role: role
   - status: status
   - floating_ip: floating_ip
   - created_at: created_at
   - updated_at: updated_at
   - links: links
   - rel: rel
   - href: href

Response Example
----------------

.. literalinclude:: samples/server-create-response.json
   :language: javascript

Show server
===========

.. rest_method:: GET /v1/stacks/{stack_id}/servers/{server_id}

Show one server.

The caller needs ``coast:server:get_one``. A server in another
stack or project returns 404.

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
   - stack_id: stack_id_path
   - server_id: server_id_path
   - fields: fields

Response
--------

The response uses the list server fields inside a ``server`` envelope.

Delete server
=============

.. rest_method:: DELETE /v1/stacks/{stack_id}/servers/{server_id}

Delete a worker or build server.

The caller needs ``coast:server:delete``. A ``control`` server returns
400. A server that still has a service returns 409. The status becomes
``DELETING``. The response code is 202. A body must be empty. An empty
JSON object is accepted. The service soft-deletes the row when cleanup
finishes, so history can keep the server id.

The real VM flow retains non-Docker volumes, clears their ``server_id``,
and keeps their storage quota. It removes Docker volumes. The simpler
noop remove does not prove this real-cloud retention flow. There is no
drain or force option; remove placed services and volume mounts first.

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
   - stack_id: stack_id_path
   - server_id: server_id_path

Response
--------

The response uses the show server fields. ``status`` is ``DELETING``.

Fail over server
================

.. rest_method:: POST /v1/stacks/{stack_id}/servers/{server_id}/action

Fail over a server.

The caller needs ``coast:server:failover`` (admin by default).
Send exactly ``{"failover": {}}``. For a controller, Coast rebuilds on its
recorded image, preserves the three volumes and network identity, and does
not change the engine version. A missing or wrong-version image returns
409 before the controller status changes. The status must be
``ACTIVE``, ``UNHEALTHY`` or ``ERROR``. An existing
``FAILOVER``/``FAILING_OVER`` task may be retried after
``[health_manager] admin_failover_stuck_timeout`` seconds since the last
server update (default 600). Fresh tasks and stacks held by another
failover still return 409. The status becomes ``FAILOVER``. The
response code is 202.

Controller failover accepts ERROR or STOPPED apps and reclaims expired app
claims. Live workload operations and unfinished backup jobs still return 409.
After the replacement controller is healthy, Coast marks abandoned deployments
FAILED and re-reads app status. App health does not determine controller
failover success; unhealthy servers can still leave the stack DEGRADED.

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
   - stack_id: stack_id_path
   - server_id: server_id_path
   - failover: failover_action

Response
--------

The response uses the show server fields. ``status`` is ``FAILOVER``.

Request example
---------------

.. literalinclude:: samples/server-failover-request.json
   :language: javascript

From 1.18, nested list and show accept ``all_projects=true`` for an admin
reading another project's stack. The endpoint policy is also required.
Without the flag, foreign resources return 404. Non-admin use returns
403. This flag does not permit cross-project writes.


Resize server (1.26)
====================

.. rest_method:: POST /v1/stacks/{stack_id}/servers/{server_id}/action

Requires ``OpenStack-API-Version: app-hosting 1.26`` and
``coast:server:resize`` (project member or admin). Send exactly::

    {"resize": {"flavor": "<operator flavor name or ID>"}}

The flavor maps to one operator VM plan for the server role. Plan names
are also accepted. Workers, build servers and the App engine server can
resize. Bare metal is refused. vCPUs, RAM, root disk and ephemeral disk must
all be at least the current size, and one must increase. Equal, smaller and
incomparable sizes return 400 with guidance to add a smaller worker and
move applications. Retained Cinder volumes do not grow.

Both stack and server must be ACTIVE and idle, with no other busy server,
service or pending deployment on the stack. Return 409 for busy or inactive
resources. Coast checks existing project quota limits; server and storage
counts do not increase, so no additional Coast quota is reserved.

Response: 202, ``{"server": <show-server representation>}``, with status
``FAILOVER`` and task ``FAILING_OVER`` where visible. Poll show-server until
ACTIVE or ERROR. Only successful health/workload checks persist the new
flavor; the App engine server also persists the stack plan. Admin-only
infrastructure fields keep their existing visibility. Volumes, retained
ports, fixed addresses, floating IP, image and engine version are kept.
Failure attempts to restore the old VM without changing flavor or plan.
Uncertain compensation leaves ERROR for operator recovery and keeps disks.

The stack event timeline records ``resize`` start/success/failure alongside
replacement detail. Notifications are ``coast.server.resize.start``,
``.end`` and ``.error``. No client-specific response fields are added.

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

400 includes invalid schema, unavailable flavor, non-growing size, bare
metal and versions below 1.26. 403 means policy refusal or OverQuota; 404
hides other projects' resources. 500 includes catalog or handoff failure.
Errors use the existing faultstring/faultcode/debuginfo envelope.
Asynchronous failures appear in status/fault and activity after 202.


Customer server flavor (1.27)
=============================

GET list and show include ``flavor`` from microversion 1.27 for project
readers and admins. For example::

    {"id": "<flavor-id>", "name": "Small", "vcpus": 1,
     "ram": 1024, "disk": 10}

RAM is in MiB and disk in GiB. Bare metal and VMs without a saved flavor
return null. A removed Nova flavor returns only the saved reference in
``id`` and ``name: null``. Nova failures return sanitized 503.
``fields=flavor`` is supported; filtering and sorting by flavor are not.
Versions through 1.26 and write responses retain their existing shape.
Admin-only ``flavor_id`` and other detail fields retain their policies.

List allowed flavors (1.27)
===========================

.. rest_method:: GET /v1/flavors

Requires ``OpenStack-API-Version: app-hosting 1.27`` and
``coast:flavor:get_all`` (project reader or admin). Returns 200::

    {"flavors": [{"id": "<flavor-id>", "name": "Small", "vcpus": 1,
                  "ram": 1024, "disk": 10, "plan": "small",
                  "roles": ["worker", "build"]}]}

``ram`` is MiB; ``disk`` is GiB. Only operator-mapped VM flavors are
included, ordered by plan name. ``roles`` contains the usable subset of
``control``, ``worker``, ``build``. Missing Nova flavors and roles with
ambiguous plan mappings are omitted; entries without usable roles are
omitted. An empty catalog returns ``{"flavors": []}``.

For add-server, send the entry's ``plan`` as ``server.plan``. For resize,
send it as ``resize.flavor`` or ``resize_workers.flavor``. Choose an entry
with the intended server role. Existing flavor name/ID mapping remains
supported. Resize also requires a strictly larger size and an idle ACTIVE
server/stack. Catalog inclusion does not guarantee Nova capacity.

No query parameters (400). Other methods return 405. Missing identity
returns 401; wrong role or scope returns 403; versions below 1.27 return
404; unsupported versions return 406. Invalid operator catalogs return
sanitized 500 and Nova failures return sanitized 503. Errors use the
existing ``faultstring``, ``faultcode``, ``debuginfo`` envelope.
