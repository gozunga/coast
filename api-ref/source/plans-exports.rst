=================
Plans and exports
=================

Plans come from the operator catalog. Exports belong to one
stack.

List plans
==========

.. rest_method:: GET /v1/plans

List the operator plans.

Plan descriptions returned to non-admin callers replace engine product names
with "App engine server". Operators should use neutral catalog descriptions.

Any authenticated project user can call this method. The policy is
``coast:plan:get_all``. The catalog is the JSON array in
``[plans] definitions``. The response is ``{"plans": [...]}``, sorted
by name. There is no pagination and there are no links. An empty
catalog returns ``{"plans": []}``.

Each entry has ``name``, ``kind``, ``roles``, and ``description``.
``kind`` is ``vm`` or ``baremetal``. Each role is ``control``,
``worker``, or ``build``. ``description`` can be null. At microversion
1.0 ``flavor`` is never returned. At 1.1 each entry also has ``flavor``,
the Nova flavor name or ID the plan uses (interim, Q150).

This call accepts no query parameters. Another method returns 405.
A catalog that is not valid JSON, fails the plan schema, or repeats
a name returns 500. The 500 body does not include the catalog.

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
   - plans: plans
   - name: plan_name
   - kind: plan_kind
   - roles: plan_roles
   - description: plan_description

Response Example
----------------

.. literalinclude:: samples/plans-list-response.json
   :language: javascript

List exports
============

.. rest_method:: GET /v1/stacks/{stack_id}/exports

List exports for one stack.

Export GET collection and show requests accept ``all_projects=true`` or
``project_id`` for cross-project reads when the caller passes the admin
policy as well as the endpoint policy. Non-admin use returns 403.
Without either parameter, foreign resources return 404 even for admins.
These parameters do not extend write access across projects.


The caller needs ``coast:export:get_all``. A project member or an
administrator can read exports. A bad marker returns 404.

The public fields are ``id``, ``status``, ``volume_ids``,
``artifacts``, ``object_expires_at``, and ``links``. ``created_at``
is a valid sort key and is not returned. Sort or filter on
``volume_ids`` or ``artifacts`` returns 400 because those columns
are JSON.

An artifact has ``volume_id``, ``name``, ``size_bytes``, and
``sha256``. ``download_url`` and ``expires_at`` are added only when
the caller passes ``coast:export:renew_url``, the export is
``ACTIVE``, the object expiry is in the future, and the handler
returns a URL that expires in the future and not later than the
object expiry. The service does not store URLs. Create returns an empty
artifact list until the archive is complete. The noop driver creates no
archives or download URLs. List and show sign URLs with the same call
as renewal. An engine connection or credential fault returns 503.
``faultstring`` is ``Service Unavailable``. Other faults return 500.
The body does not include the engine error.

For a provisioned VM stack, the Swift export flow snapshots the selected
volumes, resumes their owners, and uploads read-only clone archives through
the mTLS agent. Each valid receipt supplies the compressed byte count and
archive SHA-256. The conductor publishes ``ACTIVE`` only after temporary
cloud resource cleanup succeeds. A failed attempt becomes ``ERROR``.

The operator must configure the Swift account temp-URL key and keep the
same key in Barbican. Account metadata ``Coast-Temp-URL-Key-Ref`` contains
the Barbican reference. Ceph RGW uses the same account-level key contract;
the signature includes the full catalog endpoint path, including
``/swift/v1``. No container-key fallback is used. The S3 backend uses the
same ``download_url`` field with a path-style SigV4 presigned GET.
Conductor-relay uploads fail closed.

The conductor deletes retained objects on its periodic sweep. Swift
``X-Delete-At`` also schedules backend expiration after a valid upload.
RGW cleanup uses the conductor sweep; it does not require Swift's
object-expirer process. Interrupted attempts are not automatically
uploaded again. The operator must inspect tagged temporary cloud resources
and unfinished stack tasks before recovery.

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
   - exports: exports
   - id: id
   - status: status
   - volume_ids: volume_ids
   - artifacts: artifacts
   - volume_id: artifact_volume_id
   - name: artifact_name
   - size_bytes: artifact_size_bytes
   - sha256: artifact_sha256
   - download_url: artifact_download_url
   - expires_at: artifact_expires_at
   - object_expires_at: object_expires_at
   - links: links
   - rel: rel
   - href: href

Create export
=============

.. rest_method:: POST /v1/stacks/{stack_id}/exports

Create an export.

The caller needs ``coast:export:create``. The response code is 202.
Status is ``PENDING``. The body is an ``export`` object.
``volume_ids`` can be omitted or null. That selects every ``data``
and ``docker`` volume in the stack. An explicit list must be
non-empty and unique.

The stack must be ``ACTIVE``. The call takes the
``RECONCILING`` task and does not change the stack status. A
busy stack, or a status other than ``ACTIVE``, returns 409.

A missing or foreign volume returns 404. A volume in another
stack, or a ``scratch`` or ``controller`` volume, returns 400.
A volume that is not ``AVAILABLE`` or ``IN_USE`` returns 409. An
empty selection returns 400.

Export limits are separate from the seven quota fields. The defaults
are ``[export] max_concurrent_per_project`` 1 and
``[export] max_size_gb`` 500. The size is the sum of the selected
volume sizes. Over quota returns 403 and names ``exports`` or
``export_size_gb``.

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
   - export: export_request
   - volume_ids: export_volume_ids_request

Response
--------

The response uses the list export fields inside an ``export``
envelope. ``status`` is ``PENDING``. M1 ``artifacts`` is an empty
list.

Response Example
----------------

.. literalinclude:: samples/export-create-response.json
   :language: javascript

Show export
===========

.. rest_method:: GET /v1/stacks/{stack_id}/exports/{export_id}

Show one export.

The caller needs ``coast:export:get_one``. The export must belong
to this stack. Another project returns 404.

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
   - export_id: export_id
   - fields: fields

Response
--------

The response uses the list export fields inside an ``export``
envelope.

Renew export URLs
=================

.. rest_method:: POST /v1/stacks/{stack_id}/exports/{export_id}/action

Renew export download URLs.

The caller needs ``coast:export:renew_url``. The only key is
``renew_url``. Its value is an empty object. The call is
synchronous. The response code is 200. The export must be
``ACTIVE`` and ``object_expires_at`` must be in the future.
Otherwise the call returns 409. The normal noop completion has no
object expiry or archive, so renewal returns 409. For non-noop stacks,
renewal also returns 409 if artifacts or usable signed URLs are absent.
A successful renewal does not extend object retention.
An engine connection or credential fault returns 503.
``faultstring`` is ``Service Unavailable``. Other faults return 500.
The body does not include the engine error.

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
   - 503

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - Content-Type: content_type
   - stack_id: stack_id_path
   - export_id: export_id
   - renew_url: renew_action

Response
--------

The response uses the list export fields. ``status`` is unchanged.

Delete export
=============

.. rest_method:: DELETE /v1/stacks/{stack_id}/exports/{export_id}

Delete an export.

The caller needs ``coast:export:delete``. The response code is 202.
Status becomes ``DELETING`` through compare-and-swap. A second
delete returns 409. A body must be empty. An empty JSON object is
accepted.

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
   - export_id: export_id

Response
--------

The response uses the list export fields. ``status`` is
``DELETING``.
