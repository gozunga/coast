Golden image build
==================

``coast-image-create.sh`` builds the Coast guest images with
diskimage-builder. The layout follows the Octavia amphora image tools.
One image is a controller. One image is a worker. Build servers use the
worker image.

The base distro is Ubuntu Noble. The output format is qcow2. The
controller pin defaults to the release registry DEFAULT.

Coolify install time
--------------------

The controller stores the stock installer for provenance. It does not run
that installer. The element also stages unchanged support files from the
selected release tag, checks their registry SHA256 values, and saves digest-locked
container images in an archive. First boot loads that archive onto the
Docker Cinder volume and starts stock Compose definitions without pulls.
It generates unique secrets locally and preserves them on retry.

The config drive must provide static management network data and the
agent configuration. Management setup matches the configured bind IP to
the supplied link MAC. Both NICs stay in the main namespace. Management
has no default route; environment DHCP supplies the stack-router default.
On controllers, engine setup waits for cloud-final, management setup,
Docker and the Cinder mount points, before the agent can start.
``coast-volume-mounts.service`` blocks dockerd until those mounts exist.
The image does not mask ``docker.socket``. ``docker.service`` requires
that unit. A drop-in starts the socket only after the mount wait.
The config drive does not stop or mask Docker.

The guest route step, agent, controller first-boot service and Docker
are enabled under ``cloud-init.target``, not ``multi-user.target``. Their
ordering after ``cloud-final.service`` then has no cycle with Ubuntu's
cloud-init ordering. The hooks remove old multi-user target links. Chrony
and containerd keep their distro ordering; neither has a Coast dependency
on cloud-final.

All engine ports bind only to loopback. The API allowlist stays unset:
the supported releases have no supported API, env or Artisan
configuration path. Access is
protected by loopback bindings and management-IP sockets in the guest
main namespace, with Neutron security groups and operator firewall isolation.
First boot writes supported ``AUTOUPDATE=false`` and root-user variables
into the private ``.env``; stock initialization disables updates and,
after root creation succeeds, registration. First boot does not access
the engine database. Q78 is resolved; see lifecycle.md.

Requirements
------------

Install diskimage-builder, qemu-utils, curl and Docker on the build host.
The builder needs permission to pull, tag and save Docker images.
Those tools are not Coast Python dependencies. The script calls
``disk-image-create`` from ``PATH``. diskimage-builder uses sudo for
the chroot and writes to the root-owned target filesystem.
Run image builds as a normal user with passwordless sudo, as for stock
diskimage-builder. Do not run the build command with ``sudo -E``.
The build-host sudo must support ``-E`` to preserve the build environment.
Some sudo-rs versions ignore this option. On a host with both versions
installed, select standard sudo through a build-local ``PATH`` directory.
Do not change the system-wide sudo selection for an image build.

``--offline`` does not remove the need for the diskimage-builder
package cache.

Build
-----

Supported releases and their reviewed checksums are in
``coast/common/engine_versions.py``. The examples below build the default
release (the one new stacks get); set ``VERSION`` to another key of
``RELEASES`` to build that release instead::

    python3 -c \
      'from coast.common.engine_versions import DEFAULT; print(DEFAULT)'
    # Set VERSION to the printed value.

Controller installer checksums default to the reviewed release registry.
Use ``--coolify-installer-sha256`` or ``COAST_COOLIFY_INSTALLER_SHA256``
for an explicitly reviewed override. Verify downloads on the build host.
An agent source requires ``--agent-sha256`` or ``COAST_AGENT_SHA256``.
A flag replaces the environment value. A missing value or a mismatch
stops the build before the file is used.

Coolify installer::

    curl -fsSL \
        https://raw.githubusercontent.com/coollabsio/coolify/v$VERSION/scripts/install.sh \
        -o /var/tmp/install-coolify.sh

    sha256sum /var/tmp/install-coolify.sh

    sha256sum /path/to/install.sh

Agent file or ``COAST_AGENT_URL``::

    sha256sum /path/to/coast-agent.deb

    curl -fsSL <agent-url> -o /var/tmp/coast-agent
    sha256sum /var/tmp/coast-agent

