Customer Designate DNS (API 1.22)
=================================

List project zones
------------------

.. rest_method:: GET /v1/dns/zones

Project readers, members and managers can list zones under
``coast:dns:zones:list``. Even an admin receives only the caller project.
The optional ``trust_id`` query parameter selects a linked-zone Keystone
trust. Unknown query parameters return 400. Earlier API versions return 404.

Normal response code: 200. Error codes: 400, 401, 403, 404, 409.

.. code-block:: json

   {"zones": [{"id": "zone-id", "name": "example.com.",
               "project_id": "customer-project"}]}

At API 1.22, a missing DNS endpoint in the service catalog returns 404
with the fault message ``DNS zones are not available on this cloud.``
Clients should hide the zone and host fields for this response. This is
an availability signal, distinct from an empty project zone list (200),
an unsupported microversion (404), or a Designate failure (409). Designate
failures return a safe message such as ``DNS service unavailable.``;
credentials and backend exception details are never returned. This bug
fix retains microversion 1.22.

Stack default domain
--------------------

Stack POST and PATCH accept nullable ``default_domain``, ``dns_zone_id``
and ``dns_trust_id``. A domain without a zone uses the most specific
matching zone in the project. A zone without a domain uses the zone name.
Set both domain and zone to null to remove the default link.
Responses add ``default_domain`` and ``dns_zone_id``; trust references are
not returned. A link applies to new apps. Existing apps keep their links.

Automatic domains
-----------------

Domain POST and PATCH accept ``auto_dns``, ``zone_id``, ``dns_trust_id``,
``record_type`` (A, AAAA or CNAME), and either ``fqdn`` or ``host`` plus
``zone_id``. An empty host selects the apex. Host and zone require automatic
DNS. CNAME cannot use the apex. AAAA needs a public IPv6 worker address.
``tls`` keeps its existing values. Existing policies and response codes apply.

.. code-block:: json

   {"domain": {"host": "shop", "zone_id": "zone-id", "auto_dns": true,
               "record_type": "A", "tls": "letsencrypt"}}

Responses add ``zone_id``, ``record_type``, ``managed_records`` and
``status_reason``. Each managed-record entry has ``id``, ``zone_id``,
``name``, ``type`` and ``records``. These observations can be empty until
conductor completes the write. ``dns_mode`` is ``designate`` or ``manual``.
A DNS conflict, missing zone, quota, credential or availability failure has
a safe status reason. Customer records are never overwritten.

PATCH returns 202. Name and routing edits reset verification and rotate
the TXT token. Public DNS propagation and TLS readiness still gate ACTIVE.
Deletes remove only owned records. Earlier versions retain their fields
and the existing no-op automatic-DNS behavior for unlinked domains.
