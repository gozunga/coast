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

"""Coolify v4 adapter. Uses only the pinned public OpenAPI endpoints."""

import base64
import datetime
import json
import re
import time
from types import SimpleNamespace
from urllib import parse

from oslo_utils import strutils

from coast.common import engine_resources
from coast.common import engine_versions
from coast.common import states
from coast.drivers import base
from coast.drivers.coolify import catalog
from coast.drivers.coolify import client
from coast.drivers.coolify import settings


def name(resource):
    if hasattr(resource, "hostname"):
        return resource.hostname
    return "coast-" + resource.id


def ref(value):
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise base.DriverError("Coolify resource reference is missing")
    return parse.quote(value, safe="")


def parent_refs(app):
    """Return the Coolify project and environment references.

    Q94: the project is the application. The environment is the
    environment row. Do not use the stack project or "production".
    A missing reference fails closed.
    """
    return (
        ref(getattr(app, "project_engine_ref", None)),
        ref(getattr(app, "environment_engine_ref", None)),
    )


def identifier(row, key="uuid"):
    if not isinstance(row, dict) or not isinstance(row.get(key), str):
        raise base.DriverError("Coolify response has no resource reference")
    ref(row[key])
    return row[key]


def find(c, path, resource):
    rows = c.request("GET", path)
    if not isinstance(rows, list):
        raise base.DriverError("Coolify resource list is invalid")
    found = [row for row in rows if row.get("name") == name(resource)]
    if len(found) > 1:
        raise base.EngineConflict(
            "Coolify deterministic resource name is " "not unique"
        )
    return found[0] if found else None


def resource_ref(c, path, resource):
    if resource.engine_ref:
        return ref(resource.engine_ref)
    row = find(c, path, resource)
    if row is None:
        raise base.EngineNotFound("Coolify resource was not found")
    return ref(identifier(row))


def source_value(source, key):
    """Read one source field from a mapping or an object."""
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def application_server(c, app):
    """Return the Coolify server row that will run this application.

    A recorded engine_ref is the Coolify server uuid. Use that uuid
    directly. Name lookup stays only for a server Coast registered
    before the uuid was stored. Do not fall back from a recorded uuid
    to the deterministic name.
    """
    recorded = getattr(app, "server_engine_ref", None)
    if recorded:
        try:
            row = c.request("GET", "/servers/" + ref(recorded))
        except base.EngineNotFound:
            raise base.EngineNotFound(
                "Coolify application server not found"
            ) from None
        if not isinstance(row, dict) or row.get("uuid") != recorded:
            raise base.EngineNotFound(
                "Coolify application server not found"
            )
    else:
        row = find(c, "/servers",
                   SimpleNamespace(hostname=app.server_hostname))
        if row is None:
            raise base.EngineNotFound(
                "Coolify application server not found"
            )
    settings = row.get("settings") or {}
    if isinstance(settings, dict) and settings.get("server_role") == "build":
        raise base.EngineNotFound("Coolify application server not found")
    return row


def remove(c, path, params=None):
    try:
        c.request("DELETE", path, params=params)
    except base.EngineNotFound:
        pass


def project_row(c, application):
    """Return the project addressed by engine_ref.

    A recorded uuid that is missing does not use the name.
    """
    if not application.engine_ref:
        return None
    try:
        row = c.request(
            "GET", "/projects/" + ref(application.engine_ref))
    except base.EngineNotFound:
        raise base.EngineNotFound(
            "Coolify project was not found") from None
    if (not isinstance(row, dict) or
            row.get("uuid") != application.engine_ref):
        raise base.EngineNotFound("Coolify project was not found")
    return row


def project_uuid(c, application, create):
    if application.engine_ref:
        return identifier(project_row(c, application))
    found = find(c, "/projects", application)
    if found is not None:
        return identifier(found)
    if not create:
        return None
    try:
        created = c.request(
            "POST", "/projects", data={"name": name(application)})
    except base.EngineConflict:
        found = find(c, "/projects", application)
        if found is None:
            raise
        return identifier(found)
    return identifier(created)


def environment_list(c, project):
    path = "/projects/%s/environments" % ref(project)
    rows = c.request("GET", path)
    if not isinstance(rows, list):
        raise base.DriverError("Coolify resource list is invalid")
    return path, rows


def delete_empty_project(c, project):
    """Remove engine-owned defaults before deleting an empty project."""
    try:
        path, rows = environment_list(c, project)
    except base.EngineNotFound:
        return
    if any(not isinstance(row, dict) or row.get("name") != "production"
           for row in rows):
        raise base.DriverError("Coolify project still has environments")
    # Validate every UUID before changing anything. The pinned DELETE API
    # rejects environments with resources; never bypass that engine guard.
    paths = [path + "/" + ref(identifier(row)) for row in rows]
    for environment_path in paths:
        remove(c, environment_path)
    remove(c, "/projects/" + ref(project))


def match_environment(rows, environment):
    if environment.engine_ref:
        found = [row for row in rows
                 if isinstance(row, dict) and
                 row.get("uuid") == environment.engine_ref]
        if len(found) != 1:
            raise base.EngineNotFound(
                "Coolify environment was not found")
        return found[0]
    found = [row for row in rows
             if isinstance(row, dict) and
             row.get("name") == name(environment)]
    if len(found) > 1:
        raise base.EngineConflict(
            "Coolify deterministic resource name is not unique")
    return found[0] if found else None


def engine_service(app):
    return engine_resources.resource_type(app) == 'service'


def source_settings(source):
    if source.type == "dockerfile":
        content = getattr(source, 'dockerfile', None)
        if content is None:
            return {}
        return {'dockerfile': content}
    if source.type == "git":
        if source.build_pack not in (
            "nixpacks", "railpack", "static", "dockerfile", "compose",
        ):
            raise base.DriverError("Coolify source build pack is unsupported")
        result = {
            "git_repository": source.repo_url,
            "git_branch": source.branch,
            "git_commit_sha": "HEAD",
            "build_pack": (
                "dockercompose"
                if source.build_pack == "compose"
                else source.build_pack
            ),
        }
        for field, target in (
            ("dockerfile_path", "dockerfile_location"),
            ("base_directory", "base_directory"),
            ("compose_path", "docker_compose_location"),
        ):
            if getattr(source, field, None):
                result[target] = getattr(source, field)
        if source.build_pack == "compose":
            # Compose uses helpers on the run server; dedicated builders are
            # unsupported. Also repair previously enabled apps on deploy.
            result["use_build_server"] = False
        return result
    if source.type == "image":
        image = getattr(source, "image", None) or ""
        if not image or "@" in image:
            raise base.DriverError(
                "Coolify documented API cannot pin image "
                "digests; use an explicit image tag"
            )
        last = image.rsplit("/", 1)[-1]
        image_name, tag = (
            (image, source.tag) if getattr(source, "tag", None) else
            image.rsplit(":", 1) if ":" in last else (image, "latest")
        )
        return {
            "docker_registry_image_name": image_name,
            "docker_registry_image_tag": tag,
        }
    raise base.DriverError("Coolify source type is unsupported")


def app_settings(app, source):
    if app.replicas != 1 or app.auto_deploy:
        raise base.DriverError(
            "Coolify driver requires one replica and "
            "Coast-controlled deployment"
        )
    result = source_settings(source)
    result.update(
        {
            "name": name(app),
            "description": app.description or "",
            "ports_exposes": app.ports or "",
            "limits_cpus": str(app.cpu_limit or 0),
            "limits_memory": "%sm" % (app.memory_limit_mb or 0),
            "health_check_enabled": (getattr(app, "health_check", "http")
                                     == "http"),
            "health_check_port": str(getattr(app, "health_check_port", None)
                                     or (app.ports or "80").split(",")[0]),
            "health_check_path": app.healthcheck_path or "/",
            "is_auto_deploy_enabled": False,
        }
    )
    if not app.ports:
        result["domains"] = ""
        if source.type == "dockerfile":
            result.pop("ports_exposes", None)
    return result


