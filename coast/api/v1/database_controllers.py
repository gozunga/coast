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

"""Standalone databases: project isolation and reference-only handoff."""

import datetime
import secrets
from urllib import parse

from oslo_config import cfg
from oslo_utils import timeutils
import pecan

from coast.api import microversions
from coast.api import secret_store
from coast.api.v1 import backup_controllers
from coast.api.v1 import flavor_controllers
from coast.api.v1 import ingress_controllers as ingress
from coast.api.v1 import schemas
from coast.api.v1 import service_controllers as services
from coast.api.v1 import stack_controllers as common
from coast.api.v1 import validation
from coast.common import database_public
from coast.common import engine_versions
from coast.common import exception
from coast.common import notifications
from coast.common import states
from coast.db import api as db_api
from coast import objects


FIELDS = ('id', 'project_id', 'stack_id', 'environment_id', 'server_id',
          'name', 'description', 'engine', 'status', 'fault', 'cpu_limit',
          'memory_limit_mb', 'task_owner', 'task_started_at',
          'created_at', 'updated_at')
COMPUTED = ('appgroup_id', 'version', 'port', 'internal_host', 'internal_url',
            'public', 'public_url', 'restart_required')
PUBLIC = schemas.document({
    'enabled': {'type': 'boolean'},
    'port': {'type': ['integer', 'null'], 'minimum': 1024, 'maximum': 65535},
    'source': schemas.text(64)})
SETTINGS = {key: schemas.SERVICE['properties'][key] for key in (
    'name', 'description', 'cpu_limit', 'memory_limit_mb')}
