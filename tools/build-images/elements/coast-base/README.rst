coast-base
==========

Shared guest packages and host policy for controller and worker images.

The element installs distro Docker, chrony, cloud-init, OpenSSH, git and jq.
Coolify server validation requires git and jq. Both packages are installed
at image build time, so guests do not need package downloads at boot.
Cloud-init uses the config drive only. There is no default password.
sshd rejects password logins. Unattended upgrades install security
updates and do not reboot the guest.

``systemd-networkd-wait-online.service.d/90-coast.conf`` sorts after
netplan's ``10-netplan.conf`` and replaces all its ``ExecStart`` commands.
It waits for any routable NIC, without an interface-name list, for at most
30 seconds. The static management NIC can satisfy this wait even when the
environment NIC has no DHCP lease. Coast first boot marks only the
environment DHCP NIC optional; the management NIC remains required.
The agent egress check still waits for the environment IPv4 lease and
default route within the existing ``[agent] request_timeout`` grace and
reports a fixed route failure through health if that wait expires.
New images are required to apply the wait-online fix to existing guests.

``/data`` and ``/var/lib/docker`` are the Cinder mount points. Docker
does not start until both paths are mount points. The image does not
mask ``docker.socket``. The vendor unit requires that socket and starts
``dockerd`` with ``-H fd://``. A mask fails the requirement, so Docker
and the agent never start. The socket drop-in sets
``DefaultDependencies=no`` and orders the socket after
``coast-volume-mounts.service``. The package link under
``sockets.target`` is removed, so the socket does not start early.
``coast-volume-mounts.service`` waits for the paths in
``/etc/coast/volume-mounts`` after ``cloud-final.service`` and before
dockerd. The root disk holds no environment data.

Docker and the mount wait are enabled under ``cloud-init.target`` and
ordered after ``cloud-final.service``. The Docker drop-in replaces the
vendor ``WantedBy`` value, and the hook removes the old
``multi-user.target.wants`` link. This avoids a boot ordering cycle on
Ubuntu. Chrony and containerd do not wait for cloud-final and keep their
normal target links.

``root.d/10-build-env`` writes ``/etc/coast/build.env`` from the build
environment. Later elements read that file inside the chroot.

TODO: confirm the diskimage-builder Ubuntu element still honors
``DIB_DEBIAN_COMPONENTS``. ``docker.io`` is in Ubuntu universe.
