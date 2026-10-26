=========
Databases
=========

All database routes require microversion 1.29. Earlier versions return
404. Unknown request fields or query parameters return 400. Missing
resources and resources belonging to another project return 404.

Engine catalog
==============

.. rest_method:: GET /v1/database_engines

Requires the project reader role. Returns ``database_engines`` with each
engine's ``engine``, ``display_name``, ``versions``, ``default_version``,
``port``, ``backups``, ``has_username`` and ``has_database``. No query
parameters are accepted. See ``openstack coast database engine list``
for available engines and versions.

Create database
===============

.. rest_method:: POST /v1/databases

Requires the project member role. Returns 202 with a ``database`` object
in ``PENDING``. Coast starts the database asynchronously; status becomes
``STARTING``, then ``ACTIVE`` after the engine reports a healthy running
container for the configured confirmation period, or ``ERROR`` on failure.
A database counts as one service against quota (403 when exceeded).
Names must be unique among live apps, services and databases in the stack
(409 on conflict). Busy parents or tasks return 409.

Request
-------

.. rest_parameters:: parameters.yaml

   - database: database_body
   - name: database_name
   - engine: database_engine
   - version: database_version
   - appgroup_id: database_appgroup_id
   - environment_id: database_environment_id
   - server_id: database_server_id
   - description: database_description
   - database: database_db_name
   - username: database_username
   - cpu_limit: database_cpu_limit
   - memory_limit_mb: database_memory_limit_mb
   - public: database_public

The environment must belong to the app group. The server must be a worker
in the app group's stack. Coast generates passwords and stores them in
protected secret storage. Callers cannot supply passwords. Username and
database name default to ``app`` when supported; supplying either for an
engine without that capability returns 400. Version defaults to the
catalog default. Public access is disabled by default. See public access below.
Engine, version, placement and parents cannot be changed after create.

List databases
==============

.. rest_method:: GET /v1/databases

Requires a project reader. Returns ``databases`` and pagination ``links``.
Filters: ``stack_id``, ``appgroup_id``, ``environment_id``, ``server_id``,
``engine``, ``status``. Common ``fields``, ``limit``, ``marker``,
``sort_key`` and ``sort_dir`` parameters apply. Administrators can use
``all_projects`` or ``project_id``. Sort keys are stored response fields;
computed values such as ``appgroup_id`` and ``internal_url`` cannot be
used for sorting. Services routes never expose database rows;
``/v1/services?kind=database`` returns 400.

Show database
=============

.. rest_method:: GET /v1/databases/{database_id}

Requires a project reader. Returns ``database``. ``fields`` is accepted.
The response fields are ``id``, ``project_id``, ``stack_id``,
``appgroup_id``, ``environment_id``, ``server_id``, ``name``,
``description``, ``engine``, ``version``, ``status``, ``fault``, ``cpu_limit``,
``memory_limit_mb``, ``port``, ``internal_host``, ``internal_url``,
``public``, ``public_url``, ``task_owner``, ``task_started_at``,
``created_at``, ``updated_at``, ``restart_required`` and ``links``.

``internal_host`` is the database hostname on the worker container network.
It is null until creation completes. ``internal_url`` never contains a
password. ``public`` has ``enabled``, ``port``, ``source``, ``host`` and
``private_host``; host values and ``public_url`` are null while public
access is disabled. No secret references are returned.

``fault`` is a read-only string or null, also included in list responses.
An ERROR database carries a generic reason and suggested next steps. It
clears when a new action starts or the database reaches ACTIVE or STOPPED.
POST and PATCH reject caller-supplied ``fault`` with 400.

Update database
===============

.. rest_method:: PATCH /v1/databases/{database_id}

Requires a project member. Accepts a ``database`` envelope with ``name``,
``description``, ``cpu_limit``, ``memory_limit_mb`` and ``public``.
Returns 200 with the updated database. Settings are applied asynchronously
and reconciled against the desired state. CPU and memory changes set
``restart_required: true`` and take effect on the next start or restart.
The flag clears after a successful action. Limits exceeding worker capacity
return 400. A stopped database remains stopped.
Other fields return 400.

Delete database
===============

.. rest_method:: DELETE /v1/databases/{database_id}

Requires a project member. Returns 202 with status ``DELETING``. Poll until
404. Deletes the database container, named data volumes and stored
credentials. Stack deletion includes databases; a live database blocks
its environment's deletion, just like a service.

Read credentials
================

.. rest_method:: GET /v1/databases/{database_id}/credentials

