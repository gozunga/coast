======================
Container config files
======================

Available from microversion 1.15. Older versions return 404.

File mounts place UTF-8 content at a container path. Content is write-only,
limited to the smaller of 64 KiB and ``[secrets] max_secret_bytes``
(default 20000 UTF-8 bytes), and held in secret storage. Oversized content
returns 400 naming the byte limit before secret writes. Empty content is
valid. NUL and invalid Unicode return 400. Host paths, Docker sockets, host
namespaces, privileged mode, devices and added capabilities are configured
through Compose or on the dedicated worker, not through this content API.
The server rejects unknown keys, including ``mode``: the pinned engine API
has no documented permission-mode control.

Changes take effect on the next deploy, including deletes. A rollback uses
current desired file settings. Writes return
``X-Coast-Redeploy-Required: true`` and set pending settings on the workload.
They make no engine call. A busy workload or an exclusive stack task returns
409. An engine apply failure prevents deployment enqueue.

List file mounts
================

.. rest_method:: GET /v1/services/{service_id}/file_mounts

Return all metadata in ``file_mounts``; content is never returned.

Create file mount
=================

.. rest_method:: POST /v1/services/{service_id}/file_mounts

Apps also support every route on this page under ``/v1/apps/{app_id}``:
list and create at ``/v1/apps/{app_id}/file_mounts``, and show, update and
delete at ``/v1/apps/{app_id}/file_mounts/{file_mount_id}``. Policies and
bodies are the same. Those routes require ``kind=app`` and return 404 for
another kind; the services routes accept either workload kind.

List requires a project reader. Create requires a project member. Policies
are ``coast:file_mount:get_all`` and ``coast:file_mount:create``. An admin
must still use the owning project context. All list parameters are rejected
with 400; the response contains all file mounts for this workload.

Create example::

  {"file_mount": {
    "container_path": "/etc/nginx/conf.d/site.conf",
    "content": "server { listen 8080; }\n"
  }}

``container_path`` must be absolute, at most 255 characters, and normalized.
Segments use ASCII letters, digits, dot, underscore or hyphen. Empty, ``.``
and ``..`` segments, repeated or trailing separators, and ``/`` return 400.
Worker system paths and socket paths are accepted at API 1.17 on all
microversions. No workload safety checker is applied.
A file path cannot equal, contain, or be contained by another file path or
an attached named-volume path. Conflicts return 409, in either direction.
Paths are unique per Coast workload, even across template containers.

Optional ``resource_name`` selects a template application/database container
by name. It is invalid on a non-template app (400). Without a name, a
template must have exactly one container. A missing or ambiguous engine
container fails deploy before writes and enqueue. Coast generates all
engine source paths under its own resource directory. Customers cannot
choose a host path or a directory mount.

Create returns 201 with envelope ``file_mount``. Metadata fields are ``id``,
``project_id``, ``service_id``, ``container_path``, ``resource_name`` (nullable),
``created_at``, ``updated_at`` (nullable). Neither content nor secret
references appear. List returns 200 with ``{"file_mounts": [...]}``.

Show file mount
===============

.. rest_method:: GET /v1/services/{service_id}/file_mounts/{file_mount_id}

Return one ``file_mount`` metadata object; content is never returned.

Update file mount
=================

.. rest_method:: PATCH /v1/services/{service_id}/file_mounts/{file_mount_id}

Update a nonempty subset of ``container_path``, ``content``, and
``resource_name`` using the ``file_mount`` envelope. Returns 200 metadata.

Delete file mount
=================

.. rest_method:: DELETE /v1/services/{service_id}/file_mounts/{file_mount_id}

Apps support the same item suffix under ``/v1/apps/{service_id}``.
Show requires reader, update requires member, and delete requires manager.
Policies are ``coast:file_mount:get_one``, ``coast:file_mount:update``, and
``coast:file_mount:delete``. Another project or workload returns 404.
Unauthenticated requests return 401; denied roles return 403.

Show and PATCH return 200 with ``file_mount`` metadata. PATCH accepts a
nonempty subset of ``container_path``, ``content``, and ``resource_name``.
DELETE accepts no body or ``{}`` and returns 204. Deleting a file does not
change a running container; deploy to remove it. Old content versions remain
in Barbican for log masking until workload deletion. Workload and stack
delete remove all file mounts and retained content secrets.

Compose build packs are accepted on create/update. Templates and customer
Compose have no workload safety filter. Dedicated workers may use host
paths, sockets, privilege, host namespaces, devices and capabilities.
Control-plane network and credential protection remains enforced.
