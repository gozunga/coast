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

"""Project-scoped stack, server, and volume endpoints."""

import datetime
import re
from urllib import parse

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import timeutils
import pecan

from coast.api import handlers
from coast.api import microversions
from coast.api.v1 import schemas
from coast.api.v1 import validation
from coast.common import clients
from coast.common import context as context_module
from coast.common import exception
from coast.common import naming
from coast.common import notifications
from coast.common import policy
from coast.common import quota
from coast.common import stack_delete
from coast.common import states
from coast.conductor import controller
from coast.conductor import controller_failover
from coast.conductor import engine_upgrade
from coast.conductor import rpcapi
from coast.conductor import volume_delete
from coast.db import api as db_api
from coast.db import models
from coast import objects


LOG = logging.getLogger(__name__)

ENV_FIELDS = ('id', 'project_id', 'name', 'description', 'plan',
              'engine_version', 'status', 'created_at', 'updated_at', 'fault')
ENV_DETAIL = ('driver', 'network_id', 'engine_ref', 'task_state')
ADMIN_STACK_FIELDS = ('subnet_id', 'router_id', 'sg_ingress_id', 'sg_mgmt_id',
                      'service_project_id', 'server_count', 'workload_count')
SERVER_FIELDS = ('id', 'project_id', 'stack_id', 'name', 'hostname',
                 'kind', 'role',
                 'status', 'floating_ip', 'created_at', 'updated_at')
SERVER_DETAIL = ('flavor_id', 'image_id', 'compute_id', 'ironic_node_id',
                 'mgmt_ip', 'private_ip', 'floating_ip_id',
                 'agent_cert_serial',
                 'agent_cert_expires_at', 'last_heartbeat_at', 'engine_ref',
                 'task_state', 'fault')
VOLUME_FIELDS = ('id', 'project_id', 'stack_id', 'server_id', 'name',
                 'size_gb', 'volume_type', 'mount_path', 'purpose', 'status',
                 'created_at', 'updated_at')
STORAGE_FIELDS = ('id', 'project_id', 'stack_id', 'name', 'size_gb', 'status',
                  'origin', 'service_id', 'server_id', 'container_path',
                  'created_at', 'updated_at')
VOLUME_DETAIL = ('cinder_volume_id',)
EXPORT_FIELDS = ('id', 'status', 'volume_ids', 'artifacts',
                 'object_expires_at')
DATA_PURPOSE = 'data'
SNAPSHOT_PURPOSES = ('data', 'docker')
# PENDING, BUILDING, ACTIVE and FAILOVER already move to DELETING.
# ERROR and UNHEALTHY are failed rows. DELETING starts a stuck delete again.
_REMOVABLE_SERVER_STATUSES = frozenset((
    states.PENDING, states.BUILDING, states.ACTIVE, states.UNHEALTHY,
    states.FAILOVER, states.ERROR, states.DELETING))
_FIELDS = {
    'stack': (ENV_FIELDS, ENV_DETAIL),
    'server': (SERVER_FIELDS, SERVER_DETAIL),
    'volume': (VOLUME_FIELDS, VOLUME_DETAIL),
    'export': (EXPORT_FIELDS, ()),
    'storage': (STORAGE_FIELDS, ()),
}


def context():
    ctx = pecan.request.context['coast_context']
    if not ctx.project_id or not ctx.user_id:
        pecan.abort(401)
    return ctx


def authorize(resource, action, row=None):
    ctx = context()
    target = {'project_id': row.project_id if row else ctx.project_id}
    policy.authorize(ctx, 'coast:%s:%s' % (resource, action), target)
    return ctx


def url(path):
    endpoint = cfg.CONF.api.public_endpoint or pecan.request.host_url
    return endpoint.rstrip('/') + '/v1/stacks' + path


def admin_inventory():
    return microversions.at_least(
        pecan.request.version, microversions.ADMIN_STACK_VERSION)


def read_all_projects(resource='stack'):
    """Allow policy-authorized cross-project reads, never writes."""
    if pecan.request.method != 'GET':
        return False
    if resource in ('stack', 'server', 'volume') and not admin_inventory():
        return False
    value = pecan.request.params.get('all_projects', 'false')
    validation.choice('all_projects', value, ('true', 'false'))
    if value == 'true' or 'project_id' in pecan.request.params:
        ctx = context()
        policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
        ctx.is_admin = True
        return True
    return False


def resource_fields(resource):
    names, detail = _FIELDS[resource]
    from coast.api.v1 import dns_controllers
    if resource == 'stack' and dns_controllers.enabled():
        names += ('default_domain', 'dns_zone_id')
    if (resource == 'stack' and admin_inventory() and
            pecan.request.method == 'GET'):
        detail += ADMIN_STACK_FIELDS
    return names, detail