Online controller build. Replace ``<sha256>`` with the ``sha256sum``
output::

    tools/build-images/coast-image-create.sh \
        --role controller \
        --distro ubuntu-noble \
        --format qcow2 \
        --coolify-version "$VERSION" \
        --coolify-installer-sha256 <sha256>

The online build downloads
``https://raw.githubusercontent.com/coollabsio/coolify/v$VERSION/scripts/install.sh``
and checks that file before use.

Offline image build (support cache and Docker digest images required)::

    tools/build-images/coast-image-create.sh \
        --role controller \
        --distro ubuntu-noble \
        --format qcow2 \
        --coolify-version "$VERSION" \
        --offline \
        --coolify-installer /path/to/install.sh \
        --coolify-installer-sha256 <sha256>

Worker build::

    tools/build-images/coast-image-create.sh \
        --role worker \
        --distro ubuntu-noble \
        --format qcow2 \
        --coolify-version "$VERSION"

``--print-command`` prints the plan and does not call
diskimage-builder.

Build the agent zipapp with Python 3.12. Build tools and temporary files
use ``/var/tmp`` by default. Set ``COAST_AGENT_BUILD_PYTHON`` if Python 3.12
is not on ``PATH``. Set ``COAST_AGENT_BUILD_DIR`` to change the build
directory. The optional first argument sets the output path::

    tools/build-images/build-agent-pyz.sh /var/tmp/coast-images/coast-agent.pyz
    sha256sum /var/tmp/coast-images/coast-agent.pyz

The script packages this repository and its dependencies with shiv. The
entry point is ``coast.cmd.agent:main``. It uses the OpenStack 2026.1 upper
constraints. A different host Python uses binary-only manylinux CPython
3.12 dependency wheels. If a dependency has no compatible wheel, use a
Python 3.12 build interpreter instead. The image installs Python 3.

Pass the payload and its printed checksum to both image builds::

    --agent-source /var/tmp/coast-images/coast-agent.pyz \
    --agent-sha256 <sha256>

For a build host with a small ``/tmp``, set ``TMPDIR=/var/tmp/coast-dib``
and ``DIB_NO_TMPFS=1``. Create the directory before the build. Pass
``--work-dir /var/tmp/coast-dib/work`` after creating that directory.
Use ``taskset`` to limit the build to the operator's selected cores.
Run builds sequentially when they share a work directory and apt cache.

Elements
--------

``coast-base``
    Docker, chrony, git, jq, cloud-init with the config drive, no default
    password, unattended-upgrades, and a hardened sshd.
    Coolify server validation requires git and jq; both are installed
    during the image build, without package downloads at guest boot.

``coast-controller``
    Stages checksum-pinned support files and a digest-locked image archive.
    Installs the offline controller oneshot. Does not run the installer.

``coast-worker``
    Worker and build-server marker. Does not install Coolify.

``coast-agent``
    Agent payload installation, persistent guest routing, and agent units.
    Both NICs stay in the main namespace. Management has static IPv4 and
    configured heartbeat host routes, without a default gateway.
    Environment DHCP supplies the stack-router default route.

    ``build-agent-pyz.sh`` builds a runnable archive with entry point
    ``coast.cmd.agent:main``. Supply it through ``--agent-source`` and
    ``--agent-sha256``. The element also accepts a local Debian package.

Image lock and support cache
----------------------------

Controller builds require ``COAST_COOLIFY_IMAGES_LOCK`` pointing to a
reviewed lock for the selected release. The historical lock at
``elements/coast-controller/files/coolify-images.lock`` is provenance for
the older supported release. Each lock must have exactly
these six tags, each followed by an immutable
``sha256:<64 hexadecimal characters>`` digest, separated by one space:

* ``coollabsio/coolify:<VERSION>``
* ``coollabsio/coolify-helper:<helper>`` (the helper pin in that release's
  versions.json; also recorded in ``engine_versions.py``)
* ``postgres:15-alpine``
* ``redis:7-alpine``
* ``busybox:1.37`` (inactive legacy Compose profile)
* ``registry:3`` (private per-stack registry; manifest-list digest)

