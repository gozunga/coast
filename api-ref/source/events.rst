======
Events
======

Events are project-scoped. The only call is list. It requires
``coast:event:get_all``. There is no show call and no write call.
A path under ``/v1/events/{id}`` returns 404. Another method on the
collection returns 405.

The self link points at the list URL with ``?id={id}``. It is not a
show URL. ``environment_id`` is not a response field or an accepted
filter. There is no streaming or tenant broker-subscription endpoint.

A list can filter on the public event columns, including
``stack_id``, ``resource_type``, ``resource_id``, ``action``,
``outcome``, ``message``, and ``request_id``. The ``outcome`` filter accepts
``start``, ``success``, or ``failure``. Any other value returns 400 with
the rejected value and the valid choices in ``faultstring``.

List events
===========

.. rest_method:: GET /v1/events

List events in the token project.

The caller needs ``coast:event:get_all``. A bad marker returns 404.

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
   - stack_id: stack_id_query
   - resource_id: resource_id_query

Response
--------

.. rest_parameters:: parameters.yaml

   - X-OpenStack-Request-Id: x_openstack_request_id
   - OpenStack-API-Version: openstack_api_version
   - events: events
   - id: id
   - project_id: project_id
   - stack_id: event_stack_id
   - resource_type: event_resource_type
   - resource_id: event_resource_id
   - action: event_action
   - outcome: event_outcome
   - message: event_message
   - request_id: event_request_id
   - created_at: created_at
   - updated_at: updated_at
   - links: links
   - rel: rel
   - href: href

This response was captured from the noop functional test app.

Response Example
----------------

.. code-block:: json

   {
     "events": [
       {
         "id": "d6362a86-403c-5fb3-a49e-6d317c7403cb",
         "project_id": "project",
         "stack_id": "84b93c26-f107-44cf-8a3d-dacd4e62c969",
         "resource_type": "app",
         "resource_id": "bbf16995-64cc-430a-bfbc-86717d8dc7b0",
         "action": "restart",
         "outcome": "success",
         "message": null,
         "request_id": "req-cfea17fb-adac-429c-95a2-dd3b0fe34ee6",
         "created_at": "2026-10-11T16:32:17.555684Z",
         "updated_at": null,
         "links": [
           {
             "rel": "self",
             "href": "https://api.example.com/v1/events?id=d6362a86-403c-5fb3-a49e-6d317c7403cb"
           }
         ]
       }
     ],
     "links": [
       {
         "rel": "next",
         "href": "https://api.example.com/v1/events?resource_id=bbf16995-64cc-430a-bfbc-86717d8dc7b0&limit=1&sort_key=created_at&sort_dir=desc&marker=d6362a86-403c-5fb3-a49e-6d317c7403cb"
       }
     ]
   }

Notification resource names
===========================

Notification strings use ``environment`` for Coast stacks and ``app`` for
Coast services.

Stack event timeline (1.8)
==========================

.. rest_method:: GET /v1/stacks/{stack_id}/events

Read infrastructure activity for one stack. Requires microversion 1.8;
older versions return 404. The policy ``coast:stack:events:get`` defaults
to ``rule:coast:stack:get_one``. Unknown or other-project stacks return 404.
Below microversion 1.9, only ``stack`` and ``server`` event rows for
this stack are returned. At 1.9, ``app`` and ``service`` rows are also
returned. Those rows record shell open and close. Their detail has
``user_id``, ``resource_type``, ``resource_id`` and, on close,
``duration`` in whole seconds. Detail has no command, keystrokes or
output. ``volume`` stays invalid.

Request query parameters:

* ``limit``: positive integer, default 50, maximum 200.
* ``marker``: event ID from the same stack and selected resource type.
* ``outcome``: optional filter, one of ``start``, ``success``, or
  ``failure``. Invalid values return 400 naming the value and valid choices.
* ``resource_type``: optional filter. Below 1.9 the values are
  ``stack`` or ``server``. At 1.9, ``app`` and ``service`` are also
  valid.

Rows sort by ``created_at`` descending, then ``id`` descending. The response
has ``events`` and ``links`` arrays. Each event contains ``id``, ``stack_id``,
``resource_type``, ``resource_id``, ``action``, ``outcome``, ``message``,
``detail``, ``request_id`` and ``created_at``. ``detail`` is a nullable JSON
object of selected infrastructure facts. Rows without infrastructure facts have null detail.
Credentials and environment variable values are never included.

A next link has the same shape as other list endpoints::

  {"events": [], "links": [{"rel": "next",
    "href": "https://api.example.com/v1/stacks/STACK_ID/events?limit=50&marker=EVENT_ID"}]}

Success returns 200. Errors return 400 for invalid parameters, 401 for
missing authentication, 403 for denied policy, 404 for missing resources
or markers, 405 for other methods, and 406 for unsupported microversions.

Admin stack timeline reads
==========================

From 1.18, ``GET /v1/stacks/{stack_id}/events?all_projects=true`` permits
admin reads across projects, in addition to ``coast:stack:events:get``.
Non-admin use returns 403. Without the flag, foreign stacks return 404.
Next links preserve the flag and markers from another stack return 404.
Server create/delete detail includes ``compute_id``; failover detail keeps
``old_compute_id`` and ``new_compute_id``. Engine server upgrade/rollback
stack events also include both Nova IDs and ``server_id``. Existing events
are not backfilled. The flat event list keeps its existing fields.
