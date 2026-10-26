# python-coastclient

`python-coastclient` is the OpenStackClient plugin for Coast. It lives in
its own repository, <https://github.com/gozunga/python-coastclient>,
and adds the `openstack coast ...` commands. The client's own `doc/source/commands.rst`
is the reference for each command's options.

## Install and connect

The client needs Python 3.10 or later, `python-openstackclient`,
`osc-lib` and `keystoneauth1` (see its `requirements.txt`). Install it
next to `python-openstackclient` and use normal OpenStack credentials
(`OS_CLOUD` or an OpenRC file). It finds Coast in the service catalog by
service type `app-hosting`.

## Microversions

When `OS_COAST_API_MICROVERSION` (`--os-coast-api-microversion`) is unset,
the client reads the API version document and uses the newest
microversion that both sides support. API 1.30 adds private Git deploy
keys, and 1.31 adds repository-free
Dockerfile and Compose sources. API 1.27 adds
`flavor list` and the flavor shown by `server list` and `server show`.
`OS_COAST_API_VERSION` (or `--os-coast-api-version`) defaults to `1`,
which selects the command major version and allows negotiation. It also
accepts a supported REST microversion such as `1.31` to pin it, overriding
`OS_COAST_API_MICROVERSION` / `--os-coast-api-microversion`. The API
version document reports the server maximum. For example:

```console
$ openstack --os-coast-api-version 1.28 coast stack list
```

Here `--os-coast-api-version 1.28` explicitly selects REST microversion 1.28.

## Commands

Each group maps to routes documented in the API reference.

| Group | Commands |
|---|---|
| Stacks | `stack create`, `list`, `show`, `set`, `delete`, `resize`, `upgrade`, `upgrade rollback`, `stack event list`, `event list` |
| Stack ingress | `stack ingress rule list`, `create`, `delete` |
| Servers | `server add`, `list`, `show`, `remove`, `resize`, `failover` (admin) |
| Plans, flavors and quotas | `plan list`, `flavor list`, `quota defaults`, `list`, `show`, `set`, `delete` |
| App groups and environments | `appgroup create`, `list`, `show`, `set`, `delete`; `environment create`, `list`, `show`, `set`, `delete` |
| Apps | `app create`, `list`, `show`, `set`, `delete`, `start`, `stop`, `restart`, `exec`, `app webhook show`, `app webhook regenerate` |
| One-click services | `template list`, `template show`; `service create`, `list`, `show`, `set`, `delete`, `start`, `stop`, `restart`, `exec` |
| Deployments and logs | `deploy`, `deployment list`, `show`, `log`, `cancel`, `rollback`; `logs`; `container list`, `show`, `logs`; `metrics show` |
| Configuration | `env list`, `set`, `unset`, `replace`; `compose show`, `set`; `file-mount create`, `list`, `show`, `set`, `delete` |
| Domains and DNS | `domain add`, `list`, `show`, `set`, `verify`, `remove`; `dns zone list` |
| Storage | `storage create`, `list`, `show`, `set`, `attach`, `detach`, `delete`; `volume create`, `list`, `show`, `set`, `snapshot`, `delete` |
| Backups and exports | `backup create`, `list`, `show`, `restore`, `download`, `delete`, `backup policy show`, `set`; `export create`, `list`, `show`, `renew`, `delete` |

`exec` opens the container terminal over the API 1.9 console token.
Commands that start work return when the API accepts it (202); poll the
matching `show` command for ACTIVE or ERROR.

`app` and `service` `create` and `set` flags use the API field names:
`--memory-limit-mb`, `--health-check`, `--health-check-port`,
`--health-check-path` and `--ports`. `app set` and `service set` also
take `--enable-default-domain` / `--disable-default-domain`.

## App source commands

See [App sources](app-sources.md) for the private repository, Dockerfile
and Compose workflows. Names and UUIDs are accepted for `APP` and
`--service SERVICE`. Creation also requires `--appgroup`, `--environment`
and `--server` in the same stack.

