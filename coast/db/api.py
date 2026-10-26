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

import contextlib
import contextvars
import functools

from oslo_config import cfg
from oslo_db import api as db_api
from oslo_db import exception as db_exception
from oslo_db.sqlalchemy import enginefacade
from oslo_log import log as logging
from oslo_utils import timeutils
import sqlalchemy as sa

from coast.common import context as context_module
from coast.common import exception
from coast.common import states
from coast.db import models
from coast.db import secret_pruning


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

_CONTEXT = enginefacade.transaction_context()
_CONTEXT.configure(sqlite_fk=True)
reader = _CONTEXT.reader
writer = _CONTEXT.writer


def get_engine():
    return writer.get_engine()


def get_session():
    return writer.get_sessionmaker()()


def _scope(query, model, context, all_projects=False, show_deleted=False):
    if all_projects:
        if not context.is_admin:
            raise exception.NotAuthorized()
    else:
        if not context.project_id:
            raise exception.NotAuthorized()
        query = query.filter(model.project_id == context.project_id)
    if hasattr(model, 'deleted_at') and not show_deleted:
        query = query.filter(model.deleted_at.is_(None))
    return query


def _lock_query(session, query, model):
    # SQLite ignores FOR UPDATE. Reserve its database write lock before reads.
    if session.bind.dialect.name == 'sqlite':
        query.update({model.project_id: model.project_id,
                      model.updated_at: model.updated_at},
                     synchronize_session=False)
    return query.populate_existing().with_for_update()


def _lock_project(session, project_id):
    query = session.query(models.QuotaUsage).filter_by(project_id=project_id)
    row = _lock_query(session, query, models.QuotaUsage).first()
    if row is None:
        try:
            # A savepoint keeps a racing insert from aborting the transaction.
            with session.begin_nested():
                session.add(models.QuotaUsage(project_id=project_id))
                session.flush()
        except db_exception.DBDuplicateEntry:
            pass
        row = query.populate_existing().with_for_update().one()
    return row


_RETRY_TRANSACTION = contextvars.ContextVar(
    'coast_retry_transaction', default=None)


