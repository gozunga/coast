# Driver interface

Engine releases and defaults come from `coast/common/engine_versions.py`.
Upstream source paths below refer to that release’s tag; dated evidence
refers to the recorded supported release, not the new-stack default.

Drivers turn Coast desired state into calls against a PaaS engine running on
the environment's controller. They are loaded with stevedore from the
`coast.drivers` namespace:

```ini
# setup.cfg
[entry_points]
coast.drivers =
    coolify_v4 = coast.drivers.coolify.v4:CoolifyV4Driver
    noop = coast.drivers.noop:NoopDriver
```

```python
from stevedore import driver as sd
mgr = sd.DriverManager('coast.drivers', env.driver, invoke_on_load=True,
                       invoke_kwds={'conf': CONF})
drv = mgr.driver
```

## Rules for drivers

1. **No cloud calls.** The conductor owns Nova/Neutron/Cinder/Barbican/Ironic.
   Drivers receive everything they need (IPs, decrypted secrets, keys) as
   arguments and return engine identifiers.
2. **Only documented engine APIs**, reached through the env's coast-agent
   mTLS endpoint (`EngineEndpoint`). Never patch the engine, read its DB, or
   exec into its containers.
3. **Idempotent.** Retry idempotent assignments and reads after a crash. Never blindly
   repeat a deployment enqueue or rollback POST after an uncertain reply. Use
   `engine_ref` when present; look up by deterministic name/tag
   (`coast-<id>`) when absent.
4. **No secrets in logs or exceptions.** Raise `DriverError` subclasses with
   sanitized messages.
5. **Version-declared.** Each driver declares `SUPPORTED_ENGINE_VERSIONS`;
   the conductor refuses to operate an env whose `engine_version` is not listed.

## Selected Python signatures (`coast/drivers/base.py`)

The module is the complete interface; the excerpts below show core calls.
The `app` method names operate on Coast service records.

```python
import abc
import dataclasses
from typing import Iterator, Mapping, Optional, Sequence

@dataclasses.dataclass(frozen=True)
class EngineEndpoint:
    """How to reach the engine API on the controller (via coast-agent)."""
    base_url: str               # https://<controller mgmt ip>:9443/api/v1
    client_cert: str            # PEM path, Coast CA-issued client cert
    client_key: str             # PEM path
    ca_cert: str                # Coast CA bundle
    token: Optional[str] = dataclasses.field(repr=False)
    timeout: int = 30
    registry_image_template: Optional[str] = None
    registry_host: Optional[str] = None
    registry_username: Optional[str] = dataclasses.field(default=None, repr=False)
    registry_password: Optional[str] = dataclasses.field(default=None, repr=False)

@dataclasses.dataclass(frozen=True)
class DriverResult:
    engine_ref: Optional[str] = None
    extra: Mapping[str, str] = dataclasses.field(default_factory=dict)

@dataclasses.dataclass(frozen=True)
class ObservedStatus:
    status: str                 # coast.common.states value
    detail: Optional[str] = None
    engine_version: Optional[str] = None
    resolved_ref: str | None = None
    container: str | None = None

@dataclasses.dataclass(frozen=True)
class LogLine:
    ts: str
    stream: str                 # build|deploy|stdout|stderr
    line: str

@dataclasses.dataclass(frozen=True)
class EnvVar:
    value: str = dataclasses.field(repr=False)  # plaintext; NEVER log
    is_secret: bool = False
    build_time: bool = False

@dataclasses.dataclass(frozen=True)
class Mount:
    name: str | None            # named volume; None for a bind
    container_path: str
    read_only: bool = False
    type: str = "volume"
    host_path: str | None = None
    id: str | None = None

class DriverError(Exception): ...
class EngineUnreachable(DriverError): ...
class EngineRateLimited(DriverError): ...
class EngineAuthError(DriverError): ...
class EngineConflict(DriverError): ...
class EngineNotFound(DriverError): ...
class UnsupportedEngineVersion(DriverError): ...

class CoastDriver(metaclass=abc.ABCMeta):
    """Base class for all engine drivers. All methods are synchronous and
    are called from conductor/health-manager worker threads."""

    #: exact versions validated by contract tests; for coolify_v4 this is
    #: SUPPORTED_ENGINE_VERSIONS from coast/common/engine_versions.py
    SUPPORTED_ENGINE_VERSIONS: Sequence[str] = ()
    #: Feature flags the API may consult (e.g. 'rollback', 'build_server')
    CAPABILITIES: frozenset = frozenset()

    def __init__(self, conf):
        self.conf = conf

    # ---- engine / stack (Q94) ------------------------------------------
    @abc.abstractmethod
    def get_engine_version(self, ep: EngineEndpoint) -> str:
        """Return the running engine version (health + upgrade checks)."""

    @abc.abstractmethod
    def bootstrap_environment(self, ep: EngineEndpoint, stack) -> DriverResult:
        """One-time engine-side setup on a freshly bootstrapped controller.

        Called AFTER coast-agent has installed the engine and returned an
        API token (lifecycle.md "Engine bootstrap"); ep.token is that token.
        Applies documented settings (for example build-server fallback off).
        Does not create a Coolify project or a Coolify environment (Q94).
        Idempotent. Controller VM deletion is the conductor's job."""

    @abc.abstractmethod
    def ensure_project(self, ep: EngineEndpoint, stack,
                       appgroup) -> DriverResult:
        """Create or adopt the Coolify project for appgroup (Q94).

        One Coolify project per appgroup. Returns that project UUID in
        engine_ref. Idempotent. Does not create a Coolify environment."""

    @abc.abstractmethod
    def delete_project(self, ep: EngineEndpoint, stack, appgroup) -> None:
        """Delete that Coolify project after its environments are gone."""

    @abc.abstractmethod
    def delete_stack_project(self, ep: EngineEndpoint, stack) -> None:
        """Delete the Coolify project and key that belong to this stack.

        Stack teardown calls this method. It is separate from
        delete_project, which removes one appgroup project."""

    @abc.abstractmethod
    def ensure_environment(self, ep: EngineEndpoint, stack, appgroup,
                           environment) -> DriverResult:
        """Create or adopt the Coolify environment for environment (Q94).

        One Coolify environment per Coast environment. Returns that UUID in
        engine_ref. Idempotent."""

    @abc.abstractmethod
    def delete_environment(self, ep: EngineEndpoint, stack, appgroup,
                           environment) -> None:
        """Delete that Coolify environment after its services are gone.

        This is not stack deletion. Controller VM deletion stays with the
        conductor. Stack project cleanup uses `delete_stack_project`."""

    # ---- servers ----------------------------------------------------------
    @abc.abstractmethod
    def register_server(self, ep: EngineEndpoint, env, server,
                        ssh_private_key: str) -> DriverResult:
        """Register a worker/build server reachable at server.private_ip with
        the env's SSH key; wait for validation; ensure proxy (Traefik) on
        workers only after the server is reachable and usable.
        Returns engine server ref."""

    @abc.abstractmethod
    def set_build_server_mode(self, ep: EngineEndpoint, stack, workers,
                              services, enabled: bool) -> None:
        """Switch registered workers and git services in this stack.

        Read previous settings, patch absolute values, confirm with GET.
        Compensate attempted writes on failure, including uncertain replies.
        Raise sanitized DriverError if switch or compensation fails.
        Never change Coast roles, sources, or engine references.
        """

    @abc.abstractmethod
    def unregister_server(self, ep: EngineEndpoint, env, server) -> None: ...

    @abc.abstractmethod
    def get_server_status(self, ep: EngineEndpoint, env,
                          server) -> ObservedStatus: ...

    # ---- services (implemented method names use app) -------------------
    @abc.abstractmethod
    def create_app(self, ep: EngineEndpoint, env, service, source,
                   source_credentials: Optional[str]) -> DriverResult:
        """Create the engine workload (no deploy). Returns its engine ref.

        Git, image, and dockerfile sources map to a Coolify application.
        A template source maps to a Coolify service, including databases."""

    @abc.abstractmethod
    def update_app(self, ep: EngineEndpoint, env, service, source) -> None:
        """Apply non-secret service settings (ports, limits, healthcheck...)."""

    @abc.abstractmethod
    def delete_app(self, ep: EngineEndpoint, env, service) -> None: ...

    @abc.abstractmethod
    def set_env(self, ep: EngineEndpoint, env, service,
                variables: Mapping[str, EnvVar]) -> None:
        """Replace the service's full env var set (bulk). EnvVar = (value,
        is_secret, build_time). Values are plaintext here; never log them.
        Template services replace Coast-managed keys only and preserve
        engine defaults. Their variables are runtime-only."""

    @abc.abstractmethod
    def set_volume_mounts(self, ep: EngineEndpoint, env, service,
                          mounts: Sequence[Mount]) -> None: ...

    @abc.abstractmethod
    def start_app(self, ep: EngineEndpoint, env, service) -> None: ...

    @abc.abstractmethod
    def stop_app(self, ep: EngineEndpoint, env, service) -> None: ...

    @abc.abstractmethod
    def restart_app(self, ep: EngineEndpoint, env, service) -> None: ...

    @abc.abstractmethod
    def get_app_status(self, ep: EngineEndpoint, env,
                       service) -> ObservedStatus: ...

    # ---- deployments ------------------------------------------------------
    @abc.abstractmethod
    def deploy_app(self, ep: EngineEndpoint, env, service,
                   deployment) -> DriverResult:
        """Trigger a deployment of deployment.spec / deployment.source_ref.
        Returns engine deployment ref. Must not block until finished."""

    @abc.abstractmethod
    def get_deployment_status(self, ep: EngineEndpoint, env, service,
                              deployment) -> ObservedStatus:
        """Includes the reported commit/image digest in resolved_ref."""

    @abc.abstractmethod
    def cancel_deployment(self, ep: EngineEndpoint, env, service,
                          deployment) -> None: ...

    @abc.abstractmethod
    def rollback(self, ep: EngineEndpoint, env, service, target_deployment,
                 new_deployment) -> DriverResult:
        """Redeploy target_deployment's image digest/commit as
        new_deployment. Returns engine deployment ref."""

    @abc.abstractmethod
    def get_deployment_log(self, ep: EngineEndpoint, env, service, deployment,
                           since: Optional[str] = None) -> Iterator[LogLine]: ...

    @abc.abstractmethod
    def get_app_logs(self, ep: EngineEndpoint, env, service,
                     tail: int = 200) -> Iterator[LogLine]: ...

    # ---- domains ----------------------------------------------------------
    @abc.abstractmethod
    def set_domains(self, ep: EngineEndpoint, env, service,
                    fqdns: Sequence[str]) -> None:
        """Replace the service's full domain list (default + verified custom).
        Apply changed routing on running workloads without an image build.
        Unchanged lists must not restart; stopped workloads remain stopped.
        TLS via the engine's proxy (Traefik + Let's Encrypt)."""

    # ---- templates (Q95, packaged metadata) ----------------------
    def list_templates(self, ep: EngineEndpoint, stack) -> Sequence[Mapping]:
        """Return the catalog for stack.engine_version.

        Each item is {id, name, description, logo, category, tags,
        required_env, ports}. Coolify one-click services now. Dokploy
        templates later. The catalog is generated at build/release time
        from the pinned tag's service-templates.json."""

    def get_template(self, ep: EngineEndpoint, stack, template_id) -> Mapping:
        """Return one normalized template, or fail when the id is absent."""
```

