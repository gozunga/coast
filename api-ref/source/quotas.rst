======
Quotas
======

Show returns limits and usage counters. The seven resources are
``environments``, ``servers``, ``baremetal_servers``, ``build_servers``,
``apps``, ``domains``, and ``volume_gb``.

The configured defaults are 1 stack, 2 servers, 0 bare-metal
servers, 1 build server, 10 services, 20 domains, and 200 GiB. ``-1`` is
unlimited. ``0`` blocks new allocations. A null stored override uses
the default.

Show returns the effective integers in a ``quota`` object. It also
returns a ``usage`` object with ``in_use`` and ``reserved`` counts for
the same seven resources. The counts are the quota engine counters.
Empty counters are zero. The read does not create
a usage row. Defaults and update return the ``quota`` object only.
The list returns stored override rows, including nulls, in
``{"quotas": [...]}``. Rows are sorted by ``project_id``. A project
with no override row is absent. Usage rows are not listed.

``PUT`` accepts any subset of the seven limits and returns the
effective quota. An omitted field is unchanged. Null clears that
override. A lower limit does not delete existing resources.

``DELETE`` returns 204 and has no body. It is idempotent. It removes
the override row and keeps the usage sentinel and the counters.

A boolean, a value below ``-1``, a usage counter, ``project_id`` in
the body, or an unknown field returns 400. A ``DELETE`` body returns
400. A query parameter on a supported method returns 400. An
unsupported method returns 405 before that query check.

The path project ID has 1 to 36 characters. It does not have to be a
UUID. A non-administrator path for another project returns 404. The
path segment ``defaults`` is the defaults operation, not a project
ID.

Create reserves capacity in the resource transaction. Volume extend
reserves only the increase. A bare-metal server and a build server
also consume ``servers``. A control server does not consume
``servers``. A stack create reserves the requested child
servers and the known initial data capacity. A custom domain counts.
The computed default domain does not. Over quota returns 403.
``faultstring`` is ``Quota exceeded for resources: ...``.

Export concurrency and export size use ``[export]`` limits. They are
not these seven fields. See the export create call.

Show quota
==========

.. rest_method:: GET /v1/quotas/{project_id}

Show the effective quota and the usage counters for one project.

The caller needs ``coast:quota:get_one``. A project reader can read
the token project. An administrator can read any project.

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
   - project_id: project_id_path

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - quota: quota
   - environments: quota_environments
   - servers: quota_servers
   - baremetal_servers: quota_baremetal_servers
   - build_servers: quota_build_servers
   - apps: quota_apps
   - domains: quota_domains
   - volume_gb: quota_volume_gb
   - usage: quota_usage
   - in_use: quota_usage_in_use
   - reserved: quota_usage_reserved

Response Example
----------------

.. literalinclude:: samples/quota-show-response.json
   :language: javascript

Show quota defaults
===================

.. rest_method:: GET /v1/quotas/defaults

Show the configured default quota.

The caller needs ``coast:quota:get_defaults``. A project reader or
an administrator can call this method. The body uses the same
``quota`` object as show. It does not include ``usage``.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 405
   - 406
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token

Response
--------

The response uses the show ``quota`` fields. It does not include
``usage``.

List quotas
===========

.. rest_method:: GET /v1/quotas

List stored quota overrides.

The caller needs ``coast:quota:get_all``. Only an administrator can
call this method.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 405
   - 406
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - quotas: quotas
   - project_id: quota_project_id
   - environments: quota_environments
   - servers: quota_servers
   - baremetal_servers: quota_baremetal_servers
   - build_servers: quota_build_servers
   - apps: quota_apps
   - domains: quota_domains
   - volume_gb: quota_volume_gb

Response Example
----------------

.. literalinclude:: samples/quota-list-response.json
   :language: javascript

Update quota
============

.. rest_method:: PUT /v1/quotas/{project_id}

Update quota overrides for one project.

The caller needs ``coast:quota:update``. Only an administrator can
call this method. The response code is 200. The response is the
effective quota.

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
   - project_id: project_id_path
   - quota: quota_request
   - environments: quota_limit_request
   - servers: quota_limit_request
   - baremetal_servers: quota_limit_request
   - build_servers: quota_limit_request
   - apps: quota_limit_request
   - domains: quota_limit_request
   - volume_gb: quota_limit_request

Response
--------

The response uses the show ``quota`` fields and does not include
``usage``. Null overrides are returned as the effective integers.

Request example
---------------

.. literalinclude:: samples/quota-update-request.json
   :language: javascript

Response example
----------------

.. literalinclude:: samples/quota-update-response.json
   :language: javascript

Delete quota
============

.. rest_method:: DELETE /v1/quotas/{project_id}

Reset quota overrides for one project.

The caller needs ``coast:quota:delete``. Only an administrator can
call this method. The response code is 204. There is no response
body. A request body returns 400. The call keeps usage counters.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 204

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
   - project_id: project_id_path

Response
--------

This method returns no body.

Quota resource names
====================
``environments`` counts infrastructure stacks, not app group environments.
``apps`` counts Coast services. No app group or environment quota is added.
