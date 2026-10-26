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

"""Live public-API contracts; never run against a tenant controller."""

import dataclasses
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from urllib import parse
import uuid

import requests
from testtools import content

from coast.common import states
from coast.drivers import base as engine
from coast.drivers.coolify import client
from coast.drivers.coolify import v4
from coast.tests import base
from coast.tests.contract.coolify import recording
from coast.tests.contract.coolify.version import EXPECTED_VERSION


class ContractDriver(v4.CoolifyV4Driver):
    """Test-only HTTP transport; production still requires verified HTTPS."""

    def _client(self, ep):
        url = parse.urlsplit(ep.base_url)
        if (url.scheme != "http" or
                not self.conf.coolify.insecure_direct_url):
            transport = super()._client(ep)
            if getattr(self, "recorder", None):
                transport.session.hooks["response"].append(self.recorder.hook)
            return transport
        secure = dataclasses.replace(
            ep, base_url=parse.urlunsplit(url._replace(scheme="https")))
        transport = client.Client(secure, self.conf)
        transport.url = parse.urlunsplit(
            parse.urlsplit(transport.url)._replace(scheme="http"))
        if getattr(self, "recorder", None):
            transport.session.hooks["response"].append(self.recorder.hook)
        return transport


def endpoint(conf, values):
    """Configure the selected live transport without replacing driver code."""
    prefix = "COAST_CONTRACT_COOLIFY_"
    mode = os.environ.get(prefix + "TRANSPORT", "agent")
    if mode not in ("agent", "direct"):
        raise ValueError("Contract transport must be agent or direct")
    credentials = [os.environ.get(prefix + key, "") for key in
                   ("CLIENT_CERT", "CLIENT_KEY", "CA_CERT")]
    if mode == "agent" and not all(credentials):
        raise ValueError("Agent transport requires CLIENT_CERT, CLIENT_KEY "
                         "and CA_CERT")
    conf.set_override("insecure_direct_url", mode == "direct", "coolify")
    conf.set_override("mgmt_netns", os.environ.get(prefix + "MGMT_NETNS", ""),
                      "controller_worker")
    return engine.EngineEndpoint(
        values["URL"], *credentials,
        Path(values["TOKEN_FILE"]).read_text().strip())


def engine_app(**values):
    """An engine app shaped like the production Service object.

    The driver reads production fields such as template_id; a fixture that
    omits one fails with AttributeError instead of testing the engine.
    """
    values.setdefault("template_id", None)
    return SimpleNamespace(**values)


def original_fallback():
    value = os.environ.get("COAST_CONTRACT_COOLIFY_FALLBACK_ENABLED")
    if value not in ("true", "false"):
        raise ValueError("Set FALLBACK_ENABLED to the original team flag "
                         "(true or false); Coast controllers use false")
    return value == "true"