`add_domain`/`remove_domain` are intentionally expressed as `set_domains`
(full desired list) to keep the driver idempotent and avoid drift.

## Coolify v4 mapping (`coolify_v4`)

Reference: the `openapi.json` in the coollabsio/coolify repo **at the pinned
tag** (each release in `coast/common/engine_versions.py`). Base `/api/v1`, `Authorization: Bearer <token>`.
API access must be enabled on the instance (part of bootstrap/image build).

| Driver method | Coolify call(s) |
|---|---|
| `get_engine_version` | `GET /version` (and `GET /health`) |
| `bootstrap_environment` | Documented instance settings only. No Coolify project and no Coolify environment (Q94). Also `GET /servers` and `PATCH /servers/{uuid}` with `{"is_terminal_enabled": false}` when that setting is not already false. |
| `ensure_project` | `POST /projects` (name `coast-<appgroup_id>`) |
| `delete_project` | `DELETE /projects/{uuid}` after Coolify environments are gone |
| `delete_stack_project` | `DELETE /projects/{uuid}` and `DELETE /security/keys/{uuid}` for the stack project from bootstrap |
| `ensure_environment` | `GET/POST /projects/{uuid}/environments` for that Coast environment |
| `delete_environment` | `DELETE /projects/{uuid}/environments/{environment_name_or_uuid}` after its services are gone; permitted by the agent (M3 c4ed) |
| `register_server` | `POST /security/keys` (env private key, once per env), `POST /servers` (`ip`, `port=22`, `user`, `private_key_uuid`, `server_role=deployment` for workers only when `GET /servers` reports a server with `settings.server_role=build` and `settings.is_usable=true`; otherwise `both` for workers; `build` for build servers, `proxy_type=traefik`), `POST /servers/{uuid}/validate`, poll `GET /servers/{uuid}`; after the server is usable, `PATCH /servers/{uuid}` with `{"is_terminal_enabled": false}` when that setting is not already false. Workers only after that patch: `PATCH /servers/{uuid}/proxy`, `POST /servers/{uuid}/proxy/restart`. When registry credentials are complete: `POST /servers/{uuid}/registries` and `GET` confirmation. A build server always logs in. A worker logs in only when `GET /servers` contains `settings.server_role=build` (usable or not). A worker with no build server skips the POST. |
| `disable_builtin_terminals` | `GET /servers`, then `PATCH /servers/{uuid}` `{"is_terminal_enabled": false}` for each server that is not already false. The base method does nothing. Service reconcile calls it and logs a failure without failing the pass. |
| `set_build_server_mode` | `GET` and `PATCH /servers/{uuid}` (`server_role=deployment` or `both`); `GET` and `PATCH /applications/{uuid}` (`use_build_server`, plus configured `docker_registry_image_name` if missing). Read all targets first; confirm writes and compensation with GET. When enabling, `POST /servers/{uuid}/registries` for each registered worker before the patches. Disabling does not log out. Git and inline Dockerfile apps only; Compose is excluded. PM-decided 2026-10-13 (Q108). |
| `unregister_server` | `DELETE /servers/{uuid}` |
| `get_server_status` | `GET /servers/{uuid}` (+ `/servers/{uuid}/resources`) |
| `create_app` | Git/image: `POST /applications/public` \| `/applications/private-deploy-key` \| `/applications/dockerimage`, with `instant_deploy=false`, `autogenerate_domain=false`, `use_build_server` per stack, and `is_auto_deploy_enabled=false` for Git sources only (the image path omits it). When `default_domain` is set, the body also sends `domains` as one `https://` URL. Place the service in `environment.engine_ref`. Template: `POST /services` with pinned `docker_compose_raw`, not the engine’s mutable `type` catalog. |
| `update_app` | `PATCH /applications/{uuid}` |
| `delete_app` | `DELETE /applications/{uuid}` or `/services/{uuid}` for templates, with `delete_volumes=false`. |
| `set_env` | Applications: runtime sets use `POST /applications/{uuid}/envs` for missing keys and `PATCH /applications/{uuid}/envs` for existing keys; `GET /applications/{uuid}/envs` and `DELETE /applications/{uuid}/envs/{env_uuid}` remove only absent Coast-managed keys. Application build-time exposure follows each variable flag (Q52, M23). Templates: `GET /services/{uuid}/envs`, `PATCH /services/{uuid}/envs/bulk`, `DELETE /services/{uuid}/envs/{env_uuid}`. |
| `set_volume_mounts` | `GET/POST /applications/{uuid}/storages`, `DELETE /applications/{uuid}/storages/{storage_uuid}`. Named volumes use persistent `type`, `name`, and `mount_path`. Keep a persistent row when `mount_path` matches and the name equals the Coast name or ends with `-{name}`. Delete other persistent rows and directory file storages that match no desired bind mount. Non-directory file storages (content files) are left alone here; `set_file_mounts` manages them. Bind mounts include `host_path` and read-only access. Templates apply desired mounts through Compose; see Typed mounts below. |
| `start/stop/restart_app` | `POST /applications/{uuid}/start\|stop\|restart`, or the corresponding `/services` route for templates. |
| `get_app_status` | `GET /applications/{uuid}` (`status`), or `GET /services/{uuid}` for templates. |
| `deploy_app` | Applications: `PATCH /applications/{uuid}` with the frozen settings, then `POST /deploy?uuid={app_uuid}` once (never retried). Templates have no engine deployment row: `GET /services/{uuid}`, then `POST /services/{uuid}/restart` if running or `/start` otherwise. |
| `get_deployment_status` | `GET /deployments/{uuid}` |
| `cancel_deployment` | `POST /deployments/{uuid}/cancel` |
| `rollback` | Git: restore frozen settings, then `POST /applications/{uuid}/rollback` with the resolved commit. Image: restore the frozen image/tag with `PATCH /applications/{uuid}`, then `POST /deploy`. Neither path blindly retries an uncertain POST. |
| `get_deployment_log` | Applications: `GET /deployments/{uuid}` (`logs`). Templates: `GET /services/{uuid}` then `GET /services/{uuid}/logs` with `sub_service_name`. |
| `get_app_logs` | Applications: `GET /applications/{uuid}/logs`. Templates: `GET /services/{uuid}`, then `GET /services/{uuid}/logs` with `sub_service_name` set to the single exposed (or only) container. |
| `set_domains` | Applications: `PATCH /applications/{uuid}` with `domains`. A bare host becomes `https://host`. An `http://` or `https://` URL is kept, including host case. A path, port, or other scheme is rejected. Templates: `GET /services/{uuid}`, `PATCH /services/{uuid}` with `urls=[{"name": container_name, "url": "https://fqdn,..."}]`, `instant_deploy=false`. |
| `list_domains` | `GET /applications/{uuid}` field `fqdn`, or `GET /services/{uuid}` for a template. A missing application returns none. A deploy does not treat that as an empty domain list. |
| `update_metadata` | `PATCH /applications/{uuid}` with `name` `coast-<id>` and `description`. A template is not patched. |
| `list_templates` / `get_template` | Q95. No Coolify list endpoint. Read the build-time catalog for `engine_version`. Do not call the controller for metadata. |

