coast-agent
===========

Installs a supplied guest-agent payload.

The console script is ``coast-agent`` (``coast.cmd.agent:main``).
``tools/build-images/build-agent-pyz.sh`` builds a runnable archive. This
element does not choose a repository, a pip index, or a git URL, and it
does not install the full Coast service into the guest. Pass
``--agent-source`` when a guest file exists.

Supported input:

* a local ``.deb``, installed with ``dpkg``
* any other local file, installed as ``/usr/local/bin/coast-agent``
* an ``http`` or ``https`` URL, and only when the build is online

``--agent-sha256`` (``COAST_AGENT_SHA256``) is required with
``--agent-source``. The element checks the local file and the
``COAST_AGENT_URL`` download before install. A missing or different
checksum stops the build. Compute the value with ``sha256sum`` or
download the payload to a local file, then run ``sha256sum`` on that file.

The element installs ``coast-guest-routes.service`` and
``coast-agent.service``. Both use ``WantedBy=cloud-init.target`` to avoid
a cloud-final ordering cycle. The route service runs after cloud-final
and network-online; the agent requires successful route setup.

The route step reads Nova config-drive network data and matches the static
``[agent] bind_ip`` and NIC MAC addresses. It replaces the cloud-init
Netplan file and disables cloud-init network regeneration. Management uses
static IPv4 without a default gateway; off-link destinations in the guest
``health_manager_ips`` list get host routes through the management subnet
gateway. On-link destinations need no extra route. Environment networking
uses Neutron DHCP and the stack router supplies the only default route.
The step removes any stale management default route. It disables DHCPv6
and router advertisements. Missing or ambiguous data fails closed.

There is no guest network namespace. The agent binds management sockets
to the management IP in the main namespace. Control-side namespace
options remain active. Management isolation uses Neutron SGs and the operator firewall.
The agent starts only when its binary exists.

The agent checks environment routing and DNS after guest route setup.
An optional operator HTTPS registry URL enables a verified HTTPS probe;
any HTTP response is success. The internal mTLS health response reports
pending/ok/error without target values or exception text. No internet
installer is added; the Coolify offline path stays unchanged.
