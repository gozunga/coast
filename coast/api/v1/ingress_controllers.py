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

"""Project-owned worker firewall desired state."""

import copy
import ipaddress

from oslo_config import cfg
import pecan

from coast.api import microversions
from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers as stacks
from coast.common import database_public
from coast.common import exception
from coast.common import notifications
from coast.common import states
from coast.conductor import volume_delete
from coast.db import api as db
from coast import objects


FIELDS = ('id', 'protocol', 'port_range_min', 'port_range_max', 'source',
          'description')
SCHEMA = schemas.document({
    'protocol': {'enum': ['tcp', 'udp', 'icmp']},
    'port_range_min': {'type': 'integer', 'minimum': 1, 'maximum': 65535},
    'port_range_max': {'type': 'integer', 'minimum': 1, 'maximum': 65535},
    'source': schemas.text(18),
    'description': {'type': 'string', 'maxLength': 255}}, ('protocol',))


def validate(values):
    values = dict(values)
    try:
        values['source'] = str(ipaddress.IPv4Network(
            values.get('source', '0.0.0.0/0'), strict=False))
    except ValueError:
        raise exception.Invalid('Source must be an IPv4 CIDR.') from None
    low = values.get('port_range_min')
    high = values.get('port_range_max', low)
    if values['protocol'] == 'icmp':
        if low is not None or high is not None:
            raise exception.Invalid('ICMP rules cannot specify ports.')
    elif low is None or high is None or high < low:
        raise exception.Invalid('TCP and UDP require an ordered port range.')
    values.update(port_range_min=low, port_range_max=high)
    return values


def managed(ctx, stack_id):
    result = []
    servers = objects.ServerList.get_all(
        ctx, filters={'stack_id': stack_id, 'role': 'control'}, limit=1)
    controller = next((s.private_ip for s in servers
                       if s.role == 'control' and s.private_ip), None)
    for name, port, source in [('http', 80, '0.0.0.0/0'),
                               ('https', 443, '0.0.0.0/0')] + (
            [('ssh', 22, controller + '/32')] if controller else []):
        result.append(dict(id='managed-' + name, protocol='tcp',
                           port_range_min=port, port_range_max=port,
                           source=source, description='Managed by Coast',
                           managed=True))
    return result + database_public.rules(ctx, stack_id)


def signature(rule):
    return tuple(rule.get(key) for key in
                 ('protocol', 'port_range_min', 'port_range_max', 'source'))


class IngressRules:
    def __init__(self, stack_id, rule_id=None):
        self.stack_id = stack_id
        self.rule_id = rule_id

    @pecan.expose()
    def _lookup(self, rule_id, *remainder):
        return IngressRules(self.stack_id, rule_id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        if not microversions.at_least(pecan.request.version, '1.25'):
            pecan.abort(404)
        ctx = stacks.context()
        all_projects = stacks.read_all_projects('ingress_rule')
        stack = objects.Stack.get_by_id(
            ctx, self.stack_id, all_projects=all_projects)
        method = pecan.request.method
        action = {'GET': 'list', 'POST': 'create', 'DELETE': 'delete'}.get(
            method)
        if (action is None or
                (self.rule_id is not None) != (method == 'DELETE')):
            pecan.abort(405)
        stacks.authorize('stack', 'ingress_rules:' + action, stack)
        read_ctx = copy.copy(ctx)
        if all_projects:
            read_ctx.project_id = stack.project_id
        required = managed(read_ctx, stack.id)
        if method == 'GET':
            rows = db.ingress_rules.list_for_stack(read_ctx, stack.id)
            return {'ingress_rules': required + [dict(
                {key: getattr(row, key) for key in FIELDS}, managed=False)
                for row in rows]}
        values = None
        if method == 'POST':
            try:
                values = schemas.body(SCHEMA, 'ingress_rule')
            except exception.Invalid:
                raise exception.Invalid(
                    'Use IPv4 ingress only: protocol tcp, udp or icmp, '
                    'port_range_min/max, source and optional description. '
                    'Egress, IPv6, remote groups and address groups are '
                    'not supported.') from None
            values = validate(values)
        with db.writer.using(ctx) as session:
            db._lock_project(session, ctx.project_id)
            stack = volume_delete.lock_stack(ctx, stack.id)
            if stack.task_state != states.NONE:
                raise exception.Conflict()
            required = managed(ctx, stack.id)
            if method == 'DELETE':
                database_rule = next((r for r in required
                                      if r['id'] == self.rule_id and
                                      r['id'].startswith('managed-database-')),
                                     None)
                if database_rule:
                    raise exception.Conflict(
                        database_rule['description'] +
                        ': change public access on the database.')
                if self.rule_id in ('managed-http', 'managed-https',
                                    'managed-ssh'):
                    raise exception.Conflict(
                        'Coast managed rules are required.')
                row = db.ingress_rules.get_for_stack(
                    ctx, stack.id, self.rule_id)
                session.delete(row)
                pecan.response.status = 204
            else:
                rows = db.ingress_rules.list_for_stack(ctx, stack.id)
                existing = required + [
                    {key: getattr(row, key) for key in FIELDS} for row in rows]
                databases = [row for row in
                             database_public.databases(ctx, stack.id)
                             if row.public_enabled]
                if any(values['port_range_min'] is not None and
                       row.public_port is not None and
                       values['port_range_min'] <= row.public_port <=
                       values['port_range_max'] for row in databases):
                    raise exception.Conflict('Port is used by a database.')
                if any(signature(r) == signature(values) for r in existing):
                    raise exception.Conflict('Ingress rule already exists.')
                limit = cfg.CONF.controller_worker.ingress_rules_per_stack
                count = len(rows) + len(databases)
                if count >= limit:
                    raise exception.OverQuota(resources='ingress_rules')
                row = db.ingress_rules.create(
                    ctx, dict(values, stack_id=stack.id))
                result = {key: getattr(row, key) for key in FIELDS}
        for phase in ('start', 'end'):
            notifications.emit(ctx, notifications.API, 'ingress_rule',
                               action, phase, {'id': row.id,
                                               'project_id': ctx.project_id})
        if method == 'DELETE':
            return
        pecan.response.status = 201
        return {'ingress_rule': dict(result, managed=False)}