Project deletion first rejects any remaining environment not named
`production`. The driver deletes each engine-owned `production` environment
by UUID before it deletes the project. The pinned environment DELETE API
rejects environments with resources (HTTP 400); the driver raises
`DriverError` and keeps the project. This guard applies to appgroup
projects and the bootstrap stack project. The stack key is deleted only
after project cleanup succeeds. No new agent proxy paths are required.
See the `openapi.json` at the selected upstream tag.

`create_app` places the service on the Coast server `engine_ref` when
that Coolify server uuid is recorded. The conductor passes the uuid on the
in-memory service argument as `server_engine_ref`. That value is not a driver
method, database column, or API field. The driver loads the server with
pinned `GET /servers/{uuid}` and sends the same uuid as `server_uuid`.
When the uuid is absent, the driver selects the server named
`coast-<server_id>`. A missing server, or a server whose role is `build`,
fails closed. A recorded uuid that does not resolve does not fall back to
the deterministic name.

**Verify every row against the pinned tag's openapi.json** before relying on
it (the table was drafted from `main` on 2026-10-09 and checked against
the pinned release spec, decision Q3). Endpoints not in the spec of every
supported release must not be used.

Q94 method text is the target contract. Stack teardown uses
`delete_stack_project(ep, stack)`. Workload creation sends the selected Coast environment’s
`engine_ref` as `environment_uuid`; it does not hard-code `production`.

Existing method signatures on this page still use the name `env` where they
were not replaced above. For server and bootstrap calls, that argument is
the stack. For service, deployment and domain calls, the Coolify environment
is `environment.engine_ref`. The implemented method name is `create_app`.

Disabled/never used: Coolify cloud-provider endpoints (Hetzner/DO), GitHub
App integration, notifications, MCP, Coolify's own self-update, team/user
management beyond the bootstrap token.

## Engine bootstrap and token (decision Q4)

Drivers never create the engine's first credential. The engine-specific
install + token creation lives in **coast-agent** (`coast/agent/engines/
coolify.py`), invoked by the conductor via the agent's one-shot
`POST /v1/bootstrap` over mTLS. The token is stored in Barbican and passed to
the driver in `EngineEndpoint.token`. Full flow, methods and the single
permitted `docker exec` exception: lifecycle.md "Engine bootstrap".

Each driver version declares which agent bootstrap method it expects
(`BOOTSTRAP_METHOD = 'coolify-artisan-token-v1'`, or a documented API/CLI
method when available); the contract suite exercises it on the pinned version.

### M2 bootstrap prototype candidates (the pinned release)

Status: **verified on the pinned release 2026-10-10**. The prototype is
`tools/coolify/bootstrap_token.py`; setup and recovery instructions are in
`tools/coolify/README.rst`. No driver methods or production agent behavior
change in this prototype.

| Candidate | Evidence and choice |
|---|---|
| Documented API/CLI token creation from root credentials | Preferred by Q4, but no first-token creation endpoint was found in the pinned routes or authorization docs. `/security/integration-tokens` is not an instance API-token bootstrap endpoint. Re-check at each version bump. |
| UI token creation | Documented manual dev fallback, not agent automation; no UI scraping. |
| Local Laravel console bootstrap + Sanctum `createToken` | Chosen prototype: `BOOTSTRAP_METHOD = 'coolify-artisan-token-v1'`. Run PHP through `docker exec` on the controller, load the Artisan console kernel without Tinker result printing, set the root-team session, and create the root token directly into a private file. This uses only the Q4 exception. |

The pinned user model overrides Sanctum token creation and requires
`session('currentTeam')->id`. The root seeder uses user ID 0 and team ID 0;
the prototype checks owner membership and sets the session explicitly.
It checks the stock image tag before internal access and the Laravel
version before token creation. It then uses documented `POST /api/v1/enable`
and `GET /api/v1/version` on loopback. The token never appears on stdout,
in command arguments, or in forwarded engine errors. An existing private
host file is reused; a fixed token name prevents silent duplicate creation
if the plaintext is lost. This is not a rotation mechanism.