def _owned_domain(app):
    checker = getattr(app, 'obj_attr_is_set', None)
    if checker is not None and not checker('default_domain'):
        return None
    return getattr(app, 'default_domain', None) or None


def _domain_url(value, port=None):
    """Accept a bare hostname or one http(s) URL. Do not double-prefix."""
    text = str(value).strip()
    lowered = text.lower()
    scheme = 'https://'
    host = text
    if lowered.startswith('https://'):
        host = text[8:]
    elif lowered.startswith('http://'):
        host = text[7:]
        scheme = 'http://'
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise base.DriverError('Coolify domain must be a hostname')
    if port is not None:
        if not str(port).isdigit() or not 1 <= int(port) <= 65535:
            raise base.DriverError('Coolify domain port is invalid')
        host += ":%s" % port
    return scheme + host


def _split_urls(value):
    if not isinstance(value, str):
        return None
    return [item.strip() for item in value.split(',') if item.strip()]


def _one_target(targets):
    if len(targets) != 1:
        return None
    return _split_urls(targets[0].get('fqdn'))


def _template_domain_list(row):
    apps = row.get('applications') if isinstance(row, dict) else None
    if not isinstance(apps, list):
        return None
    exposed = [item for item in apps
               if isinstance(item, dict) and item.get('fqdn')]
    targets = exposed or [item for item in apps if isinstance(item, dict)]
    urls = _one_target(targets)
    return ([settings.template_domain_url(url) for url in urls]
            if urls is not None else None)


def _application_domain_list(row):
    if not isinstance(row, dict):
        return None
    if row.get('build_pack') == 'dockercompose':
        try:
            targets = settings.compose_targets(row, ['placeholder'])
        except ValueError:
            return None
        return _one_target(targets)
    return _split_urls(row.get('fqdn'))


def relabel_settings(row, settings):
    """Make the engine rebuild proxy labels when the exposed port changes.

    The engine keeps generated proxy labels and only regenerates them when
    domains are part of an update, so a port change alone would keep
    routing to the old port. Resend the current domains in that case.
    """
    old = str(row.get("ports_exposes") or "")
    new = str(settings.get("ports_exposes") or "")
    if new and old != new and row.get("fqdn") and "domains" not in settings:
        settings = dict(settings, domains=row["fqdn"])
    return settings


def frozen_settings(app, deployment):
    spec = deployment.spec
    if not isinstance(spec, dict) or not isinstance(spec.get("source"), dict):
        raise base.DriverError("Coolify deployment requires a frozen source")
    source = SimpleNamespace(**spec["source"])
    frozen = SimpleNamespace(
        **{
            key: spec.get(key, "http" if key == "health_check" else
                          getattr(app, key, None))
            for key in (
                "ports", "replicas", "cpu_limit", "memory_limit_mb",
                "healthcheck_path", "health_check", "health_check_port",
            )
        },
        id=app.id,
        description=app.description,
        auto_deploy=app.auto_deploy
    )
    result = app_settings(frozen, source)
    # The repository is immutable; preserve Coolify's normalized clone source.
    result.pop("git_repository", None)
    requested = deployment.source_ref
    if deployment.image_digest:
        raise base.DriverError(
            "Coolify documented API cannot pin image digests"
        )
    if requested:
        if source.type == "image":
            if not re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", requested
            ):
                raise base.DriverError("Coolify image reference must be a tag")
            result["docker_registry_image_tag"] = requested
        elif re.fullmatch(r"[0-9a-fA-F]{40}", requested):
            result["git_commit_sha"] = requested
        else:
            result["git_branch"] = requested
    return result


