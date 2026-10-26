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

"""RPC handoff for M1 infrastructure and workload operations."""

from oslo_log import log as logging

from coast.common import exception
from coast.common import notifications
from coast.common import quota
from coast.common import states
from coast.conductor import rpcapi
from coast.db import api as db_api
from coast.db import models
from coast import objects


LOG = logging.getLogger(__name__)


DEFAULT_ENVIRONMENT = 'production'


class Handler:
    def __init__(self, rpc=None):
        self.rpc = rpc or rpcapi.ConductorAPI()

    @staticmethod
    def _accepted(context, resource, action, name=None):
        if getattr(resource, 'kind', None) == 'database':
            name = 'database'
        notifications.accepted(context, resource, action, name)

    def _cast(self, context, operation, resource):
        # Identifiers only. Options can carry plaintext env values.
        LOG.debug('%s accepted: %s', operation, resource.id)
        options = {}
        kind = states.kind(resource.repository.resource)
        if kind in ('service', 'deployment') or (kind == 'domain' and
                                                 operation != 'create_domain'):
            service_id = (resource.id if kind == 'service' else
                          resource.service_id)
            claim = getattr(context, '_task_claims', {}).get(
                ('service', service_id))
            if claim is not None:
                options['_task_claim'] = dict(claim)
        if options:
            self.rpc.cast(context, operation, resource, options)
        else:
            self.rpc.cast(context, operation, resource)

    def create_stack(self, context, stack, options):
        self._accepted(context, stack, 'create')
        try:
            self.rpc.cast(context, 'create_stack', stack, options)
        except Exception:
            abandon_stack_create(context, stack.id)
            raise

    def delete_stack(self, context, stack, options):
        self._accepted(context, stack, 'delete')
        self.rpc.cast(context, 'delete_stack', stack, options)

    def upgrade_stack(self, context, stack):
        self.rpc.cast(context, 'upgrade_stack', stack)

    def upgrade_rollback_stack(self, context, stack):
        self.rpc.cast(context, 'upgrade_rollback_stack', stack)

    def create_server(self, context, server, options):
        self._accepted(context, server, 'create')
        self.rpc.cast(context, 'create_server', server, options)

    def delete_server(self, context, server):
        self._accepted(context, server, 'delete')
        self.rpc.cast(context, 'delete_server', server)

    def failover_server(self, context, server, admission_status=None):
        self.rpc.cast(context, 'failover_server', server,
                      {'admission_status': admission_status})

    def create_volume(self, context, volume, options):
        self._accepted(context, volume, 'create')
        self.rpc.cast(context, 'create_volume', volume, options)

    def extend_volume(self, context, volume):
        self._accepted(context, volume, 'update')
        self.rpc.cast(context, 'extend_volume', volume)

    def delete_volume(self, context, volume):
        self._accepted(context, volume, 'delete')
        self.rpc.cast(context, 'delete_volume', volume)

    def snapshot_volume(self, context, volume):
        self.rpc.cast(context, 'snapshot_volume', volume)

    def create_service(self, context, service, options):
        self._accepted(context, service, 'create')
        self._cast(context, 'create_service', service)

    def update_service_env(self, context, service):
        self._cast(context, 'update_service', service)

    def rename_service(self, context, service):
        self._cast(context, 'rename_service', service)

    def delete_service(self, context, service):
        self._accepted(context, service, 'delete')
        self._cast(context, 'delete_service', service)

    def start_service(self, context, service):
        self._cast(context, 'start_service', service)

    def stop_service(self, context, service):
        self._cast(context, 'stop_service', service)

    def restart_service(self, context, service):
        self._cast(context, 'restart_service', service)

    def create_deployment(self, context, deployment, options):
        self._accepted(context, deployment, 'create')
        self._cast(context, 'create_deployment', deployment)

    def rollback_deployment(self, context, deployment):
        # Rollback stores a new deployment row. That row is a create.
        self._accepted(context, deployment, 'create')
        self._cast(context, 'rollback_deployment', deployment)

    def cancel_deployment(self, context, deployment):
        self._accepted(context, deployment, 'update')
        self._cast(context, 'cancel_deployment', deployment)

    def create_domain(self, context, domain, options):
        self._accepted(context, domain, 'create')
        self._cast(context, 'create_domain', domain)

    def delete_domain(self, context, domain):
        self._accepted(context, domain, 'delete')
        self._cast(context, 'delete_domain', domain)

    def verify_domain(self, context, domain):
        self._cast(context, 'verify_domain', domain)

    def update_domain(self, context, domain):
        self._cast(context, 'update_domain', domain)

    def deployment_log(self, context, deployment):
        service = objects.Service.get_by_id(context, deployment.service_id)
        if not self._live_logs(context, service):
            return {'deployment_id': deployment.id, 'complete': False,
                    'lines': []}
        return self.rpc.get_logs(context, 'deployment', deployment.id)

    def service_logs(self, context, service, options):
        if not self._live_logs(context, service):
            return {'lines': []}
        return self.rpc.get_logs(context, 'service', service.id, options)

    @staticmethod
    def _live_logs(context, service):
        env = objects.Stack.get_by_id(context, service.stack_id)
        return env.driver != 'noop'

    def create_export(self, context, export, options):
        self._accepted(context, export, 'create')
        self._cast(context, 'create_export', export)

    def delete_export(self, context, export):
        self._accepted(context, export, 'delete')
        self._cast(context, 'delete_export', export)

    def export_urls(self, context, export):
        """Return ephemeral artifact URLs; object storage follows in M3."""
        return []

    @staticmethod
    def _pending(context, cls, values, **parent):
        # The row must exist before the cast. task_state stays NONE.
        # The conductor sets ACTIVE after the engine accepts the row.
        data = dict(parent)
        data.update(name=values['name'], status=states.PENDING,
                    task_state=states.NONE)
        if 'description' in values:
            data['description'] = values['description']
        row = cls(context=context, **data)
        row.create()
        return row

    @staticmethod
    def _rename(context, row, values):
        row._context = context
        for key in ('name', 'description'):
            if key in values:
                setattr(row, key, values[key])
        row.save()
        return row

    def _mark_deleting(self, context, row, state_name):
        # Do not use the policy resource name here. ``environment`` is the
        # stack state alias. The Coolify environment key is app_environment.
        # Child and stack checks share this write so a child cannot appear
        # between the check and the DELETING status.
        stack_id, parent_id, child_model, child_column = self._delete_parents(
            context, row, state_name)
        with db_api.writer.using(context) as session:
            db_api._lock_project(session, context.project_id)
            stack = db_api.stacks._get(context, stack_id, lock=True)
            if parent_id is not None:
                parent = db_api.appgroups._get(
                    context, parent_id, lock=True)
                if parent.stack_id != stack.id:
                    raise exception.Conflict()
            current = row.repository._get(context, row.id, lock=True)
            self._require_deletable(
                context, current, stack, state_name, child_model,
                child_column)
            current.status = states.DELETING
            session.flush()
            return type(row).obj_from_db(context, current)

    @staticmethod
    def _delete_parents(context, row, state_name):
        if state_name == 'appgroup':
            return (row.stack_id, None, models.Environment, 'appgroup_id')
        parent = objects.Appgroup.get_by_id(context, row.appgroup_id)
        return (
            parent.stack_id, parent.id, models.Service, 'environment_id')

    def _require_deletable(self, context, current, stack, state_name,
                           child_model, child_column):
        # PENDING and DELETING are not deletable. ERROR is a retry.
        if current.status not in (states.ACTIVE, states.ERROR):
            raise exception.Conflict(
                'Cannot delete while the resource is %s; wait for the '
                'current operation to finish.' % current.status)
        if stack.status not in (states.ACTIVE, states.DEGRADED, states.ERROR):
            raise exception.Conflict(
                'Cannot delete while the stack is %s; wait for the '
                'current operation to finish.' % stack.status)
        if stack.task_state not in (states.NONE, states.RECONCILING):
            raise exception.Conflict(
                'Cannot delete while a stack operation is running.')
        if self._live_child(context, child_model, child_column, current.id):
            raise exception.Conflict(
                'Delete the remaining %s first.' % (
                    'environments' if state_name == 'appgroup' else 'apps'))
        states.check_transition(state_name, current.status, states.DELETING)

    @staticmethod
    def _live_child(context, model, column, parent_id):
        query = context.session.query(model).filter(
            model.project_id == context.project_id,
            getattr(model, column) == parent_id,
            model.deleted_at.is_(None))
        locked = db_api._lock_query(context.session, query, model)
        return locked.first() is not None

    def create_appgroup(self, context, stack, values):
        with db_api.writer.using(context) as session:
            db_api._lock_project(session, context.project_id)
            current = db_api.stacks._get(context, stack.id, lock=True)
            if (current.status != states.ACTIVE or
                    current.task_state not in (states.NONE,
                                               states.RECONCILING)):
                raise exception.Conflict()
            row = self._pending(context, objects.Appgroup, values,
                                stack_id=stack.id)
            # Q147: each appgroup starts with a production environment.
            self._pending(context, objects.Environment,
                          {'name': DEFAULT_ENVIRONMENT}, appgroup_id=row.id)
        self._cast(context, 'create_appgroup', row)
        return row

    def update_appgroup(self, context, row, values):
        return self._rename(context, row, values)

    def delete_appgroup(self, context, row):
        row = self._mark_deleting(context, row, 'appgroup')
        self._accepted(context, row, 'delete')
        self.rpc.delete_appgroup(context, row)
        return row

    def create_environment(self, context, appgroup, values):
        row = self._pending(context, objects.Environment, values,
                            appgroup_id=appgroup.id)
        self._cast(context, 'create_app_environment', row)
        return row

    def update_environment(self, context, row, values):
        return self._rename(context, row, values)

    def delete_environment(self, context, row):
        row = self._mark_deleting(context, row, 'app_environment')
        # The repository key is app_environment. Notifications use environment.
        self._accepted(context, row, 'delete', 'environment')
        self.rpc.delete_app_environment(context, row)
        return row


