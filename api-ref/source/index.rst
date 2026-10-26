:tocdepth: 2

=========
Coast API
=========

This is a reference for the Coast application hosting API.

The service type is ``app-hosting``. The service name is ``coast``.
The API uses JSON. A resource call uses a Keystone project-scoped token
in ``X-Auth-Token``. The project ID comes from the token.

Send ``OpenStack-API-Version: app-hosting 1.N`` with any microversion
from 1.0 through 1.31, or
``OpenStack-API-Version: app-hosting latest``.
An absent header selects
``1.0``. The response repeats the selected version in
``OpenStack-API-Version`` and adds that name to ``Vary``. A malformed
header returns 400. An unsupported version returns 406.
``GET /healthcheck`` does not use this header.

An error body has this shape:

.. literalinclude:: samples/error-response.json
   :language: javascript

``faultcode`` is ``Client`` for a 4xx response and ``Server`` for a 5xx
response. ``debuginfo`` is null. A 500 response uses a generic message.
A 503 response from a synchronous engine read also uses a generic
message: ``Service Unavailable``.

A database-backed resource list accepts ``limit``, ``marker``, ``sort_key``,
``sort_dir``, and
``fields``. You can also filter by equality on the public scalar fields
of that resource. A list response has ``links``. The ``next`` link is
present only when another page exists. An administrator can send
``all_projects=true`` or ``project_id`` on a list. Another caller
receives 403.

A resource in another project returns 404. A denied policy returns 403.
Quota failure returns 403 and names the resources in ``faultstring``.
There is no machine-readable ``OverQuota`` field.

Plans, templates, environment variables, file mounts, logs, and quotas do not use the
common pagination contract; see their endpoint pages. Limit defaults to
``[api] max_limit`` (1000), is capped there, and 0 returns an empty list.
The default order is ``created_at`` ascending with an ID tie-breaker.
Unknown filter, sort, and projection fields return 400. Invalid query
parameters identify the rejected value in ``faultstring`` and describe
the accepted values or format. ``sort_key`` uses the resource's public
scalar fields listed on its endpoint page; ``sort_dir`` accepts only
``asc`` or ``desc``. Invalid sort keys and directions return 400 with
the rejected value and allowed choices.

The API serves microversions 1.0 to 1.31; ``versions`` lists what each
one adds. Microversion 1.3 adds deploy and push webhooks. Microversion 1.8 adds the stack event
timeline. Microversion 1.9 adds shell sessions. Microversion 1.10 adds
named storage. Microversion 1.11 adds explicit workload kind. Microversion
1.12 adds the Compose editor. Microversion 1.13 adds service container views, 1.14 adds image updates,
1.15 adds container config files, and 1.16 adds observed named volumes.
Dedicated stack workers permit host paths, Docker sockets, privileged
containers, host namespaces, devices and capabilities on every supported
microversion. The template index supplies named-volume accounting metadata.
API 1.18 adds admin stack inventory and cross-project infrastructure reads.
This reference follows the API contract
in ``doc/source/api.md``. The resource hierarchy is
stack > app group > environment > service. A stack owns infrastructure.
An app group maps to an engine project. An environment maps to an engine
environment. A service is the workload. Service create requires both parent
IDs; the app group selects the stack.

A 202 response means acceptance, not completion. Poll the resource until
its status is final. Poll a deleted resource until it returns 404.
Do not repeat an accepted action while the resource is busy.

Synchronous log reads and export URL renewal return 503 for an unreachable
engine or a rejected Coast credential. The body uses the generic message
``Service Unavailable`` in every supported microversion. Other engine faults return
500. Neither response exposes engine error text.

.. literalinclude:: samples/service-unavailable-response.json
   :language: javascript

Retry reads with a delay. Use the resource status, not log output, to
determine operation success.

The CLI nouns are ``openstack coast stack``, ``openstack coast appgroup``,
``openstack coast environment``, ``openstack coast app`` (image, Git and
compose applications) and ``openstack coast service`` (one-click
templates, Q148).
Q47 notification strings retain ``environment`` for stacks and ``app``
for services. Quota keys and option names remain unchanged, including
``environments``, ``apps``, and ``[domains] default_suffix``.

Default policy roles are independent: a manager-only token does not imply
reader or member. Q96 permits members to create stacks. Operators can
override policies. App group/environment delete has no cascade.
A live child returns 409.
Async acceptance is not proof of backend cleanup.

.. rest_expand_all::

.. toctree::
   :maxdepth: 2

   versions
   stacks
   ingress-rules
   appgroups
   environments
   servers
   volumes
   storages
   file-mounts
   backups
   services
   databases
   metrics
   templates
   deployments
   domains
   dns
   env
   events
   consoles
   webhooks
   plans-exports
   quotas

Build and download this reference
=================================

Run ``tox -e api-ref`` to build ``api-ref/build/html/index.html``.
The build treats Sphinx warnings as errors. The GitLab ``api-ref`` job runs
on merge requests and main pipelines and keeps ``api-ref/build/html`` as
a downloadable artifact for one week. Download the artifact and open
``index.html`` with its adjacent assets. No public hosting is configured.