class CoolifyV4Driver(base.CoastDriver):
    # M2 contract tests must validate this candidate pin before deployment.
    SUPPORTED_ENGINE_VERSIONS = engine_versions.SUPPORTED_ENGINE_VERSIONS
    CAPABILITIES = frozenset({"rollback", "build_server"})

    def _client(self, ep):
        return client.Client(ep, self.conf)

    def get_engine_version(self, ep):
        with self._client(ep) as c:
            c.request("GET", "/health", text=True)
            version = c.request("GET", "/version", text=True).removeprefix("v")
        if version not in self.SUPPORTED_ENGINE_VERSIONS:
            raise base.UnsupportedEngineVersion(
                "Coolify engine version is " "not supported"
            )
        return version

    def _patch_terminal_off(self, client, server_id, settings):
        # Stock releases accept this on server update, not on create.
        if (isinstance(settings, dict) and
                settings.get("is_terminal_enabled") is False):
            return
        client.request(
            "PATCH", "/servers/" + ref(server_id),
            data={"is_terminal_enabled": False})

    def _disable_terminals(self, client):
        servers = client.request("GET", "/servers")
        if not isinstance(servers, list):
            raise base.DriverError("Coolify resource list is invalid")
        for item in servers:
            if not isinstance(item, dict):
                raise base.DriverError("Coolify resource list is invalid")
            self._patch_terminal_off(
                client, item.get("uuid"), item.get("settings"))

    def disable_builtin_terminals(self, ep, stack):
        with self._client(ep) as client:
            self._disable_terminals(client)

    def bootstrap_environment(self, ep, env):
        if self.get_engine_version(ep) != env.engine_version:
            raise base.UnsupportedEngineVersion(
                "App engine version does not match the stack")
        with self._client(ep) as c:
            self._disable_terminals(c)
            c.request(
                "PATCH", "/team",
                data={"is_build_server_fallback_enabled": False},
            )
            project = env.engine_ref
            if not project:
                row = find(c, "/projects", env)
                project = identifier(
                    row
                    or c.request("POST", "/projects", data={"name": name(env)})
                )
            path = "/projects/%s/environments" % ref(project)
            environments = c.request("GET", path)
            if not any(
                row.get("name") == "production" for row in environments
            ):
                c.request("POST", path, data={"name": "production"})
            return base.DriverResult(engine_ref=project)

    def ensure_project(self, ep, stack, application):
        with self._client(ep) as c:
            return base.DriverResult(
                engine_ref=project_uuid(c, application, True))

    def delete_project(self, ep, stack, application):
        with self._client(ep) as c:
            if application.engine_ref:
                try:
                    project_row(c, application)
                except base.EngineNotFound:
                    return
                project = application.engine_ref
            else:
                project = project_uuid(c, application, False)
                if project is None:
                    return
            delete_empty_project(c, project)

    def delete_stack_project(self, ep, stack):
        with self._client(ep) as c:
            try:
                project = resource_ref(c, "/projects", stack)
            except base.EngineNotFound:
                project = None
            if project:
                delete_empty_project(c, parse.unquote(project))
            key = find(c, "/security/keys", stack)
            if key:
                remove(
                    c, "/security/keys/" + ref(identifier(key)))

    def ensure_environment(self, ep, stack, application, environment):
        if not application.engine_ref:
            raise base.DriverError(
                "Coolify project reference is missing")
        with self._client(ep) as c:
            path, rows = environment_list(c, application.engine_ref)
            row = match_environment(rows, environment)
            if row is None:
                try:
                    row = c.request(
                        "POST", path,
                        data={"name": name(environment)})
                except base.EngineConflict:
                    path, rows = environment_list(
                        c, application.engine_ref)
                    row = match_environment(rows, environment)
                    if row is None:
                        raise
            return base.DriverResult(engine_ref=identifier(row))

    def delete_environment(self, ep, stack, application, environment):
        if not application.engine_ref:
            if getattr(environment, "engine_ref", None):
                raise base.DriverError(
                    "Coolify project reference is missing")
            return
        with self._client(ep) as c:
            project = ref(application.engine_ref)
            if environment.engine_ref:
                remove(c, "/projects/%s/environments/%s" % (
                    project, ref(environment.engine_ref)))
                return
            path, rows = environment_list(c, application.engine_ref)
            row = match_environment(rows, environment)
            if row is not None:
                remove(c, path + "/" + ref(identifier(row)))

    def _key(self, c, resource, private_key):
        row = find(c, "/security/keys", resource)
        if row is None:
            row = c.request(
                "POST", "/security/keys",
                data={"name": name(resource), "private_key": private_key},
            )
        return identifier(row)

    def register_server(self, ep, env, server, ssh_private_key):
        if server.role not in ("worker", "build") or not server.private_ip:
            raise base.DriverError(
                "Coolify requires a worker or build server "
                "with a private IP"
            )
        with self._client(ep) as c:
            row = (
                find(c, "/servers", server) if not server.engine_ref else None
            )
            server_id = server.engine_ref or (identifier(row) if row else None)
            if not server_id:
                role = "build"
                if server.role == "worker":
                    servers = c.request("GET", "/servers")
                    if not isinstance(servers, list):
                        raise base.DriverError(
                            "Coolify resource list is invalid")
                    has_build = any(
                        item.get("settings", {}).get("server_role") == "build"
                        and item.get("settings", {}).get("is_usable") is True
                        for item in servers
                    )
                    role = "deployment" if has_build else "both"
                key = self._key(c, env, ssh_private_key)
                server_id = identifier(
                    c.request(
                        "POST", "/servers",
                        data={
                            "name": name(server),
                            "ip": server.private_ip,
                            "port": 22,
                            "user": self.conf.coolify.ssh_user,
                            "private_key_uuid": key,
                            "server_role": role,
                            "proxy_type": (
                                "none" if server.role == "build" else "traefik"
                            ),
                            "instant_validate": False,
                        },
                    )
                )
            path = "/servers/" + ref(server_id)
            c.request("POST", path + "/validate", data={"install": False})
            deadline = (time.monotonic() +
                        self.conf.controller_worker.build_timeout)
            while time.monotonic() < deadline:
                settings = c.request("GET", path).get("settings", {})
                if settings.get("is_reachable") and settings.get("is_usable"):
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(
                        self.conf.controller_worker.build_poll_interval,
                        remaining))
            else:
                raise base.DriverError("Coolify server validation timed out")
            self._patch_terminal_off(c, server_id, settings)
            if server.role == "worker":
                c.request(
                    "PATCH", path + "/proxy", data={"proxy_type": "traefik"}
                )
                c.request("POST", path + "/proxy/restart")
            self._login_registry(c, ep, server_id, server)
            return base.DriverResult(engine_ref=server_id)

    def _build_server_registered(self, client):
        servers = client.request("GET", "/servers")
        if not isinstance(servers, list):
            raise base.DriverError("Coolify resource list is invalid")
        return any(
            item.get("settings", {}).get("server_role") == "build"
            for item in servers)

    def _login_registry(self, client, ep, server_id, server=None):
        host = ep.registry_host
        username = ep.registry_username
        password = ep.registry_password
        if not any((host, username, password)):
            return
        if not all((host, username, password)):
            raise base.DriverError("Registry login is incomplete")
        # Coolify runs docker login on the target. The registry port is
        # closed until a build server exists, so a worker login fails.
        if (server is not None and server.role == "worker" and
                not self._build_server_registered(client)):
            return
        path = "/servers/" + ref(server_id) + "/registries"
        client.request("POST", path, data={
            "registry": host, "username": username, "password": password})
        listed = client.request("GET", path)
        registries = (listed.get("registries") if isinstance(listed, dict)
                      else None)
        if not isinstance(registries, list):
            raise base.DriverError("Registry login was not confirmed")
        matches = [item for item in registries if item.get("registry") == host]
        if (len(matches) != 1 or matches[0].get("username") != username or
                matches[0].get("logged_in") is not True):
            raise base.DriverError("Registry login was not confirmed")

    def _image_template(self, ep):
        return (ep.registry_image_template or
                self.conf.coolify.build_registry_image_template)

    def set_build_server_mode(self, ep, stack, workers, services, enabled):
        role = "deployment" if enabled else "both"
        with self._client(ep) as c:
            changes = []
            # Snapshot all targets before the first write. Do not infer old
            # settings from the desired mode: retries can see partial writes.
            for worker in workers:
                path = "/servers/" + ref(worker.engine_ref)
                old = c.request("GET", path).get("settings", {}).get(
                    "server_role")
                if old not in ("both", "deployment"):
                    raise base.DriverError("Coolify worker role is invalid")
                if old != role:
                    changes.append((path, {"server_role": old},
                                    {"server_role": role}))
            for service in services:
                path = "/applications/" + ref(service.engine_ref)
                row = c.request("GET", path)
                old = row.get("settings", {}).get("is_build_server_enabled")
                if not isinstance(old, bool):
                    raise base.DriverError("Coolify build setting is invalid")
                before = {"use_build_server": old}
                use_builder = enabled and row.get("build_pack") != (
                    "dockercompose")
                after = {"use_build_server": use_builder}
                if use_builder and not row.get("docker_registry_image_name"):
                    template = self._image_template(ep)
                    if not template or not all(key in template for key in
                                               ("{environment_id}",
                                                "{app_id}")):
                        raise base.DriverError(
                            "Coolify dedicated builds require a registry "
                            "image template")
                    try:
                        image = template.format(environment_id=stack.id,
                                                app_id=service.id)
                    except (KeyError, ValueError):
                        raise base.DriverError(
                            "Coolify registry image template is invalid"
                        ) from None
                    before["docker_registry_image_name"] = ""
                    after["docker_registry_image_name"] = image
                if old != use_builder or len(after) > 1:
                    changes.append((path, before, after))
            # Workers registered with no build server skipped login.
            # Log them in before any role or service patch.
            if enabled:
                for worker in workers:
                    self._login_registry(c, ep, worker.engine_ref)
            attempted = []
            try:
                for path, before, after in changes:
                    # Include this write before sending: a failed reply does
                    # not prove that Coolify rejected the patch.
                    attempted.append((path, before))
                    c.request("PATCH", path, data=after)
                    self._confirm_build_mode(c, path, after)
            except Exception:
                restored = True
                for path, before in reversed(attempted):
                    try:
                        c.request("PATCH", path, data=before)
                        self._confirm_build_mode(c, path, before)
                    except Exception:
                        restored = False
                raise base.DriverError(
                    "Coolify build mode switch failed; " +
                    ("previous settings restored" if restored else
                     "operator recovery required")
                ) from None

    @staticmethod
    def _confirm_build_mode(c, path, expected):
        row = c.request("GET", path)
        if path.startswith("/servers/"):
            row = row.get("settings", {})
        else:
            row["use_build_server"] = row.get("settings", {}).get(
                "is_build_server_enabled")
        if any(row.get(key) != value and not (
                value == "" and row.get(key) is None)
               for key, value in expected.items()):
            raise base.DriverError("Coolify build mode was not confirmed")

    def unregister_server(self, ep, env, server):
        with self._client(ep) as c:
            try:
                server_id = resource_ref(c, "/servers", server)
            except base.EngineNotFound:
                return
            remove(c, "/servers/" + server_id)

    def get_server_status(self, ep, env, server):
        with self._client(ep) as c:
            server_id = resource_ref(c, "/servers", server)
            row = c.request("GET", "/servers/" + server_id)
        settings = row.get("settings", {})
        status = (
            states.ACTIVE
            if settings.get("is_reachable") and settings.get("is_usable")
            else states.UNHEALTHY
        )
        return base.ObservedStatus(status=status)

    def list_templates(self, ep, stack):
        return catalog.read(stack.engine_version)

    def get_template(self, ep, stack, template_id):
        for item in self.list_templates(ep, stack):
            if item['id'] == template_id:
                return item
        raise base.EngineNotFound("Template was not found")

    def _create_template(self, ep, stack, app, source, credentials):
        template_id = source_value(source, "template_id")
        if template_id:
            self.get_template(ep, stack, template_id)
        if credentials:
            raise base.DriverError("Template credentials are unsupported")
        # These explicit refs must be supplied by the conductor's Q94 flow.
        # Never substitute the stack project or a guessed environment.
        project, environment = parent_refs(app)
        if app.engine_ref:
            return base.DriverResult(engine_ref=ref(app.engine_ref))
        with self._client(ep) as c:
            existing = find(c, "/services", app)
            if existing:
                return base.DriverResult(engine_ref=identifier(existing))
            server = application_server(c, app)
            settings = {
                "name": name(app),
                "docker_compose_raw": (catalog.compose(
                    stack.engine_version, template_id) if template_id else
                    base64.b64encode(source_value(source, 'compose').encode(
                        'utf-8')).decode('ascii')),
                "project_uuid": project, "environment_uuid": environment,
                "server_uuid": identifier(server), "instant_deploy": False,
            }
            if template_id in catalog.PREDEFINED_NETWORK:
                settings["connect_to_docker_network"] = True
            destination = getattr(app, 'destination_engine_ref', None)
            if destination:
                settings['destination_uuid'] = ref(destination)
            # Keep only the UUID. Coolify generates SERVICE_* values; neither
            # responses nor generated compose/env values are logged or stored.
            row = c.request("POST", "/services", data=settings)
            return base.DriverResult(engine_ref=identifier(row))

    @staticmethod
    def _database_settings(database):
        return dict(name=name(database),
                    description=database.description or '',
                    limits_cpus=str(database.cpu_limit or 0),
                    limits_memory=('%sM' % database.memory_limit_mb
                                   if database.memory_limit_mb else '0'),
                    is_public=bool(getattr(database, 'public_enabled', False)),
                    public_port=getattr(database, 'public_port', None))

    def create_database(self, ep, env, database, credentials):
        info = engine_versions.DATABASES[database.engine]
        project, environment = parent_refs(database)
        with self._client(ep) as c:
            existing = find(c, '/databases', database)
            if database.engine_ref or existing:
                return base.DriverResult(
                    engine_ref=database.engine_ref or identifier(existing))
            server = application_server(c, database)
            data = self._database_settings(database)
            data.update(server_uuid=identifier(server), project_uuid=project,
                        environment_uuid=environment, instant_deploy=True,
                        image=info['versions'][database.engine_version])
            destination = getattr(database, 'destination_engine_ref', None)
            if destination:
                data['destination_uuid'] = ref(destination)
            for key, field in info['credentials'].items():
                data[field] = credentials[key]
            result = c.request('POST', '/databases/' +
                               database.engine, data=data)
            return base.DriverResult(engine_ref=identifier(result))

    def delete_database(self, ep, env, database):
        with self._client(ep) as c:
            try:
                identifier_ = resource_ref(c, '/databases', database)
            except base.EngineNotFound:
                return
            if identifier_:
                remove(c, '/databases/' + identifier_, params={
                    key: 'true' for key in (
                        'delete_configurations', 'delete_volumes',
                        'docker_cleanup', 'delete_connected_networks')})

    def get_database_status(self, ep, env, database):
        with self._client(ep) as c:
            row = c.request('GET', '/databases/' + ref(database.engine_ref))
        status, _, health = row.get('status', '').partition(':')
        mapped = {'running': states.ACTIVE, 'healthy': states.ACTIVE,
                  'exited': states.STOPPED, 'stopped': states.STOPPED,
                  'starting': states.DEPLOYING,
                  'restarting': states.DEPLOYING}.get(status, states.ERROR)
        if health == 'unhealthy':
            mapped = states.ERROR
        return base.ObservedStatus(status=mapped)

    def update_database(self, ep, env, database):
        with self._client(ep) as c:
            path = '/databases/' + ref(database.engine_ref)
            current = c.request('GET', path)
            desired = self._database_settings(database)
            limit_keys = ('limits_cpus', 'limits_memory')
            try:
                cpu_changed = (float(current.get('limits_cpus') or 0) !=
                               float(desired['limits_cpus']))
            except (TypeError, ValueError):
                cpu_changed = True
            limits_changed = (cpu_changed or
                              str(current.get('limits_memory') or '0').upper()
                              != desired['limits_memory'])
            public_changed = any(
                current.get(key) != desired[key]
                for key in ('is_public', 'public_port'))
            pending = getattr(database, 'settings_changed_at', None)
            status = current.get('status', '').split(':')[0]
            if ((limits_changed or public_changed or pending) and
                    status not in ('running', 'healthy', 'exited', 'stopped')):
                return None
            if limits_changed or public_changed or any(
                    current.get(key) != value for key, value in desired.items()
                    if key not in limit_keys):
                c.request('PATCH', path, data=desired)
            if public_changed and status in ('running', 'healthy'):
                try:
                    c.request('POST', path + '/restart')
                except Exception:
                    # Restore public configuration so retry still detects the
                    # required recreation after an unsuccessful restart.
                    c.request('PATCH', path, data={
                        key: current.get(key)
                        for key in ('is_public', 'public_port')})
                    raise
                return True
            # Runtime limits await an explicit start/restart. Retain the
            # Coast checkpoint until the action succeeds.
        return False

    def create_app(self, ep, env, app, source, source_credentials):
        if source_value(source, "type") in ("template", "compose"):
            return self._create_template(ep, env, app, source,
                                         source_credentials)
        settings = app_settings(app, source)
        if source.type == "dockerfile":
            settings["dockerfile"] = base64.b64encode(
                settings["dockerfile"].encode("utf-8")).decode("ascii")
        # Same fail-closed refs as the template path. The stack project
        # and the name "production" are not a Coolify environment.
        project, environment = parent_refs(app)
        ref(app.server_id)
        if source.type == "image" and (
            source_credentials or source.credentials_ref
        ):
            raise base.DriverError(
                "Coolify image credentials must be "
                "configured per environment in advance"
            )
        if (
            source.type == "git"
            and source.credentials_ref
            and not source_credentials
        ):
            raise base.DriverError(
                "Coolify deploy-key credentials are required"
            )
        with self._client(ep) as c:
            row = find(c, "/applications", app)
            if app.engine_ref or row:
                return base.DriverResult(
                    engine_ref=app.engine_ref or identifier(row)
                )
            server = application_server(c, app)
            dedicated = (source.type == "dockerfile" or
                         (source.type == "git" and
                          source.build_pack != "compose"))
            servers = c.request("GET", "/servers") if dedicated else []
            build = any(
                item.get("settings", {}).get("server_role") == "build"
                for item in servers
            )
            if build:
                template = self._image_template(ep)
                if not template or not all(
                    key in template for key in ("{environment_id}", "{app_id}")
                ):
                    raise base.DriverError(
                        "Coolify dedicated builds require "
                        "an environment-specific registry "
                        "image template"
                    )
                try:
                    settings["docker_registry_image_name"] = template.format(
                        environment_id=env.id, app_id=app.id
                    )
                except (KeyError, ValueError):
                    raise base.DriverError(
                        "Coolify registry image template " "is invalid"
                    ) from None
            settings.update(
                {
                    "project_uuid": project,
                    "environment_uuid": environment,
                    "server_uuid": identifier(server),
                    "instant_deploy": False,
                    "autogenerate_domain": False,
                    "use_build_server": build,
                }
            )
            owned = _owned_domain(app) if app.ports else None
            if owned:
                settings["domains"] = "https://" + owned
            kind = ("dockerimage" if source.type == "image" else
                    "dockerfile" if source.type == "dockerfile" else "public")
            if source.type == "git" and source_credentials:
                settings["private_key_uuid"] = self._key(
                    c, app, source_credentials
                )
                kind = "private-deploy-key"
            if kind in ("dockerimage", "dockerfile"):
                # Docker image creation has no git auto-deploy setting.
                settings.pop("is_auto_deploy_enabled")
            row = c.request("POST", "/applications/" + kind, data=settings)
            return base.DriverResult(engine_ref=identifier(row))

    def update_metadata(self, ep, env, app):
        # Templates reject unknown PATCH fields. Applications accept
        # name and description. The name stays the deterministic key.
        if engine_service(app) or not app.engine_ref:
            return None
        data = {"name": name(app), "description": app.description or ""}
        with self._client(ep) as c:
            c.request("PATCH", "/applications/" + ref(app.engine_ref),
                      data=data)

    def get_compose(self, ep, env, app):
        if not engine_service(app):
            raise base.DriverError('Compose application import is unsupported')
        with self._client(ep) as c:
            row = c.request('GET', '/services/' + ref(app.engine_ref))
        content = row.get('docker_compose_raw')
        if not isinstance(content, str) or not content:
            raise base.EngineConflict('Engine compose content is unavailable')
        return content

    def set_compose(self, ep, env, app, content):
        if not engine_service(app):
            raise base.DriverError(
                'Compose application writes are unsupported')
        with self._client(ep) as c:
            c.request('PATCH', '/services/' + ref(app.engine_ref), data={
                'docker_compose_raw': base64.b64encode(
                    content.encode('utf-8')).decode('ascii'),
                'instant_deploy': False})

    SUPPORTS_DEPLOY_KEY_ROTATION = True
    DEPLOY_KEY_IN_PLACE = True

    def prepare_deploy_key(self, ep, env, app, private_key, reference):
        # Coast owns a distinct, deterministically named key for each app.
        # Discover its public UUID without changing the application's binding.
        with self._client(ep) as c:
            key = find(c, '/security/keys', app)
            if key is None:
                raise base.EngineNotFound('Deploy key was not found.')
            key_id = identifier(key)
            return key_id, key_id

    def write_deploy_key(self, ep, env, app, key_ref, private_key):
        with self._client(ep) as c:
            c.request('PATCH', '/security/keys/' + ref(key_ref),
                      data={'private_key': private_key})

    def delete_deploy_key(self, ep, key_ref):
        if key_ref:
            with self._client(ep) as c:
                remove(c, '/security/keys/' + ref(key_ref))

    def set_dockerfile(self, ep, env, app, content):
        with self._client(ep) as c:
            current = c.request('GET', '/applications/' + ref(app.engine_ref))
        if not isinstance(current, dict) or not isinstance(
                current.get('dockerfile'), str):
            raise base.DriverError('App Dockerfile could not be read')
        if current['dockerfile'] != content:
            raise base.DockerfileChangeUnsupported(
                'App Dockerfile replacement is unsupported')

    def update_app(self, ep, env, app, source):
        settings = app_settings(app, source)
        settings.pop("git_repository", None)
        with self._client(ep) as c:
            path = "/applications/" + ref(app.engine_ref)
            settings = relabel_settings(c.request("GET", path), settings)
            c.request("PATCH", path, data=settings)

    def delete_app(self, ep, env, app):
        if engine_resources.resource_type(app) == 'database':
            return self.delete_database(ep, env, app)
        kind = ("/services" if engine_service(app)
                else "/applications")
        with self._client(ep) as c:
            try:
                app_id = resource_ref(c, kind, app)
            except base.EngineNotFound:
                app_id = None
            if app_id:
                remove(
                    c,
                    kind + "/" + app_id,
                    params={"delete_volumes": "false"},
                )
            if kind == "/services":
                return
            for key in c.request('GET', '/security/keys'):
                if key.get('name') == name(app):
                    remove(c, '/security/keys/' + ref(identifier(key)))

    def _set_template_env(self, ep, app, variables):
        # One-click services run images, not builds. Reject build-only intent.
        if any(item.build_time for item in variables.values()):
            raise base.DriverError(
                "Coolify service variables are runtime-only")
        marker = "coast-managed"
        with self._client(ep) as c:
            path = "/services/%s/envs" % ref(app.engine_ref)
            observed = c.request("GET", path)
            # Preserve defaults and generated values; remove only Coast keys.
            if variables:
                c.request("PATCH", path + "/bulk", data={"data": [
                    {"key": key, "value": item.value,
                     "is_literal": True, "is_multiline": "\n" in item.value,
                     "is_shown_once": item.is_secret, "comment": marker}
                    for key, item in variables.items()
                ]})
            for row in observed:
                if (row.get("comment") == marker and
                        (row.get("key") not in variables or
                         row.get("is_preview") is True)):
                    remove(c, path + "/" + ref(identifier(row)))

    def compare_settings(self, ep, stack, service, variables, fqdns):
        with self._client(ep) as c:
            kind = ('services' if engine_service(service)
                    else 'applications')
            path = '/' + kind + '/' + ref(service.engine_ref)
            return settings.compare(c.request('GET', path + '/envs'),
                                    c.request('GET', path), variables, fqdns,
                                    kind == 'services')

    def set_env(self, ep, env, app, variables):
        if engine_service(app):
            return self._set_template_env(ep, app, variables)
        with self._client(ep) as c:
            path = "/applications/%s/envs" % ref(app.engine_ref)
            observed = c.request("GET", path)
            existing = {row["key"] for row in observed
                        if row.get("is_preview") is not True}
            for key, item in variables.items():
                c.request("PATCH" if key in existing else "POST", path, data={
                    "key": key, "value": item.value,
                    "is_literal": True, "is_multiline": "\n" in item.value,
                    "is_shown_once": item.is_secret, "is_preview": False,
                    "is_buildtime": item.build_time, "is_runtime": True,
                    "comment": "coast-managed"})
            # Writes can create preview copies. Read them before cleanup.
            if variables:
                observed = c.request("GET", path)
            for row in observed:
                if (row.get("comment") == "coast-managed" and
                        (row.get("key") not in variables or
                         row.get("is_preview") is True)):
                    remove(c, path + "/" + ref(identifier(row)))

    def set_volume_mounts(self, ep, env, app, mounts):
        apply = self.prepare_volume_mounts(ep, env, app, mounts)
        if apply is not None:
            apply()

    def prepare_volume_mounts(self, ep, env, app, mounts):
        base.validate_mounts(mounts)
        if engine_service(app):
            return self._prepare_template_mounts(ep, env, app, mounts)
        desired = {mount.container_path: mount for mount in mounts}
        deletes, creates = [], []
        with self._client(ep) as c:
            path = "/applications/%s/storages" % ref(app.engine_ref)
            observed = c.request("GET", path)
            retained = set()
            for group in ('persistent_storages', 'file_storages'):
                for row in observed.get(group) or []:
                    fs_path = row.get('fs_path') or ''
                    # Content files belong to set_file_mounts, not storage.
                    if (group == 'file_storages' and
                            not row.get('is_directory')):
                        continue
                    target = row.get('mount_path') or ''
                    container = target.removesuffix(':ro')
                    mount = desired.get(container)
                    stored = row.get('name') or ''
                    same = mount and (target.endswith(':ro') ==
                                      mount.read_only)
                    if same and mount.type == 'bind':
                        same = (group == 'file_storages' and
                                fs_path == mount.host_path and
                                row.get('is_directory') is True)
                    elif same:
                        same = (group == 'persistent_storages' and
                                (stored == mount.name or
                                 stored.endswith('-' + mount.name)))
                    if same:
                        retained.add(container)
                    else:
                        deletes.append(path + '/' + ref(identifier(row)))
            for container, mount in desired.items():
                if container in retained:
                    continue
                # The public API emits mount_path in Compose short syntax.
                target = container + (':ro' if mount.read_only else '')
                data = {'mount_path': target}
                if mount.type == 'bind':
                    data.update(type='file', fs_path=mount.host_path,
                                is_directory=True)
                else:
                    data.update(type='persistent', name=mount.name)
                creates.append(data)

        if not deletes and not creates:
            return None

        def apply():
            with self._client(ep) as c:
                for target in deletes:
                    remove(c, target)
                for data in creates:
                    c.request('POST', path, data=data)
        return apply

    def _prepare_template_mounts(self, ep, env, app, mounts):
        from coast.common import compose
        import yaml
        document = compose.parse(
            self.get_compose(ep, env, app), max_bytes=16 * 1024 * 1024)
        original = yaml.safe_dump(document)
        services = document['services']
        managed = any(isinstance(item, dict) and
                      item.get('x-coast-storage') for service in
                      services.values() for item in service.get('volumes', []))
        if not mounts and not managed:
            return
        if mounts and len(services) != 1:
            raise base.EngineConflict(
                'Storage requires a template with one container')
        for service in services.values():
            if not service.get('volumes'):
                continue
            service['volumes'] = [item for item in service.get('volumes', [])
                                  if not (isinstance(item, dict) and
                                          item.get('x-coast-storage'))]
        service = next(iter(services.values()))
        declared = set(compose.mount_targets(document))
        for mount in mounts:
            if mount.container_path in declared:
                raise base.EngineConflict('Container path is template-defined')
            source = mount.host_path if mount.type == 'bind' else mount.name
            item = {'type': mount.type, 'source': source,
                    'target': mount.container_path,
                    'read_only': mount.read_only, 'x-coast-storage': mount.id}
            if mount.type == 'bind':
                item['bind'] = {'create_host_path': True}
            else:
                document.setdefault('volumes', {}).setdefault(source, {})
            service['volumes'].append(item)
        content = yaml.safe_dump(document)
        if content != original:
            return lambda: self.set_compose(ep, env, app, content)

    def list_volume_mounts(self, ep, env, app):
        if engine_service(app):
            from coast.common import compose
            document = compose.parse(
                self.get_compose(ep, env, app), max_bytes=16 * 1024 * 1024)
            return _compose_mounts(document)
        kind = 'services' if engine_service(app) else 'applications'
        with self._client(ep) as c:
            observed = c.request(
                'GET', '/%s/%s/storages' % (kind, ref(app.engine_ref)))
        result = []
        for group in ('persistent_storages', 'file_storages'):
            for row in observed.get(group) or []:
                source = row.get('host_path') or row.get('fs_path')
                mount_type = 'bind' if source else 'volume'
                if group == 'file_storages' and not source:
                    continue
                if group == 'file_storages' and _managed_file(row):
                    continue
                if not source:
                    source = row.get('name')
                target = row.get('mount_path') or ''
                path = target.removesuffix(':ro').removesuffix(':rw')
                if not source or not path.startswith('/'):
                    continue
                result.append({'name': source, 'source': source,
                               'type': mount_type, 'container_path': path,
                               'read_only': (target.endswith(':ro') or
                                             row.get('read_only') is True)})
        return result

    def set_file_mounts(self, ep, env, app, mounts):
        from coast.common import file_mounts
        paths = set()
        for mount in mounts:
            try:
                file_mounts.validate({'container_path': mount.container_path,
                                      'content': mount.content})
                ref(mount.id)
                if mount.container_path in paths:
                    raise ValueError()
                paths.add(mount.container_path)
            except Exception:
                raise base.DriverError(
                    'Invalid container file mount') from None
        template = bool(engine_service(app))
        kind = 'services' if template else 'applications'
        path = '/%s/%s/storages' % (kind, ref(app.engine_ref))
        with self._client(ep) as c:
            resources = []
            if template and mounts:
                service = c.request('GET', '/services/' + ref(app.engine_ref))
                resources = ((service.get('applications') or []) +
                             (service.get('databases') or []))
            # Resolve all targets before changing engine state.
            desired = []
            for mount in mounts:
                data = {'type': 'file', 'mount_path': mount.container_path,
                        'content': mount.content, 'is_directory': False,
                        'fs_path': 'coast-files/' + ref(mount.id)}
                if template:
                    candidates = [row for row in resources if
                                  not mount.resource_name or
                                  row.get('name') == mount.resource_name]
                    if len(candidates) != 1:
                        raise base.DriverError(
                            'Select one template container with resource_name')
                    data['resource_uuid'] = ref(candidates[0].get('uuid'))
                elif mount.resource_name:
                    raise base.DriverError('resource_name requires a template')
                desired.append(data)
            observed = c.request('GET', path)
            for row in observed.get('file_storages') or []:
                if _managed_file(row):
                    remove(c, path + '/' + ref(identifier(row)))
            for data in desired:
                c.request('POST', path, data=data)

    def _action(self, ep, app, action):
        kind = {'database': 'databases', 'service': 'services',
                'app': 'applications'}[engine_resources.resource_type(app)]
        with self._client(ep) as c:
            c.request(
                "POST", "/%s/%s/%s" % (kind, ref(app.engine_ref), action)
            )

    def start_app(self, ep, env, app):
        self._action(ep, app, "start")

    def stop_app(self, ep, env, app):
        self._action(ep, app, "stop")

    def restart_app(self, ep, env, app):
        self._action(ep, app, "restart")

    def get_app_status(self, ep, env, app):
        if engine_resources.resource_type(app) == 'database':
            return self.get_database_status(ep, env, app)
        with self._client(ep) as c:
            kind = ("services" if engine_service(app)
                    else "applications")
            row = c.request("GET", "/%s/%s" % (kind, ref(app.engine_ref)))
        status, _, health = row.get("status", "").partition(":")
        mapped = {
            "running": states.ACTIVE,
            "exited": states.STOPPED,
            "stopped": states.STOPPED,
            "starting": states.DEPLOYING,
            "restarting": states.DEPLOYING,
        }.get(status, states.ERROR)
        if (status == "running" and health == "unhealthy" and
                getattr(app, "health_check", "http") != "none"):
            mapped = states.ERROR
        return base.ObservedStatus(status=mapped)

    def _deploy_template(self, ep, app, deployment):
        # One-click services have no Coolify deployment row: the service
        # uuid is the reference and its container status is the outcome.
        with self._client(ep) as c:
            uuid = ref(app.engine_ref)
            if not deployment.engine_ref:
                row = c.request("GET", "/services/" + uuid)
                running = row.get("status", "").startswith("running")
                c.request("POST", "/services/%s/%s" % (
                    uuid, "restart" if running else "start"))
        return base.DriverResult(engine_ref=uuid, extra={})

    def deploy_app(self, ep, env, app, deployment):
        if engine_service(app):
            return self._deploy_template(ep, app, deployment)
        with self._client(ep) as c:
            if deployment.engine_ref:
                row = c.request(
                    "GET", "/deployments/" + ref(deployment.engine_ref)
                )
                return self._deployment_result(row)
            settings = frozen_settings(app, deployment)
            path = "/applications/" + ref(app.engine_ref)
            settings = relabel_settings(c.request("GET", path), settings)
            # No API idempotency key. Never retry the enqueue POST. The real
            # conductor must persist the returned reference before polling.
            c.request("PATCH", path, data=settings)
            rows = c.request(
                "POST", "/deploy", params={"uuid": app.engine_ref}
            )["deployments"]
            matching = [
                row
                for row in rows
                if row.get("resource_uuid") == app.engine_ref
            ]
            if len(matching) != 1:
                raise base.DriverError("Coolify deploy response is ambiguous")
            return self._deployment_result(matching[0])

    @staticmethod
    def _deployment_result(row):
        extra = {}
        commit = row.get("commit")
        if isinstance(commit, str) and re.fullmatch(
            r"[0-9a-fA-F]{40}", commit
        ):
            extra["resolved_ref"] = commit
        return base.DriverResult(
            engine_ref=identifier(row, "deployment_uuid"), extra=extra
        )

    @staticmethod
    def _running(row):
        # Never report ACTIVE before the containers are up. Coolify reports
        # "running:healthy" or, without a healthcheck, "running:unknown".
        status, _, health = row.get("status", "").partition(":")
        return status == "running" and health != "unhealthy"

    def get_deployment_status(self, ep, env, app, deployment):
        template = engine_service(app)
        with self._client(ep) as c:
            if template:
                row = c.request("GET", "/services/" + ref(app.engine_ref))
                # Starting, exited or unhealthy: keep waiting. The conductor
                # deploy_timeout turns a service that never runs into FAILED.
                return base.ObservedStatus(
                    status=(states.ACTIVE if self._running(row)
                            else states.DEPLOYING))
            container = None
            row = c.request(
                "GET", "/deployments/" + ref(deployment.engine_ref)
            )
            if row.get("status") == "finished" and app.engine_ref:
                live = c.request(
                    "GET", "/applications/" + ref(app.engine_ref))
                container = live.get("status", "").partition(":")[0]
                if not self._running(live):
                    row = dict(row, status="in_progress")
        mapped = {
            "queued": states.PENDING,
            "in_progress": states.DEPLOYING,
            "finished": states.ACTIVE,
            "failed": states.FAILED,
            "cancelled": states.CANCELLED,
            "cancelled-by-user": states.CANCELLED,
        }.get(row.get("status"), states.FAILED)
        result = self._deployment_result(row)
        return base.ObservedStatus(
            status=mapped, detail=result.extra.get("resolved_ref"),
            resolved_ref=result.extra.get("resolved_ref"),
            container=container or None,
        )

    def cancel_deployment(self, ep, env, app, deployment):
        if engine_service(app):
            with self._client(ep) as c:
                c.request("POST", "/services/%s/stop" % ref(app.engine_ref))
            return
        with self._client(ep) as c:
            path = "/deployments/" + ref(deployment.engine_ref)
            row = c.request("GET", path)
            if row.get("status") in ("queued", "in_progress"):
                c.request("POST", path + "/cancel")

    def rollback(self, ep, env, app, target_deployment, new_deployment):
        source = (getattr(target_deployment, "spec", None) or {}).get(
            "source", {})
        if source.get("type") in ("dockerfile", "compose"):
            return self.deploy_app(ep, env, app, new_deployment)
        if engine_service(app):
            raise base.DriverError(
                "Coolify one-click services have no deployment history")
        if new_deployment.engine_ref:
            with self._client(ep) as c:
                return self._deployment_result(c.request(
                    "GET", "/deployments/" + ref(new_deployment.engine_ref)))
        settings = frozen_settings(app, target_deployment)
        with self._client(ep) as c:
            row = c.request(
                "GET", "/deployments/" + ref(target_deployment.engine_ref)
            )
            if row.get("status") != "finished":
                raise base.DriverError(
                    "Coolify rollback target is not successful"
                )
            if target_deployment.spec["source"]["type"] == "image":
                # Stock releases record no image tag on image deployments.
                # (live DevStack m2svc) and its rollback endpoint is commit
                # based. Re-apply the target's frozen image and tag, then
                # enqueue one ordinary deploy. Never retry the POST.
                if not settings.get("docker_registry_image_tag"):
                    raise base.DriverError(
                        "Coolify rollback target has no image tag")
                c.request(
                    "PATCH", "/applications/" + ref(app.engine_ref),
                    data=settings
                )
                rows = c.request(
                    "POST", "/deploy", params={"uuid": app.engine_ref}
                )["deployments"]
                matching = [
                    item for item in rows
                    if item.get("resource_uuid") == app.engine_ref
                ]
                if len(matching) != 1:
                    raise base.DriverError(
                        "Coolify deploy response is ambiguous")
                return self._deployment_result(matching[0])
            commit = self._deployment_result(row).extra.get("resolved_ref")
            if not commit:
                raise base.DriverError(
                    "Coolify rollback target has no resolved "
                    "commit or image tag"
                )
            if target_deployment.spec["source"]["type"] == "git":
                settings["git_commit_sha"] = commit
            c.request(
                "PATCH", "/applications/" + ref(app.engine_ref), data=settings
            )
            row = c.request(
                "POST",
                "/applications/%s/rollback" % ref(app.engine_ref),
                data={"commit": commit},
            )
            return self._deployment_result(row)

    def _logs(self, c, app, payload, stream, since=None, masks=None):
        prefix = "/services/" if engine_service(app) else (
            "/applications/")
        database = engine_resources.resource_type(app) == 'database'
        variables = ([] if database else
                     c.request("GET", prefix + ref(app.engine_ref) + "/envs"))
        masks = masks or {}
        # Only Coast-known keys have values available despite shown-once.
        suppress = any(item.get("is_shown_once") and
                       item.get("key") not in masks for item in variables)
        secrets = [c.ep.token] + [
            variable.value for variable in masks.values() if variable.is_secret
        ] + [
            item.get(key)
            for item in variables if item.get("key") not in masks
            for key in ("value", "real_value")
        ]
        # Replace longer values first so overlapping secrets cannot leak.
        secrets = sorted({value for value in secrets if value},
                         key=len, reverse=True)
        try:
            rows = json.loads(payload) if stream == "build" else None
            if rows is not None and not isinstance(rows, list):
                raise ValueError()
        except (TypeError, ValueError):
            raise base.DriverError(
                "Coolify deployment log format is invalid"
            ) from None
        if rows is None:
            rows = []
            for line in payload.splitlines():
                ts, _, content = line.partition(" ")
                rows.append({"timestamp": ts, "output": content})
        result = []
        # Hidden build batches that already have output, and placeholder
        # indexes for batches that have only blank output.
        produced = set()
        placeholders = {}
        for row in rows:
            timestamp = row.get("timestamp", "")
            try:
                dt = datetime.datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )
                if since and dt <= datetime.datetime.fromisoformat(
                    since.replace("Z", "+00:00")
                ):
                    continue
            except (ValueError, TypeError):
                raise base.DriverError(
                    "Coolify log timestamp is invalid"
                ) from None
            output = self._mask_log_text(
                row.get("output", ""), suppress, secrets
            )
            self._append_log_line(
                result, produced, placeholders, row, stream, timestamp,
                output)
        return iter(line for line in result if line is not None)

    def _append_log_line(self, result, produced, placeholders, row,
                         stream, timestamp, output):
        # Stock releases mark Docker build output hidden. Include that
        # output so a running build is not an empty log. Do not copy
        # command: it can contain credentials.
        if stream == "build" and row.get("hidden") is True:
            batch = self._log_batch(row)
            if not output.strip():
                if batch in produced or batch in placeholders:
                    return
                result.append(base.LogLine(
                    ts=timestamp, stream=stream,
                    line="Build step in progress."))
                placeholders[batch] = len(result) - 1
                return
            produced.add(batch)
            index = placeholders.pop(batch, None)
            if index is not None:
                result[index] = None
        result.append(
            base.LogLine(ts=timestamp, stream=stream, line=output)
        )

    @staticmethod
    def _mask_log_text(text, suppress, secrets):
        if text is None:
            text = ""
        text = str(text)
        if suppress:
            return "***"
        text = strutils.mask_password(text)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    @staticmethod
    def _log_batch(row):
        batch = row.get("batch")
        if isinstance(batch, bool) or not isinstance(batch, (int, str)):
            return None
        return batch

    def get_deployment_log(self, ep, env, app, deployment, since=None,
                           masks=None):
        if engine_service(app):
            try:
                return self._template_logs(ep, app, since=since, masks=masks)
            except base.EngineNotFound:
                return iter(())
        with self._client(ep) as c:
            row = c.request(
                "GET", "/deployments/" + ref(deployment.engine_ref)
            )
            return self._logs(c, app, row.get("logs") or "[]", "build",
                              since, masks=masks)

    def get_app_logs(self, ep, env, app, tail=200, masks=None):
        if not isinstance(tail, int) or not 1 <= tail <= 10000:
            raise base.DriverError("Coolify log tail must be from 1 to 10000")
        if engine_service(app):
            return self._template_logs(ep, app, tail=tail, masks=masks)
        with self._client(ep) as c:
            row = c.request(
                "GET",
                "/%s/%s/logs" % (
                    {'database': 'databases', 'app': 'applications'}[
                        engine_resources.resource_type(app)],
                    ref(app.engine_ref)),
                params={"lines": tail, "show_timestamps": "true"},
            )
            return self._logs(c, app, row.get("logs", ""), "stdout",
                              masks=masks)

    def _template_logs(self, ep, app, tail=200, since=None, masks=None):
        with self._client(ep) as c:
            path = "/services/" + ref(app.engine_ref)
            row = c.request("GET", path)
            applications = row.get("applications") or []
            exposed = [item for item in applications if item.get("fqdn")]
            targets = exposed or applications + (row.get("databases") or [])
            if len(targets) != 1 or not targets[0].get("name"):
                raise base.EngineNotFound("Template log target is unavailable")
            try:
                logs = c.request("GET", path + "/logs", params={
                    "sub_service_name": targets[0]["name"], "lines": tail,
                    "show_timestamps": "true"})
            except base.EngineAuthError as error:
                if getattr(error, "status_code", None) != 403:
                    raise
                # Older controller agents deny this read-only route.
                raise base.EngineNotFound(
                    "Template logs are unavailable") from None
            return self._logs(c, app, logs.get("logs") or "", "stdout",
                              since, masks=masks)

    def _set_template_domains(self, ep, app, fqdns):
        with self._client(ep) as c:
            path = "/services/" + ref(app.engine_ref)
            row = c.request("GET", path)
            applications = row.get("applications", [])
            # Coast has no sub-application selector. Never choose arbitrarily.
            exposed = [item for item in applications if item.get("fqdn")]
            targets = exposed or applications
            if fqdns and len(targets) != 1:
                raise base.DriverError(
                    "Coolify service domain target is ambiguous")
            if not targets:
                return
            if settings.domain_drift(row, fqdns, True) is False:
                return
            ports = getattr(app, "ports", None)
            port = ports.split(",")[0].strip() if ports else None
            joined = ",".join(_domain_url(fqdn, port) for fqdn in fqdns)
            data = {"urls": [{"name": item["name"], "url": joined}
                             for item in targets], "instant_deploy": False}
            previous = {"urls": [{"name": item["name"],
                                  "url": item.get("fqdn") or ""}
                                 for item in targets], "instant_deploy": False}
            self._apply_domains(c, path, row, data, previous)

    def list_domains(self, ep, env, app):
        if not getattr(app, "engine_ref", None):
            return None
        try:
            with self._client(ep) as c:
                if engine_service(app):
                    row = c.request("GET", "/services/" + ref(app.engine_ref))
                    return _template_domain_list(row)
                row = c.request("GET", "/applications/" + ref(app.engine_ref))
                return _application_domain_list(row)
        except base.EngineNotFound:
            return None

    def set_domains(self, ep, env, app, fqdns):
        joined = ",".join(_domain_url(fqdn) for fqdn in fqdns)
        if engine_service(app):
            return self._set_template_domains(ep, app, fqdns)
        with self._client(ep) as c:
            path = "/applications/" + ref(app.engine_ref)
            row = c.request("GET", path)
            domains = joined
            if row.get("build_pack") == "dockercompose":
                try:
                    targets = settings.compose_targets(row, fqdns)
                except ValueError:
                    raise base.DriverError(
                        "Coolify compose domain target is unknown or "
                        "ambiguous") from None
                if not targets:
                    return
                data = {"docker_compose_domains": [
                    {"name": item["name"], "domain": domains}
                    for item in targets]}
                previous = {"docker_compose_domains": [
                    {"name": item["name"], "domain": item["fqdn"] or ""}
                    for item in targets]}
            else:
                data = {"domains": domains}
                previous = {"domains": row.get("fqdn") or ""}
            if settings.domain_drift(row, fqdns, False) is False:
                return
            self._apply_domains(c, path, row, data, previous)

    @staticmethod
    def _apply_domains(c, path, row, data, previous):
        c.request("PATCH", path, data=data)
        if (row.get("status") or "").partition(":")[0] == "running":
            try:
                # The public restart action recreates routing labels using
                # the existing image; it must not enqueue a build.
                c.request("POST", path + "/restart")
            except base.DriverError:
                # Keep drift visible so a retry does not mistake the saved
                # configuration for routing that was successfully applied.
                c.request("PATCH", path, data=previous)
                raise


def _compose_mounts(document):
    """Describe every Compose mount without exposing file contents."""
    result = []
    for service in document['services'].values():
        for item in service.get('volumes', []):
            if isinstance(item, str):
                parts = item.split(':')
                source = parts[0] if len(parts) > 1 else None
                path = parts[1] if len(parts) > 1 else parts[0]
                kind = ('bind' if source and source.startswith(('/', '.', '~'))
                        else 'volume')
                read_only = len(parts) > 2 and 'ro' in parts[2].split(',')
            else:
                source, path = item.get('source'), item['target']
                kind = item.get('type', 'volume')
                read_only = item.get('read_only', False)
                if item.get('x-coast-storage'):
                    continue
            if kind not in ('bind', 'volume'):
                continue
            result.append({'name': source, 'source': source, 'type': kind,
                           'container_path': path, 'read_only': read_only})
    return result


def _managed_file(row):
    source = row.get('fs_path') or ''
    return (not row.get('is_directory') and
            (source.startswith('coast-files/') or '/coast-files/' in source))