class LiveCoolifyBase(base.TestCase):
    def setUp(self):
        super().setUp()
        required = ("URL", "TOKEN_FILE", "WORKER_UUIDS", "PRIVATE_KEY_UUID")
        values = {
            key: os.environ.get("COAST_CONTRACT_COOLIFY_" + key)
            for key in required
        }
        if not all(values.values()):
            self.skipTest("Live Coolify contract settings are not supplied")
        fallback = original_fallback()
        self.ep = endpoint(self.conf, values)
        self.driver = ContractDriver(self.conf)
        self.recorder = (recording.Recorder() if os.environ.get(
            "COAST_CONTRACT_COOLIFY_RECORD") == "1" else None)
        self.driver.recorder = self.recorder
        self.timeout = int(os.environ.get(
            "COAST_CONTRACT_COOLIFY_TIMEOUT", "900"))
        self.env = SimpleNamespace(
            id="contract-" + uuid.uuid4().hex, engine_ref=None,
            engine_version=EXPECTED_VERSION)
        if self.recorder:
            self.recorder.bind(self.ep.token, "[redacted]")
            self.recorder.bind(v4.name(self.env), "coast-env")
        # Register cleanup before mutating calls, including uncertain creates.
        self.addCleanup(self.driver.delete_stack_project, self.ep, self.env)
        self.addCleanup(
            self.api, "PATCH", "/team",
            data={"is_build_server_fallback_enabled":
                  fallback})
        self.assertEqual(
            EXPECTED_VERSION, self.driver.get_engine_version(self.ep))
        self.env.engine_ref = self.driver.bootstrap_environment(
            self.ep, self.env).engine_ref
        self.assertEqual(self.env.engine_ref,
                         self.driver.bootstrap_environment(
                             self.ep, self.env).engine_ref)
        if self.recorder:
            self.recorder.bind(self.env.engine_ref, "project-uuid")
        self.private_key_uuid = values["PRIVATE_KEY_UUID"]
        self.servers = []
        self.environments = []
        for index, worker in enumerate(values["WORKER_UUIDS"].split(",")):
            if self.recorder:
                self.recorder.bind(worker.strip(),
                                   "server-uuid" if index == 0 else
                                   "second-server-uuid")
            row = self.api("GET", "/servers/" + v4.ref(worker.strip()))
            server = SimpleNamespace(
                id=uuid.uuid4().hex, engine_ref=row["uuid"],
                hostname="coast-contract-worker-" + str(index),
                private_ip=row["ip"], role="worker")
            if self.recorder:
                self.recorder.bind(v4.name(server),
                                   ("coast-shop-5bad06-worker-1"
                                    if index == 0 else "coast-second-server"))
                self.recorder.bind(row["name"], "original-worker-%d" % index)
            server_path = "/servers/" + v4.ref(server.engine_ref)
            self.addCleanup(self.api, "PATCH", server_path,
                            data={"name": row["name"]})
            self.api("PATCH", server_path, data={"name": v4.name(server)})
            server.engine_ref = self.driver.register_server(
                self.ep, self.env, server, "").engine_ref
            self.wait(lambda: self.driver.get_server_status(
                self.ep, self.env, server), states.ACTIVE)
            self.servers.append(server)

    def api(self, method, path, **kwargs):
        with self.driver._client(self.ep) as transport:
            return transport.request(method, path, **kwargs)

    def wait(self, observe, expected, terminal=()):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            result = observe()
            if result.status == expected:
                return result
            if result.status in terminal:
                self.fail("Live operation ended in " + result.status)
            time.sleep(3)
        self.fail("Live operation timed out waiting for " + expected)

    def prepare_source(self, source):
        """Supply build prerequisites or explain the missing input."""
        if source.type != "git":
            return
        template = os.environ.get(
            "COAST_CONTRACT_COOLIFY_BUILD_REGISTRY_TEMPLATE")
        if template:
            self.config.config(build_registry_image_template=template,
                               group="coolify")
            return
        if any(row.get("settings", {}).get("server_role") == "build"
               for row in self.api("GET", "/servers")):
            self.skipTest(
                "Dedicated build server present; set "
                "COAST_CONTRACT_COOLIFY_BUILD_REGISTRY_TEMPLATE to an "
                "environment-specific registry image template")

    def bind_parent_refs(self, app):
        """Use the application project and one Coolify environment uuid."""
        application = SimpleNamespace(engine_ref=self.env.engine_ref)
        environment = SimpleNamespace(id=uuid.uuid4().hex, engine_ref=None)
        self.addCleanup(
            self.driver.delete_environment, self.ep, self.env, application,
            environment)
        environment.engine_ref = self.driver.ensure_environment(
            self.ep, self.env, application, environment).engine_ref
        self.environments.append((application, environment))
        app.project_engine_ref = application.engine_ref
        app.environment_engine_ref = environment.engine_ref

    def deployment(self, source):
        return SimpleNamespace(
            id=uuid.uuid4().hex, engine_ref=None, source_ref=None,
            image_digest=None, spec={"source": vars(source).copy()})

    def deploy(self, app, source, source_ref=None):
        deployment = self.deployment(source)
        deployment.source_ref = source_ref
        deployment.engine_ref = self.driver.deploy_app(
            self.ep, self.env, app, deployment).engine_ref
        self.assertEqual(deployment.engine_ref, self.driver.deploy_app(
            self.ep, self.env, app, deployment).engine_ref)
        try:
            self.wait(lambda: self.driver.get_deployment_status(
                self.ep, self.env, app, deployment), states.ACTIVE,
                (states.FAILED, states.CANCELLED))
        except AssertionError:
            logs = self.driver.get_deployment_log(
                self.ep, self.env, app, deployment)
            self.addDetail("sanitized-build-log", content.text_content(
                "\n".join(line.line for line in logs)))
            raise
        self.wait(lambda: self.driver.get_app_status(
            self.ep, self.env, app), states.ACTIVE)
        return deployment


