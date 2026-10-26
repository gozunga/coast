coast-controller
================

Controller element for a stock Coolify release listed in
``coast/common/engine_versions.py``. The image build does not
execute the installer or generate secrets. It stages unchanged upstream
support files with committed checksums and a digest-locked image archive.
The build requires ``COAST_COOLIFY_IMAGES_LOCK`` to name a reviewed lock
for the selected release. The committed ``files/coolify-images.lock``
records the older supported release.
See the parent build README for the exact format and offline cache inputs.

The first-boot oneshot requires cloud-final, persistent guest route setup,
Docker, ``coast-volume-mounts.service`` and the Cinder mounts for
``/data``, ``/var/lib/docker`` and ``/var/lib/registry``. The controller
image adds ``/var/lib/registry`` to ``/etc/coast/volume-mounts``.
The image ships with no Coolify containers. Compose services use
``restart: "no"``, so dockerd never starts Coolify on boot; this
oneshot starts it on every boot after the mount and state checks.
It generates unique local credentials,
starts Compose without pulls, and binds engine ports to loopback only.
After Coolify is up it starts the pinned ``registry:3`` container and
publishes only the environment address from ``[registry]`` in
``coast.conf``.
The supported ``AUTOUPDATE=false`` and root-user variables are written
to the private ``.env``. Stock ``app:init`` disables updates on every start;
``RootUserSeeder`` creates root and disables registration when creation
succeeds. The API allowlist remains unset. The agent requires this oneshot
to succeed. Retry preserves secrets and engine data. Replacement refreshes
only ``LATEST_IMAGE`` and ``AUTOUPDATE`` in the retained environment, so
Compose selects the golden image version without changing credentials.
First boot does not write a new ``.env`` when engine directories already
exist and ``.env`` is absent.

The oneshot uses ``WantedBy=cloud-init.target``. The hook links it into
``cloud-init.target.wants`` and removes any old multi-user target link.
The agent and Docker use the same target so that their dependencies after
cloud-final do not create a boot ordering cycle on Ubuntu.

Q78 is resolved. See lifecycle.md for the supported
mechanisms and the version-gated agent token bootstrap exception.
First boot performs no engine database access.

The management NIC stays in the main namespace without a default route.
The environment NIC uses DHCP and the stack router for internet access.
Coolify remains pre-baked; first boot does not run the installer.

The agent checks environment routing and DNS after guest route setup.
An optional operator HTTPS registry URL enables a verified HTTPS probe;
any HTTP response is success. The internal mTLS health response reports
pending/ok/error without target values or exception text. No internet
installer is added; the Coolify offline path stays unchanged.
