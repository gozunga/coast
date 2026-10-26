======
Stacks
======

A stack groups the control server, workers, build servers, and
volumes for one project. Create returns status ``PENDING``. The
response does not include ``workers``, ``build_server``, or
``data_volume_gb``.

List and show return the public fields, including ``fault``. An
administrator who passes
``coast:stack:get_detail`` also receives ``driver``, ``network_id``,
``engine_ref`` and ``task_state``.

Public ``fault`` values use generic engine and routing names and plain
hyphens. The stack detail policy preserves the masked diagnostic fault
for administrators. Deletion failure events use the public wording.

From 1.18, the same policy adds ``subnet_id``, ``router_id``,
``sg_ingress_id``, ``sg_mgmt_id``, ``server_count`` and ``workload_count``.
Counts include all live server roles and both workload kinds in services;
soft-deleted rows are excluded. Empty counts are zero. These read-only
fields support ``fields`` but cannot be filter or sort keys.

``project_id`` remains the owner. Nova servers and Cinder volumes live
in the project of ``[service_auth]``. ``service_project_id`` is returned
only when that project differs from the owner, under the same detail
policy. Failure to resolve that project returns 500.

At 1.18, stack show, nested server/volume list and show, and stack event
list accept ``all_projects=true``. This requires ``ADMIN`` in addition to
the endpoint policy. Foreign resources otherwise return 404, including
for admins. Non-admin use returns 403. The flag does not change writes.
Existing stack, service and flat event list support remains unchanged.
For example, use ``GET /v1/services?stack_id={id}&all_projects=true`` for
all workloads on a stack.

A list can filter on ``id``, ``project_id``, ``name``, ``description``,
``plan``, ``engine_version``, ``status``, ``created_at``,
and ``updated_at``.

List stacks
===========

.. rest_method:: GET /v1/stacks

List stacks in the token project.

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

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - stacks: stacks
   - id: id
   - project_id: project_id
   - name: name
   - description: description
   - plan: plan
   - driver: driver
   - engine_version: engine_version
   - status: status
   - created_at: created_at
   - updated_at: updated_at
   - subnet_id: stack_subnet_id
   - router_id: stack_router_id
   - sg_ingress_id: stack_sg_ingress_id
   - sg_mgmt_id: stack_sg_mgmt_id
   - service_project_id: stack_service_project_id
   - server_count: stack_server_count
   - workload_count: stack_workload_count
   - network_id: network_id
   - engine_ref: engine_ref
   - task_state: task_state
   - fault: fault
   - links: links
   - rel: rel
   - href: href

Create stack
============

.. rest_method:: POST /v1/stacks

Create a stack.

The call requires ``coast:stack:create``. The new row uses
status ``PENDING`` and task ``PROVISIONING``. ``driver`` and
``engine_version`` come from the operator configuration. The call
requires member or admin by default (Q96). It
reserves one stack, each requested worker, the optional VM build
server, and the requested data capacity. Over-quota returns 403.
A duplicate live name returns 409. The response code is 202.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 202

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403: over_quota
   - 406
   - 409
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - Content-Type: content_type
   - stack: stack_request
   - name: stack_name_request
   - description: stack_description_request
   - plan: stack_plan_request
   - image: stack_image_request
   - workers: workers
   - kind: worker_kind
   - plan: worker_plan
   - image: worker_image_request
   - build_server: build_server
   - build_server_plan: build_server_plan
   - build_server_image: build_server_image
   - data_volume_gb: data_volume_gb

Microversion 1.1 stack create requires one or more VM workers (the
project quota limits the count; each worker may set its own ``plan`` and
``image``; Q151). Image selections are accepted from microversion 1.1 onward.
Later versions retain optional workers and accept VM or bare-metal workers;
bare metal requires the operator to enable it.
At every microversion, Coast validates controller, worker, and optional
build plans against the operator catalog, including the selected kind and
role, before creating a stack, reserving quota, or contacting the conductor.
Unknown plans return 400 naming the plan and directing callers to
``openstack coast plan list``. Incompatible plans return 400 naming the
selection and required role and kind.
``build_server`` remains a boolean. Build selections without a true flag
return 400. Image names or IDs must resolve to exactly one allowed image;
missing, ambiguous, inactive, wrong-owner, wrong-tag, or wrong-role images
return 400. An explicit image replaces the configured exact image ID but
retains all catalog checks. Image names resolve to IDs before RPC and the
conductor checks the catalog again before boot. New fields are request-only.
Version 1.0 rejects the new fields and retains its worker-count rules.
Quota semantics do not change. Additional servers use ``POST /servers``.

Request Example
---------------

.. literalinclude:: samples/stack-create-request.json
   :language: javascript

