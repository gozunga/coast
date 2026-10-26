=======
Volumes
=======

A user can create a ``data`` volume. Coast creates ``docker``,
``scratch``, and ``controller`` volumes. Create returns status
``CREATING`` and code 202.

``mount_path`` defaults to ``/data``. ``purpose`` defaults to ``data``.

Delete and snapshot of a managed volume follow the purpose rules below.
Extend and delete return 400 for ``docker``, ``scratch``, and
``controller``, including for an administrator. Snapshot is allowed for
``data`` and ``docker`` only.

Extend does not change ``status``. The new ``size_gb`` must be greater
than the stored size. The call reserves only the increase.

Delete and snapshot move ``status`` with a compare-and-swap. A repeated
delete or snapshot returns 409. Delete from ``CREATING`` or
``SNAPSHOTTING`` returns 409 under the retained-data safety gate.
Snapshot from ``AVAILABLE`` or ``IN_USE`` stores ``SNAPSHOTTING``.

``cinder_volume_id`` is returned only when the caller passes
``coast:volume:get_detail``. A ``fields`` request for that name
without the policy returns 400.

A list can filter on the public volume fields.

Retained data cleanup uses the service-project Cinder SDK. Coast checks
ownership, attachments, snapshots, clones and unresolved exports, then
uses ordinary deletion with force and cascade disabled. Detached-volume
export remains deferred.

List volumes
============

.. rest_method:: GET /v1/stacks/{stack_id}/volumes

List volumes in one stack.

The caller needs ``coast:volume:get_all``.

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
   - volumes: volumes
   - id: id
   - project_id: project_id
   - stack_id: stack_id
   - server_id: server_id
   - name: name
   - size_gb: size_gb
   - volume_type: volume_type
   - mount_path: mount_path
   - purpose: purpose
   - status: status
   - created_at: created_at
   - updated_at: updated_at
   - cinder_volume_id: cinder_volume_id
   - links: links
   - rel: rel
   - href: href

Create volume
=============

.. rest_method:: POST /v1/stacks/{stack_id}/volumes

Create a data volume.

The caller needs ``coast:volume:create``. The response code is 202.
Status is ``CREATING``. The call reserves ``size_gb`` against
``volume_gb``.

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
   - 500

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - Content-Type: content_type
   - stack_id: stack_id_path
   - volume: volume_request
   - name: volume_name_request
   - size_gb: volume_size_request
   - server_id: volume_server_request
   - volume_type: volume_type_request
   - mount_path: mount_path_request
   - purpose: purpose_request

Request Example
---------------

.. literalinclude:: samples/volume-create-request.json
   :language: javascript

Response
--------

The response uses the list volume fields inside a ``volume`` envelope.
``status`` is ``CREATING``.

Response Example
----------------

.. literalinclude:: samples/volume-create-response.json
   :language: javascript

Show volume
===========

.. rest_method:: GET /v1/stacks/{stack_id}/volumes/{volume_id}

Show one volume.

The caller needs ``coast:volume:get_one``. A volume in another
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
   - volume_id: volume_id_path
   - fields: fields

Response
--------

The response uses the list volume fields inside a ``volume`` envelope.

Extend volume
=============

.. rest_method:: PATCH /v1/stacks/{stack_id}/volumes/{volume_id}

Increase the size of a data volume.

The caller needs ``coast:volume:update``. The response code is 202.
``status`` does not change. A managed purpose returns 400.

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
   - volume_id: volume_id_path
   - volume: volume_extend
   - size_gb: volume_extend_size

Response
--------

The response uses the show volume fields. ``size_gb`` is the requested
size. ``status`` is unchanged.

Delete volume
=============

.. rest_method:: DELETE /v1/stacks/{stack_id}/volumes/{volume_id}

Delete a data volume.

The caller needs ``coast:volume:delete``. A managed purpose returns
400. The status becomes ``DELETING``. The response code is 202. A body
must be empty. An empty JSON object is accepted. A status that cannot
move to ``DELETING`` returns 409. A server association, mount, active
creation, snapshot, unsettled extension or competing stack operation also
returns 409. Coast verifies Cinder ownership and attachments after
acceptance. Unresolved exports also block deletion. Acceptance acquires
stack ``RECONCILING``; the stack must be ``ACTIVE``, ``DEGRADED`` or
``ERROR`` with no competing task. Quota is released only after confirmed
Cinder absence.
Failure retains the row and quota with status ``ERROR``. Successful
cleanup removes the row; subsequent GET returns 404. A new authorized
DELETE from ``ERROR`` repeats the safety checks. A crashed operation
requires operator review and explicit replay, not automatic takeover.

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
   - volume_id: volume_id_path

Response
--------

The response uses the show volume fields. ``status`` is ``DELETING``.

Snapshot volume
===============

.. rest_method:: POST /v1/stacks/{stack_id}/volumes/{volume_id}/action

Snapshot a data or Docker volume.

The caller needs ``coast:volume:snapshot``. ``scratch`` and
``controller`` return 400. The status becomes ``SNAPSHOTTING``. The
response code is 202. A Docker snapshot covers only the ``volumes/``
subtree.

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
   - volume_id: volume_id_path
   - snapshot: snapshot_action

Response
--------

The response uses the show volume fields. ``status`` is
``SNAPSHOTTING``.

From 1.18, nested list and show accept ``all_projects=true`` for an admin
reading another project's stack. The endpoint policy is also required.
Without the flag, foreign resources return 404. Non-admin use returns
403. This flag does not permit cross-project writes.