def record_stack_create(ctx, stack):
    """Persist the exact reservation before the request leaves the API."""
    objects.Event(
        context=ctx, id=rpcapi.receipt_id('create_stack', stack),
        stack_id=stack.id, resource_type='environment', resource_id=stack.id,
        action='create', outcome='start', request_id=ctx.request_id,
        detail={'quota_reservation': dict(ctx.quota_reservation)}).create()


def abandon_stack_create(ctx, stack_id, notify=True):
    """Cancel only an unclaimed create, serialized against conductor claim.

    A late cast sees the terminal receipt/status and cannot provision or
    settle the reservation again. Claimed work keeps its capacity and locks.
    """
    with db_api.writer.using(ctx) as session:
        db_api._lock_project(session, ctx.project_id)
        stack = objects.Stack.obj_from_db(
            ctx, db_api.stacks._get(ctx, stack_id, lock=True))
        if (stack.status != states.PENDING or stack.conductor_host or
                stack.task_state != states.PROVISIONING):
            return False
        states.check_transition('stack', stack.status, states.ERROR)
        receipts = objects.EventList.get_all(ctx, filters={
            'resource_id': stack.id, 'resource_type': 'environment',
            'action': 'create', 'outcome': 'start'})
        if not receipts:
            reservation = quota.recover_unclaimed_stack_reservation(
                ctx, stack.id)
            objects.Event(
                context=ctx, id=rpcapi.receipt_id('create_stack', stack),
                stack_id=stack.id, resource_type='environment',
                resource_id=stack.id, action='create', outcome='failure',
                message='Conductor handoff failed.',
                detail={'quota_reservation': reservation},
                request_id=ctx.request_id).create()
        for receipt in receipts:
            reservation = (receipt.detail or {}).get('quota_reservation')
            if reservation is not None:
                quota.rollback(ctx, reservation)
            receipt.outcome = 'failure'
            receipt.message = 'Conductor handoff failed.'
            receipt.save()
        stack.status = states.ERROR
        stack.fault = 'Conductor handoff failed.'
        stack.save()
        stack.release_task(states.PROVISIONING)
    if notify:
        notifications.emit(ctx, notifications.API, 'environment', 'create',
                           'error', notifications.payload_for(stack, ctx))
    return True
