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

"""Move a pending appgroup or environment to ACTIVE on the engine."""

from oslo_log import log as logging

from coast.common import exception
from coast.common import states
from coast.drivers import base
from coast import objects


LOG = logging.getLogger(__name__)


def activate(manager, ctx, resource):
    """Set engine_ref and ACTIVE. A later cast does not repeat the work."""
    try:
        current = type(resource).get_by_id(ctx, resource.id)
    except exception.NotFound:
        return
    if current.status != states.PENDING:
        return
    try:
        if current.repository.resource == 'appgroup':
            _sync_appgroup(manager, ctx, current)
        else:
            _sync_environment(manager, ctx, current)
    except Exception:
        LOG.warning('Engine sync failed for %s', resource.id)
        _mark_error(manager, ctx, resource)


def _mark_error(manager, ctx, resource):
    try:
        fresh = type(resource).get_by_id(ctx, resource.id)
    except exception.NotFound:
        return
    if fresh.status != states.PENDING:
        return
    manager._save(fresh, status=states.ERROR)
    if fresh.repository.resource == 'appgroup':
        # Q147: the default environment cannot activate without the project;
        # ERROR lets the user delete it and then the appgroup.
        for child in objects.EnvironmentList.get_all(
                ctx, filters={'appgroup_id': fresh.id,
                              'status': states.PENDING}):
            manager._save(child, status=states.ERROR)


def _driver(manager, stack):
    if stack.status != states.ACTIVE:
        raise base.DriverError('Stack is not active')
    return manager._driver(stack), manager._endpoint(stack)


def _sync_appgroup(manager, ctx, appgroup):
    stack = objects.Stack.get_by_id(ctx, appgroup.stack_id)
    driver, endpoint = _driver(manager, stack)
    result = driver.ensure_project(endpoint, stack, appgroup)
    if not result.engine_ref:
        raise base.DriverError('Engine project reference is missing')
    manager._save(appgroup, engine_ref=result.engine_ref,
                  status=states.ACTIVE)
    # Q147: activate environments created with the appgroup (production).
    pending = objects.EnvironmentList.get_all(
        ctx, filters={'appgroup_id': appgroup.id, 'status': states.PENDING})
    for environment in pending:
        activate(manager, ctx, environment)


def _sync_environment(manager, ctx, environment):
    appgroup = objects.Appgroup.get_by_id(
        ctx, environment.appgroup_id)
    if appgroup.status in (
            states.DELETING, states.DELETED, states.ERROR):
        raise base.DriverError('Appgroup is not active')
    stack = objects.Stack.get_by_id(ctx, appgroup.stack_id)
    driver, endpoint = _driver(manager, stack)
    if not appgroup.engine_ref:
        if appgroup.status != states.PENDING:
            raise base.DriverError(
                'Engine project reference is missing')
        project = driver.ensure_project(endpoint, stack, appgroup)
        if not project.engine_ref:
            raise base.DriverError(
                'Engine project reference is missing')
        manager._save(appgroup, engine_ref=project.engine_ref,
                      status=states.ACTIVE)
        appgroup.engine_ref = project.engine_ref
    result = driver.ensure_environment(
        endpoint, stack, appgroup, environment)
    if not result.engine_ref:
        raise base.DriverError(
            'Engine environment reference is missing')
    manager._save(environment, engine_ref=result.engine_ref,
                  status=states.ACTIVE)
