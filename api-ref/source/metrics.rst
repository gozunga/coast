===============
Service metrics
===============

Read container history
======================

.. rest_method:: GET /v1/services/{service_id}/metrics

Available from API 1.21. Uses ``coast:service:get_one``: project readers
can read their own service and administrators can read any service.
Another project's service returns 404 to a non-admin caller.

The optional ``range`` query parameter accepts ``15m``, ``1h`` (default),
``6h``, ``24h``, ``7d`` or ``30d``. Unknown parameters, repeated ranges and
invalid ranges return 400. Other methods return 405.
Earlier microversions return 404.

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
   - 500

Response
--------

The ``metrics`` object has the service UUID, requested range, step in
seconds, and a ``containers`` list. Each container has a ``name`` and
``points`` in ascending UTC timestamp order. Empty history returns an empty
list. Points have no engine IDs or worker IDs.

``step_seconds`` is 30 for ranges through ``24h``, 300 for ``7d`` and 3600
for ``30d``. Every point has ``timestamp`` (UTC ISO 8601), ``cpu_percent``
(percentage of allowed CPUs), ``memory_bytes`` (usage minus inactive page
cache) and ``memory_limit_bytes`` (null when unlimited).

Coast combines retained detail into UTC buckets of the requested step.
Averages are weighted by sample counts. Byte averages round down to
whole bytes. A bucket's memory limit is null if any sample was unlimited.
Missing samples stay absent. The first bucket can begin before the exact
window boundary. Containers are sorted by name.

Response example
----------------

.. literalinclude:: samples/service-metrics-response.json
   :language: javascript
