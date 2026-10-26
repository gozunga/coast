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

"""Check walkthrough failure handling without a cloud."""
import importlib.util
import json
import os
import pathlib
import signal
from unittest import mock

import fixtures

from coast.tests import base


SCRIPT = (pathlib.Path(__file__).resolve().parents[3] /
          'tools/walkthrough/quickstart.py')
SPEC = importlib.util.spec_from_file_location('walkthrough', SCRIPT)
walkthrough = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(walkthrough)


class WalkthroughTest(base.TestCase):

    def test_cleanup_after_success_and_failure(self):
        config = {'COAST_WALK_' + key: 'input' for key in (
            'CONTROL_PLAN', 'WORKER_PLAN', 'GIT_URL', 'GIT_BRANCH',
            'APP_PORT', 'TEMPLATE_ID')}
        for failure in (None, RuntimeError('deployment failed')):
            stacks = iter([[{'ID': 'owned'}], []])
            deleted = []

            def command(*args, **kwargs):
                if args[:2] == ('stack', 'create'):
                    return {'ID': 'owned'}
                if args[:2] == ('stack', 'delete'):
                    deleted.append(args[2])
                    return None
                if args[:2] == ('stack', 'list'):
                    return next(stacks)
                if args[:2] == ('stack', 'show'):
                    raise walkthrough.CliError('gone', 404)
                if args[:2] == ('appgroup', 'list'):
                    return []
                raise AssertionError(args)

            with mock.patch.dict(os.environ, config), \
                    mock.patch.object(walkthrough, 'cli', command), \
                    mock.patch.object(walkthrough, 'wait'), \
                    mock.patch.object(walkthrough.time, 'sleep'), \
                    mock.patch.object(walkthrough, 'workloads',
                                      side_effect=failure):
                if failure:
                    self.assertRaises(RuntimeError, walkthrough.run)
                else:
                    walkthrough.run()
            self.assertEqual(['owned'], deleted)
            self.assertRaises(StopIteration, next, stacks)

    def test_cleanup_deletes_children_before_parents(self):
        cloud = FakeCloud()
        cloud.busy.add('app1')
        with mock.patch.object(walkthrough, 'cli', cloud.cli), \
                mock.patch.object(walkthrough.time, 'sleep'):
            walkthrough.cleanup('owned', 30)
        deletes = [args[2] for args in cloud.calls if args[1] == 'delete']
        # app1 got one in-flight 409, then a retry.
        self.assertEqual(
            ['app1', 'app1', 'svc1', 'env-test', 'env-prod', 'group',
             'owned'], deletes)
        self.assertEqual(
            ['app1', 'svc1', 'env-test', 'env-prod', 'group', 'owned'],
            cloud.removed)
        self.assertFalse(cloud.conflicts)
        # Each child was polled until 404 before its parent's delete.
        # The stack too: it shows DELETING once before 404.
        for row in ('app1', 'svc1', 'env-test', 'env-prod', 'group',
                    'owned'):
            self.assertIn(row, cloud.shown_gone)
        self.assertIn(('stack', 'show', 'owned'), cloud.calls)

    def test_cleanup_is_idempotent_after_partial_run(self):
        cloud = FakeCloud()
        # A previous cleanup removed everything but the stack, but the
        # listing is stale: deletes and shows answer 404.
        cloud.removed.extend(['app1', 'svc1', 'env-test', 'env-prod'])
        cloud.stale = True
        with mock.patch.object(walkthrough, 'cli', cloud.cli), \
                mock.patch.object(walkthrough.time, 'sleep'):
            walkthrough.cleanup('owned', 30)
        self.assertEqual(
            ['app1', 'svc1', 'env-test', 'env-prod', 'group', 'owned'],
            cloud.removed)
        self.assertFalse(cloud.conflicts)

    def test_cleanup_accepts_stack_already_gone(self):
        def command(*args, **kwargs):
            if args[:2] in (('appgroup', 'list'), ('stack', 'delete')):
                raise walkthrough.CliError('gone', 404)
            if args[:2] == ('stack', 'list'):
                return []
            raise AssertionError(args)

        with mock.patch.object(walkthrough, 'cli', command), \
                mock.patch.object(walkthrough.time, 'sleep'):
            walkthrough.cleanup('owned', 30)

    def test_conflict_retry_is_bounded(self):
        calls = []

        def command(*args, **kwargs):
            calls.append(args)
            raise walkthrough.CliError('busy', 409)

        clock = iter(range(0, 10000, 300))
        with mock.patch.object(walkthrough, 'cli', command), \
                mock.patch.object(walkthrough.time, 'sleep'), \
                mock.patch.object(walkthrough.time, 'monotonic',
                                  lambda: next(clock)):
            error = self.assertRaises(walkthrough.CliError,
                                      walkthrough.delete, 'service', 'id', 30)
        self.assertEqual(409, error.status)
        # Each attempt advances the clock 300 s; the third hits 900 s.
        self.assertEqual(3, len(calls))

    def test_other_delete_errors_are_not_retried(self):
        calls = []

        def command(*args, **kwargs):
            calls.append(args)
            raise walkthrough.CliError('denied', 403)

        with mock.patch.object(walkthrough, 'cli', command), \
                mock.patch.object(walkthrough.time, 'sleep'):
            self.assertRaises(walkthrough.CliError,
                              walkthrough.delete, 'service', 'id', 30)
        self.assertEqual(1, len(calls))

    def test_delete_wait_has_deadline(self):
        def command(*args, **kwargs):
            return {'Status': 'DELETING'} if args[1] == 'show' else None

        with mock.patch.object(walkthrough, 'cli', command), \
                mock.patch.object(walkthrough.time, 'sleep'), \
                mock.patch.object(walkthrough.time, 'monotonic',
                                  side_effect=[0, 0, 0, 31]):
            self.assertRaises(RuntimeError, walkthrough.delete,
                              'service', 'id', 30)

    def test_delete_error_status_fails(self):
        def command(*args, **kwargs):
            return {'Status': 'ERROR'} if args[1] == 'show' else None

        with mock.patch.object(walkthrough, 'cli', command), \
                mock.patch.object(walkthrough.time, 'sleep'):
            self.assertRaises(RuntimeError, walkthrough.delete,
                              'environment', 'id', 30)

    def test_cli_error_keeps_only_http_status(self):
        result = mock.Mock(returncode=1, stdout='',
                           stderr='Coast API: private (HTTP 409)')
        with mock.patch.object(walkthrough.subprocess, 'run',
                               return_value=result):
            error = self.assertRaises(walkthrough.CliError, walkthrough.cli,
                                      'stack', 'delete', 'id')
        self.assertEqual(409, error.status)
        self.assertNotIn('private', str(error))

    def test_cleanup_blocks_interrupt_until_finished(self):
        seen = []

        def command(*args, **kwargs):
            seen.append((signal.getsignal(signal.SIGTERM),
                         signal.getsignal(signal.SIGINT)))
            if args[:2] == ('stack', 'list'):
                return []
            if args[:2] == ('stack', 'show'):
                raise walkthrough.CliError('gone', 404)
            return None

        before_term = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        with mock.patch.object(walkthrough, 'cli', command), \
                mock.patch.object(walkthrough.time, 'sleep'):
            walkthrough.cleanup('owned', 30)
        self.assertTrue(seen)
        self.assertTrue(all(
            item == (signal.SIG_IGN, signal.SIG_IGN) for item in seen))
        self.assertIs(signal.getsignal(signal.SIGTERM), before_term)
        self.assertIs(signal.getsignal(signal.SIGINT), before_int)

    def test_wait_rejects_terminal_failure(self):
        with mock.patch.object(walkthrough, 'cli',
                               return_value={'Status': 'FAILED'}):
            self.assertRaises(RuntimeError, walkthrough.wait,
                              'deployment', 'id', 10)

    def test_wait_has_deadline(self):
        with mock.patch.object(walkthrough.time, 'monotonic',
                               side_effect=[0, 11]):
            self.assertRaises(RuntimeError, walkthrough.wait,
                              'stack', 'id', 10)

    def test_cli_failure_does_not_expose_output(self):
        result = mock.Mock(returncode=1, stdout='private', stderr='private')
        with mock.patch.object(walkthrough.subprocess, 'run',
                               return_value=result):
            error = self.assertRaises(RuntimeError, walkthrough.cli,
                                      'app', 'create', 'name')
        self.assertNotIn('private', str(error))

    def test_full_cli_flow_with_fake_cloud_state(self):
        self._full_flow({})

    def _full_flow(self, optional):
        config = {'COAST_WALK_' + key: 'input' for key in (
            'CONTROL_PLAN', 'WORKER_PLAN', 'GIT_URL', 'GIT_BRANCH',
            'APP_PORT', 'TEMPLATE_ID')}
        state = {'stack': None, 'deployments': [], 'env': {}}
        config.update(optional)

        def command(*args, value=None, **kwargs):
            kind, action = args[:2]
            if kind == 'stack' and action == 'create':
                state['stack'] = 'owned'
                return {'ID': 'owned'}
            if kind == 'stack' and action == 'delete':
                state['stack'] = None
                return
            if kind == 'stack' and action == 'list':
                return [] if state['stack'] is None else [{'ID': 'owned'}]
            if kind == 'stack' and action == 'show' and not state['stack']:
                raise walkthrough.CliError('gone', 404)
            if kind == 'server':
                return [{'ID': 'worker', 'Role': 'worker'}]
            if action == 'list':
                return []
            if action == 'create':
                # Create commands print a table, read back as JSON.
                self.assertTrue(kwargs.get('formatted', True))
                if kind == 'app':
                    self.assertIn('--ports', args)
                return {'ID': kind}
            if action == 'show':
                return {'Status': 'ACTIVE'}
            if kind == 'env' and action == 'set':
                key, sign, inline = args[4].partition('=')
                if sign:
                    # Plain values go inline as KEY=VALUE.
                    self.assertIsNone(value)
                else:
                    # A bare key reads its secret from stdin.
                    self.assertIn('--secret', args)
                    self.assertIsNotNone(value)
                state['env'][(args[3], key)] = inline or value
                return
            if kind == 'domain' and action == 'add':
                self.assertEqual(('--service', 'app'), args[2:4])
                self.assertTrue(kwargs.get('formatted', True))
                return {'ID': 'domain-id', 'Status': 'PENDING'}
            if kind == 'domain' and action == 'verify':
                state['verified'] = args[2:]
                return {'ID': 'domain-id', 'Status': 'ACTIVE'}
            if kind == 'deploy':
                # Deploy has a positional service, unlike env/domain commands.
                self.assertNotEqual('--service', action)
                state['deployments'].append(action)
                self.assertTrue(kwargs.get('formatted', True))
                return {'ID': 'deployment'}
            return {}

        printed = []
        with mock.patch.dict(os.environ, config, clear=True), \
                mock.patch.object(walkthrough, 'cli', command), \
                mock.patch('builtins.input', return_value=''), \
                mock.patch('builtins.print',
                           lambda *a, **k: printed.append(a[0])):
            walkthrough.run()
        self.assertIsNone(state['stack'])
        self.assertEqual(['app', 'service'], state['deployments'])
        self.assertEqual('production', state['env'][('app', 'APP_MODE')])
        self.assertEqual(32, len(state['env'][('app', 'WALK_TOKEN')]))
        return state, printed

    def test_full_cli_flow_with_template_env_and_domain(self):
        env_file = self.useFixture(fixtures.TempDir()).join('env.json')
        with open(env_file, 'w') as stream:
            json.dump({'DB_PASSWORD': 'pw'}, stream)
        state, printed = self._full_flow(
            {'COAST_WALK_TEMPLATE_ENV_FILE': env_file,
             'COAST_WALK_DOMAIN': 'web.example.com'})
        self.assertEqual('pw', state['env'][('service', 'DB_PASSWORD')])
        self.assertEqual(('--service', 'app', 'domain-id'),
                         state['verified'])
        self.assertIn('Publish DNS using: openstack coast domain show '
                      '--service app domain-id', printed)

    def test_cli_maps_not_found_to_404(self):
        result = mock.Mock(returncode=1, stdout='',
                           stderr="Stack 'owned' not found.\n")
        with mock.patch.object(walkthrough.subprocess, 'run',
                               return_value=result):
            error = self.assertRaises(walkthrough.CliError, walkthrough.cli,
                                      'stack', 'show', 'owned')
        self.assertEqual(404, error.status)
        self.assertNotIn('owned', str(error))

    def test_delete_finishes_when_client_reports_not_found(self):
        results = [mock.Mock(returncode=0, stdout='', stderr=''),
                   mock.Mock(returncode=1, stdout='',
                             stderr="App group 'group' not found.\n")]
        with mock.patch.object(walkthrough.subprocess, 'run',
                               side_effect=results) as run, \
                mock.patch.object(walkthrough.time, 'sleep'):
            walkthrough.delete('appgroup', 'group', 30)
        self.assertEqual(2, run.call_count)

    def test_other_cli_failures_have_no_status(self):
        self.assertIsNone(walkthrough.http_status('Missing value for KEY'))
        self.assertEqual(409, walkthrough.http_status(
            'Coast API: busy (HTTP 409)'))