def inventory(rows, all_projects=False):
    """Fetch admin inventory once per response, independent of page size."""
    if not rows or not admin_inventory():
        return {}
    visible = []
    for row in rows:
        try:
            authorize('stack', 'get_detail', row)
        except exception.NotAuthorized:
            continue
        visible.append(row)
    if not visible:
        return {}
    rows = visible
    counts = db_api.stack_resource_counts(
        context(), [row.id for row in rows], all_projects=all_projects)
    project_id = clients.get_session().get_project_id()
    if not project_id:
        raise exception.ServiceConfigError()
    for row in rows:
        if project_id != row.project_id:
            counts[row.id]['service_project_id'] = project_id
    return counts


def render(row, resource, path, extra=None):
    names, detail = resource_fields(resource)
    # A resource with no detail fields has no get_detail rule.
    if detail:
        try:
            authorize(resource, 'get_detail', row)
        except exception.NotAuthorized:
            detail = ()
        names += detail
    result = {}
    for name in names:
        if name == 'service_project_id' and name not in (extra or {}):
            continue
        value = (extra[name] if name in (extra or {})
                 else getattr(row, name))
        if resource == 'stack' and name == 'fault' and not detail:
            value = stack_delete.public_fault(value)
        if isinstance(value, datetime.datetime):
            value = value.isoformat().replace('+00:00', 'Z')
        result[name] = value
    if resource == 'storage' and microversions.at_least(
            pecan.request.version, '1.16'):
        result['source'] = 'coast' if row.origin == 'api' else 'template'
        names += ('source',)
        if microversions.at_least(
                pecan.request.version, microversions.STORAGE_TYPES_VERSION):
            result.update(type=row.type, host_path=row.host_path,
                          read_only=row.read_only,
                          source=(row.host_path if row.type == 'bind'
                                  else row.name),
                          origin=('user' if row.origin == 'api'
                                  else 'template'))
            if row.origin == 'template':
                result['id'] = None
                result['links'] = []
            names += ('type', 'host_path', 'read_only')
    result['links'] = [{'rel': 'self', 'href': url(path)}]
    if (resource == 'storage' and row.origin == 'template' and
            microversions.at_least(
                pecan.request.version, microversions.STORAGE_TYPES_VERSION)):
        result['links'] = []
    if (resource == 'server' and pecan.request.method == 'GET' and
            microversions.at_least(
                pecan.request.version, microversions.FLAVOR_VERSION)):
        from coast.api.v1 import flavor_controllers
        result['flavor'] = flavor_controllers.server_flavor(row)
        names += ('flavor',)
    fields = (pecan.request.params.get('fields')
              if pecan.request.method == 'GET' else None)
    if fields:
        requested = fields.split(',')
        for field in requested:
            validation.choice('field', field, set(names) | {'links'})
        result = {key: value for key, value in result.items()
                  if key in requested or key == 'links'}
    return result


def quota_hook(context, resource, values):
    """Reserve in the caller's resource transaction."""
    if resource == 'export':
        return quota.check_export(context, values['size_gb'])
    reservation = quota.reserve(context, **quota.deltas(resource, values))
    context.quota_reservation = reservation
    return reservation


def volume_start(ctx, row, action):
    # Volumes have no task column. Keep held capacity identifiable across
    # unordered casts, using the existing activity record and its fields.
    objects.Event(
        context=ctx,
        id=rpcapi.receipt_id(action + '_' + row.repository.resource, row),
        stack_id=row.stack_id,
        resource_type=row.repository.resource,
        resource_id=row.id, action=action, outcome='start',
        message=str(ctx.quota_reservation.get('volume_gb', 0)),
        request_id=ctx.request_id).create()


ROLE_NAMES = {'control': 'App engine server', 'worker': 'workers',
              'build': 'build servers'}
IMAGE_ROLES = {'controller': 'App engine server', 'worker': 'workers'}


