========
Webhooks
========

Deploy and Git push webhooks start a normal Coast deployment for one
application. Requires microversion 1.3; older versions return 404. The
operator enables webhooks by setting ``[webhooks] secret_key``; when it is
unset, ``enabled`` is false and no URL is returned.

Each application has one token. It is derived from the operator secret,
the application ID and a per-application nonce, so regenerating the token
revokes every earlier URL. Treat the URLs as secrets.

Show webhook URLs
=================

.. rest_method:: GET /v1/services/{service_id}/webhooks

Return the webhook URLs for one application. Requires a project member or
admin (``coast:service:get_webhooks``). Query parameters return 400.
Another project returns 404.

When webhooks are enabled the ``webhooks`` object contains ``enabled``
(true), ``token``, ``deploy`` (the deploy-now URL) and one push URL each
for ``github``, ``gitlab``, ``bitbucket`` and ``gitea``. When disabled it
contains only ``{"enabled": false}``.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 400
   - 401
   - 403
   - 404

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - service_id: service_id

Example response::

  {"webhooks": {
    "enabled": true,
    "token": "<token>",
    "deploy": "https://coast.example.com/v1/apps/<id>/deploy?token=<token>",
    "github": "https://coast.example.com/v1/hooks/<id>/github?token=<token>",
    "gitlab": "https://coast.example.com/v1/hooks/<id>/gitlab?token=<token>",
    "bitbucket": "https://coast.example.com/v1/hooks/<id>/bitbucket?token=<token>",
    "gitea": "https://coast.example.com/v1/hooks/<id>/gitea?token=<token>"
  }}

Regenerate the webhook token
============================

.. rest_method:: POST /v1/services/{service_id}/webhooks/regenerate

Issue a new token and revoke the old URLs. Requires a project member or
admin (``coast:service:regenerate_webhooks``). Returns 200 with the new
``webhooks`` object. Returns 409 when webhooks are disabled.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 200

.. rest_status_code:: error http-status.yaml

   - 401
   - 403
   - 404
   - 409

Request
-------

.. rest_parameters:: parameters.yaml

   - OpenStack-API-Version: openstack_api_version
   - service_id: service_id

Deploy receiver
===============

.. rest_method:: POST /v1/apps/{app_id}/deploy

Deploy-now receiver. It starts a deployment without reading the body.
The receiver routes on this page take no Keystone token and no
microversion header; the ``token`` query parameter authenticates them.

Git push receiver
=================

.. rest_method:: POST /v1/hooks/{app_id}/{provider}

``provider`` is ``github``,
``gitlab``, ``bitbucket`` or ``gitea``. Configure the push webhook on the
Git host with the token as its secret: Coast checks
``X-Hub-Signature-256`` (GitHub), ``X-Gitea-Signature``, Bitbucket
``X-Hub-Signature`` or ``X-Gitlab-Token``.

* A ping returns 200 ``{"result": "pong"}``.
* A non-push event, or a push to another branch or a tag, returns 202
  ``{"result": "ignored", "reason": "..."}``.
* A push to the application's branch (or the repository default branch
  when the application has none) returns 202
  ``{"result": "queued", "deployment_id": "<id>"}``.

An unknown application or a wrong token returns 404, and so does any call
while webhooks are disabled. A bad signature returns 401. A body larger
than ``[webhooks] max_body_size`` returns 413. A deployment already running
for the application returns 409. A deployment that cannot be started
returns 503.