class FakeCloud:
    """One stack: app group, two environments, an app and a service.

    Mirrors the API: a parent delete with live children is a 409, a
    deleted row shows DELETING once and then 404, a second delete is 404.
    """

    def __init__(self):
        self.parents = {'app1': 'env-test', 'svc1': 'env-test',
                        'env-test': 'group', 'env-prod': 'group',
                        'group': 'owned', 'owned': None}
        self.kinds = {'app1': 'app', 'svc1': 'service'}
        self.removed = []
        self.deleting = set()
        self.busy = set()
        self.conflicts = []
        self.shown_gone = []
        self.stale = False
        self.calls = []

    def live(self, resource_id):
        return resource_id not in self.removed

    def children(self, parent, kind=None):
        rows = [key for key, value in self.parents.items()
                if value == parent and (self.stale or self.live(key))]
        if kind:
            rows = [key for key in rows if self.kinds.get(key) == kind]
        return [{'ID': key} for key in rows]

    def cli(self, *args, **kwargs):
        self.calls.append(args)
        kind, action = args[:2]
        if action == 'list':
            if kind == 'stack':
                return [{'ID': 'owned'}] if self.live('owned') else []
            parent = args[3]
            if kind in ('app', 'service'):
                envs = [row['ID'] for row in self.children(parent)]
                return [row for env in envs
                        for row in self.children(env, kind)]
            return self.children(parent)
        resource_id = args[2]
        if action == 'delete':
            if not self.live(resource_id):
                raise walkthrough.CliError('gone', 404)
            if resource_id in self.busy:
                self.busy.discard(resource_id)
                raise walkthrough.CliError('busy', 409)
            if any(self.live(key) for key, value in self.parents.items()
                   if value == resource_id):
                self.conflicts.append(resource_id)
                raise walkthrough.CliError('children', 409)
            self.deleting.add(resource_id)
            return None
        if action == 'show':
            if not self.live(resource_id):
                self.shown_gone.append(resource_id)
                raise walkthrough.CliError('gone', 404)
            if resource_id in self.deleting:
                self.removed.append(resource_id)
                return {'Status': 'DELETING'}
            return {'Status': 'ACTIVE'}
        raise AssertionError(args)
