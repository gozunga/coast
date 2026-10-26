==================================
Coast - OpenStack App Hosting
==================================

Coast is an OpenStack service for cloud operators who want to offer
application hosting to their projects. A project creates a stack: an app
engine server plus worker and build servers on Nova and Cinder, on a
per-stack Neutron network. Project users then deploy apps from container
images, Git repositories or compose files, and one-click services from a
template catalog, with domains, HTTPS, environment variables, storage,
backups, logs and metrics, through the Coast API, the
``openstack coast`` CLI or the Horizon dashboard.

This documentation covers Coast 0.1.0. Operators start
with ``install-kolla`` and ``operations``; portal and client developers
with ``quickstart``, ``portal-guide`` and the API reference;
contributors with ``architecture`` and the repository ``README.md``.

.. toctree::
   :maxdepth: 1
   :caption: Guides

   install-kolla
   quickstart
   service-metrics
   app-sources

.. toctree::
   :maxdepth: 2
   :caption: Design

   architecture
   data-model
   api
   api-apps
   portal-guide
   driver-interface
   security
   secrets
   service-permissions
   networking
   lifecycle
   design/engine-upgrade
   design/build-servers
   design/volume-cleanup
   design/registry-push
   contributor/specs/database-backups
   contributor/specs/database-backup-agent
   contributor/specs/storage
   contributor/specs/compose-editor
   operations
   kolla
   ui-mockups

.. toctree::
   :maxdepth: 1
   :caption: Planning

   client

.. toctree::
   :maxdepth: 1
   :caption: Dated acceptance records (not current setup instructions)

   m2-acceptance