def flavor_to_plan(values):
    """Interim (Q150): map a Nova flavor name or ID to its plan name.

    The dashboard offers Nova flavors. A value that is already a plan name
    is left alone, as is any value that matches no plan for its role, so
    existing validation still decides. Only VM plans are considered.
    """
    from coast.api.v1 import plan_controllers
    plans = [item for item in plan_controllers.load_plans()
             if item['kind'] == 'vm']
    names = {item['name'] for item in plans}
    cloud = None
    found = {}

    def resolve(value, role):
        nonlocal cloud
        if value is None or value in names:
            return value
        compatible = [item for item in plans if role in item['roles']]
        match = [item for item in compatible if item['flavor'] == value]
        if not match:
            if value not in found:
                try:
                    cloud = cloud or controller.ControllerCloud(cfg.CONF)
                    flavor = cloud.proxy('nova').find_flavor(value)
                except Exception:
                    flavor = None
                found[value] = {flavor.id, flavor.name} if flavor else set()
            match = [item for item in compatible
                     if item['flavor'] in found[value]]
            if found[value] and len(match) != 1:
                # A real Nova flavor without exactly one plan for this role
                # would only fail later in the conductor.
                raise exception.Invalid(
                    'Flavor %s is not available for %s.' % (
                        value, ROLE_NAMES[role]))
        return match[0]['name'] if len(match) == 1 else value

    values['plan'] = resolve(values.get('plan'), 'control')
    for worker in values.get('workers', []):
        if worker.get('kind', 'vm') == 'vm':
            worker['plan'] = resolve(worker.get('plan'), 'worker')
    if values.get('build_server_plan') is not None:
        values['build_server_plan'] = resolve(
            values['build_server_plan'], 'build')


def validate_selection(values):
    """Validate role selections before any quota or resource writes."""
    from coast.api.v1 import plan_controllers
    plans = plan_controllers.load_plans()

    def plan(name, role, kind='vm'):
        if name is not None and not any(
                item['name'] == name for item in plans):
            raise exception.Invalid(
                "Plan '%s' does not exist. Run 'openstack coast plan list' "
                "to see the available plans." % name)
        allowed = [item for item in plans if item['kind'] == kind and
                   role in item['roles'] and
                   (name is None or item['name'] == name)]
        if len(allowed) != 1:
            raise exception.Invalid(
                ("Plan '%s' is not available for %s with kind '%s'. "
                 "Run 'openstack coast plan list' to choose a compatible "
                 "plan." % (name, ROLE_NAMES[role], kind)
                 if name else "Choose a build plan. Run 'openstack coast "
                 "plan list' to find a unique build-compatible VM plan."))
        return allowed[0]['name']

    plan(values['plan'], 'control')
    # Q151: 1.1 takes one or more VM workers (quota limits the count).
    for worker in values.get('workers', []):
        plan(worker['plan'], 'worker', worker['kind'])
    build = values.get('build_server', False)
    if not build and any(key in values for key in (
            'build_server_plan', 'build_server_image')):
        raise exception.Invalid(
            'A build server flavor or image needs "Add a build server".')
    if build:
        values['build_server_plan'] = plan(
            values.get('build_server_plan'), 'build')
    cloud = controller.ControllerCloud(cfg.CONF)
    selections = [(values, 'image', 'controller')]
    selections.extend((worker, 'image', 'worker')
                      for worker in values.get('workers', []))
    if build:
        # Build VMs use the existing operator-published worker image catalog.
        selections.append((values, 'build_server_image', 'worker'))
    for target, key, role in selections:
        if key in target:
            try:
                target[key] = cloud.select_image(role, target[key]).id
            except ValueError:
                raise exception.Invalid(
                    'Image %s is not available for %s.' % (
                        target[key], IMAGE_ROLES[role])) from None


def failover_services_idle(ctx, stack, server):
    if server.role != 'control' or stack.driver == 'noop':
        return services_idle(ctx, stack.id)
    try:
        controller_failover.idle_workloads(ctx, stack, cfg.CONF)
    except exception.Conflict as exc:
        LOG.warning(
            'Controller failover refused for stack %(stack)s '
            'server %(server)s: %(reason)s',
            {'stack': stack.id, 'server': server.id, 'reason': str(exc)})
        raise


def services_idle(ctx, stack_id):
    """Raise 409 when any service on the stack still holds a task (Q145).

    Services lock only themselves. Exclusive stack work (delete, failover;
    upgrade uses engine_upgrade.idle) waits for them.
    """
    busy = db_api.services._query(ctx).filter(
        models.Service.stack_id == stack_id,
        models.Service.task_state != states.NONE).first()
    if busy is not None:
        raise exception.Conflict()