class LiveCoolifyTest(LiveCoolifyBase):
    def test_default_environment_project_cleanup(self):
        application = SimpleNamespace(id=uuid.uuid4().hex, engine_ref=None)
        self.addCleanup(self.driver.delete_project, self.ep, self.env,
                        application)
        application.engine_ref = self.driver.ensure_project(
            self.ep, self.env, application).engine_ref
        path = "/projects/" + v4.ref(application.engine_ref)
        rows = self.api("GET", path + "/environments")
        self.assertEqual(["production"], [row["name"] for row in rows])
        environment = SimpleNamespace(id=uuid.uuid4().hex, engine_ref=None)
        self.addCleanup(self.driver.delete_environment, self.ep, self.env,
                        application, environment)
        environment.engine_ref = self.driver.ensure_environment(
            self.ep, self.env, application, environment).engine_ref
        self.assertRaises(engine.DriverError, self.driver.delete_project,
                          self.ep, self.env, application)
        self.driver.delete_environment(
            self.ep, self.env, application, environment)
        self.driver.delete_project(self.ep, self.env, application)
        self.assertRaises(engine.EngineNotFound, self.api, "GET", path)
        self.driver.delete_stack_project(self.ep, self.env)
        self.assertRaises(engine.EngineNotFound, self.api, "GET",
                          "/projects/" + v4.ref(self.env.engine_ref))

    def test_full_lifecycle(self):
        sources = [SimpleNamespace(
            type="git", repo_url=os.environ.get(
                "COAST_CONTRACT_COOLIFY_GIT_URL",
                "https://github.com/coollabsio/coolify-examples"),
            branch="main", build_pack="nixpacks",
            base_directory="/javascript/runtimes/nodejs", dockerfile_path=None,
            compose_path=None, credentials_ref=None), SimpleNamespace(
                type="image", image="nginx:alpine", credentials_ref=None)]
        for source in sources:
            self.prepare_source(source)
        disposable = SimpleNamespace(id=uuid.uuid4().hex, engine_ref=None)
        self.addCleanup(self.driver.unregister_server,
                        self.ep, self.env, disposable)
        disposable.engine_ref = self.api("POST", "/servers", data={
            "name": v4.name(disposable), "ip": "192.0.2.254",
            "port": 22, "user": "root", "instant_validate": False,
            "private_key_uuid": self.private_key_uuid,
            "server_role": "both", "proxy_type": "none"})["uuid"]
        self.driver.unregister_server(self.ep, self.env, disposable)
        self.assertRaises(engine.EngineNotFound, self.api, "GET",
                          "/servers/" + v4.ref(disposable.engine_ref))
        for index, source in enumerate(sources):
            worker = self.servers[index % len(self.servers)]
            app = engine_app(
                id=uuid.uuid4().hex, engine_ref=None,
                server_id=worker.id, server_engine_ref=worker.engine_ref,
                description="Live contract app", ports=(
                    "3000" if source.type == "git" else "80"),
                replicas=1, auto_deploy=False, cpu_limit=1,
                memory_limit_mb=512, healthcheck_path=None)
            if self.recorder:
                self.recorder.bind(v4.name(app),
                                   "coast-app" if index == 0 else
                                   "coast-image-app")
            self.bind_parent_refs(app)
            self.addCleanup(self.driver.delete_app, self.ep, self.env, app)
            app.engine_ref = self.driver.create_app(
                self.ep, self.env, app, source, None).engine_ref
            if self.recorder:
                self.recorder.bind(app.engine_ref,
                                   "app-uuid" if index == 0 else
                                   "image-app-uuid")
            self.assertEqual(app.engine_ref, self.driver.create_app(
                self.ep, self.env, app, source, None).engine_ref)
            app.description = "Updated live contract app"
            self.driver.update_app(self.ep, self.env, app, source)
            path = "/applications/" + v4.ref(app.engine_ref)
            self.assertEqual(app.description,
                             self.api("GET", path)["description"])
            for build_time in (True, False):
                self.driver.set_env(self.ep, self.env, app, {
                    "TEST": engine.EnvVar("contract", build_time=build_time)})
                rows = self.api("GET", path + "/envs")
                managed = [row for row in rows if row['key'] == 'TEST']
                self.assertEqual(1, len(managed))
                self.assertIs(build_time, managed[0]['is_buildtime'])
                self.assertIs(True, managed[0]['is_runtime'])
                self.assertIs(False, managed[0]['is_preview'])
            self.api("POST", path + "/envs", data={
                "key": "COAST_CONTRACT", "value": "nonsecret",
                "comment": "coast-managed"})
            self.driver.set_env(self.ep, self.env, app, {})
            # The raw engine list includes generated buildpack controls.
            # Customer env is Coast-owned; defaults must survive clearing it.
            self.assertEqual([], [row for row in self.api(
                "GET", path + "/envs")
                if row.get("comment") == "coast-managed"])
            self.assertIs(False, self.driver.compare_settings(
                self.ep, self.env, app, {}, [])['env'])
            mount = engine.Mount("contract-data", "/contract-data")
            self.driver.set_volume_mounts(self.ep, self.env, app, [mount])
            self.driver.set_volume_mounts(self.ep, self.env, app, [mount])
            storage = self.api("GET", path + "/storages")
            self.assertEqual(1, len(storage["persistent_storages"]))
            self.assertEqual([], storage["file_storages"])
            for read_only in (False, True):
                bind = engine.Mount(
                    None, '/contract-host', read_only, type='bind',
                    host_path='/tmp/coast-contract-' + uuid.uuid4().hex)
                volume = engine.Mount('contract-data', '/contract-data',
                                      read_only)
                for desired in ([bind], [volume]):
                    self.driver.set_volume_mounts(
                        self.ep, self.env, app, desired)
                    self.driver.set_volume_mounts(
                        self.ep, self.env, app, desired)
                    observed = self.driver.list_volume_mounts(
                        self.ep, self.env, app)
                    self.assertEqual(1, len(observed))
                    self.assertEqual(read_only, observed[0]['read_only'])
                    self.assertEqual(desired[0].type, observed[0]['type'])
            self.driver.set_volume_mounts(self.ep, self.env, app, [])
            cleared = self.api("GET", path + "/storages")
            self.assertEqual([], cleared["persistent_storages"])
            self.assertEqual([], cleared["file_storages"])
            file_mount = engine.FileMount(
                uuid.uuid4().hex, '/etc/coast-contract.conf',
                'contract content')
            for text in ('contract content', 'updated content'):
                self.driver.set_file_mounts(self.ep, self.env, app, [
                    dataclasses.replace(file_mount, content=text)])
                files = self.api('GET', path + '/storages')['file_storages']
                self.assertEqual(1, len(files))
                self.assertEqual(text, files[0]['content'])
                self.assertFalse(files[0]['is_directory'])
                self.assertFalse(files[0]['is_host_file'])
            self.driver.set_file_mounts(self.ep, self.env, app, [])
            self.assertEqual([], self.api(
                'GET', path + '/storages')['file_storages'])
            domain = app.id + ".example.com"
            self.driver.set_domains(self.ep, self.env, app, [domain])
            self.assertEqual("https://" + domain,
                             self.api("GET", path)["fqdn"])
            self.driver.set_domains(self.ep, self.env, app, [])
            self.assertEqual({'env': False, 'domains': False},
                             self.driver.compare_settings(
                                 self.ep, self.env, app, {}, []))
            self.assertFalse(self.api("GET", path)["fqdn"])
            target = self.deploy(app, source)
            if self.recorder and index == 0:
                self.recorder.bind(target.engine_ref, "deployment-uuid")
            logs = list(self.driver.get_deployment_log(
                self.ep, self.env, app, target))
            self.assertTrue(logs)
            self.assertTrue(all(isinstance(line, engine.LogLine)
                                for line in logs))
            self.assertEqual([], list(self.driver.get_deployment_log(
                self.ep, self.env, app, target, since=logs[-1].ts)))
            runtime_logs = list(self.driver.get_app_logs(
                self.ep, self.env, app))
            self.assertTrue(runtime_logs)
            self.assertTrue(all(isinstance(line, engine.LogLine)
                                for line in runtime_logs))
            if source.type == "git":
                observed = self.driver.get_deployment_status(
                    self.ep, self.env, app, target)
                self.assertRegex(observed.resolved_ref, r"^[0-9a-f]{40}$")
                second = self.deploy(app, source, os.environ.get(
                    "COAST_CONTRACT_COOLIFY_SECOND_GIT_REF",
                    "ff425d7999a5bb92bc0e977e89567d68c2a6b45b"))
                self.assertNotEqual(observed.resolved_ref,
                                    self.driver.get_deployment_status(
                                        self.ep, self.env, app,
                                        second).resolved_ref)
                rollback = self.deployment(source)
                rollback.engine_ref = self.driver.rollback(
                    self.ep, self.env, app, target, rollback).engine_ref
                if self.recorder:
                    self.recorder.bind(rollback.engine_ref, "rollback-uuid")
                self.wait(lambda: self.driver.get_deployment_status(
                    self.ep, self.env, app, rollback), states.ACTIVE,
                    (states.FAILED, states.CANCELLED))
                self.assertEqual(observed.resolved_ref,
                                 self.driver.get_deployment_status(
                                     self.ep, self.env, app,
                                     rollback).resolved_ref)
            cancelled = self.deployment(source)
            cancelled.engine_ref = self.driver.deploy_app(
                self.ep, self.env, app, cancelled).engine_ref
            self.driver.cancel_deployment(
                self.ep, self.env, app, cancelled)
            try:
                self.wait(lambda: self.driver.get_deployment_status(
                    self.ep, self.env, app, cancelled), states.CANCELLED,
                    (states.ACTIVE, states.FAILED))
            except AssertionError:
                row = self.api("GET", "/deployments/" +
                               v4.ref(cancelled.engine_ref))
                self.addDetail("cancel-status", content.text_content(
                    row["status"]))
                raise
            self.driver.stop_app(self.ep, self.env, app)
            self.wait(lambda: self.driver.get_app_status(
                self.ep, self.env, app), states.STOPPED)
            self.driver.start_app(self.ep, self.env, app)
            self.wait(lambda: self.driver.get_app_status(
                self.ep, self.env, app), states.ACTIVE)
            self.driver.restart_app(self.ep, self.env, app)
            self.wait(lambda: self.driver.get_app_status(
                self.ep, self.env, app), states.ACTIVE)
            self.driver.delete_app(self.ep, self.env, app)
            self.assertRaises(engine.EngineNotFound, self.api, "GET", path)
            self.driver.delete_app(self.ep, self.env, app)
        for application, environment in self.environments:
            self.driver.delete_environment(
                self.ep, self.env, application, environment)
        self.driver.delete_stack_project(self.ep, self.env)
        self.assertRaises(engine.EngineNotFound, self.api, "GET",
                          "/projects/" + v4.ref(self.env.engine_ref))
        if self.recorder:
            self.assertTrue(self.doCleanups())
            self.recorder.write()