Microversion 1.1 example with independent role selections:

.. literalinclude:: samples/stack-create-1.1-request.json
   :language: javascript

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - stack: stack
   - id: id
   - project_id: project_id
   - name: name
   - description: description
   - plan: plan
   - driver: driver
   - engine_version: engine_version
   - status: status
   - created_at: created_at
   - updated_at: updated_at
   - links: links
   - rel: rel
   - href: href

Response Example
----------------

.. literalinclude:: samples/stack-create-response.json
   :language: javascript

Show stack
==========

.. rest_method:: GET /v1/stacks/{stack_id}

Show one stack.

The caller needs ``coast:stack:get_one``. Another project
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

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - stack: stack
   - id: id
   - project_id: project_id
   - name: name
   - description: description
   - plan: plan
   - driver: driver
   - engine_version: engine_version
   - status: status
   - created_at: created_at
   - updated_at: updated_at
   - subnet_id: stack_subnet_id
   - router_id: stack_router_id
   - sg_ingress_id: stack_sg_ingress_id
   - sg_mgmt_id: stack_sg_mgmt_id
   - service_project_id: stack_service_project_id
   - server_count: stack_server_count
   - workload_count: stack_workload_count
   - network_id: network_id
   - engine_ref: engine_ref
   - task_state: task_state
   - fault: fault
   - links: links
   - rel: rel
   - href: href

Update stack
============

.. rest_method:: PATCH /v1/stacks/{stack_id}

Update the name, the description, or both.

The caller needs ``coast:stack:update``. The call is synchronous
and returns 200. It does not change the status and it does not redeploy
services. Unknown fields return 400.

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
   - Content-Type: content_type
   - stack_id: stack_id_path
   - stack: stack_update
   - name: stack_name_request
   - description: stack_description_request

Response
--------

The response uses the show stack fields.

Delete stack
============

.. rest_method:: DELETE /v1/stacks/{stack_id}

Delete a stack.

Stack deletion refuses with HTTP 409 while app groups remain. The error
names the stack and its app groups. Delete their services, environments
and app groups first; administrators have no force override. This behavior
fix applies to all supported microversions.

A teardown failure sets ERROR, releases the task and records the masked
reason in ``fault`` (visible on stack list and show). Child deletion works
on an ERROR stack. Retry stack deletion after child cleanup; missing cloud
resources are skipped.

The caller needs ``coast:stack:delete``. The status becomes
``DELETING``. The response code is 202. A body is optional. When you
send a body, send a JSON object of delete options, not an
``stack`` envelope. A status that cannot move to ``DELETING``
returns 409.

``export_before_delete=true`` is supported. Real teardown runs the
export to completion before it deletes cloud resources. The export
and its Swift objects remain after the stack is deleted. Normal
export retention still applies. An export failure or timeout sets
the stack to ERROR and deletes nothing. You can set this flag with
``snapshot_volumes``. ``snapshot_retention_days`` without
``snapshot_volumes=true`` returns 400 before any write (Q102). The
stack does not change. ``snapshot_volumes=true`` is supported. Real
stack teardown (!89) creates one Cinder snapshot for each volume and
keeps that snapshot after the volume is deleted (!90). A 202 or a
later 404 does not prove removal of every real cloud resource. The
delete response does not list snapshot ids. Noop cleanup with both
flags false, or with the body omitted, is tested. The noop
fake-cloud path still rejects ``export_before_delete`` after the API
accepts it. Remove services first; noop stack cleanup can leave
service history visible.

Whole-stack teardown continues cloud cleanup when engine cleanup raises
``EngineUnreachable``. A durable ``delete.engine.skip`` event records the
skip with outcome ``failure``. Authentication rejection and other engine
errors still stop deletion with ERROR. Export requirements and volume
safety checks do not change. Quota completion and the
``coast.environment.delete.end`` notification require full teardown.
The skip does not change individual resource deletion or add certificate
recovery.

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
   - Content-Type: content_type
   - stack_id: stack_id_path
   - snapshot_volumes: snapshot_volumes
   - snapshot_retention_days: snapshot_retention_days
   - export_before_delete: export_before_delete

Response
--------

The response uses the show stack fields. ``status`` is
``DELETING``.

Upgrade stack
=============

.. rest_method:: POST /v1/stacks/{stack_id}/action

Start a rolling VM replacement from a newer published App engine server image.
The image must be active, operator-owned, tagged for publication, and have
the operator's controller role and engine properties, and an allow-listed
``coast_engine_version``. A newer engine version is allowed. For the same
version, require a different image UUID and a later Glance creation time than
the recorded controller image. Engine downgrades are refused.