| Command or option | Behavior |
|---|---|
| `app create NAME --repo-url URL --deploy-key generate` | Generate an ed25519 deploy key; create without deploying. Requires API 1.30. |
| `app create NAME --repo-url URL --deploy-key-file FILE` | Upload an unencrypted private key. Deploys unless `--no-deploy` is given. Requires API 1.30. |
| `app set APP --deploy-key generate` | Replace the key with a generated key without deploying. Requires API 1.30. |
| `app set APP --deploy-key-file FILE` | Replace the key from a file without deploying. Requires API 1.30. |
| `app deploy-key show APP` | Print only the public deploy key, for repository settings. Requires API 1.30. |
| `app create NAME --dockerfile FILE` | Create and deploy an app from Dockerfile text; `--no-deploy` saves it for later. Requires API 1.31. |
| `app set APP --dockerfile FILE` | Replace stored Dockerfile text without deploying; a changed Dockerfile can't be deployed to an existing app. See [App sources](app-sources.md#deploy-from-a-dockerfile). Requires API 1.31. |
| `dockerfile show APP` | Print the stored Dockerfile text. Requires API 1.31. |
| `app create NAME --compose-file FILE` | Create and deploy a service from Compose YAML; `--no-deploy` saves it for later. Requires API 1.31. |
| `compose show --service SERVICE` | Explicitly read desired Compose through the API 1.12 compose endpoint. |
| `compose set --service SERVICE --file FILE` | Save desired Compose without deploying through the API 1.12 compose endpoint. |

Every command above starts with `openstack coast`. `FILE` accepts `-` to
read standard input. Source files use UTF-8. `--deploy-key generate` and
`--deploy-key-file` are mutually exclusive and require an SSH Git source;
HTTPS sources reject deploy keys. Only unencrypted keys are accepted;
passphrase-protected uploads return HTTP 400. Private key text is never
printed. `app show` includes the public key and SHA256 fingerprint.

Choose one creation source: `--repo-url`, `--image` (with `--tag`),
`--dockerfile` or `--compose-file`. Repository options such as `--branch`,
`--build-pack`, `--dockerfile-path`, `--compose-path` and `--base-directory`
apply to Git sources. `--dockerfile-path` selects a file inside a repository;
`--dockerfile` uploads text without a repository. `--compose-file` belongs
to `app create`, not `service create`; its result has kind `service`.

The source options request API 1.30 for deploy keys and API 1.31 for
Dockerfile or Compose text. Other commands do not require these versions
merely because the options exist. An older server gets a clear
`App source needs API version X or later.` error. After saving source
changes, run `openstack coast deploy NAME` to apply them. Uploaded source
text is omitted from ordinary output; the explicit show commands retrieve
it.

## Worked examples

These examples use the python-coastclient command parsers. Resources and
parent options (`--stack`, `--appgroup`,
`--environment`, `--server`, `--service`) accept a name or a UUID, so the
examples use names. Outputs are abridged and IDs are examples.

### Create a stack and add a worker

`plan list` shows Name, Kind, Roles and Description. Choose a `vm` plan
with the `control` role for the app engine server and a `vm` plan with
the `worker` role for workers. The examples use `standard-2` and
`worker-small`; replace them with names from `openstack coast plan list`.
Roles are alphabetically sorted and comma-separated in tables.

```console
$ openstack coast plan list
$ openstack coast stack create shop --plan standard-2 --worker kind=vm,plan=worker-small
$ openstack coast stack show shop -c Name -c Status -c 'Engine version'
+----------------+------------------+
| Field          | Value            |
+----------------+------------------+
| Engine version | <engine version> |
| Name           | shop             |
| Status         | ACTIVE           |
+----------------+------------------+
$ openstack coast server add --stack shop --kind vm --role worker \
    --plan worker-small -c Name -c Role -c Status
+--------+----------+
| Field  | Value    |
+--------+----------+
| Name   | worker-2 |
| Role   | worker   |
| Status | PENDING  |
+--------+----------+
$ openstack coast server list --stack shop
```

For stack creation, `--plan` selects the controller plan; for server add,
it selects the added server's plan. `--worker` supplies a worker kind and plan. `--stack` selects the stack, `--kind vm` requests a VM,
`--role worker` sets its role, and `-c` selects output columns.

API: `POST /v1/stacks` with `{"stack": {"name": "shop", "plan":
"<control plan>", "workers": [{"kind": "vm", "plan": "<worker plan>"}]}}`, then
`POST /v1/stacks/{stack_id}/servers` with `{"server": {"kind": "vm",
"role": "worker", "plan": "<worker plan>"}}`. Adding or removing servers
needs the project `manager` role. The next steps place apps on
`worker-1`, a worker name from `server list`.

