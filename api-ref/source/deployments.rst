===========
Deployments
===========

Deployments belong to one service. A read requires
``coast:deployment:get_all``, ``coast:deployment:get_one``, or
``coast:deployment:get_log``. Create, rollback, and cancel require
the matching member policy.

The public fields are ``id``, ``project_id``, ``service_id``, ``number``,
``source_ref``, ``server_id``, ``trigger``, ``rollback_of``,
``status``, ``started_at``, ``finished_at``, ``created_at``,
``updated_at``, and ``resolved_ref``. ``resolved_ref`` is the exact
commit SHA or image digest used. It is null until the driver reports it
and is read-only. ``spec`` and ``image_digest`` are not returned.

``number`` is the next integer for that service. Deleted deployments stay
in the count. Automatic app group recovery after worker failover uses
the existing ``reconcile`` trigger. It copies the current deployment and
does not enqueue again after an uncertain engine reply.
An API create uses trigger ``api``. A rollback uses
``rollback``.

Create stores status ``PENDING`` and sets the service task to
``DEPLOYING``. The response code is 202. Rollback stores a new
``PENDING`` deployment and sets the service task to ``ROLLING_BACK``.
Cancel stores ``CANCELLED`` and keeps the existing service task.

A list can filter on the public deployment columns. You cannot sort
or filter on ``resolved_ref``.

List deployments
================

.. rest_method:: GET /v1/services/{service_id}/deployments

List deployments for one service.

The caller needs ``coast:deployment:get_all``. A bad marker returns
404.

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
   - deployments: deployments
   - id: id
   - project_id: project_id
   - service_id: service_id_body
   - number: number
   - source_ref: source_ref
   - server_id: server_id
   - trigger: trigger
   - rollback_of: rollback_of
   - status: status
   - started_at: started_at
   - finished_at: finished_at
   - created_at: created_at
   - updated_at: updated_at
   - resolved_ref: resolved_ref
   - links: links
   - rel: rel
   - href: href

Create deployment
=================

.. rest_method:: POST /v1/services/{service_id}/deployments

Create a deployment.

The caller needs ``coast:deployment:create``. The body is a
``deployment`` object. It can be empty. ``source_ref`` defaults to
the service source branch. The response code is 202. Status is
``PENDING``. A deleting or busy service returns 409.

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
   - service_id: service_id
   - deployment: deployment_request
   - source_ref: source_ref_request

Response
--------

The response uses the list deployment fields inside a ``deployment``
envelope.

Response Example
----------------

.. literalinclude:: samples/deployment-create-response.json
   :language: javascript

Show deployment
===============

.. rest_method:: GET /v1/services/{service_id}/deployments/{dep_id}

``fault`` is a short Coast-written reason for a ``FAILED`` deployment, for
example "App is running but nothing answered on port 8080. Check the port
setting." It never contains engine output. It is null otherwise.

Show one deployment.

The caller needs ``coast:deployment:get_one``. The deployment must
belong to this service. Another project returns 404.

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
   - dep_id: dep_id
   - fields: fields

Response
--------

The response uses the list deployment fields inside a ``deployment``
envelope.

.. literalinclude:: samples/deployment-show-response.json
   :language: javascript

Roll back deployment
====================

.. rest_method:: POST /v1/services/{service_id}/deployments/{dep_id}/action

Roll back to an older deployment, or cancel the latest in-flight
deployment.

Send exactly one key. The value is an empty object.

Rollback needs ``coast:deployment:rollback``. The target status must
be ``ACTIVE``, ``SUPERSEDED``, or ``ROLLED_BACK``. Its ``number`` must
be lower than the current deployment. The call creates a new
deployment, copies the stored spec and image digest, and sets the service
task to ``ROLLING_BACK``. Its ``source_ref`` uses the target
``resolved_ref`` when available, otherwise its original ``source_ref``.
The response is that new deployment. Its
status is ``PENDING``, its trigger is ``rollback``, and
``rollback_of`` is the target ID. The response code is 202. A
deleting service, a missing current deployment, or a target that is not
older returns 409.

Cancel needs ``coast:deployment:cancel``. The service task must be
``PROVISIONING``, ``DEPLOYING``, or ``ROLLING_BACK``. The target must
be the latest deployment with status ``PENDING``, ``BUILDING``, or
``DEPLOYING``. The call sets status ``CANCELLED`` and keeps the service task. The response
code is 202. Any other case returns 409.

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
   - service_id: service_id
   - dep_id: dep_id
   - rollback: deployment_action
   - cancel: deployment_action

Response
--------

Rollback returns the new deployment. Cancel returns the cancelled
deployment. Both use the list deployment fields.

Show deployment log
===================

.. rest_method:: GET /v1/services/{service_id}/deployments/{dep_id}/log

Show the deployment log.

The caller needs ``coast:deployment:get_log``. This call accepts no
query parameters. Noop, a missing service engine reference, or a missing
deployment engine reference returns ``complete`` false and an empty
``lines`` list. Live reads return driver line objects with ``ts``,
``stream``, and ``line``. Build output that the engine marks hidden
is included in ``line``, and a blank hidden build entry is
``Build step in progress.``. Use the deployment status, not the log
``complete`` flag, as the authoritative success/failure result. For live
reads, ``complete`` is false while an application deployment is
``PENDING``, ``BUILDING``, or ``DEPLOYING``. Template deployment logs are a
completed snapshot of service container output. With no unambiguous target
or no available service logs, the snapshot is empty and complete. Missing
engine log resources also return empty lines with ``complete: true`` and
are not retried. HTTP 403 from the template service logs route, including
an older controller agent's rejection, also returns empty, complete output
without retries. HTTP 401 and failures on service metadata or env reads
retain their existing error handling. The call
returns 503 when the engine is unreachable or the engine rejects the
Coast credential. ``faultstring`` is ``Service Unavailable``. The body
does not include the engine error. Another engine fault returns 500.

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
   - dep_id: dep_id

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - log: log
   - deployment_id: log_deployment_id
   - complete: log_complete
   - lines: log_lines
   - ts: log_ts
   - stream: log_stream
   - line: log_line

Response Example
----------------

.. literalinclude:: samples/deployment-log-response.json
   :language: javascript

Live response example
---------------------

.. literalinclude:: samples/deployment-log-live-response.json
   :language: javascript


Service status after deployment failure
=======================================

A FAILED deployment keeps the service ACTIVE only when a previous release
exists and the engine reports it ACTIVE. Otherwise the service becomes
ERROR. Crash loops always produce ERROR. The previous deployment pointer
and history are kept. FAILED is a deployment state, not a service state.
