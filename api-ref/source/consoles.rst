==============
Shell sessions
==============

Open a shell on an application or a service. Requires microversion 1.9.
Older versions return 404. A project member or admin can call these
routes. A reader receives 403. A manager-only role receives 403.

The body is optional. Omit it, or send ``{"command": null}``, for an
interactive shell. Otherwise ``command`` is an array of 1 to 32
non-empty strings. Each string is at most 256 characters.

The application route accepts a workload with no template id. The
service route accepts a workload with a template id. The other route
returns 404. A resource in another project returns 404.

The workload must be active and placed on a worker that has a
management address. Otherwise the response is 409 and ``faultstring``
is ``The app is not available.`` No token is stored.

The URL is single-use. When ``[console] tls`` is false, the scheme is
``ws``. When the public host is not configured, the response is 503 and
``faultstring`` is ``Service Unavailable``.

An idle session closes after 15 minutes. Every session closes after 4
hours. The stack timeline records open and close. It does not record
the command, keystrokes, or output.

Open an application shell
=========================

.. rest_method:: POST /v1/apps/{app_id}/console

Open a shell on one application.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 201

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404
   - 405
   - 406
   - 409
   - 500
   - 503

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - Content-Type: content_type
   - app_id: app_id
   - command: console_command

Request Example
---------------

.. literalinclude:: samples/console-create-request.json
   :language: javascript

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - Vary: vary
   - url: console_url
   - expires_at: console_expires_at

Response Example
----------------

.. literalinclude:: samples/console-create-response.json
   :language: javascript

Open a service shell
====================

.. rest_method:: POST /v1/services/{service_id}/console

Open a shell on one service. The request, the response and the error
codes match the application shell. The path parameter is the service id.
The policy is ``coast:service:console``.

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - Content-Type: content_type
   - service_id: service_id
   - command: console_command
