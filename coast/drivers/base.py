# Copyright 2026 Gozunga and Coast contributors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import dataclasses
from typing import Iterator
from typing import Mapping
from typing import Optional
from typing import Sequence

from coast.common.named_volumes import container_ok
from coast.common.named_volumes import volume_name_ok


@dataclasses.dataclass(frozen=True)
class EngineEndpoint:
    """How to reach the engine API on the controller (via coast-agent)."""
    base_url: str               # https://<controller mgmt ip>:9443/api/v1
    client_cert: str            # PEM path, Coast CA-issued client cert
    client_key: str             # PEM path
    ca_cert: str                # Coast CA bundle
    token: Optional[str] = dataclasses.field(repr=False)
    timeout: int = 30
    # Per-stack registry. Empty values keep the global image template and
    # skip Coolify docker login. Password and username stay out of repr.
    registry_image_template: Optional[str] = None
    registry_host: Optional[str] = None
    registry_username: Optional[str] = dataclasses.field(
        default=None, repr=False)
    registry_password: Optional[str] = dataclasses.field(
        default=None, repr=False)


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
    # Engine container state when it disagrees with a finished rollout,
    # e.g. 'restarting' or 'exited' (crash loop).
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
    type: str = 'volume'
    host_path: str | None = None
    id: str | None = None


@dataclasses.dataclass(frozen=True)
class FileMount:
    id: str
    container_path: str
    content: str = dataclasses.field(repr=False)
    resource_name: str | None = None


def validate_mounts(mounts):
    """Validate typed mounts and unique container paths."""
    from coast.common import storage
    seen = set()
    for mount in mounts:
        valid_source = (volume_name_ok(mount.name) if mount.type == 'volume'
                        else mount.type == 'bind' and
                        storage.host_path_ok(mount.host_path))
        if (not valid_source or not isinstance(mount.read_only, bool) or
                not container_ok(mount.container_path) or
                mount.container_path in seen):
            raise DriverError(
                'Named volumes need a volume name and a unique '
                'absolute container path')
        seen.add(mount.container_path)


class DriverError(Exception):
    """DriverError; messages must not contain secrets."""


class EngineUnreachable(DriverError):
    """EngineUnreachable; messages must not contain secrets."""


class EngineRateLimited(DriverError):
    """Engine rejected the request because its rate limit was exceeded."""


class EngineAuthError(DriverError):
    """EngineAuthError; messages must not contain secrets."""


class EngineConflict(DriverError):
    """EngineConflict; messages must not contain secrets."""


class DockerfileChangeUnsupported(DriverError):
    """The existing app cannot accept replacement Dockerfile content."""


class EngineNotFound(DriverError):
    """EngineNotFound; messages must not contain secrets."""


class UnsupportedEngineVersion(DriverError):
    """UnsupportedEngineVersion; messages must not contain secrets."""


