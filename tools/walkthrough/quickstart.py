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

"""Run the documented CLI steps in an isolated cloud project."""
import json
import os
import re
import signal
import subprocess
import time
import uuid


# A 409 from in-flight work clears once that work settles.
CONFLICT_RETRY = 900
POLL = 5


class CliError(RuntimeError):
    """A failed CLI call. Only the HTTP status is kept, never the output."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def cli(*args, value=None, formatted=True):
    command = ['openstack', 'coast', *args]
    if formatted:
        command += ['-f', 'json']
    result = subprocess.run(command, input=value, text=True,
                            capture_output=True, timeout=120)
    if result.returncode:
        raise CliError('Coast command failed: ' + ' '.join(args[:2]),
                       http_status(result.stderr or ''))
    return json.loads(result.stdout) if result.stdout.strip() else None


def http_status(stderr):
    """Return the HTTP status a failed command reports, if any.

    The client prints "Coast API: ... (HTTP 409)" for API errors and
    "Stack '...' not found." for a 404 or a name that does not resolve.
    """
    match = re.search(r'\(HTTP (\d{3})\)', stderr)
    if match:
        return int(match.group(1))
    if re.search(r"^.+ '[^']*' not found\.$", stderr, re.MULTILINE):
        return 404
    return None


def wait(kind, resource_id, timeout, **scope):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        args = [kind, 'show', resource_id]
        for key, value in scope.items():
            args += ['--' + key, value]
        row = cli(*args)
        if row['Status'] == 'ACTIVE':
            return
        if row['Status'] in ('ERROR', 'FAILED', 'CANCELLED'):
            raise RuntimeError(kind + ' failed')
        time.sleep(5)
    raise RuntimeError(kind + ' timed out')


def gone(error):
    return isinstance(error, CliError) and error.status == 404


def submit(kind, resource_id):
    """Request a delete. Return False when it is already gone (404).

    409 means in-flight work; retry until it settles, up to
    CONFLICT_RETRY seconds.
    """
    conflict_deadline = time.monotonic() + CONFLICT_RETRY
    while True:
        try:
            cli(kind, 'delete', resource_id, formatted=False)
            return True
        except CliError as error:
            if gone(error):
                return False
            if (error.status != 409 or
                    time.monotonic() >= conflict_deadline):
                raise
        time.sleep(POLL)


def delete(kind, resource_id, timeout):
    """Delete one resource and wait until show returns 404."""
    if not submit(kind, resource_id):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            row = cli(kind, 'show', resource_id)
        except CliError as error:
            if gone(error):
                return
            raise
        if row and row.get('Status') == 'ERROR':
            raise RuntimeError(kind + ' delete failed: ' + resource_id)
        time.sleep(POLL)
    raise RuntimeError('Cleanup timed out for ' + kind + ' ' + resource_id)


def listed(*args):
    """List rows; a 404 parent has no children left."""
    try:
        return cli(*args) or []
    except CliError as error:
        if gone(error):
            return []
        raise


def cleanup(stack_id, timeout):
    """Delete one stack bottom-up. Ignore a second interrupt meanwhile.

    The API refuses a parent delete while children remain: apps and
    services, then environments, then app groups, then the stack.
    """
    blocked = (signal.SIGTERM, signal.SIGINT)
    previous = [signal.signal(number, signal.SIG_IGN) for number in blocked]
    try:
        groups = listed('appgroup', 'list', '--stack', stack_id)
        for group in groups:
            for kind in ('app', 'service'):
                for row in listed(kind, 'list', '--appgroup', group['ID']):
                    delete('service', row['ID'], timeout)
        for group in groups:
            for row in listed('environment', 'list',
                              '--appgroup', group['ID']):
                delete('environment', row['ID'], timeout)
        for group in groups:
            delete('appgroup', group['ID'], timeout)
        delete('stack', stack_id, timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = cli('stack', 'list') or []
            if not any(row['ID'] == stack_id for row in rows):
                print('Stack cleanup completed.', flush=True)
                return
            time.sleep(POLL)
        raise RuntimeError('Cleanup timed out for stack ' + stack_id)
    finally:
        for number, handler in zip(blocked, previous):
            signal.signal(number, handler)


def workloads(config, stack_id, timeout, name):
    workers = cli('server', 'list', '--stack', stack_id)
    worker = next(row['ID'] for row in workers if row['Role'] == 'worker')
    group = cli('appgroup', 'create', name, '--stack', stack_id)
    wait('appgroup', group['ID'], timeout)
    env = cli('environment', 'create', 'test', '--appgroup', group['ID'])
    wait('environment', env['ID'], timeout)
    parents = ['--appgroup', group['ID'], '--environment', env['ID'],
               '--server', worker]
    app = cli('app', 'create', name, *parents,
              '--repo-url', config['GIT_URL'],
              '--branch', config['GIT_BRANCH'],
              '--build-pack', 'dockerfile', '--ports', config['APP_PORT'],
              '--no-deploy')
    app_id = app['ID']
    cli('env', 'set', '--service', app_id, 'APP_MODE=production',
        '--build-time', formatted=False)
    cli('env', 'set', '--service', app_id, 'WALK_TOKEN', '--secret',
        '--no-build-time', value=uuid.uuid4().hex, formatted=False)
    cli('env', 'list', '--service', app_id)
    deployment = cli('deploy', app_id)
    wait('deployment', deployment['ID'], timeout, service=app_id)
    cli('deployment', 'log', '--service', app_id, deployment['ID'])
    cli('logs', app_id, '--tail', '50')
    service = cli('service', 'create', name + '-template', *parents,
                  '--template', config['TEMPLATE_ID'], '--no-deploy')
    env_file = os.environ.get('COAST_WALK_TEMPLATE_ENV_FILE')
    if env_file:
        with open(env_file) as stream:
            values = json.load(stream)
        for key, value in values.items():
            cli('env', 'set', '--service', service['ID'], key, '--secret',
                '--no-build-time', value=value, formatted=False)
    deployment = cli('deploy', service['ID'])
    wait('deployment', deployment['ID'], timeout, service=service['ID'])
    domain = os.environ.get('COAST_WALK_DOMAIN')
    if domain:
        row = cli('domain', 'add', '--service', app_id, domain,
                  '--no-auto-dns')
        print('Publish DNS using: openstack coast domain show --service '
              + app_id + ' ' + row['ID'], flush=True)
        input('Press Enter after publishing the returned DNS records: ')
        cli('domain', 'verify', '--service', app_id, row['ID'])
    cli('stack', 'event', 'list', stack_id, '--long')


def run():
    keys = ('CONTROL_PLAN', 'WORKER_PLAN', 'GIT_URL', 'GIT_BRANCH',
            'APP_PORT', 'TEMPLATE_ID')
    config = {key: os.environ['COAST_WALK_' + key] for key in keys}
    timeout = int(os.environ.get('COAST_WALK_TIMEOUT', '1800'))
    if timeout <= 0:
        raise ValueError('Timeout must be positive')
    os.environ['OS_COAST_API_MICROVERSION'] = '1.8'
    name = 'walk-' + uuid.uuid4().hex[:12]
    stack_id = None
    try:
        stack = cli('stack', 'create', name, '--plan', config['CONTROL_PLAN'],
                    '--worker', 'kind=vm,plan=' + config['WORKER_PLAN'])
        stack_id = stack['ID']
        print('Created stack ' + stack_id, flush=True)
        wait('stack', stack_id, timeout)
        workloads(config, stack_id, timeout, name)
        print('Quickstart CLI steps passed.', flush=True)
    finally:
        if stack_id:
            cleanup(stack_id, timeout)


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run()
    except (Exception, KeyboardInterrupt):
        print('Walkthrough failed. Check stack Activity and cleanup status.')
        raise SystemExit(1)
