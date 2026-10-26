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

"""Project quotas; completion callers own the resource transaction."""

from oslo_config import cfg

from coast.common import exception
from coast.common import states
from coast.db import api as db_api
from coast.db import models


RESOURCES = ('environments', 'servers', 'baremetal_servers', 'build_servers',
             'apps', 'domains', 'volume_gb', 'storage_gb')


def defaults():
    return {name: getattr(cfg.CONF.quotas, name) for name in RESOURCES}


def _project(context, project_id=None):
    project_id = project_id or context.project_id
    if not project_id or (project_id != context.project_id and
                          not context.is_admin):
        raise exception.NotAuthorized()
    return project_id


def _limits(session, project_id):
    return session.query(models.Quota).filter_by(
        project_id=project_id).populate_existing().with_for_update().first()


def _render(row, effective=True):
    result = defaults() if effective else dict.fromkeys(RESOURCES)
    if row:
        result.update({name: getattr(row, name) for name in RESOURCES
                       if not effective or getattr(row, name) is not None})
    return result


def get(context, project_id=None):
    project_id = _project(context, project_id)
    with db_api.reader.using(context):
        row = context.session.query(models.Quota).filter_by(
            project_id=project_id).first()
        return _render(row)


def get_all(context):
    if not context.is_admin:
        raise exception.NotAuthorized()
    with db_api.reader.using(context):
        rows = context.session.query(models.Quota).order_by(
            models.Quota.project_id).all()
        return [dict(project_id=row.project_id, **_render(row, False))
                for row in rows]


def _counters(row, prefix):
    if row is None:
        return dict.fromkeys(RESOURCES, 0)
    return {name: getattr(row, prefix + name) for name in RESOURCES}


def usage(context, project_id=None):
    """Return engine in-use and reserved counts. Do not create a row."""
    project_id = _project(context, project_id)
    with db_api.reader.using(context):
        row = context.session.query(models.QuotaUsage).filter_by(
            project_id=project_id).first()
        return {'in_use': _counters(row, 'in_use_'),
                'reserved': _counters(row, 'reserved_')}


def _validate(values, limits=False):
    if (not isinstance(values, dict) or set(values) - set(RESOURCES) or
            any(not (limits and value is None) and
                (isinstance(value, bool) or not isinstance(value, int) or
                 value < (-1 if limits else 0))
                for value in values.values())):
        raise exception.Invalid()


@db_api.retry_transaction
def update(context, project_id, values):
    project_id = _project(context, project_id)
    if not context.is_admin:
        raise exception.NotAuthorized()
    _validate(values, limits=True)
    with db_api._write(context) as session:
        db_api._lock_project(session, project_id)
        row = _limits(session, project_id)
        if row is None:
            row = models.Quota(project_id=project_id)
            session.add(row)
        for name, value in values.items():
            setattr(row, name, value)
        session.flush()
        return _render(row)


@db_api.retry_transaction
def reset(context, project_id):
    project_id = _project(context, project_id)
    if not context.is_admin:
        raise exception.NotAuthorized()
    with db_api._write(context) as session:
        db_api._lock_project(session, project_id)
        row = _limits(session, project_id)
        if row:
            session.delete(row)
        # Keep the sentinel and counters: resetting limits frees no capacity.


def _existing(session, project_id, name):
    model = {'environments': models.Stack, 'servers': models.Server,
             'baremetal_servers': models.Server,
             'build_servers': models.Server,
             'apps': models.Service, 'domains': models.Domain,
             'volume_gb': models.Volume,
             'storage_gb': models.Storage}[name]
    query = session.query(model).filter_by(project_id=project_id)
    if hasattr(model, 'deleted_at'):
        query = query.filter(model.deleted_at.is_(None))
    if name == 'environments':
        query = query.filter(model.status != 'DELETED')
    if model is models.Server:
        query = query.filter(model.role != 'control')
    if name == 'baremetal_servers':
        query = query.filter_by(kind='baremetal')
    if name == 'build_servers':
        query = query.filter_by(role='build')
    # Completion events distinguish rolled-back creates from live ERROR
    # resources whose earlier successful create still consumes capacity.
    failed_creates = session.query(models.Event.resource_id).filter_by(
        project_id=project_id, resource_type={
            models.Stack: 'environment', models.Server: 'server',
            models.Service: 'app', models.Domain: 'domain',
            models.Volume: 'volume', models.Storage: 'storage'}[model],
        action='create', outcome='failure')
    query = query.filter(model.id.not_in(failed_creates))
    rows = query.with_for_update().all()
    if name in ('volume_gb', 'storage_gb'):
        return sum(row.size_gb or 0 for row in rows)
    return len(rows)


