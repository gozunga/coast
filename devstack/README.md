# DevStack plugin

Enable Coast in a DevStack cloud with:

```bash
enable_plugin coast <coast git url>
```

The plugin does two jobs. It creates the limited agent Keystone user,
and it can start the Coast API.

## Limited agent user

DevStack sources `settings`, then `plugin.sh <mode> [phase]`. The user
is created in the `stack` / `extra` phase, after Keystone accounts
exist. `clean` and `unstack` do not delete the user.

Set `COAST_AGENT_PASSWORD` in `local.conf` to a generated value. Do not
reuse `SERVICE_PASSWORD`. The default user is `coast-agent`. The default
role is `coast_agent`. The plugin does not grant `admin` or the Keystone
`service` role.

The project is `COAST_AGENT_PROJECT`, or `SERVICE_PROJECT_NAME`, or
`service`. The domain is `SERVICE_DOMAIN_NAME`, or `Default`.

If `/etc/coast/coast.conf` exists (`COAST_CONF`), the plugin writes
`[agent_auth]`. Cloud clients stay on the `coast` service user.

See `doc/source/operations.md` for scope and password rotation.

## API process

When the `coast-api` service is enabled, the plugin starts one API
process:

```bash
uwsgi --ini /etc/coast/coast-api-uwsgi.ini
```

The ini file sets `wsgi-file` to the `coast-wsgi-api` script. The plugin
does not start the `coast-api` development server.

The listen address is `COAST_SERVICE_LISTEN_ADDRESS`. The default is
`HOST_IP`. DevStack's `SERVICE_LISTEN_ADDRESS` is `0.0.0.0`, and Coast
does not use that value. `0.0.0.0`, `::` and `[::]` are refused.

Keystone public, internal and admin endpoints use `COAST_SERVICE_HOST`
(default: `COAST_SERVICE_LISTEN_ADDRESS`) and `COAST_SERVICE_PORT`
(default: 9600). The plugin does not register a second internet address and
does not attach a floating IP to the API.

This plugin does not start the conductor, the health-manager or guest
images. Those stay on the Kolla path in `doc/source/kolla.md`.

DevStack plugin phases are described in
https://docs.openstack.org/devstack/latest/plugins.html
The Coast ini uses `http-socket` so uWSGI binds one address directly.
See https://uwsgi-docs.readthedocs.io/en/latest/HTTP.html
