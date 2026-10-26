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

"""Project-scoped, secret-filtered container observations."""

from coast.common import engine_resources
from coast.common import exception
from coast.conductor import agent_client
from coast.drivers import base
from coast import objects


FIELDS = ('id', 'name', 'image', 'state', 'health', 'restart_count')


def read(manager, ctx, service_id, container=None, logs=False, options=None):
    service = objects.Service.get_by_id(ctx, service_id)
    source = objects.Source.get_by_id(ctx, service.source_id)
    if not service.is_engine_service and source.build_pack != 'compose':
        raise exception.Conflict('Container view requires a compose service.')
    if not service.engine_ref or not service.server_id:
        raise base.EngineUnreachable()
    try:
        server = objects.Server.get_by_id(ctx, service.server_id)
    except exception.NotFound:
        raise base.EngineUnreachable() from None
    if (server.stack_id != service.stack_id or server.role != 'worker' or
            not server.mgmt_ip):
        raise base.EngineUnreachable()
    body = dict(project_id=service.project_id,
                resource_type=engine_resources.resource_type(service),
                resource_uuid=service.engine_ref, logs=logs)
    if container is not None:
        body['container'] = container
    body.update({key: value for key, value in (options or {}).items()
                 if key in ('tail', 'since')})
    result = agent_client.AgentClient(manager.conf, server.mgmt_ip).request(
        'GET', '/v1/containers', body)
    if result.get('not_found'):
        # Return a sentinel: unknown container must remain 404 across RPC.
        return None
    variables = manager._log_masks(ctx, service)
    hidden = {key for key, var in variables.items() if var.is_secret}
    observed = (result.get('environment', {}) if logs else
                result.get('container', {}).get('environment', {}))
    values = {var.value for var in variables.values()
              if var.is_secret and var.value}
    values.update(observed[key] for key in hidden if observed.get(key))
    values = sorted(values, key=len, reverse=True)

    def mask(value):
        for secret in values:
            value = value.replace(secret, '***')
        return value

    if logs:
        return {'lines': [{
            'ts': line['ts'], 'stream': line['stream'],
            'line': mask(line['line'])} for line in result['lines']]}
    if container is None:
        return {'containers': [{key: item[key] for key in FIELDS}
                               for item in result['containers']]}
    item = result['container']
    view = {key: item[key] for key in FIELDS}
    view['environment'] = {
        key: '***' if key in hidden else mask(value)
        for key, value in item['environment'].items()
        if not key.startswith(('COOLIFY_', 'COAST_'))}
    view['limits'] = {key: item['limits'][key]
                      for key in ('cpu', 'memory_bytes')}
    return {'container': view}
