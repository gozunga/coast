=====================
Environment variables
=====================

Environment variables belong to one service. Read and write require
``coast:secret:get_all`` or ``coast:secret:update``. Both policies
allow a project member or an administrator.

The response is a map. Each key has ``value``, ``secret``, ``version``
and the stored ``build_time`` flag in GET, PUT and PATCH responses.
A secret entry returns ``value`` null. The service stores an opaque
reference, not the value.

A name matches ``^[A-Za-z_][A-Za-z0-9_]*$`` and has at most 255
characters. ``value`` is required. ``secret`` and ``build_time``
are booleans. ``secret`` defaults false. From microversion 1.6,
``build_time`` defaults true for applications and false for template
services. At 1.0 through 1.5 it defaults false. Template services reject
true with HTTP 400 at every version.

PUT replaces the whole map. A key that you omit is deleted. A flag
that you omit uses the create default above.

PATCH merges the map. A null entry deletes that key. A flag that you
omit keeps its stored value. A new key still defaults a missing flag
to the create default above.

PUT and PATCH return 200 and set
``X-Coast-Redeploy-Required: true``. They do not start a
deployment. A busy or deleting service returns 409. These calls accept
no query parameters.

Show environment variables
==========================

.. rest_method:: GET /v1/services/{service_id}/env

Show the environment variables for one service.

The caller needs ``coast:secret:get_all``. Secret values are null.

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

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - env: env
   - value: env_value
   - secret: env_secret
   - version: env_version
   - build_time: env_build_time

Response Example
----------------

.. literalinclude:: samples/env-show-response.json
   :language: javascript

Replace environment variables
=============================

.. rest_method:: PUT /v1/services/{service_id}/env

Each submitted value is limited to ``[secrets] max_secret_bytes``
(default 20000 UTF-8 bytes). Oversized values return 400 naming the limit
before any submitted values are stored. The same limit applies to PATCH
and values supplied when creating a service.

Replace every variable on this service.

The caller needs ``coast:secret:update``. The response code is 200.
Omitted keys are deleted. Omitted ``secret`` defaults to false. Omitted
``build_time`` defaults to true for apps at microversion 1.6 and later,
and false for templates (and for apps at 1.0 through 1.5). The response
header ``X-Coast-Redeploy-Required`` is ``true``.

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
   - env: env_body
   - value: env_value
   - secret: env_secret
   - build_time: env_build_time

Response
--------

The response uses the show fields. The response also sets this
header:

.. rest_parameters:: parameters.yaml

   - X-Coast-Redeploy-Required: x_coast_redeploy_required
   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version

Update environment variables
============================

.. rest_method:: PATCH /v1/services/{service_id}/env

Merge environment variables.

The caller needs ``coast:secret:update``. The response code is 200.
A null entry deletes that key. Omitted flags keep their stored
values. The response header ``X-Coast-Redeploy-Required`` is
``true``.

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
   - env: env_body
   - value: env_value
   - secret: env_secret
   - build_time: env_build_time

Response
--------

The response uses the show fields and sets
``X-Coast-Redeploy-Required: true``.