Sources checked for the pinned release:
[authorization](https://coolify.io/docs/api-reference/authorization),
`app/Models/User.php` at the selected upstream tag,
`database/seeders/RootUserSeeder.php` at the selected upstream tag,
and `routes/api.php` at the selected upstream tag.
Live evidence for `BOOTSTRAP_METHOD = 'coolify-artisan-token-v1'` on
2026-10-10, using the stock `docker.io/coollabsio/coolify:<supported-version>` image and
container `coolify`:

| Check | Observed result |
|---|---|
| First run with `--output /root/coast-bootstrap-token` | Exit 0; stdout and stderr each 0 bytes; file owned by root:root, mode 0600. |
| Token scope | Root user ID 0, root team ID 0, owner membership checked by the script, and `root` ability. |
| Local API verification | Authenticated `GET http://127.0.0.1:8000/api/v1/version` returned HTTP 200 and the pinned version. The script also completed documented API enablement. |
| Repeat with the same output path | Exit 0; stdout and stderr each 0 bytes; unchanged token file; exactly one token named `coast-agent-bootstrap-v1`. |
| Container staging copy | Absent after successful verification. |
| Dev cleanup | Only the created token was revoked; named-token count became 0. The host token file, test script, captured output and test locks were removed. The existing token record was unchanged, checked by a hash comparison without printing its contents. |

No token was printed or copied to the repository. The live check exercised
successful creation and same-path retry, not interrupted publication or
lost-file recovery. Existing unit tests cover host-file retry behavior.
Production coast-agent support is implemented in `coast/agent/engines/coolify.py`
and covered by the bootstrap contract in
`coast/tests/contract/coolify/test_agent_bootstrap.py`. This dated evidence
verifies only the prototype run.

## Build servers (decision Q13)

Coolify supports **multiple build servers** per team: servers registered with
`server_role=build` cannot host deployments, and when more than one usable
build server exists Coolify selects one **at random** per build (Coolify docs,
"Build Servers", checked 2026-10-09). Implications for `coolify_v4`:

- `register_server` maps Coast `role=build` -> `server_role=build`,
  `role=worker` -> `server_role=deployment` when a usable build server
  already exists, otherwise `both`.
- Coolify `POST /servers/{uuid}/registries` runs `docker login` on that
  server. Source:
  `app/Http/Controllers/Api/ServerDockerRegistriesController.php` at the selected upstream tag
  (checked 2026-10-13). The controller registry port is open only while
  the stack has a build server, so a worker login cannot succeed before
  that. `register_server` skips the POST for a worker when no Coolify
  server has `server_role=build`. A build server always logs in. The
  first `set_build_server_mode(enabled=true)` logs in workers that
  skipped registration login, and it does that before the role patches.
  A failed login leaves those roles unchanged. Removal does not log out.
- Services in an environment with >= 1 build server are created with
  `use_build_server=true` and a registry image name
  (`docker_registry_image_name`), because built images must be pushed to a
  registry the workers pull from (per-stack registry, decision Q16).
- Build-server fallback (team setting) is kept disabled so builds never land
  on workers when build servers exist; if the pinned version exposes no
  documented API for this setting, the driver verifies the default and the
  contract tests assert it.
- Coast does not choose which build server runs a build (Coolify does);
  Coast only scales the count.

## Versioning

- Driver name encodes engine **major** API: `coolify_v4`. A breaking engine
  API change -> new driver entry point (`coolify_v5`), old one kept until no
  env uses it.
- Within a major: `SUPPORTED_ENGINE_VERSIONS` is an explicit allow-list of
  exact reviewed releases from `coast/common/engine_versions.py`. Adding a
  release changes that registry, tests and published images. Live contract
  acceptance runs against each release after test-cloud deployment.
- Version-specific quirks live in small `if version >= X` shims inside the
  driver, covered by recorded fixtures per version.

## Testing

| Layer | What | Where |
|---|---|---|
| Unit | Each method against recorded HTTP fixtures (requests-mock); error mapping (401->EngineAuthError, 404->EngineNotFound, 409/422->EngineConflict, conn errors->EngineUnreachable); no secrets in log output | `coast/tests/unit/drivers/coolify/` |
| Noop | `NoopDriver` in-memory engine implementing the full ABC, used by API/conductor tests | `coast/drivers/noop.py` |
| Contract | Runs the full method suite against a **live pinned Coolify** (+1 worker VM) in CI: bootstrap -> register server -> create service (code method `create_app`; public git sample + docker image) -> set env -> deploy -> status ACTIVE -> logs -> set domains -> rollback -> stop/start -> delete. Asserts both return values and observed engine state. | `coast/tests/contract/coolify/`, `tox -e contract-coolify` |
| ABC conformance | Generic test that every registered driver implements every abstract method and declares versions | `coast/tests/unit/drivers/test_base.py` |

## Upgrade process for a new Coolify release

1. Open an MR adding a release data row to `engine_versions.py` and tests.
2. Diff `openapi.json` old tag vs new tag for endpoints in the mapping table;
   attach diff to MR.
3. Contract tests must pass on the new tag. Fix driver shims if not.
4. Build matching controller and worker images with reviewed digest locks.
5. Publish the reviewed controller golden image. Each stack owner or admin
   selects when to upgrade with the image-backed stack action. There is no
   staged canary rollout; see [engine upgrade](design/engine-upgrade.md).

## M1 noop and loader (Q41, Q42)

`coast.drivers.loader.load_driver(name, conf)` uses `DriverManager` with
`invoke_on_load=True` and `invoke_kwds={'conf': conf}`. It checks the ABC
and a non-empty version declaration. Load errors are sanitized `DriverError`
exceptions, with no fallback to noop. The caller selects `environment.driver`
and checks the environment's pinned version before engine operations.
Only implemented drivers are registered; `coolify_v4` follows in M2.

The noop engine version is `noop-1.0`. References are
`noop-<resource-kind>-<Coast-id>`, or the supplied `engine_ref`.
Observed server/service/deployment state is local to the driver instance and
keyed by environment ID and resource ID. A fresh instance has empty observed
state; deterministic references do not require stored engine state.
Registration succeeds immediately. Service creation without deployment is
STOPPED; deployment, rollback, start and restart produce ACTIVE immediately.
Cancel of an already completed noop deployment is a no-op. Deletes can be
retried. Missing observed resources raise sanitized `EngineNotFound`.
Settings calls check service existence. Q110 retains env fingerprints and
non-secret flags plus domain sets in memory for drift comparison; no plaintext
secrets are stored. Log iterators are empty. Coast objects are never modified by noop;
the conductor will own persistence, status transitions and notifications.

No new driver methods are needed for volume provisioning, exports or domain
ownership verification. The conductor owns cloud operations and export
coordination; the agent will stream archives. Verified domain routing and
volume mount settings use the existing `set_domains` and `set_volume_mounts`.
M1 added no resolved-reference column. M2 resolved Q33 with the existing
deployments.resolved_ref column; Q58 adds the optional observation field.

Driver authors can combine
`coast.tests.driver_conformance.DriverConformanceMixin` with an oslotest
TestCase. Supply a driver, endpoint and disposable resource fixtures as
specified in the mixin docstring. Live engines can override `wait_status`
to poll for completion. This shared lifecycle checks ABC return types,
version support, retry-safe references, status observations and cleanup.
Noop-specific tests cover completed cancellation, empty logs, isolation and
sanitized failures. Recorded HTTP tests are under
`coast/tests/unit/drivers/coolify/`; live tests are under
`coast/tests/contract/coolify/` and run only in the independent live lane.

Loader reference:
https://docs.openstack.org/stevedore/latest/reference/index.html

## M2 offline Coolify adapter (Q51-Q58)

The implemented adapter uses the public OpenAPI for supported releases:
`openapi.json` at the selected upstream tag
The entry point is available to the adopted-environment allow-list (Q50).
This MR adds no conductor flow or driver-loading change.
No token bootstrap, cloud operation, DB column or driver method is added.
Supported versions are `SUPPORTED_ENGINE_VERSIONS` in
`coast/common/engine_versions.py`. The links in this section cite the
the pinned release sources that were reviewed when the adapter was written.
The separate M2 contract-test item must validate it before operator use.

Projects, servers, services and keys use deterministic Coast UUID names.
Server keys belong to one environment; deploy keys belong to one service.
Bootstrap disables build-server fallback through documented PATCH /team.
Server validation is asynchronous. After POST /servers/{uuid}/validate,
register_server polls GET /servers/{uuid} until settings.is_reachable and
settings.is_usable are both true, as required by get_server_status.
The wait uses [controller_worker] build_timeout and build_poll_interval.
Only then does worker registration PATCH /servers/{uuid}/proxy to Traefik
and POST /servers/{uuid}/proxy/restart. Build servers do not start a proxy.
A validation timeout raises DriverError, so the conductor fails registration
and runs its existing deregistration revert instead of marking the server
ACTIVE.
Dedicated Git and inline Dockerfile builds require an operator image
template with `{environment_id}` and `{app_id}` (the service id), and preconfigured
per-stack registry credentials.
No default registry hostname or shared registry credential is supplied.
Compose maps to dockercompose. Creation does not deploy or auto-publish
an engine-generated domain. Service deletion preserves volume data.

Image digests and supplied image credentials fail closed. Application env
writes support Secret and per-variable build-time flags, with runtime
exposure enabled. Named volumes and bind mounts support read-only access;
configuration-file content uses the separate file-mount flow. Frozen
specifications hold secret references; the conductor restores the selected
env versions from Barbican before driver calls.
Resolved git commits use DriverResult.extra['resolved_ref'] on replay and
ObservedStatus.resolved_ref during polling (Q58). The optional field defaults
to None and does not change the method set. ObservedStatus.detail can also carry
the observed commit. The existing conductor persists the reference
in the existing deployment column. The pinned API has no resolved image
digest field; image observations without a commit keep resolved_ref=None.
Enqueue responses contain only refs;
no extra poll is performed before returning the ref for persistence.
Image tags are never reported as resolved digests.

GET, DELETE and assignment PATCH requests retry transient connection
and server failures with bounded exponential delay. Explicit HTTP 429
rejections retry for all methods, including POST: the engine rejected the
request without applying it. Retry-After delta seconds and HTTP dates take
precedence; otherwise use exponential delay with jitter, capped at 10 seconds.
The separate rate-limit budget defaults to eight attempts and 60 elapsed
seconds per request. Do not retry earlier than Retry-After if it exceeds the
budget. An in-flight request can add at most api_timeout to that elapsed cap.
Exhaustion raises EngineRateLimited with a sanitized rate-limit fault
in deployment records and failure activity;
intermediate 429 responses are not logged as faults. POST transport and server
failures never retry.
Known deployment refs replay by GET. An uncertain enqueue must be
reconciled before another POST. The conductor recovers known references
through get_deployment_status. Unknown outcomes fail without another POST
and require an operator check (lifecycle.md). Discovery is deferred
(Q59). The pinned API documents no list order and no correlation marker,
so this contract does not add a list method.
Native rollback requires a successful target with a resolved commit/image
tag; it does not fall back to an unverified rebuild.
HTTP errors contain status codes only; transport errors never include
request/response bodies or URLs. Redirects and ambient netrc/proxies are
not used. Development direct access still requires verified HTTPS.

Log iterators parse engine log timestamps and mask known token/variable
values plus password patterns. Hidden engine variables suppress log content
instead of risking disclosure. OpenAPI leaves storage item and serialized
log shapes incomplete: fixtures are explicitly schema-derived, not live
recordings. Live captures and shape validation remain the contract MR.
No engine internals were inspected to fill those gaps.

The pinned Coolify release returns a deployment log as one JSON string. Each entry
can set `hidden` to true. The pinned capture uses that flag on docker
build output. Coast returns that output on the build stream after the
same mask. Coast does not copy the entry command, which can hold
credentials. A hidden build entry with blank output becomes one line,
`Build step in progress.`, for that batch. The public line stays `ts`,
`stream`, and `line`. The OpenAPI document types `logs` as a string and
does not describe the entry fields:
`openapi.json` at the selected upstream tag
The entry shape is taken from the pinned capture
`coast/tests/unit/drivers/coolify/fixtures/v4.4.3.json` and the
synthetic file `hidden-build-log.json`.

Transport references:
https://requests.readthedocs.io/en/latest/user/advanced/
https://tenacity.readthedocs.io/en/latest/

## M2 live contracts (Q60-Q64)

Run ``tox -e contract-coolify`` with the settings described in
``coast/tests/contract/coolify/README.rst``. The suite uses a random
project and tests public Node git and Docker-image services through the
public API. The manual CI job uses a protected file-type token variable.
No token value is supplied as a command-line argument or stored in Git.
No fixtures are recorded in this item. The live pinned-release lifecycle passes on
both worker placements. This validates the method suite, not every source
credential path, fresh server creation or the separate build-server item.

Production transport still requires verified HTTPS. Plain HTTP for the
isolated dev controller uses a test-local override only (Q60).
Existing dev workers are validated and their names restored; existing
workers and keys are never deleted (Q61). Fresh deployment-only server
creation requires a usable build server on the live pin (Q62). The
multi-build-server contract creates an unvalidated deployment-only record
after making two builders usable. Its documentation-only address is never
contacted; new VM creation is not part of this contract.

Public git creation normalizes the repository URL on the live engine.
Update, deploy and rollback preserve it by omitting git_repository from
PATCH settings (Q63). Coast's repository field is immutable; branch,
commit and build settings are still applied from the desired/frozen spec.
The documented PATCH schema permits omission:
https://coolify.io/docs/api/endpoints/applications/update-application-by-uuid

Cancellation reports cancelled-by-user on the pinned API. The driver maps
it and cancelled to the existing Coast CANCELLED state (Q64).
https://coolify.io/docs/api/endpoints/deployments/cancel-deployment-by-uuid

### M2 live fixture recording

The contract suite has an opt-in recording mode. Set
`COAST_CONTRACT_COOLIFY_RECORD=1` to capture public API request/response
pairs after a successful lifecycle and cleanup. The fixture README records
the engine version, capture date and paths that remain synthetic. Headers
are omitted; credentials, environment values, infrastructure references,
addresses, hostnames and log content are replaced with stable placeholders.
Request schemas remain sourced from the pinned OpenAPI. No driver method,
API field, DB model or production security behavior changes.

### M2 multi-build-server contract (Q13)

The opt-in build-server test uses at least two existing, resource-free dev
workers and a distinct run server. It temporarily sets both workers to
`server_role=build` and checks the compatible `is_build_server` observation.
The existing driver image-template option configures the registry image;
`use_build_server=true` is observed as `settings.is_build_server_enabled`
in the pinned response schema. Team fallback remains disabled.

Each deployment must report a known selected builder, image push and a
completed image pull. The registry must contain the resolved commit tag,
the service must be ACTIVE on the run server, and neither builder may host
resources. Build cache is disabled on the disposable service. The test samples
at least two builds, up to a configurable bound, to observe both builders.
Coolify chooses randomly; the public API has no per-build builder selector,
and a bounded sample cannot guarantee every builder is selected.

Teardown removes only test-owned records and restores server roles/names
and team fallback. Registry storage and Docker logins are operator-managed
prerequisites. The DEV ONLY plain-HTTP registry procedure is documented in
`tools/coolify/README.rst`; production transport requirements do not change.
Pinned schema: `openapi.json` at the selected upstream tag
Selection: https://coolify.io/docs/knowledge-base/server/build-server

### Q95 catalog build and create boundary

The Coolify driver reads `coast/drivers/coolify/templates/catalog-<TEMPLATE_VERSION>.json`
(`TEMPLATE_VERSION` in `coast/common/engine_versions.py`)
from the Python package for every supported `engine_version`; the reviewed
template source is byte-identical across the supported releases.
`ep` can be `None` for both metadata methods; neither opens an engine client.
Unknown versions raise `UnsupportedEngineVersion`. Missing IDs raise
`EngineNotFound`. Base driver defaults return an empty list and raise
`EngineNotFound`, so other drivers remain compatible without a catalog.
Each read returns fresh metadata; callers cannot change the packaged catalog.

`tools/template-catalog-source.json` locks the upstream tag URL and SHA256.
`tox -e template-catalog` downloads that file, checks the digest, regenerates
metadata in memory and compares the packaged file without changing it.
Run the following to regenerate after a reviewed lock update:

```bash
python tools/gen-template-catalog.py /path/to/service-templates.json \
  --version <TEMPLATE_VERSION> --sha256 <reviewed-source-sha256>
```

The catalog keeps only metadata and variable names. The generator also
writes `compose-<version>.json`: the pinned tag's base64 compose document for
each template, with no env values. Template creates send that compose as
`docker_compose_raw`, never the template key as `type`. A running engine
replaces its bundled template list with a CDN download on every update
check, so the keys it accepts drift from the pinned tag (the 2026-10 CDN
bundle dropped 58 keys, including `wordpress-with-mariadb`, and the engine
answered `404 Invalid service type`). Sending the pinned compose keeps
creates matched to the stack's engine version. Templates the engine joins to
its predefined network by key (`pgadmin`, `databasus`, `redis-insight` at the
pinned tag) are created with `connect_to_docker_network: true`.

`required_env` lists referenced variable names, including Coolify-generated `SERVICE_*` names; it is not a claim that the
caller must supply those generated values. Invalid numeric port metadata
outside 1-65535 is omitted, not repaired. In the pinned release `healthchecks` has 80000.

The `create_app` template branch accepts a source with `type='template'`
and `template_id`. The source can be an object or a mapping. It validates
the versioned catalog before an engine call, then uses documented
`POST /services` with the pinned `docker_compose_raw` for the stack's
engine version and never sends `type`. It needs explicit
`app.project_engine_ref` (the Appgroup engine ref) and
`app.environment_engine_ref` (the Environment engine ref). It does not use
the stack project or the environment name `production`. A missing ref
raises `DriverError` before HTTP. `server_engine_ref` addresses the placed
worker; `destination_engine_ref` can select a destination. It sends
`instant_deploy=false` and returns only the service UUID. Recorded refs or a
unique deterministic service name permit adoption without a second POST.
Generated env/compose values and response bodies are never logged or stored.

Service create stores `template_id`. The conductor passes
`{'type': 'template', 'template_id': service.template_id}` and sets the
two parent refs from the service environment and its appgroup.
The noop driver accepts that source. `set_env`, `set_domains` and status
lookup dispatch on `service.template_id`. The conductor calls env and domain
methods on create and on their update paths. Env PUT/PATCH retains the service
RECONCILING lock until the reference-only `update_service` cast completes.
The conductor reads values from Barbican, not from RPC options. Domain
verification and deletion use the same driver domain dispatch. No change
starts a deployment. The conductor must not log generated `SERVICE_*` values.
Template deploy, delete and mount handling are implemented in
`coast/drivers/coolify/v4.py`; they operate on `/services` and Compose.

### Template env and domains in the pinned Coolify release (c3te)

The pinned public ServicesController defines these endpoints, relative to
`/api/v1`:

| Method | Endpoint | Fields or result |
|---|---|---|
| GET | `/services/{uuid}/envs` | Env list with `uuid`, `key`, `comment`; responses can contain secrets. |
| POST | `/services/{uuid}/envs` | Create one key; an existing key returns 409. |
| PATCH | `/services/{uuid}/envs` | Update by `key`, not env UUID; a missing key returns 404. |
| PATCH | `/services/{uuid}/envs/bulk` | `{"data": [{"key": "KEY", "value": "value", ...}]}`; upsert by key. Empty data returns 400. |
| DELETE | `/services/{uuid}/envs/{env_uuid}` | Delete one variable by env UUID. |
| GET | `/services/{uuid}` | `appgroups[].name` and `appgroups[].fqdn` identify domain targets; `databases` are not domain targets. |
| PATCH | `/services/{uuid}` | `urls`: array of `name` and `url`; `name` is the container name, not its UUID or human name. |

Env writes accept `key`, `value` (string or null), `is_literal`,
`is_multiline`, `is_shown_once` (booleans), and `comment` (nullable string,
maximum 256 characters). The driver uses bulk PATCH with `is_literal=true`,
`is_multiline` from a newline in the value, `is_shown_once=is_secret`, and
`comment="coast-managed"`. It deletes absent keys and preview rows only
when this comment marks ownership. It preserves unmarked template defaults
and generated keys.
An empty desired set does not send an empty bulk request. `build_time=true`
fails before HTTP: one-click services run images and expose no build/runtime
flags. Application variables use the same ownership marker and
fields, with `is_preview=false`, `is_buildtime=item.build_time`, and `is_runtime=true`
on both POST and PATCH. They use GET to find existing runtime keys,
POST to create missing keys, and PATCH to update existing keys by key.
The controller agent proxy permits these documented env routes.
Application build-time flags are supported (Q52, M23). Neither env values
nor service GET/PATCH response bodies are logged or saved.

Each domain `url` is a comma-separated list of `https://` hostnames; an empty
string clears that container's `fqdn`. The driver sends
`instant_deploy=false` and does not force domain conflicts. With no Coast
container selector, a non-empty domain set requires exactly one exposed
application, or exactly one application if none is exposed. Ambiguous or
missing targets fail before PATCH. Empty sets clear every exposed application
(or every application when none is exposed). Databases are never selected.

Template-defined mounts remain in Compose. Desired user mounts on
single-container templates are applied by editing Compose through the public
service API. Application named volumes and bind mounts use the application
storage routes. Typed mounts carry `read_only` and bind `host_path`; see
Typed mounts below.

Pinned source (public API only):
`app/Http/Controllers/Api/ServicesController.php` at the selected upstream tag

Public API reference:
https://coolify.io/docs/api-reference/api/operations/create-service

## M4 settings comparison (Q110)

`compare_settings(ep, stack, service, variables, fqdns)` returns `env` and
`domains` flags: True for drift, False for a match, None for unknown or
unsupported. It never returns secrets or changes desired state. The base
returns unknown. Coolify uses public GET `/applications/{uuid}/envs` and
GET `/applications/{uuid}`, or corresponding template `/services` paths.
Domain comparison uses unordered case-insensitive HTTPS URL sets and the
existing template target guards. Env comparison preserves and excludes unmarked engine defaults, including
buildpack-control variables and their preview copies. An empty desired set
with no Coast-managed rows is a definite match (`env=False`), even when
engine defaults contain duplicate keys. Only Coast-managed rows participate
in key uniqueness and value/flag comparison; a desired key represented only
by an engine default is drift. Missing/shown-once managed values are unknown. Application runtime variables use
the same comparison, including `is_preview=false`, per-variable `is_buildtime`,
and `is_runtime=true`. Managed preview rows are drift even when the normal
copy has the same key. A managed row with a build-time flag that differs from Coast is drift,
even when its value is hidden. Template build-time intent remains unknown.
The synthetic `reconcile.json` fixture uses pinned public API shapes, not a
live recording. Live application/template tests also check comparison.
M4 c4rc2 adds git/image/compose comparison contracts and an opt-in read-only existing
template contract. Runtime events, task bounds and no-restart behavior stay
unchanged. No live c4rc2 run is claimed; see the contract suite README for
settings.

Compose applications use `docker_compose_domains`, not `fqdn` or the
application PATCH `domains` field. The pinned GET value is a JSON map of
service names to domain entries; PATCH takes a list of `name`/`domain`
entries on the existing `/applications/{uuid}` route. For non-empty desired
domains, select the sole existing target with domains, or the sole map entry
if none has domains. Empty or ambiguous target maps return unknown from
comparison and reject non-empty writes. Empty desired domains clear all
known targets; an empty map is already clear. Missing or malformed maps
remain unknown and reject writes. No YAML parsing or unlisted target
selection is permitted. Target selection and URL comparison use the same
rules, including case/order normalization and strict scheme, port and path
matching. A new compose workload needs an existing engine domain target
before Coast can assign non-empty domains. This fix does not add a Coast
field, proxy route or driver method.
Sources: `openapi.json` at the selected upstream tag
and `app/Http/Controllers/Api/ServicesController.php` at the selected upstream tag.

### Issue 56 run 6: compose build placement

Dedicated build servers are not supported for Docker Compose applications
by the engine. Git compose creates, updates and frozen deployments send
`use_build_server=false`. Build-mode switches keep compose applications
on their worker, including repairing an already enabled engine row, and
neither require nor assign a registry image for them. Other git build packs
retain dedicated build selection and registry requirements. This avoids
creating a helper on a builder then referring to it on the worker; no
retry or guest/engine modification is involved.
See [engine build-server documentation](https://coolify.io/docs/core/infrastructure/servers/build-servers).

### Services without exposed ports (M18)

For a git or image app with no configured port, send `ports_exposes=""`
and `domains=""`, with HTTP health checks disabled. Do not substitute port
80. Creation also sets `autogenerate_domain=false`; updates clear old
routes. Template creates use their first catalog port when no port is supplied.
Template domain writes carry that internal port (M26).
The fields use the public [application update API](https://coolify.io/docs/api/endpoints/applications/update-application-by-uuid).

### Application env sync (M20, M23)

Before each deployment or rollback enqueue, the conductor reads the current
Coast secret rows and retrieves their values from Barbican. It applies these
variables to both applications and templates before calling the engine deploy
or rollback method. If sync fails, the deployment fails without an enqueue;
the stored fault contains no variable values or engine response text.

M23 sends each application variable with its Coast build-time flag and
runtime exposure enabled. Sync removes observed Coast-managed preview copies. After application writes, it reads
the env list again to include preview copies created by those writes.
Cleanup preserves unmarked rows.
The pinned release OpenAPI omits `is_buildtime` and `is_runtime`, but the
public POST/PATCH validation accepts both. The older `is_build_time` name
is absent from the allowed fields and must not be sent. Template services
run images and their env validation has no build/runtime flags; their
existing runtime-only writes remain unchanged.

Pinned public API validation:
`app/Http/Controllers/Api/ApplicationsController.php` at the selected upstream tag

Public API reference:
https://coolify.io/docs/api-reference/api/operations/update-env-by-application-uuid

### Log value masking (M21)

The conductor passes an optional `masks` mapping of variable keys to
`EnvVar` objects to `get_app_logs` and `get_deployment_log`, including
runtime capture during deployment and reconciliation. It retrieves the
service's current secret and non-secret values through `_variables`.
These values must never be logged.

The Coolify driver masks the engine token, password patterns through
`oslo_utils.strutils.mask_password`, and Coast-known values marked
`is_secret`. Known non-secret values remain visible. Engine-only visible
values retain conservative masking. A shown-once engine key suppresses
the whole line only when that key is absent from the Coast-known mapping.
Empty secret values do not match text; longer values are masked first.

### M26 template routing ports

Template service domain writes include the first service port as an internal
routing suffix, for example `https://app.example.com:3001`. Domain reads and
drift comparisons remove that suffix before comparing public hostnames.
Application domain handling is unchanged. See the
[Coolify domain syntax](https://coolify.io/docs/core/networking/domains).

### M27 template HTTP port metadata

The catalog adds nullable integer `http_port`. The generator scans decoded
compose text for `SERVICE_URL_` and `SERVICE_FQDN_` magic variables in text
order. The first variable with a numeric port suffix selects the HTTP port,
even if metadata lists a different port. If only bare variables exist, it
uses the first catalog port, or null without ports. With no URL magic
variables, the HTTP port is null. The driver returns this derived metadata;
API 1.7 exposes it and applies the create defaults in api.md.

### M27 template deployment logs

Template deployment references are service UUIDs, not deployment UUIDs.
Read container output through the documented service logs endpoint instead:
[Coolify service logs](https://coolify.io/docs/api/endpoints/services/get-service-logs-by-uuid).
The pinned release OpenAPI requires `sub_service_name` from the service's
`applications[].name` or `databases[].name`. Prefer the single application
with a URL, else the single application or database. With no unambiguous
target, return no log lines. Template deployment logs are a completed
snapshot, including when the service or its logs no longer exist.
Use service env values and the same Coast-known secret masking as application
logs. Never read `/deployments` for template deployment logs.

The HTTP client does not retry 404 responses. It raises `EngineNotFound`
immediately. Live-log reads map this to empty, complete output without a
capture retry. Stored failure output remains available as a fallback.
HTTP 403 from the template service logs route is also unavailable output,
as older controller agents deny this new read route. Do not retry it.
HTTP 401 and authorization failures on service metadata or env reads keep
their existing error behavior, as do connection faults. Existing controller
agents are not updated by this change.

### M39 compose editor

Drivers implement `get_compose(ep, stack, service)` and
`set_compose(ep, stack, service, content)`. Defaults fail closed. The
conductor stores only Barbican references and applies the frozen content
before deploying. The Coolify driver supports template `/services` only;
the pinned application PATCH has no compose-content field. This supersedes
the earlier statement that template compose is never stored: it is now
stored in Barbican when the editor first reads or replaces it. See the
[compose contract](contributor/specs/compose-editor.md).

### M39 service container observations

The conductor reads compose workload containers from the worker agent over
management mTLS, independently of the engine driver. The worker uses Docker
Engine API 1.44 list/inspect/logs and returns selected fields only. See
[the M39 contract](contributor/specs/compose-editor.md).
No new engine proxy route or driver method is needed.

### Content file storage (M39)

`set_file_mounts(endpoint, stack, service, mounts)` replaces Coast-owned
content files before enqueue. `FileMount` has `id`, `container_path`,
write-only `content` (excluded from repr), and optional `resource_name`.
The pinned Coolify release uses GET/POST `/applications/{uuid}/storages` or
`/services/{uuid}/storages`, plus item DELETE. POST uses `type=file`,
`is_directory=false`, `mount_path`, `content`, and a Coast-generated relative
`fs_path` (`coast-files/{id}`). Templates also supply `resource_uuid` from
exact child-name matching, or the sole application/database child.
Unsupported or ambiguous targets fail before mutation. Named storage
updates preserve files under `coast-files/`; the file apply removes and
recreates only those records. Storage HTTP faults never log response text.
No mode/chmod field is documented in the pinned public storage API:
`openapi.json` at the selected upstream tag.

## Named-volume observations (M40)

`list_volume_mounts(endpoint, stack, service)` returns a list of dictionaries
with only `name` and `container_path`. It reads the documented public
application/service storage GET endpoint. It must never write engine
state, inspect engine internals, or return host sources, file contents or
engine IDs. The noop driver returns an empty list. Unsupported drivers
raise `DriverError`; connection/authentication faults keep their types.

### Typed mounts (M45)

`Mount` adds `type`, `host_path` and `id`; `read_only` applies to both
binds and volumes. Observations include type/source/path/mode, never file
content. The application storage public API uses `type=file`, `fs_path`
and `is_directory=true` for binds. The pinned version emits `mount_path`
in Compose short syntax; append `:ro` for a read-only target. Reconcile
compares that mode as part of mount identity. Missing directories are
created by the engine directory-mount flow. No worker shell call is added.
Template mounts use the public raw Compose GET/PATCH API and long syntax,
with `read_only` and `bind.create_host_path=true`. An `x-coast-storage`
extension identifies managed entries for removal. Non-managed entries
remain unchanged. Added template mounts require one container.

Public schema: `openapi.json` at the selected upstream tag.
Compose syntax: <https://docs.docker.com/reference/compose-file/services/#volumes>.

## Domain routing apply (issue 29)

`set_domains` compares the observed full domain set before writing, using
case/order normalization and the existing compose/template target guards.
After a changed list is saved, running applications (including compose)
use public POST `/applications/{uuid}/restart`; templates use public POST
`/services/{uuid}/restart`. These actions reuse the image without rebuilding.
Stopped, starting and unknown-status workloads are not restarted. A brief
interruption is possible. Default-domain changes and automatic DNS use the
same path. No new driver signature or Coast API fields are required.

If restart fails, restore the previous saved domain configuration and
propagate the failure. This preserves observable drift for the next retry;
if restoration also fails, the operation reports failure for operator review.
An unchanged saved list cannot prove that an earlier external restart applied.
The implementation does not restart on every reconciliation pass.

This narrowly supersedes Q110's no-restart rule for domain changes;
running-state and environment-variable drift still do not restart workloads.
See `coast/drivers/coolify/v4.py` for the implemented routing restart
and compensation path.

App engine server entries for Server resources use the saved
`server.hostname`, including idempotent discovery before a create retry.
Projects, environments, applications and keys retain their ID-based names.

### Prepared storage reconciliation

`prepare_volume_mounts(endpoint, stack, service, mounts)` observes and
compares storage without the service task. It returns `None` when no change
is needed, or a local callable for the required writes. The conductor
revalidates the service and stack revisions and acquires RECONCILING before
calling it. The callable is never serialized over RPC. The Coolify driver
prepares application storage deletes/creates and template compose changes
from the documented public API. `set_volume_mounts` retains its existing
synchronous behavior for other flows by preparing and applying immediately.

## Supported engine releases (issue 56)

`coast/common/engine_versions.py` is the release registry. The last release
is the default for `[coolify] pinned_version`; the option must select a
supported release and affects new stacks only. Existing stacks continue to
use their own `engine_version`. Public engine observations accept any
supported release, then stack operations verify the stack's exact version.
Creating app groups and environments in an existing stack uses that stack's
recorded version, even when the operator pin selects a newer release.
An environment name must be unique among live environments in its app
group. A duplicate returns HTTP 409 with the existing environment ID;
choose another name or use the existing environment. This conflict is
independent of engine version.
Golden images bake one version into `/usr/lib/coast/engine-version`.
First boot and local bootstrap accept only that version's stock images.
Retained `/data` identity is preserved; first boot updates `LATEST_IMAGE`
and keeps `AUTOUPDATE=false`, loopback bindings and terminals disabled.

To add a Coolify release:

1. Verify the public API, bootstrap contract, stock installer and offline
   artifact checksums, helper/support tags and template bytes. Add one
   release data row in `engine_versions.py`, reusing a reviewed profile
   only when the bytes and contracts match. Changed contracts require code
   and tests as well as release data.
2. Extend offline tests for the release, mixed fleets, upgrade and rollback.
   The literal guard prevents new runtime version pins elsewhere.
3. Build controller and worker images with reviewed immutable digest locks;
   publish their matching `coast_engine_version` metadata. Snapshot-backed
   upgrades and rollback use these published images and retain rollback
   points on partial failure. A transient supported old version during boot
   is retried until the upgrade deadline.
4. After deploying on an isolated test cloud, run the contract suite for
   each supported release with `COAST_CONTRACT_COOLIFY_VERSION` set. Run
   live upgrade/rollback acceptance there. Offline CI is not live evidence.

## Standalone database hooks (issue #121)

`create_database(endpoint, stack, database, credentials)` returns a
`DriverResult`. The conductor passes checkpointed plaintext credentials
only in memory; drivers must never log them. Create recovers by deterministic
name after uncertain outcomes. `delete_database` removes configurations,
named volumes and containers idempotently. `get_database_status` reports
running/healthy as ACTIVE and exited as STOPPED. `update_database` applies
metadata and CPU/memory limits without changing credentials or images.
Generic runtime observations dispatch database rows to the database hook,
including failover and the existing start confirmation and timeout loop.

Database CPU/memory changes are checkpointed with `settings_changed_at`.
PATCH stores engine configuration without interrupting the database.
The response exposes `restart_required` until a successful explicit start
or restart applies the configuration. Failed actions retain the checkpoint.
Start and restart record a fresh durable readiness window and report STARTING
until continuously healthy for `stable_seconds`, or ERROR at `start_timeout`.
Metadata writes do not extend that deadline. Stop reports STOPPED.

`update_database` returns False after configuration is saved, including
when runtime limits still await restart, and None only when application is
deferred because the engine is busy. A start/restart may proceed using the
engine's last saved configuration in that case, but must retain the pending
limit checkpoint and `restart_required` flag.

Public access changes apply the engine public settings and restart a running
database, reporting STARTING until ready. A stopped database retains the
settings for its next start. CPU/memory-only updates still await an explicit
start or restart. Metadata-only updates do not restart the database.

### Deploy key replacement (#136)

`prepare_deploy_key(ep, env, app, private_key, reference)` finds the app's
owned key through `GET /security/keys` and returns its UUID as both old and
new engine references. It performs no mutation. The conductor checkpoints
that UUID before invoking `write_deploy_key` with the selected Barbican
payload. The real adapter sets `DEPLOY_KEY_IN_PLACE = True` and uses only
`PATCH /security/keys/{uuid}` with `private_key`. Neither the UUID nor the
application binding changes. Rollback invokes the same operation with the
previous secret payload. The agent allowlist admits that documented key
PATCH over the existing mTLS connection.

Equal old/new references identify a shared live registration: cleanup must
not delete it after success or rollback. Only the obsolete Barbican secret
is removed. App deletion lists and removes every matching owned key, even
if a prior delete removed the application or only some keys. Unrelated
keys remain untouched. Key-update error bodies are excluded from logs.

## Pasted source content (#137)

Repository-free Dockerfile creation receives plaintext content locally in
`source.dockerfile`; Compose creation receives `source.compose`. The
conductor reads these from Barbican immediately before the driver call.
They never travel over RPC. `set_dockerfile(ep, env, app, content)` skips
unchanged Dockerfile content and rejects changed content before deployment;
`set_compose` replaces content without deployment for pasted Compose
services. The conductor applies immutable deployment content
references before deploy or rollback. Engine resource routing uses the
persisted source type, independently of the user-facing workload kind.
