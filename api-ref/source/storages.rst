========
Storages
========

GET collection and show requests accept ``all_projects=true`` or
``project_id`` for cross-project reads when the caller passes the admin
policy as well as the endpoint policy. Non-admin use returns 403.
Without either parameter, foreign resources return 404 even for admins.
These parameters do not extend write access across projects.

A storage is a named volume on the worker Docker disk. These routes
require microversion 1.10. An older microversion returns 404.
``/v1/stacks/{stack_id}/volumes`` remains the Cinder guest-disk
collection.

Create returns ``AVAILABLE``, ``origin=api``, and null ``service_id``,
``server_id``, and ``container_path``. Attach requires a non-template
service in the same stack and an ``ACTIVE`` worker. The first attach
may use any such service. A later attach must use the same service.
Detach keeps ``service_id`` and ``server_id`` and clears
``container_path``.

Delete is allowed only while ``AVAILABLE``. ``IN_USE`` returns 409.
``origin=template`` returns 409 for attach, detach, and delete.
A host path, bind, or ``read_only`` field returns 400.

``storage_gb`` is the project quota. It is separate from ``volume_gb``.
The sum of storage sizes on a worker, including detached rows, must fit
that worker's Docker disk.

List storages
=============

.. rest_method:: GET /v1/stacks/{stack_id}/storages

List storages in one stack. From microversion 1.16, a failed conductor
storage read returns 503 without a partial list. The fault string is
"Storage details are unavailable right now. Try again in a moment."

The caller needs ``coast:storage:get_all``. A reader can call this.

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
   - 503

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
   - storages: storages
   - id: id
   - project_id: project_id
   - stack_id: stack_id
   - name: name
   - size_gb: size_gb
   - status: status
   - origin: storage_origin
   - source: storage_source
   - type: storage_mount_type
   - host_path: storage_host_path
   - read_only: storage_read_only
   - service_id: storage_service_id
   - server_id: server_id
   - container_path: storage_container_path
   - created_at: created_at
   - updated_at: updated_at
   - links: links
   - rel: rel
   - href: href

Create storage
==============

.. rest_method:: POST /v1/stacks/{stack_id}/storages

Create one storage.

The caller needs ``coast:storage:create``. A member can call this.
``name`` matches ``^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$``. A duplicate name
in the stack returns 409. Quota failure returns 403.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 201

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
   - storage: storage_request
   - name: name
   - size_gb: size_gb

Request Example
---------------

.. code-block:: json

   {
     "storage": {
       "name": "uploads",
       "size_gb": 10
     }
   }

Show storage
============

.. rest_method:: GET /v1/stacks/{stack_id}/storages/{storage_id}

Show one storage.

The caller needs ``coast:storage:get_one``.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

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
   - storage_id: storage_id_path

Response
--------

.. rest_parameters:: parameters.yaml

   - storage: storage
   - id: id
   - name: name
   - size_gb: size_gb
   - status: status
   - origin: storage_origin
   - source: storage_source
   - type: storage_mount_type
   - host_path: storage_host_path
   - read_only: storage_read_only
   - service_id: storage_service_id
   - server_id: server_id
   - container_path: storage_container_path

Attach storage
==============

.. rest_method:: POST /v1/stacks/{stack_id}/storages/{storage_id}/attach

Attach a storage to one service.

The caller needs ``coast:storage:attach``. The stack task must be
``NONE``. The container path must be unique among ``IN_USE`` rows of
that service. A missing Docker disk, or a size that does not fit,
returns 409.

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
   - stack_id: stack_id_path
   - storage_id: storage_id_path
   - attach: attach
   - service_id: storage_service_id
   - container_path: storage_container_path_request

Request Example
---------------

.. code-block:: json

   {
     "attach": {
       "service_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
       "container_path": "/var/data"
     }
   }

Detach storage
==============

.. rest_method:: POST /v1/stacks/{stack_id}/storages/{storage_id}/detach

Detach a storage.

