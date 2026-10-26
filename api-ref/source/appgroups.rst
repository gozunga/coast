App groups
============

GET collection and show requests accept ``all_projects=true`` or
``project_id`` for cross-project reads when the caller passes the admin
policy as well as the endpoint policy. Non-admin use returns 403.
Without either parameter, foreign resources return 404 even for admins.
These parameters do not extend write access across projects.

Names must be unique among live rows in the stack. A name in use returns
409. Names remain reserved while deletion is in progress and can be reused
after deletion completes.

An app group groups related apps and services inside one Coast stack, for
example one product or one team.
Calls are project-scoped by default. Another project returns 404 unless
an admin requests a cross-project GET read. Read calls
require ``coast:appgroup:get_all`` or ``coast:appgroup:get_one``.
Create and delete require the matching manager policy. Update requires
the matching member policy.

Lists accept the common pagination, equality filters and fields selection.
An administrator can use ``all_projects`` or ``project_id``.
Create responses use status ``PENDING``; delete returns ``DELETING``.
PATCH changes name or description only and returns 200.

List app groups
=================

.. rest_method:: GET /v1/stacks/{stack_id}/appgroups

The response code is 200.

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - stack_id: stack_id_path
   - all_projects: all_projects
   - project_id: project_id_query
   - limit: limit
   - marker: marker
   - sort_key: sort_key
   - sort_dir: sort_dir
   - fields: fields

Response
--------

The response uses the ``appgroups`` envelope.
Public fields are ``id``, ``project_id``, ``stack_id``, ``name``,
``description``, ``status``, ``created_at``, ``updated_at``, and ``links``.
Engine references are not public.

Create app group
==================

.. rest_method:: POST /v1/stacks/{stack_id}/appgroups

The response code is 202.

Send the ``appgroup`` envelope. ``name`` is required; ``description`` is optional.

The API also creates a ``production`` environment in the new app group
(Q147). It is ``PENDING`` until the conductor creates the app group in the app engine,
then ``ACTIVE``; list it with ``GET /v1/appgroups/{appgroup_id}/environments``.

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - stack_id: stack_id_path
   - appgroup: appgroup_request

Response
--------

The response uses the ``appgroup`` envelope.
Public fields are ``id``, ``project_id``, ``stack_id``, ``name``,
``description``, ``status``, ``created_at``, ``updated_at``, and ``links``.
Engine references are not public.

.. literalinclude:: samples/appgroup-create-request.json
   :language: javascript

.. literalinclude:: samples/appgroup-create-response.json
   :language: javascript

Show app group
================

.. rest_method:: GET /v1/appgroups/{appgroup_id}

The response code is 200.

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - appgroup_id: appgroup_id_path

Response
--------

The response uses the ``appgroup`` envelope.
Public fields are ``id``, ``project_id``, ``stack_id``, ``name``,
``description``, ``status``, ``created_at``, ``updated_at``, and ``links``.
Engine references are not public.

Update app group
==================

.. rest_method:: PATCH /v1/appgroups/{appgroup_id}

The response code is 200.

Send the ``appgroup`` envelope. Only ``name`` and ``description`` can change.

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - appgroup_id: appgroup_id_path
   - appgroup: appgroup_update

Response
--------

The response uses the ``appgroup`` envelope.
Public fields are ``id``, ``project_id``, ``stack_id``, ``name``,
``description``, ``status``, ``created_at``, ``updated_at``, and ``links``.
Engine references are not public.

Delete app group
==================

.. rest_method:: DELETE /v1/appgroups/{appgroup_id}

The response code is 202 when the stack is ACTIVE, DEGRADED or ERROR and the
app group is ACTIVE or ERROR and has no non-deleted environment. Any other
status, an exclusive stack task or a stack in another status, or a live
environment returns 409. Send no request body. There is no cascade. The
conductor removes the app group from the app engine, stores status DELETED, then
soft-deletes the row. A missing engine reference skips the driver. Driver NotFound
completes the delete. Driver failure stores ERROR. Poll GET until 404.

An ERROR parent stack skips engine cleanup with a warning. On an ACTIVE or
DEGRADED stack, an unreachable engine also permits record deletion. Other
engine failures on ACTIVE or DEGRADED stacks still fail deletion. DNS and
backup cleanup must still succeed. Poll GET until 404, then delete the
remaining parent resources and the stack to remove its cloud resources. These
rules apply to all microversions.

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - X-Auth-Token: x_auth_token
   - appgroup_id: appgroup_id_path

Response
--------

The response uses the ``appgroup`` envelope.
Public fields are ``id``, ``project_id``, ``stack_id``, ``name``,
``description``, ``status``, ``created_at``, ``updated_at``, and ``links``.
Engine references are not public.