### Create an app from an image, deploy it and read logs

Every app group starts with a `production` environment; this example
adds a `staging` one.

`app list` and `service list` accept `--appgroup` and `--environment`
filters. A missing parent reports `App group 'X' not found.` or
`Environment 'X' not found.`. With both filters, an environment name
found only in another app group reports
`Environment 'X' does not belong to app group 'Y'.`. UUIDs are sent
unchanged to the API for lookup; a returned environment must belong to
the selected app group.

```console
$ openstack coast appgroup create website --stack shop
$ openstack coast environment create staging --appgroup website
$ openstack coast app create web --appgroup website --environment staging \
    --server worker-1 --image nginx --tag stable-alpine --ports 80
$ openstack coast app show web -c Status
$ openstack coast deploy web -c ID
+-------+--------------------------------------+
| Field | Value                                |
+-------+--------------------------------------+
| ID    | d001d001-d001-d001-d001-d001d001d001 |
+-------+--------------------------------------+
$ openstack coast deployment list --service web
$ openstack coast deployment log --service web d001d001-d001-d001-d001-d001d001d001
$ openstack coast logs --service web --tail 50
+----------------------+--------+------------------------------+
| Timestamp            | Stream | Line                         |
+----------------------+--------+------------------------------+
| 2026-10-22T18:02:11Z | stdout | ... start worker processes   |
+----------------------+--------+------------------------------+
```

`--image` and `--tag` select the container image and tag; `--ports` lists
container ports. `--appgroup`, `--environment` and `--server` select parents
and the worker. `--service` selects the workload; `--tail 50` limits logs
to the last 50 lines.

`app create` deploys at once unless `--no-deploy` is given; `deploy`
starts another deployment. API: `POST /v1/services` with
`{"service": {"name": "web", "kind": "app", "appgroup_id": ...,
"environment_id": ..., "server_id": ..., "ports": "80", "source":
{"type": "image", "image": "nginx", "tag": "stable-alpine"}}}`, then
`POST /v1/services/{id}/deployments` and `GET /v1/services/{id}/logs?tail=50`.

### Create a service from an image

```console
$ openstack coast service create cache --appgroup website --environment staging \
    --server worker-1 --image redis --tag alpine --no-deploy
$ openstack coast deploy cache
```

`--appgroup`, `--environment` and `--server` select the workload's parents
and worker. `--image` selects the container image, `--tag` its required tag,
and `--no-deploy` leaves the service stopped for configuration. Choose either
`--template` (a catalog slug) or `--image` with `--tag`; they cannot be combined.
An image service retains kind `service` (API 1.11).

### Set environment variables

Synopsis:

```text
openstack coast env set --service SERVICE [KEY=VALUE ...] [KEY] [--from-file FILE|-] [--secret|--no-secret] [--build-time|--no-build-time]
```

```console
$ openstack coast env set --service web APP_MODE=production LOG_LEVEL=info
$ openstack coast env set --service web DATABASE_PASSWORD --secret --no-build-time
Value for DATABASE_PASSWORD:
$ openstack coast env set --service web --from-file .env
$ openstack coast env list --service web --long
$ openstack coast deploy web
```

Pass `KEY=VALUE` pairs, several at once. For a secret, give only the
key: the client prompts without echo, or reads the value from standard
input (`< file`), so it stays out of shell history and the process list.
`--from-file FILE` reads a `.env` file; `--from-file -` reads dotenv
entries from standard input and cannot be combined with a bare key.
`--secret` makes values write-only; `--no-secret` allows API reads.
`--build-time` exposes values during builds; `--no-build-time` restricts
them to runtime. These flags apply to every key in the command.
`env list` shows Key, Value, Secret and Version; `--long` adds Build time.
New keys on non-template apps and services default to build-time on at
API 1.6 and later; template services default to off and reject build-time
on. Every variable is available at runtime. Omitted flags preserve existing
keys' flags with `env set`. `env replace` takes the same inputs, uses
create defaults for all keys, and deletes keys you do not list.