def retry_transaction(fn):
    """Retry a complete transaction; nested calls defer to its owner."""
    @functools.wraps(fn)
    def attempt(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except db_exception.DBDeadlock:
            # oslo.db logs retries, including the final exception.
            raise db_exception.DBDeadlock() from None

    retry = db_api.wrap_db_retry(retry_on_deadlock=True, max_retries=3,
                                 retry_interval=0.1)(attempt)

    @functools.wraps(fn)
    def wrapped(context, *args, **kwargs):
        if _RETRY_TRANSACTION.get() is context:
            return fn(context, *args, **kwargs)
        try:
            context.session
        except (db_exception.NoEngineContextEstablished,
                db_exception.ContextNotRequestedError):
            pass
        else:
            return fn(context, *args, **kwargs)
        token = _RETRY_TRANSACTION.set(context)
        try:
            return retry(context, *args, **kwargs)
        except db_exception.DBDeadlock:
            raise exception.Conflict(
                'Another operation is in progress. Try again in a moment.'
            ) from None
        finally:
            _RETRY_TRANSACTION.reset(token)
    return wrapped


def retry_method(fn):
    """Apply transaction retry ownership to a context-taking method."""
    @functools.wraps(fn)
    def wrapped(self, context, *args, **kwargs):
        @retry_transaction
        def attempt(context):
            return fn(self, context, *args, **kwargs)
        return attempt(context)
    return wrapped


@contextlib.contextmanager
def _write(context):
    try:
        with writer.using(context):
            yield context.session
    except db_exception.DBDeadlock:
        # Let the transaction owner retry; never log SQL parameters.
        raise db_exception.DBDeadlock() from None
    except db_exception.DBDuplicateEntry:
        # Never expose SQL parameters (which can contain secret references).
        raise exception.Conflict() from None
    except (db_exception.DBReferenceError, db_exception.DBDataError,
            sa.exc.StatementError):
        raise exception.Invalid() from None
    except db_exception.DBError:
        raise exception.CoastException() from None


DUPLICATE_NAMES = {'Stack': ('A stack', 'this project'),
                   'Appgroup': ('An app group', 'this stack'),
                   'Environment': ('An environment', 'this app group'),
                   'Service': ('An app or service', 'this stack'),
                   'Storage': ('A storage volume', 'this stack')}


def _duplicate_message(model, values, existing=None):
    """Return a friendly 409 message for a duplicate name."""
    if model.__name__ == 'Server':
        return 'This stack already has a Coolify VM.'
    label, scope = DUPLICATE_NAMES[model.__name__]
    name = ' '.join(str(values.get('name', '')).split())[:100]
    if model.__name__ == 'Service' and values.get('kind') == 'database':
        return ('A database or app named %s already exists in this stack. '
                'Choose a different name.' % name)
    message = '%s named %s already exists in %s.' % (label, name, scope)
    if model.__name__ == 'Environment' and existing is not None:
        message += (' Existing environment ID: %s (status %s).'
                    ' Use the existing environment or choose a different name.'
                    % (existing.id, existing.status))
    if existing is not None and existing.status == states.DELETING:
        message += ' Deletion is still in progress; retry after it completes.'
    return message


class Repository:
    """Project-scoped ORM operations for one resource.

    Returned rows have loaded scalar fields. Callers authorize actions and
    emit notifications; this layer only persists state. all_projects requires
    both an admin context and an explicit opt-in, including on writes.
    """

    def __init__(self, model, resource=None):
        self.model = model
        self.resource = resource

    def _query(self, context, all_projects=False, show_deleted=False):
        return _scope(context.session.query(self.model), self.model, context,
                      all_projects, show_deleted)

    def _get(self, context, id, all_projects=False, show_deleted=False,
             lock=False):
        query = self._query(context, all_projects, show_deleted).filter_by(
            id=id)
        if lock:
            query = _lock_query(context.session, query, self.model)
        row = query.first()
        if row is None:
            raise exception.NotFound()
        return row

    def get(self, context, id, all_projects=False, show_deleted=False):
        with reader.using(context):
            return self._get(context, id, all_projects, show_deleted)

    def list(self, context, filters=None, marker=None, limit=None,
             sort_key='created_at', sort_dir='asc', all_projects=False,
             show_deleted=False):
        with reader.using(context):
            filters = dict(filters or {})
            exclude_databases = filters.pop('_exclude_databases', False)
            appgroup_id = (filters.pop('appgroup_id', None)
                           if self.model is models.Service else None)
            # Q148: kind=app (Coolify application) or kind=service
            # (one-click template), stored in the kind column.
            kind = (filters.pop('kind', None)
                    if self.model is models.Service else None)
            if kind is not None and kind not in ('app', 'service', 'database'):
                raise exception.Invalid()
            timeline = False
            include_console = False
            if self.model is models.Event:
                timeline = filters.pop('_stack_timeline', False)
                include_console = filters.pop('_include_console', False)
            columns = self.model.__table__.columns
            if (isinstance(appgroup_id, (dict, list, tuple, set)) or
                    sort_key not in columns or
                    sort_dir not in ('asc', 'desc') or
                    any(key not in columns for key in filters) or
                    any(isinstance(value, (dict, list, tuple, set))
                        for value in filters.values()) or
                    any(isinstance(columns[key].type, sa.JSON)
                        for key in [sort_key, *filters])):
                raise exception.Invalid()
            maximum = 200 if timeline else CONF.api.max_limit
            if limit is None:
                limit = maximum
            if (isinstance(limit, bool) or not isinstance(limit, int) or
                    limit < 0):
                raise exception.Invalid()
            limit = min(limit, maximum)
            query = self._query(context, all_projects, show_deleted)
            query = query.filter_by(**filters)
            if timeline:
                kinds = ('stack', 'server')
                if include_console:
                    kinds = ('stack', 'server', 'app', 'service')
                query = query.filter(
                    models.Event.resource_type.in_(kinds))
            if exclude_databases and self.model is models.Service:
                query = query.filter(self.model.kind != 'database')
            if kind is not None:
                query = query.filter(self.model.kind == kind)
            if appgroup_id is not None:
                parents = context.session.query(models.Environment.id).filter(
                    models.Environment.appgroup_id == appgroup_id,
                    models.Environment.project_id == self.model.project_id)
                query = query.filter(self.model.environment_id.in_(parents))
            marker_row = None
            if marker is not None:
                marker_row = self._get(context, marker, all_projects,
                                       show_deleted)
            if limit == 0:
                return []
            keys = [sort_key] if sort_key == 'id' else [sort_key, 'id']
            prefixes, criteria = [], []
            for key in keys:
                column = getattr(self.model, key)
                if isinstance(column.type, sa.Boolean):
                    column = sa.cast(column, sa.Integer)
                # Explicit NULL ordering is portable across SQLite and MySQL.
                order = sa.asc if sort_dir == 'asc' else sa.desc
                if columns[key].nullable:
                    query = query.order_by(order(column.is_not(None)))
                query = query.order_by(order(column))
                if marker_row is not None:
                    value = getattr(marker_row, key)
                    if isinstance(value, bool):
                        value = int(value)
                    if value is None:
                        after = (column.is_not(None) if sort_dir == 'asc'
                                 else sa.false())
                    else:
                        after = (column > value if sort_dir == 'asc' else
                                 sa.or_(column < value, column.is_(None)))
                    criteria.append(sa.and_(*prefixes, after))
                    prefixes.append(column == value)
            if marker_row is not None:
                query = query.filter(sa.or_(*criteria))
            return query.limit(limit).all()

    def _values(self, values, creating=False):
        values = dict(values)
        forbidden = {'created_at', 'updated_at', 'deleted_at'}
        if not creating:
            forbidden |= {'id', 'project_id', 'stack_id', 'service_id',
                          'appgroup_id', 'environment_id', 'task_state',
                          'task_owner', 'task_started_at'}
            if self.model is models.Storage:
                forbidden.discard('service_id')
        if (forbidden.intersection(values) or
                any(key not in self.model.__table__.columns
                    for key in values)):
            raise exception.Invalid()
        if creating:
            for column in self.model.__table__.columns:
                if (not column.nullable and column.default is None and
                        column.server_default is None and
                        column.name not in ('project_id', 'number') and
                        column.name not in values):
                    raise exception.Invalid()
        if self.model is models.Deployment:
            if creating and 'number' in values:
                raise exception.Invalid()
            if not creating and set(values) - {
                    'status', 'engine_ref', 'fault', 'log_ref', 'resolved_ref',
                    'started_at', 'finished_at', 'runtime_log'}:
                raise exception.Invalid()
        if (self.model is models.Server and 'role' in values and
                values['role'] not in ('control', 'worker', 'build')):
            raise exception.Invalid()
        if ('task_state' in values and
                values['task_state'] not in states.TASK_STATES):
            raise exception.Invalid()
        if self.model is models.Storage:
            if 'origin' in values and values['origin'] not in (
                    'api', 'template'):
                raise exception.Invalid()
            if not creating and set(values) - {
                    'service_id', 'server_id', 'container_path', 'status',
                    'name', 'host_path', 'read_only'}:
                raise exception.Invalid()
        if (self.model is models.ConsoleToken and
                'resource_type' in values and
                values['resource_type'] not in ('app', 'service')):
            raise exception.Invalid()
        if 'status' in values and self.resource:
            states.check_transition(self.resource, values['status'],
                                    values['status'])
        return values

    def _parents(self, context, values):
        # Check every FK against the row's owner, even for admin writes.
        owner = context_module.RequestContext(project_id=values['project_id'])
        parents = {}
        for column in self.model.__table__.columns:
            value = values.get(column.name)
            if value is None:
                continue
            for fk in column.foreign_keys:
                repo = _TABLES[fk.column.table.name]
                # Audit events can be recorded after stack offboarding.
                query = _scope(
                    context.session.query(repo.model), repo.model, owner,
                    show_deleted=self.model is models.Event)
                query = query.filter_by(id=value)
                parent = _lock_query(context.session, query,
                                     repo.model).first()
                if parent is None:
                    raise exception.NotFound()
                parents[column.name] = parent
        stack_id = values.get('stack_id')
        service = parents.get('service_id')
        if service is not None:
            stack_id = service.stack_id
        for key in ('server_id', 'volume_id'):
            parent = parents.get(key)
            if parent is not None and parent.stack_id != stack_id:
                raise exception.Invalid()
        if service is not None:
            for key in ('rollback_of', 'current_deployment_id'):
                parent = parents.get(key)
                if parent is not None and parent.service_id != service.id:
                    raise exception.Invalid()
            volume = parents.get('volume_id')
            if volume is not None and volume.server_id != service.server_id:
                raise exception.Invalid()
        if self.model is models.Export:
            for volume_id in values.get('volume_ids', []):
                query = _scope(context.session.query(models.Volume),
                               models.Volume, owner).filter_by(id=volume_id)
                volume = _lock_query(context.session, query,
                                     models.Volume).first()
                if volume is None:
                    raise exception.NotFound()
                if volume.stack_id != stack_id:
                    raise exception.Invalid()
        current = parents.get('current_deployment_id')
        if current is not None and current.service_id != values.get('id'):
            raise exception.Invalid()
        if self.model is models.Storage:
            service = parents.get('service_id')
            server = parents.get('server_id')
            if (service is not None and
                    service.stack_id != values.get('stack_id')):
                raise exception.Invalid()
            if (server is not None and service is not None and
                    server.id != service.server_id):
                raise exception.Invalid()
        return parents

    def _unique(self, context, values, id=None):
        filters = None
        if self.model is models.Stack:
            filters = dict(project_id=values['project_id'],
                           name=values['name'])
        elif self.model is models.Appgroup:
            filters = dict(stack_id=values['stack_id'],
                           name=values['name'])
        elif self.model is models.Environment:
            filters = dict(appgroup_id=values['appgroup_id'],
                           name=values['name'])
        elif self.model is models.Service:
            filters = dict(stack_id=values['stack_id'],
                           name=values['name'])
        elif self.model is models.Storage and values.get('name') is not None:
            filters = dict(stack_id=values['stack_id'],
                           name=values['name'])
        elif self.model is models.Server and values['role'] == 'control':
            filters = dict(stack_id=values['stack_id'],
                           role='control')
        if filters is not None:
            query = context.session.query(self.model).filter_by(**filters)
            query = query.filter(self.model.project_id == values['project_id'])
            if hasattr(self.model, 'deleted_at'):
                query = query.filter(self.model.deleted_at.is_(None))
            if id is not None:
                query = query.filter(self.model.id != id)
            # A current read also works under MySQL REPEATABLE READ.
            existing = query.with_for_update().first()
            if existing is not None:
                raise exception.Conflict(_duplicate_message(
                    self.model, values, existing))

    @retry_method
    def create(self, context, values, all_projects=False):
        values = self._values(values, creating=True)
        with _write(context) as session:
            # Validate scope before writing.
            self._query(context, all_projects)
            project_id = values.setdefault('project_id', context.project_id)
            if not project_id:
                raise exception.Invalid()
            if not all_projects and project_id != context.project_id:
                raise exception.NotFound()
            if self.model is models.Stack:
                _lock_project(session, project_id)
            self._parents(context, values)
            self._unique(context, values)
            if self.model is models.Deployment:
                # Parent service is already locked; include history in max+1.
                last = session.query(models.Deployment.number).filter_by(
                    project_id=project_id,
                    service_id=values['service_id'])
                last = last.order_by(models.Deployment.number.desc())
                last = last.with_for_update().first()
                values['number'] = (last[0] if last else 0) + 1
            row = self.model(**values)
            session.add(row)
            session.flush()
            return row

    @retry_method
    def update(self, context, id, values, all_projects=False):
        with _write(context) as session:
            row = self._get(context, id, all_projects,
                            lock=session.bind.dialect.name == 'sqlite')
            values = self._values(values)
            if self.model is models.Stack:
                _lock_project(session, row.project_id)
            data = {column.name: getattr(row, column.name)
                    for column in self.model.__table__.columns}
            data.update(values)
            self._parents(context, data)
            row = self._get(context, id, all_projects, lock=True)
            data = {column.name: getattr(row, column.name)
                    for column in self.model.__table__.columns}
            data.update(values)
            unique_fields = {
                models.Stack: ('project_id', 'name'),
                models.Appgroup: ('project_id', 'stack_id', 'name'),
                models.Environment: ('project_id', 'appgroup_id', 'name'),
                models.Service: ('project_id', 'stack_id', 'name'),
                models.Storage: ('project_id', 'stack_id', 'name'),
                models.Server: ('project_id', 'stack_id', 'role'),
            }.get(self.model, ())
            if any(key in values and values[key] != getattr(row, key)
                   for key in unique_fields):
                self._unique(context, data, id=id)
            # Cinder snapshot status is not a Coast state transition.
            if ('status' in values and
                    self.model is not models.StackDeleteSnapshot):
                states.check_transition(self.resource, row.status,
                                        values['status'])
            if (self.model is models.Service and
                    'status' in values and values['status'] != row.status):
                values.update(unhealthy_since=None, running_since=None)
            secret_pruning.track(session, row, values)
            for key, value in values.items():
                setattr(row, key, value)
            session.flush()
            return row

    @retry_method
    def delete(self, context, id, all_projects=False):
        with _write(context) as session:
            row = self._get(context, id, all_projects,
                            lock=session.bind.dialect.name == 'sqlite')
            if self.model is models.Stack:
                _lock_project(session, row.project_id)
            if self.model in (models.Service, models.Server,
                              models.Appgroup):
                query = session.query(models.Stack).filter_by(
                    id=row.stack_id, project_id=row.project_id)
                _lock_query(session, query, models.Stack).one()
            if self.model is models.Environment:
                query = session.query(models.Appgroup).filter_by(
                    id=row.appgroup_id, project_id=row.project_id)
                _lock_query(session, query, models.Appgroup).one()
            row = self._get(context, id, all_projects, lock=True)
            secret_pruning.track(session, row)
            if self.model is models.Service:
                session.query(models.ServiceMetric).filter_by(
                    service_id=row.id).delete(synchronize_session=False)
            if hasattr(row, 'deleted_at'):
                row.deleted_at = timeutils.utcnow()
            else:
                session.delete(row)
            session.flush()


class IngressRuleRepository(Repository):
    """Read complete stack desired state independently of API pagination."""

    def list_for_stack(self, context, stack_id):
        with reader.using(context):
            return self._query(context).filter_by(stack_id=stack_id).order_by(
                self.model.created_at, self.model.id).all()

    def get_for_stack(self, context, stack_id, rule_id):
        with reader.using(context):
            row = self._query(context).filter_by(
                stack_id=stack_id, id=rule_id).first()
            if row is None:
                raise exception.NotFound()
            return row


stacks = Repository(models.Stack, 'environment')
appgroups = Repository(models.Appgroup, 'appgroup')
environments = Repository(models.Environment, 'app_environment')
servers = Repository(models.Server, 'server')
volumes = Repository(models.Volume, 'volume')
sources = Repository(models.Source)
services = Repository(models.Service, 'app')
deployments = Repository(models.Deployment, 'deployment')
domains = Repository(models.Domain, 'domain')
secrets = Repository(models.Secret)
service_volume_mounts = Repository(models.AppVolumeMount)
exports = Repository(models.Export, 'export')
ingress_rules = IngressRuleRepository(models.IngressRule)
events = Repository(models.Event)
stack_delete_snapshots = Repository(models.StackDeleteSnapshot)
storages = Repository(models.Storage, 'storage')
file_mounts = Repository(models.FileMount, 'file_mount')
console_tokens = Repository(models.ConsoleToken)
backup_accounts = Repository(models.BackupAccount)
backup_policies = Repository(models.BackupPolicy)
backups = Repository(models.Backup, 'backup')
backup_jobs = Repository(models.BackupJob, 'backup_job')
_TABLES = {repo.model.__tablename__: repo for repo in (
    stacks, appgroups, environments, servers, volumes, sources, services,
    deployments, domains, secrets, service_volume_mounts, exports, events,
    stack_delete_snapshots, storages, console_tokens, file_mounts,
    ingress_rules,
    backup_policies, backups, backup_jobs, backup_accounts)}


def _console_view(row):
    return {
        'id': row.id,
        'project_id': row.project_id,
        'stack_id': row.stack_id,
        'resource_type': row.resource_type,
        'resource_id': row.resource_id,
        'command': row.command,
        'user_id': row.user_id,
        'expires_at': row.expires_at,
        'used_at': row.used_at,
    }


def find_console_token(token_hash):
    """Return token fields for one hash, or None. The caller does not log it.

    The lookup is by hash only. The session service has no project token.
    """
    context = context_module.RequestContext(is_admin=True)
    with reader.using(context) as session:
        row = session.query(models.ConsoleToken).filter_by(
            token_hash=token_hash).first()
        if row is None:
            return None
        return _console_view(row)


@db_api.wrap_db_retry(retry_on_deadlock=True)
def consume_console_token(token_hash, now):
    """Mark one unused, unexpired token used. Return its fields or None.

    Success is one updated row. A second caller gets None.
    """
    context = context_module.RequestContext(is_admin=True)
    with writer.using(context) as session:
        count = session.query(models.ConsoleToken).filter(
            models.ConsoleToken.token_hash == token_hash,
            models.ConsoleToken.used_at.is_(None),
            models.ConsoleToken.expires_at > now,
        ).update({'used_at': now}, synchronize_session=False)
        if count != 1:
            return None
        session.expire_all()
        row = session.query(models.ConsoleToken).filter_by(
            token_hash=token_hash).one()
        return _console_view(row)


@retry_transaction
def cas_task_state(context, model, id, new, expected=states.NONE,
                   all_projects=False, owner=None):
    repo = _TABLES.get(model.__tablename__)
    if (repo is None or not hasattr(model, 'task_state') or
            expected not in states.TASK_STATES or
            new not in states.TASK_STATES):
        raise exception.Invalid()
    with _write(context) as session:
        held = repo._get(context, id, all_projects,
                         lock=session.bind.dialect.name == 'sqlite')
        held_owner = getattr(held, 'task_owner', None)
        query = repo._query(context, all_projects).filter_by(
            id=id, task_state=expected)
        values = {'task_state': new}
        if hasattr(model, 'task_owner') and new != expected:
            if new == states.NONE:
                values.update(task_owner=None, task_started_at=None)
            elif expected == states.NONE:
                values.update(task_owner=owner or new,
                              task_started_at=timeutils.utcnow())
        if model is models.Stack and new == states.NONE:
            values['conductor_host'] = None
        if query.update(values, synchronize_session=False) != 1:
            current = repo._get(context, id, all_projects)
            raise exception.Conflict(_busy_message(model, current.task_state,
                                                   current))
        context.session.expire_all()
        row = repo._get(context, id, all_projects)
        if new != expected:
            LOG.debug('Task %s resource %s owner %s: %s -> %s',
                      model.__tablename__, id, getattr(
                          row, 'task_owner', None) or held_owner or expected,
                      expected, new)
        return row


BUSY_TASKS = {
    states.PROVISIONING: 'A deployment is in progress. Wait for it to '
                         'finish or cancel it.',
    states.DEPLOYING: 'A deployment is in progress. Wait for it to finish '
                      'or cancel it.',
    states.ROLLING_BACK: 'A rollback is in progress. Wait for it to finish '
                         'or cancel it.',
    states.DELETING: 'This %s is being deleted.',
}


def _busy_message(model, task, current=None):
    """Friendly 409 text for a held task lock."""
    label = {'services': 'app', 'stacks': 'stack'}.get(
        model.__tablename__, 'resource')
    text = BUSY_TASKS.get(task)
    if text:
        return text % label if '%s' in text else text
    owner = getattr(current, 'task_owner', None)
    started = getattr(current, 'task_started_at', None)
    labels = {'domain_check': 'Domain check', 'domain_change': 'Domain change',
              'settings_sync': 'Settings sync', 'reconciling': 'Settings sync',
              'restart': 'Restart', 'start': 'Start', 'stop': 'Stop',
              'deployment': 'Deployment', states.RECONCILING: 'Settings sync'}
    if owner in labels and started is not None:
        return ('%s in progress since %s UTC. Try again in a moment.' %
                (labels[owner], started.strftime('%H:%M')))
    return ('Another operation is in progress on this %s. Try again in a '
            'moment.' % label)


def release_task_state(context, model, id, expected, all_projects=False):
    return cas_task_state(context, model, id, new=states.NONE,
                          expected=expected, all_projects=all_projects)


def cas_status(context, model, id, new, expected, all_projects=False):
    """Compare-and-swap a status that has no task_state column.

    The caller must name the status it read. A forward edge is required.
    Same-state no-ops are rejected here so a repeated action cannot report
    success without a visible change.
    """
    repo = _TABLES.get(model.__tablename__)
    if repo is None or repo.resource is None or new == expected:
        raise exception.Invalid()
    states.check_transition(repo.resource, expected, new)
    with _write(context) as session:
        repo._get(context, id, all_projects,
                  lock=session.bind.dialect.name == 'sqlite')
        query = repo._query(context, all_projects).filter_by(
            id=id, status=expected)
        changed = query.update(
            {'status': new, 'updated_at': timeutils.utcnow()},
            synchronize_session=False)
        if changed != 1:
            raise exception.Conflict()
        context.session.expire_all()
        return repo._get(context, id, all_projects)


def set_volume_size(context, id, size_gb, all_projects=False):
    """Store a strictly larger volume size. Status is not a lock here."""
    if (isinstance(size_gb, bool) or not isinstance(size_gb, int) or
            size_gb < 1):
        raise exception.Invalid()
    with _write(context) as session:
        # lock=True uses FOR UPDATE. _lock_query reserves SQLite.
        row = volumes._get(context, id, all_projects, lock=True)
        if size_gb <= row.size_gb:
            raise exception.Invalid()
        row.size_gb = size_gb
        session.flush()
        return row


def update_status(context, model, id, status, all_projects=False):
    repo = _TABLES.get(model.__tablename__)
    if repo is None or repo.resource is None:
        raise exception.Invalid()
    return repo.update(context, id, {'status': status},
                       all_projects=all_projects)


def stack_resource_counts(context, stack_ids, all_projects=False):
    """Count live children for a page with two grouped, scoped queries."""
    counts = {id: {'server_count': 0, 'workload_count': 0}
              for id in stack_ids}
    if not counts:
        return counts
    with reader.using(context) as session:
        for model, key in ((models.Server, 'server_count'),
                           (models.Service, 'workload_count')):
            query = session.query(model.stack_id, sa.func.count(model.id))
            query = _scope(query, model, context, all_projects)
            for stack_id, count in query.filter(
                    model.stack_id.in_(stack_ids)).group_by(model.stack_id):
                counts[stack_id][key] = count
    return counts
