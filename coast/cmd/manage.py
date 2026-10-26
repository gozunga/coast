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

"""Operator-only commands; access requires the Coast configuration and DB."""

import ipaddress
import json
import sys

from oslo_config import cfg

from coast.common import config
from coast.common import context
from coast.common import endpoints
from coast.common import janitor
from coast.common import notifications
from coast.common import quota
from coast.common import states
from coast.db import api as db_api
from coast.drivers import base
from coast.i18n import _
from coast import objects


CONF = cfg.CONF


def add_command_parsers(subparsers):
    rotate = subparsers.add_parser(
        'rotate-engine-token', help='Rotate an app engine API token safely.')
    rotate.add_argument('stack')
    subparsers.add_parser(
        'janitor-report', help='Report orphan candidates; never delete.')
    parser = subparsers.add_parser(
        'adopt-stack', help='Adopt an existing M2 engine.')
    for name in ('project-id', 'name', 'url', 'token-ref'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--server-ip')
    parser.add_argument('--server-role', choices=['worker', 'build'],
                        default='worker')
    parser.add_argument('--engine-ref')
    parser.add_argument('--server-engine-ref')
    dns = subparsers.add_parser(
        'dns', help='Update default service hostnames.')
    commands = dns.add_subparsers(dest='dns_command', required=True)
    refresh = commands.add_parser(
        'refresh-default-domains',
        help='Update default hostnames without a redeploy.')
    refresh.add_argument('--service', help='Limit the refresh to one service.')
    refresh.add_argument(
        '--dry-run', action='store_true',
        help='Report changes and do not apply them.')


command_opt = cfg.SubCommandOpt('command', handler=add_command_parsers,
                                title='Commands', help='Operator commands')


def adopt_stack(conf, args):
    url = endpoints.validate_url(args.url)
    endpoints.validate_url(args.token_ref)
    if not args.project_id or not args.name or len(args.name) > 255:
        raise base.DriverError('Project and stack name are required')
    if len(args.token_ref) > 255:
        raise base.DriverError('Secret reference is too long')
    if args.server_ip:
        ipaddress.ip_address(args.server_ip)
    elif args.server_engine_ref:
        raise base.DriverError('Server IP is required')
    if args.server_role not in ('worker', 'build'):
        raise base.DriverError('Server role is invalid')
    ctx = context.RequestContext(project_id=args.project_id, is_admin=True)
    env = objects.Stack(
        context=ctx, name=args.name, driver=conf.controller_worker.driver,
        engine_version=conf.coolify.pinned_version, plan='adopted',
        status=states.ACTIVE, task_state=states.NONE,
        engine_token_ref=args.token_ref, engine_ref=args.engine_ref)
    servers = []
    with db_api._write(ctx):
        deltas = {'environments': 1, 'servers': 1 + bool(args.server_ip)}
        if args.server_ip and args.server_role == 'build':
            deltas['build_servers'] = 1
        reservation = quota.reserve(ctx, **deltas)
        env.create()
        # URL remains operator configuration, not a server IP or engine ref.
        specs = [('control', None, None)]
        if args.server_ip:
            specs.append((args.server_role, args.server_ip,
                          args.server_engine_ref))
        for role, ip, ref in specs:
            server = objects.Server(
                context=ctx, stack_id=env.id,
                name='%s-%s' % (args.name[:240], role), kind='byo', role=role,
                private_ip=ip, engine_ref=ref, status=states.ACTIVE,
                task_state=states.NONE)
            server.create()
            servers.append(server)
        quota.commit(ctx, reservation)
    for row in [env, *servers]:
        for phase in ('start', 'end'):
            notifications.emit(ctx, notifications.CONDUCTOR,
                               row.repository.resource, 'create', phase,
                               notifications.payload_for(row, ctx), conf)
    return env.id, 'adopted_endpoints = %s:%s' % (env.id, url)


def _refresh_default_domains(conf):
    # Import here so adopt and janitor do not load the conductor.
    from coast.conductor.manager import Manager
    ctx = context.RequestContext(is_admin=True)
    report = Manager(conf).refresh_default_domains(
        ctx, service_id=getattr(conf.command, 'service', None),
        dry_run=bool(getattr(conf.command, 'dry_run', False)))
    print(json.dumps(report, sort_keys=True))
    return 1 if any(not item.get('ok') for item in report) else 0


def _dns(conf):
    if getattr(conf.command, 'dns_command', None) != 'refresh-default-domains':
        raise SystemExit(_('An operator command is required.'))
    try:
        return _refresh_default_domains(conf)
    except Exception:
        # Database and engine errors can contain references.
        raise SystemExit(_('Default domain refresh failed.')) from None


def main():
    CONF.register_cli_opt(command_opt)
    config.parse_args()
    if CONF.command.name in ('janitor-report', 'dns'):
        # Keep stdout reserved for one complete JSON document.
        CONF.set_override('use_stderr', True)
    config.setup_logging()
    if CONF.command.name == 'rotate-engine-token':
        from coast.conductor import engine_token_rotation
        from coast.conductor.manager import Manager
        try:
            admin = context.RequestContext(is_admin=True)
            stack = objects.Stack.get_by_id(
                admin, CONF.command.stack, all_projects=True)
            ctx = context.RequestContext(project_id=stack.project_id,
                                         is_admin=True)
            engine_token_rotation.rotate(Manager(CONF), ctx, stack.id)
        except Exception:
            raise SystemExit(_('Engine token rotation failed.')) from None
        return 0
    if CONF.command.name == 'dns':
        return _dns(CONF)
    if CONF.command.name == 'janitor-report':
        try:
            result = janitor.report(CONF)
        except Exception:
            # Cloud and database exception text can contain credentials.
            print(_('Janitor report failed; no complete report is available.'),
                  file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 1 if result['orphans'] or result['missing_secrets'] else 0
    if CONF.command.name != 'adopt-stack':
        raise SystemExit(_('An operator command is required.'))
    try:
        env_id, line = adopt_stack(CONF, CONF.command)
    except Exception:
        # DB and input errors can contain references. Never expose details.
        raise SystemExit(_('Stack adoption failed.')) from None
    print(env_id)
    print('[coolify]')
    print(line)
    return 0