Resolve and review release image digests with the operator's registry inspection
tools. Do not substitute ``latest``. The lock contains no credentials.
The element pulls ``repository@sha256:...``
for linux/amd64, assigns the stock tag locally, and saves all six images.
It records checksums for both the archive and the lock. First boot verifies
those checksums before Docker loads the archive.

For an offline DIB build, set ``COAST_COOLIFY_CACHE`` to a directory with
``docker-compose.yml``, ``docker-compose.prod.yml``, ``.env.production``,
``upgrade.sh``, ``upgrade-postgres.sh`` and ``versions.json``. Download the
first three and versions.json from the ``v$VERSION`` repository root; download
the two scripts from its ``scripts/`` directory. Their checksums must
match ``engine_versions.manifest(version)``. The same digest
references must already exist in build-host Docker. ``--offline`` refuses
support downloads and image pulls. DIB still needs its package cache.

The scripts ``install.sh`` and ``upgrade.sh`` are retained unchanged but
are not executed. A private resolved Compose file replaces all engine
published ports with loopback bindings and disables pulling. The stock
root-user environment variables create a unique local root user. Never
print the environment or resolved Compose file.

Glance upload
-------------

Authenticate as the Coast service user in the ``service`` project.
The image is private. Record the checksum before upload.

Controller::

    sha256sum coast-coolify-controller-$VERSION.qcow2 \
        | tee coast-coolify-controller-$VERSION.qcow2.sha256
    openstack image create \
        --private \
        --disk-format qcow2 \
        --container-format bare \
        --file coast-coolify-controller-$VERSION.qcow2 \
        --tag coast \
        --tag coast-coolify-controller \
        --property hw_architecture=x86_64 \
        --property hw_rng_model=virtio \
        --property os_distro=ubuntu \
        --property os_version=24.04 \
        --property coast_role=controller \
        --property coast_engine=coolify \
        --property coast_engine_version="$VERSION" \
        coast-coolify-controller-$VERSION

Worker::

    sha256sum coast-worker-$VERSION.qcow2 \
        | tee coast-worker-$VERSION.qcow2.sha256
    openstack image create \
        --private \
        --disk-format qcow2 \
        --container-format bare \
        --file coast-worker-$VERSION.qcow2 \
        --tag coast \
        --tag coast-worker \
        --property hw_architecture=x86_64 \
        --property hw_rng_model=virtio \
        --property os_distro=ubuntu \
        --property os_version=24.04 \
        --property coast_role=worker \
        --property coast_engine_version="$VERSION" \
        coast-worker-$VERSION

Set ``[controller_worker] image_tag_controller`` to
``coast-coolify-controller`` and ``image_tag_worker`` to
``coast-worker``. Keep the role tag on one active image. The image
name carries the version. Record the sha256 checksum above before
upload. CI can build the same files. CI does not upload them.

CI
--

The GitLab job ``build-images`` builds the controller image and the
worker image. Start it manually on the default branch, or from a web
pipeline. A pipeline schedule runs it automatically. A merge request
pipeline does not run it.

The job builds the ``DEFAULT`` release from ``engine_versions.py``. The
controller build needs the CI file variable ``COAST_COOLIFY_IMAGES_LOCK``
pointing to a reviewed lock for that release; the committed lock covers
only the older release, and the build stops when no lock is set. The job
uses ``COAST_COOLIFY_INSTALLER_SHA256`` when that CI variable is set,
otherwise the reviewed checksum recorded for the release. The job fails
when the value is not 64 hexadecimal characters. The build plan prints the
checksum. The checksum is not a key.

The job installs diskimage-builder in a virtual environment. It also
installs the host packages that the build needs (``qemu-utils``,
``debootstrap``, ``kpartx``, ``sudo``, ``curl``, and related tools).
diskimage-builder is not a Coast Python requirement. The runner must
be able to mount loop devices. A container runner needs privileged
mode for that mount. This repository does not set a runner tag.

The job writes these artifact files:

* ``images/coast-coolify-controller-<DEFAULT>.qcow2``
* ``images/coast-worker-<DEFAULT>.qcow2``
* ``images/SHA256SUMS``

