coast-worker
============

Worker and build-server image. Build servers use this same image.
The guest role is a runtime property.

This element does not install Coolify. Docker, sshd, and the volume
mount points come from ``coast-base``. The Coolify SSH user is root.
Password login is disabled. The environment SSH public key arrives on
the config drive. coast-agent installs that key.

``/etc/coast/role`` contains ``worker``. The image stamp records the
Coolify pin of the controller this worker joins. The stamp does not
install Coolify.
