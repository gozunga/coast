=======
Domains
=======

Domains belong to one service. A read requires ``coast:domain:get_all``
or ``coast:domain:get_one``. Create, delete, and verify require the
matching member policy.

Create returns 201 and status ``PENDING_VERIFICATION``. ``auto_dns``
defaults to false. ``dns_mode`` is ``designate`` when ``auto_dns`` is
true, and ``manual`` otherwise. ``tls`` is ``letsencrypt``. The FQDN
is unique across all projects. A duplicate returns 409 and does not
show the other owner.

``verification_token`` is not a response field. The token is the
``value`` of the ``TXT`` record whose name is
``_coast-challenge.{fqdn}`` and whose purpose is ``verification``.

``dns_records`` is computed. A routing ``A`` or ``AAAA`` record is
added when the service server has a floating IP. A ``CNAME`` is added
when the service has a default URL.

Verify and delete return 202 and do not change the domain status in
the response. Both set the service task to ``RECONCILING``. A service in
``DELETING`` returns 409.

Verify from ``ERROR`` targets ``PENDING_VERIFICATION``. Verify from
another status targets ``VERIFIED``. The same status is a no-op.
``ACTIVE`` to ``VERIFIED`` is not an edge and returns 409.

A custom domain consumes the ``domains`` quota. The computed default
URL does not. A default URL or ``tls=letsencrypt`` is not proof that DNS
and a certificate are ready. Verify records the request and leaves the
status unchanged. A conductor poll then checks the TXT record, publishes
the name, and checks routing and TLS. Status moves from
``PENDING_VERIFICATION`` through ``VERIFIED`` and ``PENDING_DNS`` to
``ACTIVE``. A timeout sets ``ERROR``. Verify from ``ERROR`` starts the
checks again. ``ACTIVE`` means those HTTPS checks passed.

A list can filter on the public domain columns. You cannot sort or
filter on ``dns_records``.

List domains
============

.. rest_method:: GET /v1/services/{service_id}/domains

List domains for one service.

The caller needs ``coast:domain:get_all``. A bad marker returns 404.

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
   - service_id: service_id
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
   - domains: domains
   - id: id
   - project_id: project_id
   - service_id: service_id_body
   - fqdn: fqdn
   - verified_at: verified_at
   - tls: tls
   - primary: primary
   - auto_dns: auto_dns
   - dns_mode: dns_mode
   - status: status
   - created_at: created_at
   - updated_at: updated_at
   - dns_records: dns_records
   - type: dns_type
   - name: dns_name
   - value: dns_value
   - purpose: dns_purpose
   - links: links
   - rel: rel
   - href: href

Create domain
=============

.. rest_method:: POST /v1/services/{service_id}/domains

Create a domain.

The caller needs ``coast:domain:create``. ``fqdn`` is required. The
response code is 201. The call reserves one domain. A busy or
deleting service returns 409. A duplicate FQDN returns 409.
Optional ``tls`` is ``letsencrypt`` or ``none``. It is accepted from
microversion 1.2. A lower version returns 406. Authorization is
checked first, so an unauthorized caller receives 403. The default
is ``letsencrypt``. ``none`` uses HTTP. ``letsencrypt`` uses HTTPS.

Response codes
--------------

.. rest_status_code:: success http-status.yaml

   - 201

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
   - service_id: service_id
   - domain: domain_request
   - fqdn: fqdn_request
   - auto_dns: auto_dns_request
   - tls: tls_request
   - primary: primary

Response
--------

The response uses the list domain fields inside a ``domain``
envelope.

Response Example
----------------

.. literalinclude:: samples/domain-create-response.json
   :language: javascript

Show domain
===========

.. rest_method:: GET /v1/services/{service_id}/domains/{domain_id}

Show one domain.

The caller needs ``coast:domain:get_one``. The domain must belong
to this service. Another project returns 404.

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
   - service_id: service_id
   - domain_id: domain_id
   - fields: fields

Response
--------

The response uses the list domain fields inside a ``domain``
envelope.

Verify domain
=============

.. rest_method:: POST /v1/services/{service_id}/domains/{domain_id}/action

Request domain verification.

The caller needs ``coast:domain:verify``. The only key is
``verify``. Its value is an empty object. The response code is 202.
The response is the domain after the TXT check in this request.
A match is ``VERIFIED``. No match stays ``PENDING_VERIFICATION``.
An ``ERROR`` row moves to ``PENDING_VERIFICATION`` first. The service
task becomes ``RECONCILING``. A service in ``DELETING``, or a status
that cannot move to the verify target, returns 409.

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
   - service_id: service_id
   - domain_id: domain_id
   - verify: domain_action

Response
--------

The response uses the list domain fields. ``status`` is the result
of the TXT check in this request.

Update domain
=============

.. rest_method:: PATCH /v1/services/{service_id}/domains/{domain_id}

Change the certificate mode of a domain.

The caller needs ``coast:domain:update``. The body is
``{"domain": {"tls": "letsencrypt"}}`` or ``none``. The response
code is 202. Coast publishes the domain list again. Microversion
1.2 is required. A lower version returns 406. Authorization is
checked first, so an unauthorized caller receives 403. A deleting
service, or a status that cannot stay in place, returns 409.

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
   - service_id: service_id
   - domain_id: domain_id
   - domain: domain_request
   - tls: tls_request
   - primary: primary

Response
--------

The response uses the list domain fields inside a ``domain``
envelope.

Delete domain
=============

.. rest_method:: DELETE /v1/services/{service_id}/domains/{domain_id}

Delete a domain.

The caller needs ``coast:domain:delete``. The response code is 202.
The domain status in the response does not change. The service task
becomes ``RECONCILING``. A body must be empty. An empty JSON object
is accepted. A service in ``DELETING`` returns 409.

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
   - service_id: service_id
   - domain_id: domain_id

Response
--------

The response uses the list domain fields. ``status`` is unchanged.


Primary domain (API 1.23)
=========================

Create and PATCH accept boolean ``primary`` (default false), and domain
responses include it. Setting true atomically clears the other primary flags
on this service. Choosing a primary does not bypass domain verification.

Deleting or un-primarying the primary domain while the default address is
disabled returns 409: ``Turn the default address back on or pick another main
domain first.`` This protection applies at every microversion. Below 1.23,
requests containing ``primary`` return 406 and responses omit it.