``SHA256SUMS`` lists the sha256 checksum of each qcow2 file. The job
does not upload to Glance. The job has no cloud credentials. The job
does not sign the images.

Unset ``COAST_COOLIFY_INSTALLER_SHA256`` for the worker command. The
script rejects that variable when ``--role`` is ``worker``.

The job ``image-plan`` runs ``--print-command`` for both roles. It
does not install diskimage-builder and it does not build an image.
``coast.tests.unit.test_image_create`` already runs in the ``py3``
job. ``image-plan`` does not run those tests again.

Image signing
-------------

TODO. This tree does not sign images. Do not put a signing key in CI.
Do not use cosign. Do not use gpg.

Sign the qcow2 file before the Glance upload. Store the signing
certificate in Barbican. Glance stores the signature on the image.
Nova reads the certificate from Barbican when signature checks are
on. The private key stays with the operator.

Set these image properties:

* ``img_signature`` is the base64 signature of the image data.
* ``img_signature_hash_method`` is the hash name, for example
  ``SHA-256``.
* ``img_signature_key_type`` is the key type, for example ``RSA-PSS``.
* ``img_signature_certificate_uuid`` is the Barbican certificate UUID.

``SHA256SUMS`` checks the artifact bytes. It is not ``img_signature``.

Nova describes these properties in
`Image Signature Certificate Validation`_.

.. _Image Signature Certificate Validation: https://docs.openstack.org/nova/2026.2/user/certificate-validation.html

shellcheck and bashate
----------------------

No tox environment installs shellcheck or bashate. Run shellcheck on the
build scripts separately. Unit tests include an offline element dry run
with fake Docker, checksum-failure checks, version gates, loopback bindings,
secret-file permissions and management network parsing.

Run the focused checks::

    tox -e py3 -- coast.tests.unit.test_agent_firstboot coast.tests.unit.test_image_create
    shellcheck tools/build-images/coast-image-create.sh \
        tools/build-images/elements/*/root.d/* \
        tools/build-images/elements/*/install.d/*
    TMPDIR=/var/tmp bash tools/build-images/tests/test-copied-hooks.sh
    tox -e venv -- python tools/build-images/tests/test-unit-ordering.py

A fake-Docker dry run is not a qcow2 boot test. Before live E2E, boot the
image with a config drive and both Cinder volumes while blocking CDN and
registry access. Check that all three units are active, the management
NIC exists only in ``coast-mgmt``, and engine ports listen only on loopback.
Verify agent health over mTLS, then run the existing one-shot bootstrap.
Replace the guest with both volumes retained and verify unchanged secrets
and successful first-boot services without another token bootstrap.

Local first-boot contract
-------------------------

On an isolated disposable root controller with a supported stock release already
installed and its API enabled, set ``COAST_CONTRACT_COOLIFY_FIRSTBOOT=1``
and ``COAST_CONTRACT_COOLIFY_TOKEN_FILE`` to a private existing token file.
Run::

    tox -e contract-coolify -- coast.tests.contract.coolify.test_firstboot

This read-only opt-in contract checks supported root-user and update
variables in the private environment file, loopback-only published ports,
and authenticated loopback API access. It does not execute container
commands or change engine settings. Do not print the environment or token.
Without the opt-in variable, the contract skips all engine access.

Release selection
-----------------

The default and supported releases come from
``coast/common/engine_versions.py``. Obtain the reviewed installer SHA256
from ``RELEASES[version][0]``; the installer URL is
``https://raw.githubusercontent.com/coollabsio/coolify/v<VERSION>/scripts/install.sh``.
The build wrapper uses that checksum by default. An explicit checksum is
an operator-reviewed override and is still checked against the file bytes.

Provide ``COAST_COOLIFY_IMAGES_LOCK`` with exactly the selected release's
stock engine/helper/support images and reviewed immutable SHA256 digests.
The historical committed lock records an older release and must not be
relabelled for a new release. The version file baked into each guest is
independent of the new-stack default configured on the control plane.
See architecture.md for the release addition and acceptance checklist.
