Worker ingress rules (1.25)
===========================

List worker ingress
-------------------

.. rest_method:: GET /v1/stacks/{stack_id}/ingress_rules

Project readers can list custom and required ingress rules. Returns 200
with an ``ingress_rules`` array. Each entry contains ``id``, ``protocol``,
``port_range_min``, ``port_range_max``, ``source``, ``description`` and
``managed``. Required HTTP, HTTPS and app engine server SSH rules have
``managed: true``. Enabled databases also contribute managed rules with ID
``managed-database-<database_id>`` and description ``Database <name>``.
These count toward the configurable per-stack limit. Change or remove them
through database public settings; direct DELETE returns 409 naming the
database. Egress and security group IDs are not exposed here.

Admins can request ``all_projects=true`` or ``project_id`` to resolve a
stack in another project. The admin and endpoint policies both apply;
non-admin use returns 403. Writes remain project-scoped.

Create worker ingress
---------------------

.. rest_method:: POST /v1/stacks/{stack_id}/ingress_rules

Project members can create rules. Request example::

  {"ingress_rule": {"protocol": "tcp", "port_range_min": 8080,
                    "source": "0.0.0.0/0", "description": "Application"}}

Returns 201 with an ``ingress_rule`` object and ``managed: false``.
Protocols are ``tcp``, ``udp`` or ``icmp``. TCP/UDP ports are integers
1 through 65535; ``port_range_max`` defaults to ``port_range_min`` and
must be greater than or equal to it. ICMP must omit both port fields.
``source`` defaults to ``0.0.0.0/0`` and must be an IPv4 CIDR.
Descriptions are optional strings of at most 255 characters.
Egress, IPv6, remote groups, address groups and unknown fields return 400.
Duplicate normalized rules return 409. The configurable per-stack limit
(default 50) returns 403 ``OverQuota``. Stack operations in progress
return 409. Changes apply at the next successful enabled repair sweep.

Delete worker ingress
---------------------

.. rest_method:: DELETE /v1/stacks/{stack_id}/ingress_rules/{rule_id}

Project members can delete custom rules. Returns 204 with no body.
Required IDs ``managed-http``, ``managed-https`` and ``managed-ssh``
cannot be deleted (409). Unknown rules return 404. Deletion reaches
Neutron at the next successful enabled repair sweep.

All endpoints require microversion 1.25 (404 on earlier versions).
Missing identity returns 401, insufficient role returns 403, and stacks
owned by another project return 404. Readers cannot create or delete.