class CoastDriver(metaclass=abc.ABCMeta):
    """Base class for synchronous conductor/health-manager engine calls."""

    #: Exact releases validated by contract tests.
    SUPPORTED_ENGINE_VERSIONS: Sequence[str] = ()
    #: Feature flags the API may consult (e.g. 'rollback', 'build_server')
    CAPABILITIES: frozenset = frozenset()

    def __init__(self, conf):
        self.conf = conf

    def list_templates(self, ep: EngineEndpoint, stack) -> Sequence[Mapping]:
        """Read local metadata for the stack version; no engine request."""
        return []

    def get_template(self, ep: EngineEndpoint, stack, template_id) -> Mapping:
        """Return one template; unsupported drivers fail closed."""
        raise EngineNotFound('Template was not found')

    # ---- engine / environment -------------------------------------------
    @abc.abstractmethod
    def get_engine_version(self, ep: EngineEndpoint) -> str:
        """Return the running engine version (health + upgrade checks)."""

    def disable_builtin_terminals(self, ep: EngineEndpoint, stack) -> None:
        """Turn off the built-in terminal on every engine server.

        The default does nothing. A driver that exposes the setting
        overrides this method. Callers decide whether a failure is fatal.
        """

    @abc.abstractmethod
    def bootstrap_environment(self, ep: EngineEndpoint, env) -> DriverResult:
        """One-time engine-side setup on a freshly bootstrapped controller.

        Called AFTER coast-agent has installed the engine and returned an
        API token (lifecycle.md "Engine bootstrap"); ep.token is that token.
        Applies documented settings (e.g. build-server fallback off) and
        creates the engine-side project/environment. Returns engine_ref of
        the engine project. Idempotent.
        """

    @abc.abstractmethod
    def ensure_project(self, ep: EngineEndpoint, stack,
                       application) -> DriverResult:
        """Create or adopt one Coolify project for this application.

        Return the project UUID in engine_ref. Do not create an
        environment. A second call with the same engine_ref is a no-op.
        """

    @abc.abstractmethod
    def delete_project(self, ep: EngineEndpoint, stack,
                       application) -> None:
        """Delete that Coolify project after its environments are gone."""

    @abc.abstractmethod
    def delete_stack_project(self, ep: EngineEndpoint, stack) -> None:
        """Delete the Coolify project and key that belong to this stack.

        Stack teardown calls this method. It is separate from
        delete_project, which removes one application project.
        """

    @abc.abstractmethod
    def ensure_environment(self, ep: EngineEndpoint, stack, application,
                           environment) -> DriverResult:
        """Create or adopt one Coolify environment.

        Return that UUID in engine_ref. A second call is a no-op.
        """

    @abc.abstractmethod
    def delete_environment(self, ep: EngineEndpoint, stack, application,
                           environment) -> None:
        """Delete that Coolify environment after its services are gone.

        This method does not delete the stack project. Use
        delete_stack_project for stack teardown.
        """

    # ---- servers ----------------------------------------------------------
    @abc.abstractmethod
    def register_server(self, ep: EngineEndpoint, env, server,
                        ssh_private_key: str) -> DriverResult:
        """Register a worker/build server.

        Reach server.private_ip with
        the env's SSH key; validate it; ensure proxy (Traefik) on workers.
        Returns engine server ref.
        """

    @abc.abstractmethod
    def set_build_server_mode(self, ep: EngineEndpoint, stack, workers,
                              services, enabled: bool) -> None:
        """Set worker build roles and git-service builder settings.

        Inputs contain only registered workers and git services in this stack.
        Absolute writes are idempotent. Read previous settings before writing;
        compensate every attempted write on failure, including lost replies.
        Raise DriverError if the switch or compensation cannot be confirmed.
        Never alter Coast roles, source fields, or engine references.
        """

    @abc.abstractmethod
    def unregister_server(self, ep: EngineEndpoint, env, server) -> None: ...

    @abc.abstractmethod
    def get_server_status(self, ep: EngineEndpoint, env,
                          server) -> ObservedStatus: ...

    SUPPORTS_DEPLOY_KEY_ROTATION = False
    DEPLOY_KEY_IN_PLACE = False

    # ---- apps -------------------------------------------------------------
    @abc.abstractmethod
    def create_database(self, ep, env, database, credentials):
        """Create idempotently using checkpointed credentials (never log)."""
        raise NotImplementedError()

    @abc.abstractmethod
    def delete_database(self, ep, env, database):
        """Idempotently remove the database and its named data volumes."""
        raise NotImplementedError()

    @abc.abstractmethod
    def get_database_status(self, ep, env, database):
        """Return observed runtime status without credentials."""
        raise NotImplementedError()

    @abc.abstractmethod
    def update_database(self, ep, env, database):
        """Return True if restarting, False if applied, or None to defer."""
        raise NotImplementedError()

    @abc.abstractmethod
    def create_app(self, ep: EngineEndpoint, env, app, source,
                   source_credentials: Optional[str]) -> DriverResult:
        """Create the engine app (no deploy). Returns engine app ref."""

    def prepare_deploy_key(self, ep, env, app, private_key, reference):
        """Observe old/new key refs without changing live credentials.

        In-place adapters return the same owned key UUID twice. Registration
        adapters must adopt the same registration after an uncertain reply.
        """
        raise NotImplementedError()

    def write_deploy_key(self, ep, env, app, key_ref, private_key):
        """Replace the material of an app-owned key, retaining its UUID."""
        raise NotImplementedError()

    def restore_deploy_key(self, ep, env, app, key_ref):
        raise NotImplementedError()

    def delete_deploy_key(self, ep, key_ref):
        raise NotImplementedError()

    @abc.abstractmethod
    def update_app(self, ep: EngineEndpoint, env, app, source) -> None:
        """Apply non-secret app settings (ports, limits, healthcheck...)."""

    def prepare_volume_mounts(self, ep, env, app, mounts):
        """Observe without a task; return a deferred write only for drift.

        The caller must revalidate desired state and acquire the service
        task before invoking the returned callable. It is local, not RPC.
        Drivers with engine-specific ownership rules override this method.
        """
        validate_mounts(mounts)
        wanted = sorted((m.type, m.host_path if m.type == 'bind' else m.name,
                         m.container_path, m.read_only) for m in mounts)
        observed = sorted((m['type'], m['source'], m['container_path'],
                           m['read_only']) for m in
                          self.list_volume_mounts(ep, env, app))
        if wanted != observed:
            return lambda: self.set_volume_mounts(ep, env, app, mounts)

    def list_volume_mounts(self, ep, env, app):
        """Return type/source/container_path/read_only mount dictionaries.

        Host sources are tenant-visible; omit content and engine IDs.
        """
        raise DriverError('Volume observations are unsupported')

    def set_dockerfile(self, ep: EngineEndpoint, env, app,
                       content: str) -> None:
        """Replace desired Dockerfile without starting deployment."""
        raise DriverError('Dockerfile writes are unsupported')

    def get_compose(self, ep: EngineEndpoint, env, app) -> str:
        """Read engine compose for a one-time protected Coast import."""
        raise DriverError('Compose reads are unsupported')

    def set_compose(self, ep: EngineEndpoint, env, app, content: str) -> None:
        """Replace compose through the public API; do not start containers."""
        raise DriverError('Compose writes are unsupported')

    def update_metadata(self, ep: EngineEndpoint, env, app) -> None:
        """Push the service description. The default does nothing.

        The engine resource name stays the deterministic Coast name.
        """

    def list_domains(self, ep: EngineEndpoint, env, app):
        """Return the engine domain list, or None when it is unknown."""
        return None

    @abc.abstractmethod
    def delete_app(self, ep: EngineEndpoint, env, app) -> None: ...

    def compare_settings(self, ep, stack, service, variables, fqdns):
        """Return {'env': bool|None, 'domains': bool|None}; no secrets.

        True means drift, False means a match, None means unknown or
        unsupported. Drivers without observations fail closed.
        """
        return {'env': None, 'domains': None}

    @abc.abstractmethod
    def set_env(self, ep: EngineEndpoint, env, app,
                variables: Mapping[str, EnvVar]) -> None:
        """Replace all app variables; never log plaintext values."""

    def set_file_mounts(self, ep, env, app, mounts):
        """Replace Coast-managed content files before deployment enqueue."""
        if mounts:
            raise DriverError('File mounts are not supported by this driver')

    @abc.abstractmethod
    def set_volume_mounts(self, ep: EngineEndpoint, env, app,
                          mounts: Sequence[Mount]) -> None: ...

    @abc.abstractmethod
    def start_app(self, ep: EngineEndpoint, env, app) -> None: ...

    @abc.abstractmethod
    def stop_app(self, ep: EngineEndpoint, env, app) -> None: ...

    @abc.abstractmethod
    def restart_app(self, ep: EngineEndpoint, env, app) -> None: ...

    @abc.abstractmethod
    def get_app_status(self, ep: EngineEndpoint, env,
                       app) -> ObservedStatus: ...

    # ---- deployments ------------------------------------------------------
    @abc.abstractmethod
    def deploy_app(self, ep: EngineEndpoint, env, app,
                   deployment) -> DriverResult:
        """Trigger deployment.spec / deployment.source_ref.

        Returns engine deployment ref. Must not block until finished.
        """

    @abc.abstractmethod
    def get_deployment_status(self, ep: EngineEndpoint, env, app,
                              deployment) -> ObservedStatus:
        """Includes the reported commit/image digest in resolved_ref."""

    @abc.abstractmethod
    def cancel_deployment(self, ep: EngineEndpoint, env, app,
                          deployment) -> None: ...

    @abc.abstractmethod
    def rollback(self, ep: EngineEndpoint, env, app, target_deployment,
                 new_deployment) -> DriverResult:
        """Redeploy the target image/commit as new_deployment.

        Return the new engine deployment reference.

        """

    @abc.abstractmethod
    def get_deployment_log(self, ep: EngineEndpoint, env, app, deployment,
                           since: Optional[str] = None,
                           masks: Optional[Mapping[str, EnvVar]] = None
                           ) -> Iterator[LogLine]: ...

    @abc.abstractmethod
    def get_app_logs(self, ep: EngineEndpoint, env, app,
                     tail: int = 200,
                     masks: Optional[Mapping[str, EnvVar]] = None
                     ) -> Iterator[LogLine]: ...

    # ---- domains ----------------------------------------------------------
    @abc.abstractmethod
    def set_domains(self, ep: EngineEndpoint, env, app,
                    fqdns: Sequence[str]) -> None:
        """Replace the app's full domain list (default + verified custom).

        TLS via the engine's proxy (Traefik + Let's Encrypt).
        """