The caller needs ``coast:storage:detach``. The body is an empty object.

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
   - stack_id: stack_id_path
   - storage_id: storage_id_path

Request Example
---------------

.. code-block:: json

   {}

Delete storage
==============

.. rest_method:: DELETE /v1/stacks/{stack_id}/storages/{storage_id}

Delete a detached storage.

The caller needs ``coast:storage:delete``. A manager can call this.
The response has no body.

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
   - 409
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - stack_id: stack_id_path
   - storage_id: storage_id_path

Microversion 1.16: Observed named volumes
=========================================

List also reads named volumes from each deployed service through the
engine public API. Use ``service_id`` to filter for the Storage tab.
Responses add ``source``: ``coast`` for Coast-created storage and
``template`` for template or Compose storage. ``origin`` is retained.
Observed volumes without an account have null ``size_gb`` and stable
service-scoped UUIDs. Their timestamps are the service timestamps.
GET does not create accounting rows or reserve quota. File mounts and
host sources are excluded. Engine connection/authentication failures
return 503; other observation failures return 500, without partial results.

Deployed service lists exclude template accounts for mounts absent from
the workload; their quota accounts remain. Undeployed service accounts and
Coast-created detached accounts remain visible. Engine-only items support
GET at the existing item route. Attach, detach, PATCH and DELETE return
409 with a read-only message. Filters, fields, sort and paging include
observations; ``source`` is a supported filter, field and sort key.
Earlier versions retain the accounting list and omit ``source``.

Stock templates can expose one additional observed entry named
``Docker socket (from template)`` at ``/var/run/docker.sock``. It has
``source=template``, null ``size_gb``, and the same read-only 409 controls.
This exception does not permit socket mounts through storage request bodies
or Compose PUT. No other host source is returned.


Typed mounts (microversion 1.19)
================================

From 1.19, POST creates and attaches a service mount. Bind request::

  {"storage": {"type": "bind", "host_path": "/srv/data",
    "container_path": "/data", "read_only": false, "service_id": "<uuid>"}}

Named volume request::

  {"storage": {"type": "volume", "name": "pgdata", "size_gb": 10,
    "container_path": "/var/lib/postgresql/data", "read_only": false,
    "service_id": "<uuid>"}}

The required fields are ``type``, ``service_id``, ``container_path`` and
that type's source fields. Mode defaults to read-write. Binds reject name
and size; volumes reject host_path. Host paths must be absolute and
normalized, with no dot segments, repeated/trailing separators, control
characters, colon or backslash. Root is valid. No worker path denylist is
added. Binds have null name/size and no storage capacity charge.

The worker must be active in the same stack. Writes require an idle stack
and service. Create returns 201 and ``IN_USE``. Writes set
``X-Coast-Redeploy-Required: true``; deploy/rollback and reconcile apply the
configuration. Missing bind folders are created on application. Delete
leaves the host folder and volume bytes in place.

GET with ``service_id`` lists every user or template mount. Entries include
``id``, ``type`` (``bind`` or ``volume``), ``source`` (host path or volume
source), ``container_path``, ``read_only``, and ``origin`` (``user`` or
``template``). Template entries have null IDs and no item links; anonymous
template volumes have null sources. Other metadata fields remain available.
Template entries are immutable. A template with added user mounts must
have one container; ambiguity or a template target conflict fails apply.

.. rest_method:: PATCH /v1/stacks/{stack_id}/storages/{storage_id}

Update a user mount with the ``storage`` envelope. Accepts ``container_path``
and ``read_only``, plus ``host_path`` for binds or ``name`` for volumes.
Type, service and size are fixed. Source edits do not move or rename data.
Requires ``coast:storage:attach`` (member).
Success returns 200 and the storage object. Schema errors return 400;
wrong project returns 404; a busy workload or template row returns 409.

At 1.19, DELETE also permits attached user mounts (204). The existing delete
policy requires a manager. Binds use create/PATCH/delete; named volumes also support
the attach/detach routes.