Requires ``coast:database:get_credentials`` (project member by default).
A reader receives 403. Returns ``credentials`` with ``username``,
``password``, ``database``, ``internal_url`` and ``public_url``; engines
with a separate administrator password also return ``root_password``.
Unsupported username and database values are null. These connection URLs
include the password. The response sends ``Cache-Control: no-store``.
No query parameters are accepted.

Database actions
================

.. rest_method:: POST /v1/databases/{database_id}/action

Requires a project member. Accepts exactly one of ``{"start": {}}``,
``{"stop": {}}`` or ``{"restart": {}}`` and returns 202 with ``database``.
Start requires ``STOPPED``; stop and restart require ``ACTIVE``. Invalid
states and busy tasks return 409; busy errors identify the task owner.
Start/restart report ``STARTING`` until stable and healthy, then ``ACTIVE``,
or ``ERROR`` after the configured timeout. Stop reports ``STOPPED``.
Unknown fields and query parameters return 400.

Database logs
=============

.. rest_method:: GET /v1/databases/{database_id}/logs

Requires a project reader. Accepts ``tail`` (nonnegative integer) and
``since`` (ISO timestamp), with the same validation and ``logs`` response
as service logs, including captured output after runtime failure.
Unknown query parameters return 400.

Public access
=============

Create and PATCH accept ``public`` with ``enabled``, ``port`` and ``source``.
Omitted PATCH members retain their values. Ports must be 1024 through 65535;
a null port when enabling allocates a free port from the operator's range
(default 15432 through 15999). Reserved management and app engine ports
return 400. Ports used by another enabled database anywhere in the stack
or covered by a custom or required ingress rule return 409. Exhausted
allocation returns 409. Disabled settings retain ports without reserving
them; enabling access checks availability again.

``source`` must be an IPv4 CIDR (IPv6 returns 400), defaulting to
``0.0.0.0/0``. This allows connections from anywhere. Choose the stack subnet
as source for access only from other workers in the stack. ``public.host``
is the worker's floating IP; ``public.private_host`` is its fixed IP.
Other workers connect to ``private_host:port``. ``public_url`` uses the
floating IP when present, otherwise the fixed IP. Metadata URLs omit the
password; credentials URLs include it.

Enabling creates a managed TCP ingress rule described as ``Database <name>``.
The worker firewall is shared within the stack, so this rule applies to all
workers. It counts against the per-stack ingress limit (403 ``OverQuota``).
Change its source or port through the database; direct rule DELETE returns
409 naming the database. Disabling, deleting, or failed creation removes
the desired rule. The conductor applies firewall changes synchronously,
including when periodic network repair is disabled. Public setting changes
restart running databases and report ``STARTING`` until ready; stopped databases remain stopped.

Database backups
================

Database backups require Swift-compatible object storage in the cloud.
They are not verified end to end in 0.1.0; full verification is tracked for 0.1.1 in issue #124.

These routes require microversion 1.29. Engines with ``backups: false``
in the engine catalog return 409, ``Backups are not available for this
engine.`` Readers can read policies and list backups; members can change
policies and create backups under the existing backup policies.

Policy PUT accepts a ``backup_policy`` envelope with ``enabled``, ``hour``,
``minute``, ``keep_daily`` and ``keep_weekly``. Defaults and UTC scheduling
match service backups. Coast derives the engine and container; supplying
``engine`` or ``container`` returns 400. Save a policy before creating a
manual backup, even when scheduling is disabled. Backup POST accepts an
empty body or ``{}`` and returns 202.

The existing ``/v1/backups/{id}`` show, delete, download and restore routes
also handle database backups, requiring 1.29 for those records. Downloads
are authenticated gzip streams with ``Cache-Control: no-store``.
``new_copy`` restore requires ``name`` and creates a database with the same
engine, version, worker, app group, environment, database name and username,
fresh credentials and public access disabled. Normal service quota applies.
``overwrite`` requires the exact database name in ``confirmation`` and
first creates a safety backup; a failed safety backup prevents restore.
Deleting a database or its stack removes its backup objects and encryption
key. See :doc:`backups` for retention, encryption and restore progress.

Read database backup policy
---------------------------

.. rest_method:: GET /v1/databases/{database_id}/backup_policy

Returns ``backup_policy`` with schedule and retention fields. Without a
saved policy, returns defaults with scheduling disabled.

Update database backup policy
-----------------------------

.. rest_method:: PUT /v1/databases/{database_id}/backup_policy

Returns 200 with the saved ``backup_policy`` schedule and retention fields.

List database backups
---------------------

.. rest_method:: GET /v1/databases/{database_id}/backups

Returns ``backups`` with the record fields documented in :doc:`backups`.

Create database backup
----------------------

.. rest_method:: POST /v1/databases/{database_id}/backups

Returns 202 with ``backup`` in ``PENDING``. Busy databases return 409.
