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

"""Resolve a consumed console token to one worker management address."""

from oslo_utils import timeutils

from coast.common import context as context_module
from coast.common import engine_resources
from coast.common import exception
from coast.common import states
from coast.db import api as db_api
from coast import objects


def workload_available(service, server):
    """Return whether a shell can be opened on this workload."""
    if (service.status != states.ACTIVE or not service.server_id or
            not service.engine_ref):
        return False
    if server is None or server.role != 'worker' or not server.mgmt_ip:
        return False
    if server.stack_id != service.stack_id:
        return False
    return True


def _kind(service):
    return service.kind


def target_for(view, now=None):
    """Return session fields, or None when the grant cannot be used.

    ``view`` comes from the token row. It must not include the secret.
    """
    now = now or timeutils.utcnow()
    if (view['used_at'] is not None or view['expires_at'] <= now or
            view['resource_type'] not in ('app', 'service')):
        return None
    ctx = context_module.RequestContext(project_id=view['project_id'],
                                        user_id=view['user_id'])
    try:
        service = objects.Service.get_by_id(ctx, view['resource_id'])
        server = objects.Server.get_by_id(ctx, service.server_id)
    except exception.NotFound:
        return None
    if _kind(service) != view['resource_type']:
        return None
    if not workload_available(service, server):
        return None
    target = dict(view)
    target['address'] = server.mgmt_ip
    target['server_id'] = server.id
    # The agent finds the container by the app engine id, not the Coast id.
    target['engine_ref'] = service.engine_ref
    # Templates and pasted Compose run as engine services; Git and image
    # workloads run as engine applications. The agent looks up containers
    # by that creation identity, independently of the Coast workload kind.
    target['engine_type'] = engine_resources.resource_type(service)
    return target


def record(target, action, duration=None):
    """Write one timeline row. Never store keystrokes, output, or commands."""
    detail = {
        'user_id': target['user_id'],
        'resource_type': target['resource_type'],
        'resource_id': target['resource_id'],
    }
    if duration is not None:
        detail['duration'] = int(duration)
    ctx = context_module.RequestContext(project_id=target['project_id'],
                                        user_id=target['user_id'])
    objects.Event(
        context=ctx, stack_id=target['stack_id'],
        resource_type=target['resource_type'],
        resource_id=target['resource_id'], action=action,
        outcome='success', message=None, detail=detail).create()


def consume(token_hash, now=None):
    now = now or timeutils.utcnow()
    return db_api.consume_console_token(token_hash, now)