class BuildModeTest(LiveCoolifyBase):
    """Opt-in Q108 public API switch and repeat-write contract."""

    def setUp(self):
        self.registry_template = os.environ.get(
            "COAST_CONTRACT_COOLIFY_BUILD_REGISTRY_TEMPLATE")
        if not self.registry_template:
            self.skipTest("Build-server registry settings missing")
        super().setUp()

    def test_build_mode_round_trip(self):
        self.assertGreaterEqual(len(self.servers), 2)
        builder, worker = self.servers[:2]
        for server in (builder, worker):
            path = "/servers/" + v4.ref(server.engine_ref)
            self.assertEqual([], self.api("GET", path + "/resources"))
            old = self.api("GET", path)["settings"]["server_role"]
            self.addCleanup(self.api, "PATCH", path,
                            data={"server_role": old})
        self.api("PATCH", "/servers/" + v4.ref(builder.engine_ref),
                 data={"server_role": "build"})
        self.api("PATCH", "/servers/" + v4.ref(worker.engine_ref),
                 data={"server_role": "both"})
        self.config.config(
            build_registry_image_template=self.registry_template,
            group="coolify")
        source = SimpleNamespace(
            type="git",
            repo_url="https://github.com/coollabsio/coolify-examples",
            branch="main", build_pack="nixpacks", base_directory="/",
            dockerfile_path=None, compose_path=None, credentials_ref=None)
        service = engine_app(
            id=uuid.uuid4().hex, engine_ref=None, server_id=worker.id,
            server_engine_ref=worker.engine_ref,
            description="Build-mode contract", ports="3000", replicas=1,
            auto_deploy=False, cpu_limit=1, memory_limit_mb=512,
            healthcheck_path=None)
        self.bind_parent_refs(service)
        self.addCleanup(self.driver.delete_app, self.ep, self.env, service)
        service.engine_ref = self.driver.create_app(
            self.ep, self.env, service, source, None).engine_ref
        for enabled in (False, False, True, True, False):
            self.driver.set_build_server_mode(
                self.ep, self.env, [worker], [service], enabled)
            worker_row = self.api(
                "GET", "/servers/" + v4.ref(worker.engine_ref))
            service_row = self.api(
                "GET", "/applications/" + v4.ref(service.engine_ref))
            self.assertEqual("deployment" if enabled else "both",
                             worker_row["settings"]["server_role"])
            self.assertEqual(enabled, service_row["settings"][
                "is_build_server_enabled"])


