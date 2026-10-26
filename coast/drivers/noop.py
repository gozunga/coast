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

"""Immediate, in-memory engine fake. No cloud, network or secret storage."""

import hashlib

from coast.common import states
from coast.drivers import base


class NoopDriver(base.CoastDriver):
    SUPPORTS_DEPLOY_KEY_ROTATION = True
    DEPLOY_KEY_IN_PLACE = True

    SUPPORTED_ENGINE_VERSIONS = ('noop-1.0',)
    CAPABILITIES = frozenset({'rollback', 'build_server'})

    def __init__(self, conf):
        super().__init__(conf)
        self._build_modes = {}
        self._servers = {}
        self._apps = {}
        self.deploy_keys = {}
        self.key_material = {}
        self._settings = {}
        self._deployments = {}
        self._projects = {}
        self._coolify_envs = {}
        self.terminal_enabled = {}

    @staticmethod
    def _key(env, resource):
        return (env.id, resource.id)

    @staticmethod
    def _result(kind, resource):
        return base.DriverResult(
            engine_ref=resource.engine_ref or
            'noop-%s-%s' % (kind, resource.id))

    def _status(self, records, env, resource):
        try:
            status = records[self._key(env, resource)]
        except KeyError:
            raise base.EngineNotFound(
                'Noop engine resource not found') from None
        return base.ObservedStatus(
            status=status, engine_version=self.SUPPORTED_ENGINE_VERSIONS[0])

    def get_engine_version(self, ep):
        return self.SUPPORTED_ENGINE_VERSIONS[0]

    def disable_builtin_terminals(self, ep, stack):
        self.terminal_enabled[stack.id] = False

    def bootstrap_environment(self, ep, env):
        self.disable_builtin_terminals(ep, env)
        return self._result('environment', env)

    def ensure_project(self, ep, stack, application):
        recorded = application.engine_ref
        stored = self._projects.get(application.id)
        if recorded:
            if stored != recorded:
                raise base.EngineNotFound('Noop project was not found')
            return base.DriverResult(engine_ref=recorded)
        if stored:
            return base.DriverResult(engine_ref=stored)
        ref = 'noop-project-%s' % application.id
        self._projects[application.id] = ref
        return base.DriverResult(engine_ref=ref)

    def delete_project(self, ep, stack, application):
        stored = self._projects.get(application.id)
        recorded = application.engine_ref
        if recorded:
            if stored != recorded:
                return
        elif stored is None:
            return
        if any(app_id == application.id
               for app_id, _ref in self._coolify_envs.values()):
            raise base.DriverError(
                'Engine project still has environments')
        self._projects.pop(application.id, None)

    def delete_stack_project(self, ep, stack):
        for records in (self._servers, self._apps, self._deployments,
                        self._settings):
            for key in list(records):
                if key[0] == stack.id:
                    del records[key]

    def ensure_environment(self, ep, stack, application, environment):
        if not application.engine_ref:
            raise base.DriverError('Engine project reference is missing')
        recorded = environment.engine_ref
        stored = self._coolify_envs.get(environment.id)
        if recorded:
            if not stored or stored[1] != recorded:
                raise base.EngineNotFound(
                    'Noop environment was not found')
            return base.DriverResult(engine_ref=recorded)
        if stored:
            return base.DriverResult(engine_ref=stored[1])
        ref = 'noop-environment-%s' % environment.id
        self._coolify_envs[environment.id] = (application.id, ref)
        return base.DriverResult(engine_ref=ref)

    def delete_environment(self, ep, stack, application, environment):
        stored = self._coolify_envs.get(environment.id)
        recorded = environment.engine_ref
        if recorded:
            if stored and stored[1] == recorded:
                del self._coolify_envs[environment.id]
            return
        if stored:
            del self._coolify_envs[environment.id]

    def register_server(self, ep, env, server, ssh_private_key):
        self._servers[self._key(env, server)] = states.ACTIVE
        return self._result('server', server)

    def set_build_server_mode(self, ep, stack, workers, services, enabled):
        for worker in workers:
            self.get_server_status(ep, stack, worker)
        for service in services:
            self.get_app_status(ep, stack, service)
        self._build_modes[stack.id] = enabled

    def unregister_server(self, ep, env, server):
        self._servers.pop(self._key(env, server), None)

    def get_server_status(self, ep, env, server):
        return self._status(self._servers, env, server)

    def create_database(self, ep, env, database, credentials):
        self._apps[self._key(env, database)] = states.ACTIVE
        return self._result('database', database)

    def delete_database(self, ep, env, database):
        self._apps.pop(self._key(env, database), None)

    def get_database_status(self, ep, env, database):
        return self._status(self._apps, env, database)

    def update_database(self, ep, env, database):
        return False

    def create_app(self, ep, env, app, source, source_credentials):
        self._apps.setdefault(self._key(env, app), states.STOPPED)
        if source_credentials:
            key_ref = 'noop-key-' + app.id
            self.deploy_keys[self._key(env, app)] = key_ref
            self.write_deploy_key(ep, env, app, key_ref, source_credentials)
        return self._result('app', app)

    def prepare_deploy_key(self, ep, env, app, private_key, reference):
        key_ref = self.deploy_keys[self._key(env, app)]
        return key_ref, key_ref

    def write_deploy_key(self, ep, env, app, key_ref, private_key):
        self.key_material[key_ref] = hashlib.sha256(
            private_key.encode('utf-8')).hexdigest()

    def restore_deploy_key(self, ep, env, app, key_ref):
        pass

    def delete_deploy_key(self, ep, key_ref):
        self.key_material.pop(key_ref, None)

    def update_app(self, ep, env, app, source):
        self.get_app_status(ep, env, app)

    def list_volume_mounts(self, ep, env, app):
        return [dict(name=mount.name, type=mount.type,
                     source=(mount.host_path if mount.type == 'bind'
                             else mount.name),
                     container_path=mount.container_path,
                     read_only=mount.read_only) for mount in
                self._settings.get(self._key(env, app), {}).get('mounts', [])]

    def get_compose(self, ep, env, app):
        self.get_app_status(ep, env, app)
        return self._settings.get(self._key(env, app), {}).get(
            'compose', 'services: {web: {image: nginx}}')

    def set_dockerfile(self, ep, env, app, content):
        self._dockerfiles = getattr(self, '_dockerfiles', {})
        self._dockerfiles[app.id] = content

    def set_compose(self, ep, env, app, content):
        self.get_app_status(ep, env, app)
        self._settings.setdefault(self._key(env, app), {})['compose'] = content

    def update_metadata(self, ep, env, app):
        self.get_app_status(ep, env, app)
        self._settings.setdefault(self._key(env, app), {})['metadata'] = {
            'name': getattr(app, 'name', None),
            'description': getattr(app, 'description', None)}

    def list_domains(self, ep, env, app):
        self.get_app_status(ep, env, app)
        stored = self._settings.get(self._key(env, app), {}).get('domains')
        if stored is None:
            return None
        return sorted(stored)

    def delete_app(self, ep, env, app):
        if getattr(app, 'kind', None) == 'database':
            return self.delete_database(ep, env, app)
        key_ref = self.deploy_keys.get(self._key(env, app))
        self.delete_deploy_key(ep, key_ref)
        self.deploy_keys.pop(self._key(env, app), None)
        self._apps.pop(self._key(env, app), None)
        self._settings.pop(self._key(env, app), None)

    @staticmethod
    def _variables(variables):
        return {key: (hashlib.sha256(item.value.encode()).digest(),
                      item.is_secret, item.build_time)
                for key, item in variables.items()}

    def compare_settings(self, ep, stack, service, variables, fqdns):
        self.get_app_status(ep, stack, service)
        observed = self._settings.get(self._key(stack, service), {})
        return {'env': observed.get('env', {}) != self._variables(variables),
                'domains': observed.get('domains', frozenset()) !=
                frozenset(name.lower() for name in fqdns)}

    def set_env(self, ep, env, app, variables):
        self.get_app_status(ep, env, app)
        self._settings.setdefault(self._key(env, app), {})['env'] = (
            self._variables(variables))

    def set_file_mounts(self, ep, env, app, mounts):
        pass

    def set_volume_mounts(self, ep, env, app, mounts):
        base.validate_mounts(mounts)
        self.get_app_status(ep, env, app)
        self._settings.setdefault(self._key(env, app), {})['mounts'] = (
            list(mounts))

    def start_app(self, ep, env, app):
        self.get_app_status(ep, env, app)
        self._apps[self._key(env, app)] = states.ACTIVE

    def stop_app(self, ep, env, app):
        self.get_app_status(ep, env, app)
        self._apps[self._key(env, app)] = states.STOPPED

    def restart_app(self, ep, env, app):
        self.start_app(ep, env, app)

    def get_app_status(self, ep, env, app):
        if getattr(app, 'kind', None) == 'database':
            return self.get_database_status(ep, env, app)
        return self._status(self._apps, env, app)

    def deploy_app(self, ep, env, app, deployment):
        self.get_app_status(ep, env, app)
        self._deployments.setdefault(self._key(env, deployment), states.ACTIVE)
        self.start_app(ep, env, app)
        result = self._result('deployment', deployment)
        return base.DriverResult(
            engine_ref=result.engine_ref,
            extra={'resolved_ref': '0' * 40})

    def get_deployment_status(self, ep, env, app, deployment):
        self.get_app_status(ep, env, app)
        return self._status(self._deployments, env, deployment)

    def cancel_deployment(self, ep, env, app, deployment):
        # Immediate deployments are complete; cancellation is a no-op.
        self.get_deployment_status(ep, env, app, deployment)

    def rollback(self, ep, env, app, target_deployment, new_deployment):
        self.get_deployment_status(ep, env, app, target_deployment)
        return self.deploy_app(ep, env, app, new_deployment)

    def get_deployment_log(self, ep, env, app, deployment, since=None,
                           masks=None):
        self.get_deployment_status(ep, env, app, deployment)
        return iter(())

    def get_app_logs(self, ep, env, app, tail=200, masks=None):
        self.get_app_status(ep, env, app)
        return iter(())

    def set_domains(self, ep, env, app, fqdns):
        self.get_app_status(ep, env, app)
        self._settings.setdefault(self._key(env, app), {})['domains'] = (
            frozenset(name.lower() for name in fqdns))