@db_api.retry_transaction
def reserve(context, **deltas):
    """Reserve before resource writes, inside their writer transaction.

    The returned delta mapping is an internal completion token, not an ID.
    Call commit/rollback exactly once, under the completion state lock.
    """
    _validate(deltas)
    project_id = _project(context)
    deltas = {name: value for name, value in deltas.items() if value}
    with db_api._write(context) as session:
        usage = db_api._lock_project(session, project_id)
        limits = _render(_limits(session, project_id))
        exceeded = []
        baseline = {}
        for name, value in deltas.items():
            used = getattr(usage, 'in_use_' + name)
            held = getattr(usage, 'reserved_' + name)
            # Existing sentinels predate quota enforcement. Seed empty
            # counters from live rows; never count a held row a second time.
            baseline[name] = (used if used or held else
                              _existing(session, project_id, name))
            if (limits[name] != -1 and
                    baseline[name] + held + value > limits[name]):
                exceeded.append(name)
        if exceeded:
            raise exception.OverQuota(resources=sorted(exceeded))
        for name, value in deltas.items():
            setattr(usage, 'in_use_' + name, baseline[name])
            setattr(usage, 'reserved_' + name,
                    getattr(usage, 'reserved_' + name) + value)
        session.flush()
    return deltas


@db_api.retry_transaction
def _finish(context, deltas, commit):
    _validate(deltas)
    project_id = _project(context)
    with db_api._write(context) as session:
        usage = db_api._lock_project(session, project_id)
        if any(getattr(usage, 'reserved_' + name) < value
               for name, value in deltas.items()):
            raise exception.Conflict()
        for name, value in deltas.items():
            setattr(usage, 'reserved_' + name,
                    getattr(usage, 'reserved_' + name) - value)
            if commit:
                setattr(usage, 'in_use_' + name,
                        getattr(usage, 'in_use_' + name) + value)
        session.flush()


def recover_unclaimed_stack_reservation(context, stack_id):
    """Recover otherwise unowned stack capacity while the project is idle.

    Historical stack options were not persisted. Only use the outstanding
    counters when no other stack/server create or volume operation could
    own them. The caller holds the project sentinel and the pending stack
    lock through cancellation; new reservations cannot race this decision.
    Unrelated quota resources and other projects are never changed.
    """
    project_id = _project(context)
    with db_api._write(context) as session:
        usage = db_api._lock_project(session, project_id)
        stacks = db_api.stacks._query(context).filter(
            models.Stack.id != stack_id,
            (models.Stack.status.in_((states.PENDING, states.BUILDING))) |
            (models.Stack.task_state == states.PROVISIONING))
        servers = db_api.servers._query(context).filter(
            (models.Server.status.in_((states.PENDING, states.BUILDING))) |
            (models.Server.task_state == states.PROVISIONING))
        volumes = db_api.volumes._query(context).filter(
            models.Volume.status == states.CREATING)
        volume_holds = db_api.events._query(context).filter_by(
            resource_type='volume', outcome='start')
        if any(query.first() is not None for query in (
                stacks, servers, volumes, volume_holds)):
            raise exception.Conflict(
                'Unclaimed stack quota recovery requires other stack, '
                'server and volume operations to finish. Retry deletion.')
        reservation = {
            name: getattr(usage, 'reserved_' + name) for name in (
                'environments', 'servers', 'baremetal_servers',
                'build_servers', 'volume_gb')}
        rollback(context, reservation)
        return reservation


def commit(context, reservation):
    _finish(context, reservation, True)


def rollback(context, reservation):
    _finish(context, reservation, False)


@db_api.retry_transaction
def release(context, **deltas):
    """Release accounted capacity in the resource cleanup transaction."""
    _validate(deltas)
    project_id = _project(context)
    with db_api._write(context) as session:
        usage = db_api._lock_project(session, project_id)
        if any(getattr(usage, 'in_use_' + name) < value
               for name, value in deltas.items()):
            raise exception.Conflict()
        for name, value in deltas.items():
            setattr(usage, 'in_use_' + name,
                    getattr(usage, 'in_use_' + name) - value)
        session.flush()


def _create_in_flight(session, project_id):
    """True when a stack create may have committed quota before its rows."""
    model = models.Stack
    query = session.query(model.id).filter(
        model.project_id == project_id,
        model.deleted_at.is_(None),
        (model.status == states.BUILDING) |
        (model.task_state == states.PROVISIONING))
    return query.first() is not None