class BuildServersTest(LiveCoolifyBase):
    """Two dedicated builders, a distinct run server and registry transfer."""

    def setUp(self):
        prefix = "COAST_CONTRACT_COOLIFY_"
        self.registry_template = os.environ.get(
            prefix + "BUILD_REGISTRY_TEMPLATE")
        self.run_server_uuid = os.environ.get(prefix + "RUN_SERVER_UUID")
        if not self.registry_template or not self.run_server_uuid:
            # Skip before the shared fixture makes any live changes.
            self.skipTest("Build-server registry/run-server settings missing")
        super().setUp()

    def test_multiple_build_servers(self):
        self.assertGreaterEqual(len(self.servers), 2)
        self.assertEqual(len(self.servers), len({
            server.engine_ref for server in self.servers}))
        self.assertNotIn(self.run_server_uuid, {
            server.engine_ref for server in self.servers})
        for server in self.servers:
            path = "/servers/" + v4.ref(server.engine_ref)
            self.assertEqual([], self.api("GET", path + "/resources"))
            original = self.api("GET", path)["settings"]["server_role"]
            self.addCleanup(self.api, "PATCH", path,
                            data={"server_role": original})
            self.api("PATCH", path, data={"server_role": "build"})
            row = self.api("GET", path)
            self.assertEqual("build", row["settings"]["server_role"])
            self.assertTrue(row["settings"]["is_build_server"])
            server.role = "build"
            self.driver.register_server(self.ep, self.env, server, "")
            self.wait(lambda: self.driver.get_server_status(
                self.ep, self.env, server), states.ACTIVE)
        # Q62: deployment-only creation now has two usable builders.
        disposable = SimpleNamespace(id=uuid.uuid4().hex, engine_ref=None)
        self.addCleanup(self.driver.unregister_server,
                        self.ep, self.env, disposable)
        disposable.engine_ref = self.api("POST", "/servers", data={
            "name": v4.name(disposable), "ip": "192.0.2.254",
            "port": 22, "user": "root", "instant_validate": False,
            "private_key_uuid": self.private_key_uuid,
            "server_role": "deployment", "proxy_type": "none"})["uuid"]
        self.assertEqual("deployment", self.api(
            "GET", "/servers/" + v4.ref(disposable.engine_ref))[
                "settings"]["server_role"])
        self.driver.unregister_server(self.ep, self.env, disposable)
        run_path = "/servers/" + v4.ref(self.run_server_uuid)
        row = self.api("GET", run_path)
        self.assertNotEqual("build", row["settings"]["server_role"])
        run_server = SimpleNamespace(
            id=uuid.uuid4().hex, engine_ref=row["uuid"],
            hostname="coast-contract-worker-run",
            private_ip=row["ip"], role="worker")
        self.addCleanup(self.api, "PATCH", run_path,
                        data={"name": row["name"]})
        self.api("PATCH", run_path, data={"name": v4.name(run_server)})
        self.config.config(
            build_registry_image_template=self.registry_template,
            group="coolify")
        source = SimpleNamespace(
            type="git", repo_url=os.environ.get(
                "COAST_CONTRACT_COOLIFY_GIT_URL",
                "https://github.com/coollabsio/coolify-examples"),
            branch="main", build_pack="nixpacks",
            base_directory="/javascript/runtimes/nodejs", dockerfile_path=None,
            compose_path=None, credentials_ref=None)
        app = engine_app(
            id=uuid.uuid4().hex, engine_ref=None, server_id=run_server.id,
            server_engine_ref=run_server.engine_ref,
            description="Dedicated build contract", ports="3000",
            replicas=1, auto_deploy=False, cpu_limit=1,
            memory_limit_mb=512, healthcheck_path=None)
        self.bind_parent_refs(app)
        self.addCleanup(self.driver.delete_app, self.ep, self.env, app)
        app.engine_ref = self.driver.create_app(
            self.ep, self.env, app, source, None).engine_ref
        path = "/applications/" + v4.ref(app.engine_ref)
        row = self.api("GET", path)
        self.assertTrue(row["settings"]["is_build_server_enabled"])
        image = self.registry_template.format(
            environment_id=self.env.id, app_id=app.id)
        self.assertEqual(image, row["docker_registry_image_name"])
        # Rebuild even when random selection returns the same builder.
        self.api("PATCH", path, data={"disable_build_cache": True})
        builders = {v4.name(server): server.engine_ref
                    for server in self.servers}
        selected = set()
        attempts = int(os.environ.get(
            "COAST_CONTRACT_COOLIFY_BUILD_ATTEMPTS", "8"))
        self.assertGreaterEqual(attempts, 2)
        for index in range(attempts):
            deployment = self.deploy(app, source)
            lines = list(self.driver.get_deployment_log(
                self.ep, self.env, app, deployment))
            logs = "\n".join(line.line for line in lines)
            # Masking a variable such as "20" can also mask part of a UUID.
            # Match only known builder names in the public API payload;
            # never attach or print raw logs.
            raw = self.api("GET", "/deployments/" +
                           v4.ref(deployment.engine_ref))
            outputs = [item["output"] for item in json.loads(raw["logs"])]
            chosen = {name for name in builders if any(
                "Found a suitable build server (%s)" % name in output
                for output in outputs)}
            self.assertEqual(1, len(chosen))
            selected.update(chosen)
            self.assertRegex(
                logs.lower(), r"push(?:ing|ed).*?(?:image|registry)")
            self.assertTrue(any(
                line.line.lower().strip().startswith("image ") and
                line.line.lower().strip().endswith(" pulled")
                for line in lines), "No completed registry image pull logged")
            observed = self.driver.get_deployment_status(
                self.ep, self.env, app, deployment)
            self.assertRegex(observed.resolved_ref, r"^[0-9a-f]{40}$")
            # Inspect only this app's public registry tags, never credentials.
            host, repository = image.split("/", 1)
            registry_url = os.environ.get(
                "COAST_CONTRACT_COOLIFY_REGISTRY_URL", "https://" + host)
            registry = parse.urlsplit(registry_url)
            self.assertIn(registry.scheme, ("http", "https"))
            self.assertTrue(registry.hostname)
            self.assertFalse(registry.username or registry.password)
            self.assertFalse(registry.query or registry.fragment)
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(
                    registry_url.rstrip("/") + "/v2/" + repository +
                    "/tags/list", timeout=30, allow_redirects=False)
                self.assertEqual(200, response.status_code)
                tags = response.json()["tags"]
            self.assertIn(observed.resolved_ref, tags)
            self.addDetail("build-%d" % (index + 1), content.text_content(
                "builder_uuid=%s; registry commit tag verified; "
                "push/pull logged; run server ACTIVE" %
                builders[next(iter(chosen))]))
            if index >= 1 and selected == set(builders):
                break
        # Random sampling cannot guarantee all builders within a fixed bound.
        self.addDetail("builder-coverage", content.text_content(
            "%d/%d builders observed in %d builds (random selection)" %
            (len(selected), len(builders), index + 1)))
        resources = self.api("GET", run_path + "/resources")
        self.assertIn(app.engine_ref, {item["uuid"] for item in resources})
        for server in self.servers:
            self.assertEqual([], self.api(
                "GET", "/servers/" + v4.ref(server.engine_ref) + "/resources"))