class Collection:
    resource = 'stack'
    nest = None
    object_class = objects.Stack
    list_class = objects.StackList

    def __init__(self, stack_id=None, handler=None):
        self.stack_id = stack_id
        self.handler = handler or handlers.Handler()

    def render(self, row, extra=None):
        return render(row, self.resource, self.path(row.id), extra)

    def parent(self):
        if self.stack_id:
            return objects.Stack.get_by_id(
                context(), self.stack_id,
                all_projects=read_all_projects(self.resource))

    def path(self, id=None):
        base = ''
        if self.stack_id:
            base = '/%s/%s' % (self.stack_id, self.nest)
        return base + ('/' + id if id else '')

    @pecan.expose()
    def _lookup(self, id, *remainder):
        return Item(self, id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        self.parent()
        method = pecan.request.method
        if method == 'GET':
            return self.list()
        if method != 'POST':
            pecan.abort(405)
        ctx = authorize(self.resource, 'create')
        schema = schemas.SERVER
        if self.resource == 'stack':
            schema = schemas.STACK
            if pecan.request.version == '1.1':
                schema = schemas.STACK_1_1
            elif microversions.at_least(pecan.request.version, '1.1'):
                schema = schemas.STACK_IMAGES
        from coast.api.v1 import dns_controllers
        if self.resource == 'stack':
            schema = dns_controllers.schema(schema, 'stack')
        values = schemas.body(schema, self.resource)
        if self.resource == 'stack':
            dns_controllers.stack_link(ctx, values)
        if self.resource == 'stack':
            flavor_to_plan(values)
        if self.resource == 'stack':
            validate_selection(values)
        kinds = ([worker['kind'] for worker in values.get('workers', [])]
                 if self.resource == 'stack' else [values['kind']])
        if ('baremetal' in kinds and
                not cfg.CONF.controller_worker.enable_baremetal):
            raise exception.Invalid()
        if self.resource == 'stack':
            data = {key: values[key] for key in (
                'name', 'description', 'plan', 'default_domain',
                'dns_zone_id', 'dns_trust_id') if key in values}
            data.update(driver=cfg.CONF.controller_worker.driver,
                        engine_version=cfg.CONF.coolify.pinned_version)
        else:
            # No plans loader exists yet. Validate plan, pass it to the seam,
            # and do not invent a server plan column or flavor mapping.
            data = {key: values[key] for key in ('name', 'kind', 'role')
                    if key in values}
            data['stack_id'] = self.stack_id
        row = self.object_class(context=ctx, status=states.PENDING,
                                task_state=states.NONE, **data)
        with db_api.writer.using(ctx):
            quota_hook(ctx, self.resource, values)
            if self.resource == 'server':
                stack = volume_delete.lock_stack(ctx, self.stack_id)
                if stack.task_state != states.NONE:
                    raise exception.Conflict()
                if not row.obj_attr_is_set('name') or not row.name:
                    row.name = next_server_name(ctx, self.stack_id,
                                                values['role'])
                naming.assign_server_hostname(ctx, stack, row, conf=cfg.CONF)
            row.create()
            row.acquire_task(states.PROVISIONING)
            if self.resource == 'stack':
                handlers.record_stack_create(ctx, row)
        getattr(self.handler, 'create_' + self.resource)(ctx, row, values)
        pecan.response.status = 202
        return {self.resource: render(row, self.resource, self.path(row.id))}

    def validate_fields(self, fields):
        names, detail = resource_fields(self.resource)
        if (self.resource == 'server' and microversions.at_least(
                pecan.request.version, microversions.FLAVOR_VERSION)):
            names += ('flavor',)
        requested = set(fields.split(',')) - {'links'}
        if requested <= set(names):
            return
        if not detail or requested - set(names + detail):
            raise exception.Invalid(
                "Invalid fields '%s'. Use one of: %s." %
                (fields, ', '.join(names + detail)))
        try:
            authorize(self.resource, 'get_detail')
        except exception.NotAuthorized:
            raise exception.Invalid(
                "Invalid fields '%s'. Choose accessible fields: %s." %
                (fields, ', '.join(names))) from None

    def list(self):
        ctx = authorize(self.resource, 'get_all')
        params = dict(pecan.request.params)
        names, detail = resource_fields(self.resource)
        permitted = set(names) | {'limit', 'marker', 'sort_key', 'sort_dir',
                                  'fields', 'all_projects', 'project_id'}
        validation.parameters(params, permitted)
        all_projects = params.pop('all_projects', 'false')
        validation.choice('all_projects', all_projects, ('true', 'false'))
        all_projects = all_projects == 'true'
        if all_projects or 'project_id' in params:
            policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
            all_projects = True
            ctx.is_admin = True
        limit = validation.limit(params.pop('limit', cfg.CONF.api.max_limit))
        limit = min(limit, cfg.CONF.api.max_limit)
        marker = params.pop('marker', None)
        if marker and self.stack_id:
            marker_row = self.object_class.get_by_id(
                ctx, marker, all_projects=all_projects)
            if marker_row.stack_id != self.stack_id:
                raise exception.NotFound()
        sort_key = params.pop('sort_key', 'created_at')
        sort_dir = params.pop('sort_dir', 'asc')
        fields = params.pop('fields', None)
        if fields:
            self.validate_fields(fields)
        for key in ('created_at', 'updated_at', 'object_expires_at'):
            if key in params:
                try:
                    params[key] = datetime.datetime.fromisoformat(
                        params[key].replace('Z', '+00:00'))
                except ValueError:
                    raise validation.filter_value(
                        key, params[key], 'an ISO 8601 timestamp') from None
        sort_keys = names + (('created_at',) if self.resource == 'export'
                             else ())
        validation.choice('sort key', sort_key, sort_keys)
        validation.choice('sort direction', sort_dir, ('asc', 'desc'))
        if self.stack_id:
            params['stack_id'] = self.stack_id
        rows = self.list_class.get_all(
            ctx, filters=params, limit=limit, marker=marker,
            sort_key=sort_key, sort_dir=sort_dir, all_projects=all_projects)
        links = []
        if limit and len(rows) == limit:
            more = self.list_class.get_all(
                ctx, filters=params, limit=1, marker=rows[-1].id,
                sort_key=sort_key, sort_dir=sort_dir,
                all_projects=all_projects)
            if more:
                query = dict(pecan.request.params)
                query.update(marker=rows[-1].id, limit=limit)
                links = [{'rel': 'next', 'href': url(self.path()) + '?' +
                          parse.urlencode(query)}]
        extra = (inventory(rows, all_projects)
                 if self.resource == 'stack' else {})
        return {self.resource + 's': [
            self.render(row, extra.get(row.id))
            if self.resource == 'stack' else self.render(row)
            for row in rows],
            'links': links}


def next_server_name(ctx, stack_id, role):
    """Return the next per-role name, such as worker-3 (Q152).

    Existing names are never changed. The number is one more than the
    highest live <role>-N in the stack, so it cannot collide with them.
    """
    pattern = re.compile(r'^%s-(\d+)$' % re.escape(role))
    used = [int(match.group(1)) for match in
            (pattern.match(server.name or '') for server in
             objects.ServerList.get_all(ctx, filters={'stack_id': stack_id}))
            if match]
    return '%s-%d' % (role, max(used, default=0) + 1)


class Servers(Collection):
    resource = 'server'
    nest = 'servers'
    object_class = objects.Server
    list_class = objects.ServerList


def release_stuck_failover(ctx, stack, row):
    """Release an expired admin retry claim while holding resource locks."""
    stuck = (
        row.status == states.FAILOVER and
        row.task_state == states.FAILING_OVER and
        row.updated_at is not None and
        (timeutils.utcnow() -
         timeutils.normalize_time(row.updated_at)).total_seconds() >
        cfg.CONF.health_manager.admin_failover_stuck_timeout)
    if stuck:
        peers = objects.ServerList.get_all(ctx, filters={
            'stack_id': stack.id,
            'task_state': states.FAILING_OVER})
        if (stack.task_state == states.FAILING_OVER and
                all(peer.id == row.id for peer in peers)):
            stack.release_task(states.FAILING_OVER)
        row.release_task(states.FAILING_OVER)
    return stuck


class Item:
    def __init__(self, collection, id):
        self.collection = collection
        self.id = id
        if collection.resource == 'stack':
            from coast.api.v1 import ingress_controllers
            self.ingress_rules = ingress_controllers.IngressRules(id)
            self.events = StackEvents(id)
            self.servers = Servers(id, collection.handler)
            self.volumes = Volumes(id, collection.handler)
            from coast.api.v1 import storage_controllers
            self.storages = storage_controllers.Storages(
                id, collection.handler)
            from coast.api.v1 import export_controllers
            self.exports = export_controllers.Exports(id, collection.handler)
            from coast.api.v1 import appgroup_controllers
            self.appgroups = appgroup_controllers.StackAppgroups(
                id, collection.handler)

    def get(self):
        self.collection.parent()
        row = self.collection.object_class.get_by_id(
            context(), self.id,
            all_projects=read_all_projects(self.collection.resource))
        if (self.collection.stack_id and
                row.stack_id != self.collection.stack_id):
            raise exception.NotFound()
        return row

    def response(self, row):
        c = self.collection
        extra = (inventory([row], read_all_projects())
                 if c.resource == 'stack' and pecan.request.method == 'GET'
                 else {})
        return {c.resource: render(row, c.resource, c.path(row.id),
                                   extra.get(row.id))}

    def transition(self, row, status, task):
        states.check_transition(self.collection.resource, row.status, status)
        row.acquire_task(task)
        states.check_transition(self.collection.resource, row.status, status)
        row.status = status
        row.save()

    @pecan.expose('json')
    def index(self, **kwargs):
        row = self.get()
        c = self.collection
        method = pecan.request.method
        if method == 'GET':
            authorize(c.resource, 'get_one', row)
            return self.response(row)
        if method == 'PATCH' and c.resource == 'stack':
            ctx = authorize(c.resource, 'update', row)
            from coast.api.v1 import dns_controllers
            values = schemas.body(dns_controllers.schema(
                schemas.STACK_UPDATE, 'stack'), 'stack')
            dns_controllers.stack_link(ctx, values, row)
            with db_api.writer.using(ctx):
                for key, value in values.items():
                    setattr(row, key, value)
                row.save()
            notifications.completed(ctx, row, 'update')
            return self.response(row)
        if method != 'DELETE':
            pecan.abort(405)
        ctx = authorize(c.resource, 'delete', row)
        options = {}
        if c.resource == 'stack':
            options = schemas.body(schemas.STACK_DELETE, optional=True)
            # snapshot_retention_days still requires snapshot_volumes.
            # export_before_delete is allowed alone or with snapshots.
            if ('snapshot_retention_days' in options and
                    not options.get('snapshot_volumes')):
                raise exception.Invalid()
            options.setdefault('snapshot_retention_days',
                               cfg.CONF.export.snapshot_retention_days)
            if (options.get('snapshot_retention_days', 0) >
                    cfg.CONF.export.max_snapshot_retention_days):
                raise exception.Invalid()
        elif row.role == 'control':
            raise exception.Invalid()
        elif pecan.request.body:
            schemas.body(schemas.document({}))
        cancelled = None
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            stack = volume_delete.lock_stack(
                ctx, row.id if c.resource == 'stack' else row.stack_id)
            if c.resource == 'stack':
                stack_delete.require_empty(ctx, stack)
            if (c.resource == 'stack' and
                    handlers.abandon_stack_create(
                        ctx, stack.id, notify=False)):
                row = self.get()
                stack = row
                cancelled = notifications.payload_for(row, ctx)
            if stack.task_state != states.NONE:
                raise exception.Conflict()
            if c.resource == 'server' and objects.ServiceList.get_all(
                    ctx, filters={'server_id': row.id}, limit=1):
                raise exception.Conflict()
            if c.resource == 'server':
                if row.status not in _REMOVABLE_SERVER_STATUSES:
                    raise exception.Conflict()
                # The task is already DELETING. Release it so this request
                # can take the lock again and cast a new revision.
                if (row.status == states.DELETING and
                        row.task_state == states.DELETING):
                    row.release_task(states.DELETING)
            self.transition(row, states.DELETING, states.DELETING)
            if c.resource == 'stack':
                services_idle(ctx, row.id)
        if cancelled is not None:
            notifications.emit(ctx, notifications.API, 'environment', 'create',
                               'error', cancelled)
        if c.resource == 'stack':
            c.handler.delete_stack(ctx, row, options)
        else:
            c.handler.delete_server(ctx, row)
        pecan.response.status = 202
        return self.response(row)

    @pecan.expose('json')
    def action(self, **kwargs):
        if pecan.request.method != 'POST':
            pecan.abort(405)
        request_ctx = context()
        admin = False
        if self.collection.resource == 'stack':
            try:
                policy.authorize(request_ctx, 'ADMIN', {
                    'project_id': request_ctx.project_id})
                admin = True
            except exception.NotAuthorized:
                pass
        if admin:
            data = request_ctx.to_dict()
            data['is_admin'] = True
            admin_ctx = context_module.RequestContext.from_dict(data)
            row = objects.Stack.get_by_id(
                admin_ctx, self.id, all_projects=True)
        else:
            row = self.get()
        c = self.collection
        from coast.api.v1 import resize
        if resize.requested():
            return resize.accept(self, row)
        if c.resource == 'stack':
            values = schemas.body({'oneOf': [schemas.document({
                'upgrade': schemas.document({'image_id': schemas.text(36)},
                                            ('image_id',))}, ('upgrade',)),
                schemas.document({'upgrade_rollback': schemas.document({})},
                                 ('upgrade_rollback',))]})
            action = next(iter(values))
        else:
            action = 'failover'
            values = schemas.body(schemas.document(
                {action: schemas.document({})}, (action,)))
        ctx = authorize(c.resource, action, row)
        if c.resource == 'stack' and ctx.project_id != row.project_id:
            data = ctx.to_dict()
            data['project_id'] = row.project_id
            ctx = context_module.RequestContext.from_dict(data)
            row = objects.Stack.get_by_id(ctx, row.id)
        if c.resource == 'stack' and action == 'upgrade':
            engine_upgrade.eligible(cfg.CONF, row, values[action]['image_id'])
        if (c.resource == 'server' and row.role == 'control' and
                row.kind == 'vm'):
            preview = objects.Stack.get_by_id(ctx, row.stack_id)
            if preview.driver != 'noop':
                # Glance stays outside the project lock. A missing recorded
                # image is 409 and does not change the server.
                controller_failover.ensure_image(ctx, preview, row, cfg.CONF)
        dispatch_options = {}
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            if c.resource == 'server' and row.role == 'control':
                controller_failover.lock_workloads(ctx, row.stack_id)
            stack = volume_delete.lock_stack(
                ctx, row.id if c.resource == 'stack' else row.stack_id)
            if c.resource == 'server':
                # Recheck age and ownership under project/stack/server locks.
                row = objects.Server.obj_from_db(
                    ctx, db_api.servers._get(ctx, row.id, lock=True))
                stuck = release_stuck_failover(ctx, stack, row)
            if stack.task_state != states.NONE:
                raise exception.Conflict()
            if c.resource == 'stack':
                row = stack  # Recheck the locked row, not the pre-lock copy.
                rollback = action == 'upgrade_rollback'
                allowed = (states.ACTIVE, states.ERROR) if rollback else (
                    states.ACTIVE,)
                if row.status not in allowed:
                    raise exception.Conflict()
                engine_upgrade.idle(ctx, row)
                if rollback:
                    engine_upgrade.rollback_point(row)
                admission_status = row.status
                state = states.ROLLING_BACK if rollback else states.UPGRADING
                self.transition(row, state, state)
                if not rollback:
                    retained = (row.upgrade_checkpoint or {}).get(
                        'retained_snapshot_ids', [])
                    row.upgrade_checkpoint = {
                        'image_id': values[action]['image_id'],
                        'retained_snapshot_ids': retained}
                checkpoint = dict(row.upgrade_checkpoint or {})
                checkpoint['admission_status'] = admission_status
                row.upgrade_checkpoint = checkpoint
                row.save()
            else:
                if (row.status not in (states.ACTIVE, states.UNHEALTHY,
                                       states.ERROR) and not stuck):
                    raise exception.Conflict()
                dispatch_options['admission_status'] = row.status
                self.transition(row, states.FAILOVER, states.FAILING_OVER)
                failover_services_idle(ctx, stack, row)
        dispatch = getattr(c.handler, action + '_' + c.resource)
        dispatch(ctx, row, **dispatch_options)
        pecan.response.status = 202
        return self.response(row)


class Volumes(Collection):
    resource = 'volume'
    nest = 'volumes'
    object_class = objects.Volume
    list_class = objects.VolumeList

    @pecan.expose()
    def _lookup(self, id, *remainder):
        return VolumeItem(self, id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        self.parent()
        method = pecan.request.method
        if method == 'GET':
            return self.list()
        if method != 'POST':
            pecan.abort(405)
        ctx = authorize(self.resource, 'create')
        values = dict(schemas.body(schemas.VOLUME, 'volume'))
        values.setdefault('mount_path', '/data')
        values.setdefault('purpose', DATA_PURPOSE)
        if values['purpose'] != DATA_PURPOSE:
            raise exception.Invalid()
        data = {key: values[key] for key in (
            'name', 'size_gb', 'server_id', 'volume_type', 'mount_path',
            'purpose') if key in values}
        data['stack_id'] = self.stack_id
        row = self.object_class(
            context=ctx, status=states.CREATING, **data)
        with db_api.writer.using(ctx):
            quota_hook(ctx, self.resource, values)
            stack = volume_delete.lock_stack(ctx, self.stack_id)
            if stack.task_state != states.NONE:
                raise exception.Conflict()
            row.create()
            volume_start(ctx, row, 'create')
        self.handler.create_volume(ctx, row, values)
        pecan.response.status = 202
        return {self.resource: render(row, self.resource, self.path(row.id))}


class VolumeItem(Item):
    def _reject_managed(self, row):
        if row.purpose != DATA_PURPOSE:
            raise exception.Invalid()

    @pecan.expose('json')
    def index(self, **kwargs):
        row = self.get()
        method = pecan.request.method
        if method == 'GET':
            authorize('volume', 'get_one', row)
            return self.response(row)
        if method == 'PATCH':
            return self._extend(row)
        if method != 'DELETE':
            pecan.abort(405)
        return self._delete(row)

    def _extend(self, row):
        ctx = authorize('volume', 'update', row)
        values = schemas.body(schemas.VOLUME_EXTEND, 'volume')
        self._reject_managed(row)
        with db_api.writer.using(ctx):
            quota.reserve(ctx)  # Project, then stack, then volume.
            stack = volume_delete.lock_stack(ctx, row.stack_id)
            if stack.task_state != states.NONE:
                raise exception.Conflict()
            current = db_api.volumes._get(ctx, row.id, lock=True)
            delta = values['size_gb'] - current.size_gb
            if delta <= 0:
                raise exception.Invalid()
            quota_hook(ctx, 'volume', {'size_gb': delta})
            row.extend_to(values['size_gb'])
            volume_start(ctx, row, 'extend')
        self.collection.handler.extend_volume(ctx, row)
        pecan.response.status = 202
        return self.response(row)

    def _delete(self, row):
        ctx = authorize('volume', 'delete', row)
        if pecan.request.body:
            schemas.body(schemas.document({}))
        self._reject_managed(row)
        volume_delete.accept(ctx, row)
        self.collection.handler.delete_volume(ctx, row)
        pecan.response.status = 202
        return self.response(row)

    @pecan.expose('json')
    def action(self, **kwargs):
        if pecan.request.method != 'POST':
            pecan.abort(405)
        row = self.get()
        ctx = authorize('volume', 'snapshot', row)
        schemas.body(schemas.document(
            {'snapshot': schemas.document({})}, ('snapshot',)))
        if row.purpose not in SNAPSHOT_PURPOSES:
            raise exception.Invalid()
        with db_api.writer.using(ctx) as session:
            db_api._lock_project(session, ctx.project_id)
            stack = volume_delete.lock_stack(ctx, row.stack_id)
            if stack.task_state != states.NONE:
                raise exception.Conflict()
            row.transition_status(states.SNAPSHOTTING)
        self.collection.handler.snapshot_volume(ctx, row)
        pecan.response.status = 202
        return self.response(row)


class StackEvents:
    def __init__(self, stack_id):
        self.stack_id = stack_id

    @pecan.expose('json')
    def index(self, **kwargs):
        if not microversions.at_least(pecan.request.version, '1.8'):
            pecan.abort(404)
        if pecan.request.method != 'GET':
            pecan.abort(405)
        ctx = context()
        all_projects = read_all_projects()
        stack = objects.Stack.get_by_id(
            ctx, self.stack_id, all_projects=all_projects)
        authorize('stack', 'events:get', stack)
        params = dict(pecan.request.params)
        if admin_inventory():
            params.pop('all_projects', None)
            params.pop('project_id', None)
        validation.parameters(params, {'limit', 'marker', 'resource_type',
                                       'outcome'})
        limit = validation.limit(params.get('limit', 50), minimum=1)
        limit = min(limit, 200)
        include_console = microversions.at_least(
            pecan.request.version, '1.9')
        allowed = (('stack', 'server', 'app', 'service') if include_console
                   else ('stack', 'server'))
        filters = {'stack_id': stack.id, '_stack_timeline': True}
        if include_console:
            filters['_include_console'] = True
        kind = params.get('resource_type')
        if kind is not None:
            validation.choice('resource_type', kind, allowed)
            filters['resource_type'] = kind
        if 'outcome' in params:
            validation.choice('outcome', params['outcome'],
                              validation.EVENT_OUTCOMES)
            filters['outcome'] = params['outcome']
        marker = params.get('marker')
        if marker:
            row = objects.Event.get_by_id(
                ctx, marker, all_projects=all_projects)
            if (row.stack_id != stack.id or
                    row.resource_type not in allowed or
                    (kind and row.resource_type != kind)):
                raise exception.NotFound()
        rows = objects.EventList.get_all(
            ctx, filters=filters, limit=limit, marker=marker,
            sort_key='created_at', sort_dir='desc',
            all_projects=all_projects)
        links = []
        if rows and objects.EventList.get_all(
                ctx, filters=filters, limit=1, marker=rows[-1].id,
                sort_key='created_at', sort_dir='desc',
                all_projects=all_projects):
            params = dict(pecan.request.params, limit=limit,
                          marker=rows[-1].id)
            links = [{'rel': 'next', 'href': url('/%s/events' % stack.id) +
                      '?' + parse.urlencode(params)}]
        names = ('id', 'stack_id', 'resource_type', 'resource_id', 'action',
                 'outcome', 'message', 'detail', 'request_id', 'created_at')
        events = []
        for row in rows:
            event = {name: getattr(row, name) for name in names}
            event['created_at'] = row.created_at.isoformat().replace(
                '+00:00', 'Z')
            events.append(event)
        return {'events': events, 'links': links}
