Database backups (1.20)
=======================

These routes require ``OpenStack-API-Version: app-hosting 1.20``.
They return 404 on earlier versions. Project readers can read metadata;
project members can change policies, create/delete, download and restore.
Administrators can act on any project. Other-project resources return 404.
Unrecognized body fields and query parameters return 400.

Database backups require a Swift-compatible object-storage endpoint
in the cloud. Backups are not verified end to end in 0.1.0; full
verification is tracked for 0.1.1 in issue #124.

Backup policy
-------------

.. rest_method:: GET /v1/services/{service_id}/backup_policy

Returns ``backup_policy``. Without a saved policy, defaults are returned:
``enabled=false``, ``hour=2``, ``minute=0``, ``keep_daily=14``,
``keep_weekly=8``, ``engine=null``, ``container=null``. Time is UTC.

Update backup policy
--------------------

.. rest_method:: PUT /v1/services/{service_id}/backup_policy

Requires a database template service. Partial update with a
``backup_policy`` envelope. The first PUT requires
``engine`` and ``container``. Engines: ``postgres``, ``mysql``, ``mariadb``,
``mongodb``. Container is a Compose container name. ``hour`` is 0-23;
``minute`` is 0-59; ``keep_daily`` is 0-365; ``keep_weekly`` is 0-104.
Scheduling is off by default. Returns the policy with HTTP 200.

.. code-block:: json

   {"backup_policy": {"engine": "postgres", "container": "postgresql",
                      "enabled": false, "hour": 2, "minute": 0,
                      "keep_daily": 14, "keep_weekly": 8}}

List backups
------------

.. rest_method:: GET /v1/services/{service_id}/backups

Returns ``backups``, excluding deleted records. Each record contains
``id``, ``service_id``, ``kind`` (manual/scheduled/safety), ``status``,
``size_bytes``, ``started_at``, ``finished_at``, ``duration_seconds``,
``error``, ``engine`` and ``engine_version``. Sizes count encrypted bytes.

Create a backup
---------------

.. rest_method:: POST /v1/services/{service_id}/backups

Accepts no body or ``{}``. A policy must exist, even for a manual backup.
Returns ``backup`` with HTTP 202 and status ``PENDING``. Busy services return
409. A missing regional Swift/RGW endpoint returns 409 with
``Object storage not available.`` A saved policy can stay disabled.

Show a backup
-------------

.. rest_method:: GET /v1/backups/{backup_id}

Returns ``backup``. It includes ``restores`` with durable job ``id``, target
``service_id``, ``operation`` (new_copy/overwrite), ``status``, ``started_at``,
``finished_at``, ``error`` and ``safety_backup_id``. A successful backup is
``ACTIVE``; failures are ``ERROR``. Pending jobs become ``BUILDING`` and then
``ACTIVE`` or ``ERROR``. Safety backups are separate backup records.

Delete a backup
---------------

.. rest_method:: DELETE /v1/backups/{backup_id}

Queues removal of the manifest and all segments. Returns ``backup`` with
HTTP 202 and status ``DELETING``. GET returns 404 after deletion. Backups in
use by a restore return 409. The body must be empty or ``{}``.

Download
--------

.. rest_method:: GET /v1/backups/{backup_id}/download

Returns a decrypted, authenticated gzip dump through Coast. The response is
``application/gzip`` with attachment filename ``{backup_id}.dump.gz`` and
``Cache-Control: no-store``. It never returns the encryption key or a public
object link. HTTP 409 means the backup is not ACTIVE. A stream failure after
HTTP 200 terminates the transfer; clients must reject incomplete transfers.
Range requests and upload-to-restore are unavailable in v1.

Restore
-------

.. rest_method:: POST /v1/backups/{backup_id}/restore

Defaults to ``new_copy``. ``name`` is required for the fresh service, created
from the same database template with fresh storage and template settings.
Custom environment or Compose changes are not copied. Database backups
require a template service in v1. Normal service/storage quotas apply (403 OverQuota).

.. code-block:: json

   {"mode": "new_copy", "name": "recovered-db"}

Overwrite requires the typed current service name in ``confirmation``.
``name`` is invalid for overwrite. An automatic safety backup must complete
before the agent loads the dump. It remains available after restore failure.
The agent refuses a destination on an older database major version.

.. code-block:: json

   {"mode": "overwrite", "confirmation": "current-db-name"}

Returns ``restore`` with ``id``, destination ``service_id``, ``operation``,
``status`` and ``safety_backup_id`` (initially null), HTTP 202. Poll the source
backup GET for completion. Busy services or unavailable backups return 409.
A failed restore can leave partial data. It is never repeated automatically.

Retention and deletion
----------------------

Retention applies to successful scheduled backups only. Coast keeps the
newest backup for each configured UTC date and ISO week, using the union.
Manual and safety backups need explicit deletion. Service/stack deletion
removes the associated backups, storage container and AES key. A running
backup or restore blocks that cleanup.

Standalone databases
====================

Microversion 1.29 also supports backups of standalone databases through
``/v1/databases/{id}/backup_policy`` and ``/v1/databases/{id}/backups``.
See :doc:`databases` for engine support and database restore behavior.