class TransportTest(base.TestCase):
    def test_production_rejects_plain_http(self):
        self.config.config(insecure_direct_url=True, group="coolify")
        ep = engine.EngineEndpoint("http://controller.example.com", "", "",
                                   "", "test-token")
        self.assertRaises(engine.DriverError, client.Client, ep, self.conf)


class TemplateSettingsContractTest(LiveCoolifyBase):
    def test_template_runtime_env_and_domains(self):
        # Service responses contain generated secrets and compose content.
        # Do not record them, even when application recording is enabled.
        self.driver.recorder = None
        self.env.engine_version = EXPECTED_VERSION
        app = engine_app(
            id=uuid.uuid4().hex, engine_ref=None,
            template_id="wordpress-with-mariadb",
            server_id=self.servers[0].id,
            server_engine_ref=self.servers[0].engine_ref,
            description="Template settings contract")
        self.bind_parent_refs(app)
        app.engine_ref = self.driver.create_app(
            self.ep, self.env, app,
            {"type": "template", "template_id": app.template_id}, None
        ).engine_ref
        path = "/services/" + v4.ref(app.engine_ref)
        self.addCleanup(self.api, "DELETE", path,
                        params={"delete_volumes": "false"})
        # The disposable template remains stopped. Check the public compose
        # read and base64 update without exposing its generated values.
        compose = self.driver.get_compose(self.ep, self.env, app)
        self.driver.set_compose(self.ep, self.env, app, compose)
        self.assertEqual(compose, self.driver.get_compose(
            self.ep, self.env, app))

        target = self.api('GET', path)['applications'][0]['name']
        mount = engine.FileMount(uuid.uuid4().hex, '/etc/coast-contract.conf',
                                 'contract file', target)
        self.driver.set_file_mounts(self.ep, self.env, app, [mount])
        files = self.api('GET', path + '/storages')['file_storages']
        managed = [row for row in files if
                   (row.get('fs_path') or '').endswith(mount.id)]
        self.assertEqual(1, len(managed))
        self.assertEqual('contract file', managed[0]['content'])
        self.assertFalse(managed[0]['is_host_file'])
        self.driver.set_file_mounts(self.ep, self.env, app, [])
        before = self.api("GET", path + "/envs")
        defaults = {row["key"] for row in before}
        self.driver.set_env(self.ep, self.env, app, {
            "COAST_CONTRACT": engine.EnvVar("first")})
        self.driver.set_env(self.ep, self.env, app, {
            "COAST_CONTRACT": engine.EnvVar("second")})
        envs = self.api("GET", path + "/envs")
        selected = [row for row in envs if row["key"] == "COAST_CONTRACT"]
        self.assertEqual(1, len(selected))
        self.assertEqual("second", selected[0]["value"])
        self.driver.set_env(self.ep, self.env, app, {})
        remaining = self.api("GET", path + "/envs")
        self.assertEqual(defaults, {row["key"] for row in remaining})
        domain = app.id + ".example.com"
        self.driver.set_domains(self.ep, self.env, app, [domain])
        applications = self.api("GET", path)["applications"]
        exposed = [row for row in applications if row.get("fqdn")]
        self.assertEqual(1, len(exposed))
        self.assertEqual("https://" + domain, exposed[0]["fqdn"])
        self.driver.set_domains(self.ep, self.env, app, [])
        self.assertEqual({'env': False, 'domains': False},
                         self.driver.compare_settings(
                             self.ep, self.env, app, {}, []))
        self.assertFalse(any(row.get("fqdn") for row in
                             self.api("GET", path)["applications"]))