SETTINGS['public'] = PUBLIC
CREATE = schemas.document({
    **SETTINGS, 'engine': {'enum': list(engine_versions.DATABASES)},
    'version': schemas.text(32),
    **{key: dict(schemas.text(63), pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
       for key in ('database', 'username')},
    **{key: schemas.text(36) for key in (
        'appgroup_id', 'environment_id', 'server_id')}},
    ('name', 'engine', 'appgroup_id', 'environment_id', 'server_id'))
BUSY = ('Wait for the current operation on this database to finish, '
        'then try again.')


def stack_open(ctx, stack_id):
    try:
        services.stack_open(ctx, stack_id)
    except exception.Conflict:
        raise exception.Conflict(
            'Wait for the current operation on this stack to finish, '
            'then try again.') from None


def update_conflict(row):
    if row.status == states.PENDING:
        return 'Wait for the database to finish being created, then try again.'
    if row.status == states.DELETING:
        return 'This database is being deleted.'
    return BUSY


def action_conflict(action, status):
    if action == 'start':
        if status in (states.STARTING, states.ACTIVE):
            return 'The database is already running.'
        return 'Only a stopped database can be started.'
    if status == states.STARTING:
        return 'The database is starting. Try again when it is Active.'
    if status == states.STOPPED:
        return ('The database is already stopped.' if action == 'stop'
                else 'Start the database first.')
    return 'Only an active database can be stopped or restarted.'


def gate():
    if not microversions.at_least(pecan.request.version,
                                  microversions.DATABASE_VERSION):
        pecan.abort(404)


def get(ctx, id, **kwargs):
    gate()
    row = objects.Service.get_by_id(ctx, id, **kwargs)
    if row.kind != 'database':
        raise exception.NotFound()
    return row


def validate_limits(ctx, server_id, options):
    if not any(options.get(key) is not None
               for key in ('cpu_limit', 'memory_limit_mb')):
        return
    server = objects.Server.get_by_id(ctx, server_id)
    flavor = flavor_controllers.server_flavor(server)
    if not flavor:
        return
    for key, capacity in (('cpu_limit', 'vcpus'), ('memory_limit_mb', 'ram')):
        limit = options.get(key)
        if (limit is not None and flavor.get(capacity) is not None and
                limit > flavor[capacity]):
            raise exception.Invalid('Database limit exceeds worker capacity.')


def connection_url(row, password=None, public=False):
    host, port = row.engine_ref, engine_versions.DATABASES[row.engine]['port']
    if public:
        host, private = public_hosts(row)
        host, port = host or private, row.public_port
    if not host:
        return None
    info = engine_versions.DATABASES[row.engine]
    user = parse.quote(row.db_username or '', safe='')
    if password is not None:
        user += ':' + parse.quote(password, safe='')
    authority = (user + '@' if user else '') + host
    result = '%s://%s:%s/%s' % (
        info.get('url_scheme', row.engine), authority, port,
        parse.quote(row.db_name or '', safe=''))
    if info.get('url_query'):
        result += '?' + info['url_query']
    return result


def public_hosts(row):
    if not row.public_enabled:
        return None, None
    ctx = common.context()
    server = objects.Server.get_by_id(
        ctx, row.server_id, all_projects=ctx.is_admin)
    return server.floating_ip, server.private_ip


def public_values(ctx, options, stack_id, row=None):
    public = options.pop('public', {})
    enabled = public.get('enabled', bool(row and row.public_enabled))
    port = public.get('port', row.public_port if row else None)
    source = public.get('source', row.public_source if row else '0.0.0.0/0')
    source = ingress.validate(dict(protocol='tcp', port_range_min=1024,
                                   source=source))['source']
    reserved = {cfg.CONF.agent.bind_port,
                cfg.CONF.controller_worker.agent_port,
                8000, 6001, 6002}
    if port in reserved:
        raise exception.Invalid('Public port is reserved for the app engine '
                                'or management agent.')
    others = [item for item in database_public.databases(ctx, stack_id)
              if not row or item.id != row.id]
    custom = db_api.ingress_rules.list_for_stack(ctx, stack_id)
    required = ingress.managed(ctx, stack_id)

    def occupied(candidate):
        return (any(item.public_enabled and
                    item.public_port == candidate for item in others) or
                any(rule['port_range_min'] <= candidate <=
                    rule['port_range_max'] for rule in required
                    if not rule['id'].startswith('managed-database-')) or
                any(item.port_range_min is not None and
                    item.port_range_min <= candidate <= item.port_range_max
                    for item in custom))

    if enabled and port is None:
        config = cfg.CONF.databases
        port = next((candidate for candidate in range(
            config.public_port_min, config.public_port_max + 1)
            if candidate not in reserved and not occupied(candidate)), None)
        if port is None:
            raise exception.Conflict(
                'No public port is available. Free a port, then try again.')
    if enabled and port is not None and occupied(port):
        raise exception.Conflict(
            'Public port is already in use. Choose another port.')
    if enabled and not (row and row.public_enabled):
        count = len(custom) + sum(bool(item.public_enabled) for item in others)
        if count >= cfg.CONF.controller_worker.ingress_rules_per_stack:
            raise exception.OverQuota(resources='ingress_rules')
    return dict(public_enabled=enabled, public_port=port, public_source=source)


class Engines:
    @pecan.expose('json')
    def index(self, **kwargs):
        gate()
        if pecan.request.method != 'GET':
            pecan.abort(405)
        common.authorize('database', 'get_all')
        validation.parameters(dict(pecan.request.params), set())
        return {'database_engines': [dict(
            engine=engine, versions=list(info['versions']),
            **{key: info[key] for key in (
                'display_name', 'default_version', 'port', 'backups',
                'has_username', 'has_database')})
            for engine, info in engine_versions.DATABASES.items()]}


class Collection(services.Collection):
    resource = 'database'
    fields = FIELDS
    computed = COMPUTED

    def path(self, id=None):
        return '/databases' + ('/' + id if id else '')

    def render(self, row):
        result = services.values(row, FIELDS)
        ctx = common.context()
        parent = objects.Environment.get_by_id(
            ctx, row.environment_id, show_deleted=True,
            all_projects=ctx.is_admin)
        host, private_host = public_hosts(row)
        result.update(
            appgroup_id=parent.appgroup_id, version=row.engine_version,
            port=engine_versions.DATABASES[row.engine]['port'],
            internal_host=row.engine_ref, internal_url=connection_url(row),
            public=dict(enabled=bool(row.public_enabled), port=row.public_port,
                        source=row.public_source, host=host,
                        private_host=private_host),
            public_url=connection_url(row, public=True),
            restart_required=bool(row.settings_changed_at),
            links=[{'rel': 'self', 'href': services.url(self.path(row.id))}])
        fields = pecan.request.params.get('fields')
        if fields and pecan.request.method == 'GET':
            self.validate_fields(fields)
            result = {key: value for key, value in result.items()
                      if key in fields.split(',') or key == 'links'}
        return result

    @pecan.expose()
    def _lookup(self, id, *remainder):
        gate()
        return Item(self, id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        gate()
        if pecan.request.method == 'GET':
            return self.list()
        if pecan.request.method != 'POST':
            pecan.abort(405)
        ctx = common.authorize('database', 'create')
        validation.parameters(dict(pecan.request.params), set())
        options = schemas.body(CREATE, 'database')
        return self.create_database(ctx, options)

    def create_database(self, ctx, options):
        services.validate_cpu(options)
        info = engine_versions.DATABASES[options['engine']]
        version = options.pop('version', info['default_version'])
        if version not in info['versions']:
            raise exception.Invalid('Unsupported database version.')
        for key in ('username', 'database'):
            if key in options and not info['has_' + key]:
                raise exception.Invalid('This engine does not accept ' + key)
        group = objects.Appgroup.get_by_id(ctx, options.pop('appgroup_id'))
        environment = objects.Environment.get_by_id(
            ctx, options['environment_id'])
        server = objects.Server.get_by_id(ctx, options['server_id'])
        if (environment.appgroup_id != group.id or
                server.stack_id != group.stack_id or server.role != 'worker'):
            raise exception.Invalid('Invalid database placement.')
        if any(parent.status != states.ACTIVE or not parent.engine_ref
               for parent in (group, environment)):
            raise exception.Conflict(
                'Wait for the stack to finish setting up, then try again.')
        validate_limits(ctx, server.id, options)
        data = dict(engine_version=version,
                    db_name=options.pop('database', 'app')
                    if info['has_database'] else None,
                    db_username=options.pop('username', 'app')
                    if info['has_username'] else None)
        with services.secret_writes(ctx) as refs, \
                db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            common.quota_hook(ctx, 'service', options)
            stack_open(ctx, group.stack_id)
            data.update(public_values(ctx, options, group.stack_id))
            # The shared service schema requires a source. It is private and
            # never used to dispatch database operations.
            source = objects.Source(context=ctx, type='image',
                                    image=info['versions'][version])
            source.create()
            row = objects.Service(
                context=ctx, stack_id=group.stack_id, source_id=source.id,
                kind='database', status=states.PENDING, task_state=states.NONE,
                ports='', auto_deploy=False, health_check='none', **data,
                **options)
            row.create()
            for field in ('password', 'root_password'):
                if field not in info['credentials']:
                    continue
                reference = secret_store.call(
                    'put', ctx, secrets.token_urlsafe(32),
                    secret_store.SecretIdentity.for_resource(row, field))
                refs.append(reference)
                setattr(row, field + '_ref', reference)
            row.save()
            row.acquire_task(states.PROVISIONING, owner='create_database')
        response = {'database': self.render(row)}
        self.handler.create_service(ctx, row, {})
        pecan.response.status = 202
        return response

    def list(self):
        params = dict(pecan.request.params)
        validation.parameters(params, {
            'stack_id', 'appgroup_id', 'environment_id', 'server_id', 'engine',
            'status', 'fields', 'limit', 'marker', 'sort_key', 'sort_dir',
            'all_projects', 'project_id'})
        # The common paginator supplies policy, admin checks and next links.
        return super().list()


class RestoreCollection(Collection):
    def render(self, row):
        # Restore queues its job in the create transaction. Avoid a separate
        # response context reading while that transaction is uncommitted.
        return {'id': row.id}


class Item:
    def __init__(self, collection, id):
        self.collection = collection
        self.id = id
        self.backup_policy = backup_controllers.BackupPolicy(id, database=True)
        self.backups = backup_controllers.ServiceBackups(id, database=True)

    @pecan.expose('json')
    def index(self, **kwargs):
        row = get(common.context(), self.id)
        method = pecan.request.method
        validation.parameters(dict(pecan.request.params),
                              {'fields'} if method == 'GET' else set())
        if method == 'GET':
            common.authorize('database', 'get_one', row)
        elif method == 'PATCH':
            ctx = common.authorize('database', 'update', row)
            options = schemas.body(schemas.document(SETTINGS), 'database')
            services.validate_cpu(options)
            validate_limits(ctx, row.server_id, options)
            with db_api.writer.using(ctx) as session:
                db_api._lock_project(session, ctx.project_id)
                try:
                    services.claim_service(ctx, row, states.RECONCILING,
                                           owner='update_database')
                except exception.Conflict:
                    raise exception.Conflict(update_conflict(row)) from None
                if row.status in (states.PENDING, states.DELETING):
                    raise exception.Conflict(update_conflict(row))
                if 'public' in options:
                    options.update(public_values(
                        ctx, options, row.stack_id, row))
                if any(key in options and options[key] != getattr(row, key)
                       for key in ('cpu_limit', 'memory_limit_mb',
                                   'public_enabled', 'public_port',
                                   'public_source')):
                    row.settings_changed_at = timeutils.utcnow()
                for key, value in options.items():
                    setattr(row, key, value)
                row.fault = None
                row.save()
            self.collection.handler.rename_service(ctx, row)
            notifications.completed(ctx, row, 'update', 'database')
        elif method == 'DELETE':
            ctx = common.authorize('database', 'delete', row)
            if pecan.request.body:
                schemas.body(schemas.document({}))
            with db_api.writer.using(ctx):
                stack_open(ctx, row.stack_id)
                try:
                    states.check_transition(
                        'service', row.status, states.DELETING)
                except exception.Conflict:
                    raise exception.Conflict(
                        'This database is being deleted.') from None
                if row.task_state in services.DEPLOY_TASKS:
                    try:
                        db_api.cas_task_state(
                            ctx, objects.Service.repository.model, row.id,
                            new=states.DELETING, expected=row.task_state,
                            owner='delete_database')
                    except exception.Conflict:
                        raise exception.Conflict(BUSY) from None
                    row = objects.Service.get_by_id(ctx, row.id)
                else:
                    try:
                        row.acquire_task(states.DELETING,
                                         owner='delete_database')
                    except exception.Conflict:
                        message = ('This database is being deleted.'
                                   if row.status == states.DELETING else BUSY)
                        raise exception.Conflict(message) from None
                row.fault = None
                row.status = states.DELETING
                row.save()
            response = {'database': self.collection.render(row)}
            self.collection.handler.delete_service(ctx, row)
            pecan.response.status = 202
            return response
        else:
            pecan.abort(405)
        return {'database': self.collection.render(row)}

    @pecan.expose('json')
    def credentials(self, **kwargs):
        row = get(common.context(), self.id)
        if pecan.request.method != 'GET':
            pecan.abort(405)
        ctx = common.authorize('database', 'get_credentials', row)
        validation.parameters(dict(pecan.request.params), set())
        pecan.response.headers['Cache-Control'] = 'no-store'
        password = secret_store.call('get', ctx, row.password_ref)
        result = dict(username=row.db_username, database=row.db_name,
                      password=password, internal_url=connection_url(
                          row, password),
                      public_url=connection_url(row, password, public=True))
        if row.root_password_ref:
            result['root_password'] = secret_store.call(
                'get', ctx, row.root_password_ref)
        return {'credentials': result}

    @pecan.expose('json')
    def action(self, **kwargs):
        row = get(common.context(), self.id)
        if pecan.request.method != 'POST':
            pecan.abort(405)
        validation.parameters(dict(pecan.request.params), set())
        body = schemas.body(schemas.document({
            key: schemas.document({})
            for key in ('start', 'stop', 'restart')}))
        if len(body) != 1:
            raise exception.Invalid()
        action = next(iter(body))
        ctx = common.authorize('database', action, row)
        with db_api.writer.using(ctx):
            stack_open(ctx, row.stack_id)
            row = objects.Service.obj_from_db(
                ctx, db_api.services._get(ctx, row.id, lock=True))
            if row.task_state != states.NONE:
                raise exception.Conflict(BUSY)
            row.acquire_task(states.RECONCILING, owner=action)
            required = states.STOPPED if action == 'start' else states.ACTIVE
            if row.status != required:
                raise exception.Conflict(action_conflict(action, row.status))
            states.check_transition('service', row.status,
                                    states.STOPPED if action == 'stop'
                                    else states.STARTING)
            row.fault = None
            row.save()
        getattr(self.collection.handler, action + '_service')(ctx, row)
        pecan.response.status = 202
        return {'database': self.collection.render(row)}

    @pecan.expose('json')
    def logs(self, **kwargs):
        row = get(common.context(), self.id)
        if pecan.request.method != 'GET':
            pecan.abort(405)
        ctx = common.authorize('database', 'get_logs', row)
        params = dict(pecan.request.params)
        validation.parameters(params, {'tail', 'since'})
        try:
            if 'tail' in params:
                params['tail'] = int(params['tail'])
                if params['tail'] < 0:
                    raise ValueError()
            if 'since' in params:
                datetime.datetime.fromisoformat(
                    params['since'].replace('Z', '+00:00'))
        except ValueError:
            raise exception.Invalid() from None
        return {'logs': self.collection.handler.service_logs(ctx, row, params)}