Replace the controller first, then each worker and build VM using the
configured published worker image for that engine version. Each replacement
must complete before the next starts. Stop on the first failure. Retain
volumes, ports and addresses for each server. A partial rollout leaves the
stack in ``ERROR`` and requires operator review.

.. literalinclude:: samples/stack-upgrade-request.json
   :language: javascript

The caller needs ``coast:stack:upgrade`` (project member or admin). The stack must
be ``ACTIVE``. A deployment in ``PENDING``, ``BUILDING``, or
``DEPLOYING`` returns 409. The status becomes ``UPGRADING``. The
response code is 202.

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
   - upgrade: upgrade_action
   - image_id: upgrade_image_id

Response
--------

The response uses the show stack fields. ``status`` is
``UPGRADING``.


Rollback engine upgrade
=======================

.. rest_method:: POST /v1/stacks/{stack_id}/action

Restore the recorded previous engine image and all three retained-volume
snapshots through the same controller rebuild path. The caller needs
``coast:stack:upgrade_rollback`` (project member or admin). The stack must
be ``ACTIVE`` or ``ERROR`` with task ``NONE`` and a complete rollback point.
Busy services or deployments return 409. The accepted status is
``ROLLING_BACK``. Success returns the stack to ``ACTIVE``; failure sets
``ERROR``. Retained snapshot and displaced-volume storage is billable.
Internal rollback references are never returned in stack representations.

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
   - upgrade_rollback: upgrade_rollback_action

.. literalinclude:: samples/stack-upgrade-rollback-request.json
   :language: javascript

Response
--------

The response uses the show stack fields. ``status`` is ``ROLLING_BACK``.

Nova ID history
===============

Server create/delete events record ``compute_id`` in timeline detail.
Failover receipts preserve ``old_compute_id`` and ``new_compute_id``.
Upgrade and rollback stack events also record both IDs and ``server_id``.
Interrupted replacements preserve checkpoint IDs; no boot means a null
new ID. Existing events are not backfilled. Event policies are unchanged.
Stack event next links preserve ``all_projects``; markers from another
stack return 404.

Unclaimed stack creation
------------------------

A failed conductor handoff returns HTTP 500. If the conductor has not
claimed the stack, Coast marks it ERROR and releases its task and quota
reservation so the caller can delete it. DELETE also cancels a PENDING
stack whose PROVISIONING task has no owning conductor. A delayed create
message cannot restart the cancelled operation. An owned operation still
returns HTTP 409 on DELETE.

For an unclaimed stack without an acceptance receipt, DELETE recovers the
outstanding stack quota counters only when no other stack/server create or
volume operation in the project could own them. Otherwise it returns HTTP
409 for retry after those operations finish. Unrelated quota resources and
other projects are preserved.


Resize all workers (1.26)
========================================

.. rest_method:: POST /v1/stacks/{stack_id}/action

Requires ``OpenStack-API-Version: app-hosting 1.26`` and
``coast:stack:resize_workers`` (project member or admin). Send exactly::

    {"resize_workers": {"flavor": "<operator flavor name or ID>"}}

Uses the same flavor mapping, strict growth, quota and error contract as
server resize. Every worker must qualify before any is changed. Empty
worker sets return 400. Stack and workers must be ACTIVE and idle; busy
servers, services or deployments return 409. Build and App engine servers
are excluded from this action and use individual server resize.

Response: 202, ``{"stack": <show-stack representation>}``. Stack status
stays ACTIVE and task is FAILING_OVER where visible. Workers run in stable
UUID order, one at a time, waiting for guest, agent and workload health.
Stop at the first failure; preserve completed workers and leave unstarted
workers unchanged. The stack stays locked until the operation ends.

Poll the existing stack activity timeline for action ``resize_workers``;
its terminal outcome is ``success`` or ``failure`` and detail includes
``completed_server_ids`` and nullable ``failed_server_id``. Notifications
are ``coast.environment.resize_workers.{start,end,error}``. A partial
rollout is a failure even when some workers completed. No automatic retry
or rollback of completed workers occurs.

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

Versions below 1.26 return 400. Invalid schema, unavailable flavors,
non-growing sizes and empty worker sets return 400; policy/OverQuota is
403; absent or foreign stack is 404. Errors use the existing JSON fault
envelope. Asynchronous failure is recorded in activity after 202.

Engine release selection
------------------------

The cloud's configured default engine version chooses ``engine_version``
for new stacks only. Existing stacks retain their recorded version. Upgrade
selects a newer supported published image; ordinary upgrade refuses a
downgrade. ``upgrade_rollback`` rebuilds from the recorded previous image
and snapshots, including after a partial upgrade failure. Worker and build
images must match the target engine version.