All env values are stored in Barbican; the database holds references only.
`env list` masks every value in table output as `********`, secret or
plain. `-f json` selects JSON output and also masks values. There is no
`env show` command. Plain values are readable in the JSON response from
`GET /v1/services/{id}/env`; secret values are never returned (the API returns
`null`). See [secrets](secrets.md#per-app-service).

To rotate a credential, set the same key again with a hidden prompt, then
deploy:

```console
$ openstack coast env set --service web DATABASE_PASSWORD --secret --no-build-time
Value for DATABASE_PASSWORD:
$ openstack coast deploy web
```

To remove the key instead:

```console
$ openstack coast env unset --service web DATABASE_PASSWORD
$ openstack coast deploy web
```

`unset` removes the key from desired configuration; deploy applies its removal.
Rotation and unset retain old Barbican values while retained deployment
snapshots reference them, regardless of deployment status. Unreferenced
versions are pruned after commit; deployment history is not automatically
trimmed. App or stack deletion cleans up retained references. A changed
variable takes effect on the next deployment for non-template workloads;
provisioned template services also receive env updates immediately. API: `PATCH
/v1/services/{id}/env` with
`{"env": {"APP_MODE": {"value": "production", "build_time": true}}}`.

To copy a secret from HashiCorp Vault, use the Vault example in the
[Env commands section of the client command reference](https://github.com/gozunga/python-coastclient/blob/main/doc/source/commands.rst#env-commands).

### Add a domain and verify it

```console
$ openstack coast domain add --service web web.example.com --no-auto-dns
$ openstack coast domain show --service web web.example.com -c Status -c 'DNS records'
$ openstack coast domain verify --service web web.example.com
$ openstack coast domain show --service web web.example.com -c Status
```

`--no-auto-dns` leaves DNS publishing to you. Publish the returned records
at your DNS provider first. After `verify`,
the domain moves through `VERIFIED` and `PENDING_DNS` to `ACTIVE` once DNS
and the certificate check pass; a timeout sets `ERROR`. With a Designate
zone, `--zone ZONE_ID --host web --auto-dns` lets Coast create the records
(`openstack coast dns zone list`). `--zone` selects the Designate zone,
`--host` its relative record name, and `--auto-dns` enables managed records. API: `POST
/v1/services/{id}/domains`, then `POST .../domains/{domain_id}/action`
with `{"verify": {}}`.

### Add a worker ingress rule

```console
$ openstack coast stack ingress rule create --stack shop --protocol tcp \
    --port 8443 --source 198.51.100.0/24 --description "partner API"
$ openstack coast stack ingress rule list --stack shop
+---------------+----------+----------------+----------------+-------------------+------------------+---------+
| ID            | Protocol | Port range min | Port range max | Source            | Description      | Managed |
+---------------+----------+----------------+----------------+-------------------+------------------+---------+
| managed-http  | tcp      | 80             | 80             | 0.0.0.0/0         | Managed by Coast | True    |
| managed-https | tcp      | 443            | 443            | 0.0.0.0/0         | Managed by Coast | True    |
| managed-ssh   | tcp      | 22             | 22             | <controller IP>/32| Managed by Coast | True    |
| <rule id>     | tcp      | 8443           | 8443           | 198.51.100.0/24   | partner API      | False   |
+---------------+----------+----------------+----------------+-------------------+------------------+---------+
$ openstack coast stack ingress rule delete --stack shop RULE_ID
```

Replace `RULE_ID` with the ID returned by rule create or list.
`--protocol` selects the IP protocol, `--source` the allowed source CIDR,
and `--description` labels the rule. `--port` takes `N` or `N:M`; ICMP rules omit it. The managed rules keep
HTTP, HTTPS and app engine SSH (from the app engine server's private
address) open; deleting one returns 409, as does a duplicate rule. API (1.25): `POST /v1/stacks/{stack_id}/ingress_rules` with
`{"ingress_rule": {"protocol": "tcp", "port_range_min": 8443,
"port_range_max": 8443, "source": "198.51.100.0/24"}}`.

### Back up a database service and restore it

Backups cover database services created from a template and require a cloud
Swift-compatible object-storage endpoint. Backups are not verified end to end in 0.1.0;
full verification is tracked for 0.1.1 in issue #124.

```console
$ openstack coast service create db --appgroup website --environment staging \
    --server worker-1 --template postgresql
$ openstack coast backup policy set --service db --engine postgres \
    --container postgresql --enabled on --hour 2 --minute 0
$ openstack coast backup create --service db
$ openstack coast backup list --service db
$ openstack coast backup show BACKUP_ID
$ openstack coast backup restore BACKUP_ID --mode new_copy --name db-restored
$ openstack coast backup restore BACKUP_ID --mode overwrite --confirmation db
$ openstack coast backup download BACKUP_ID --file db.dump.gz
```

Replace `BACKUP_ID` with the ID printed by `backup create` or `backup list`;
backups have no name field. `--engine` selects the database engine,
`--container` selects its container, `--enabled on` enables the schedule,
and `--hour` / `--minute` set its daily UTC time. `--mode` selects
`new_copy` or `overwrite`; `--confirmation` supplies the current service
name for overwrite. `--name` names the restored service; `--file` selects
the local download path.

A policy (engine and container) must exist before the first backup, even
a manual one. `new_copy` creates a new service; `overwrite` needs the
current service name typed in `--confirmation` and takes a safety backup
first. API (1.20): `PUT /v1/services/{id}/backup_policy`, `POST
/v1/services/{id}/backups`, `POST /v1/backups/{id}/restore` with
`{"mode": "new_copy", "name": "db-restored"}`.

### Resize workers

```console
$ openstack coast flavor list
$ openstack coast server resize --stack shop worker-1 --flavor worker-large
$ openstack coast server show --stack shop worker-1 -c Status
$ openstack coast stack resize shop --flavor worker-large
```

`--flavor` takes a flavor name from `flavor list` (API 1.27). `server
resize` changes one server; `stack resize` replaces every worker one at a
time. The target must be strictly larger, and the stack and server must be `ACTIVE`. The
request returns 202; the server shows `FAILOVER` during VM replacement,
keeps its Coast ID, addresses and volumes, and returns to `ACTIVE` (or
`ERROR`). To shrink, add a smaller
worker and move apps to it. API (1.26): `POST
/v1/stacks/{stack_id}/servers/{server_id}/action` with
`{"resize": {"flavor": "worker-large"}}`.

### Upgrade the app engine and roll back

The operator publishes app engine server images; new stacks use the
default version. `--image` requires a published Glance image UUID; the
client does not resolve image names for upgrades. Ask the operator for
that UUID. Rollback uses the image and snapshots recorded before upgrade:

```console
$ openstack coast stack upgrade shop --image IMAGE_ID -c Status
+--------+-----------+
| Field  | Value     |
+--------+-----------+
| Status | UPGRADING |
+--------+-----------+
$ openstack coast stack show shop -c Status -c 'Engine version'
$ openstack coast stack upgrade rollback shop
```

Replace `IMAGE_ID` with the UUID supplied by the operator.
The app engine server is replaced first, then each worker and build
server one at a time; the first failure stops the rollout. Nothing rolls
back automatically. `stack upgrade rollback` works from `ACTIVE` or
`ERROR` and returns 409 when the stack has no rollback point. API: `POST
/v1/stacks/{stack_id}/action` with `{"upgrade": {"image_id": "..."}}` or
`{"upgrade_rollback": {}}`.

## Development

The client has its own tox environments (`py3`, `functional`, `pep8`,
`docs`, `genconfig`) and CI configuration. Command classes are in
`coastclient/osc/v1/`, and entry points in `setup.cfg` under
`openstack.coast.v1`. A new API microversion needs a matching entry in
`MICROVERSIONS` and tests before the client can send it.

## Standalone database commands

Use `openstack coast database engine list` to discover engines, versions,
defaults and backup support. Create with `openstack coast database create`.
Use each command's `--help` for its options. The database command names are:

| Command after `openstack coast database` | Purpose |
| --- | --- |
| `engine list` | Read the engine catalog |
| `create` | Create on a selected worker |
| `list`, `show` | Read metadata and progress |
| `set` | Change name, description, limits or public access |
| `start`, `stop`, `restart` | Control runtime state |
| `logs` | Read runtime logs |
| `credentials show` | Retrieve secrets; member only |
| `backup policy show`, `backup policy set` | Read or change policy |
| `backup create`, `backup list` | Create or list backups |
| `delete` | Remove the database and its data |

Database backup policies derive engine and container automatically.
Use the existing `openstack coast backup show`, `delete`, `download` and
`restore` commands for a backup ID. Read the
[database user guide](portal-guide.md#databases) before creating public
access, handling credentials or deleting data.