@db_api.retry_transaction
def reconcile(context, project_id=None):
    """Lower in-use counters to the live rows. Never raise them.

    Stack create commits quota before the child rows exist. Skip the
    recount while such a create is still in flight. Skip a resource that
    still has a reservation. A second call changes nothing.
    """
    project_id = _project(context, project_id)
    with db_api._write(context) as session:
        existing = session.query(models.QuotaUsage).filter_by(
            project_id=project_id).first()
        if existing is None:
            return
        usage = db_api._lock_project(session, project_id)
        if _create_in_flight(session, project_id):
            return
        for name in RESOURCES:
            if getattr(usage, 'reserved_' + name):
                continue
            live = _existing(session, project_id, name)
            used = getattr(usage, 'in_use_' + name)
            if used > live:
                setattr(usage, 'in_use_' + name, live)
        session.flush()


@db_api.retry_transaction
def release_exact_failed_volume_gb(context, amount):
    """Release volume_gb left by failed volumes, once.

    Call this after those volumes no longer count. Create failure
    events, or removed rows, are enough. Reconcile skips volume_gb
    while any volume reservation remains. Release only when in-use is
    exactly ``amount``, no volume row still counts, a reservation
    remains, and no stack create is in flight. A second call does
    nothing. Return True only when this call released the counter.
    """
    if not amount:
        return False
    _validate({'volume_gb': amount})
    project_id = _project(context)
    with db_api._write(context) as session:
        usage = db_api._lock_project(session, project_id)
        if _create_in_flight(session, project_id):
            return False
        if not getattr(usage, 'reserved_volume_gb'):
            return False
        if _existing(session, project_id, 'volume_gb'):
            return False
        if getattr(usage, 'in_use_volume_gb') != amount:
            return False
        release(context, volume_gb=amount)
    return True


@db_api.retry_transaction
def lower_volume_gb_to_live(context):
    """Lower in-use volume_gb to the live rows. Keep any reservation.

    Reconcile skips volume_gb while a volume reservation remains. A
    failed create can leave that counter charged after its volume rows
    are gone and delete has no cleanup receipt. Do not run while a
    stack create is in flight. A second call does nothing. Return True
    only when this call lowered the counter.
    """
    project_id = _project(context)
    with db_api._write(context) as session:
        usage = db_api._lock_project(session, project_id)
        if _create_in_flight(session, project_id):
            return False
        live = _existing(session, project_id, 'volume_gb')
        used = getattr(usage, 'in_use_volume_gb')
        if used <= live:
            return False
        setattr(usage, 'in_use_volume_gb', live)
        session.flush()
    return True


def deltas(resource, values):
    """Compute known create capacity without inventing plan sizes."""
    if resource == 'stack':
        workers = values.get('workers', [])
        build = int(values.get('build_server', False))
        return dict(environments=1, servers=len(workers) + build,
                    baremetal_servers=sum(w['kind'] == 'baremetal'
                                          for w in workers),
                    build_servers=build, volume_gb=values.get(
                        'data_volume_gb',
                        cfg.CONF.controller_worker.data_volume_size_gb))
    if resource == 'server':
        return dict(servers=int(values['role'] != 'control'),
                    baremetal_servers=int(values['kind'] == 'baremetal' and
                                          values['role'] != 'control'),
                    build_servers=int(values['role'] == 'build'))
    if resource == 'service':
        return {'apps': 1}
    if resource == 'domain':
        return {'domains': 1}
    if resource == 'volume':
        return {'volume_gb': values['size_gb']}
    if resource == 'storage':
        return {'storage_gb': values.get('size_gb') or 0}
    return {}


def check_export(context, size_gb):
    """Check separate export limits while the resource transaction is open."""
    project_id = _project(context)
    with db_api._write(context) as session:
        db_api._lock_project(session, project_id)
        pending = session.query(models.Export).filter_by(
            project_id=project_id).filter(models.Export.status.in_(
                ('PENDING', 'SNAPSHOTTING', 'UPLOADING')))
        pending = pending.with_for_update().all()
        exceeded = []
        if len(pending) >= cfg.CONF.export.max_concurrent_per_project:
            exceeded.append('exports')
        if size_gb > cfg.CONF.export.max_size_gb:
            exceeded.append('export_size_gb')
        if exceeded:
            raise exception.OverQuota(resources=sorted(exceeded))
