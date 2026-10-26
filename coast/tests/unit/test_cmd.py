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

import argparse
import io
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import sys
import sysconfig
from unittest import mock

from alembic import util
import fixtures

from coast.cmd import db_manage
from coast.cmd import manage
from coast.conductor import engine_token_rotation
from coast.conductor import manager
from coast.tests import base


class CommandTest(base.TestCase):
    commands = ('manage', 'api', 'conductor', 'health_manager', 'db_manage',
                'status', 'console_proxy')

    def run_command(self, command, args):
        # Test installed entry points in fresh processes. Honor the stestr
        # PYTHON launcher so coverage also records the command subprocesses.
        launcher = shlex.split(os.environ.get('PYTHON', sys.executable))
        script = Path(sysconfig.get_path('scripts')) / (
            'coast-' + command.replace('_', '-'))
        return subprocess.run(launcher + [str(script)] + args,
                              env=dict(os.environ, HOME=self.useFixture(
                                  fixtures.TempDir()).path),
                              capture_output=True, text=True, timeout=30)

    def test_help(self):
        for command in self.commands:
            result = self.run_command(command, ['--help'])
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('usage:', result.stdout)

    def test_health_manager_requires_explicit_configuration(self):
        result = self.run_command('health_manager', [])
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Q86 key lookup', result.stdout + result.stderr)

    def test_upgrade_check(self):
        result = self.run_command('status', ['upgrade', 'check'])
        self.assertEqual(0, result.returncode, result.stderr)

    def test_db_upgrade(self):
        directory = self.useFixture(fixtures.TempDir()).path
        database = Path(directory) / 'coast.sqlite'
        conf = Path(directory) / 'coast.conf'
        conf.write_text('[database]\nconnection = sqlite:///' +
                        str(database) + '\n')
        args = ['--config-file', str(conf)]
        for suffix in (['upgrade'], ['upgrade', 'head'], ['current']):
            result = self.run_command('db_manage', args + suffix)
            self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('0020_service_dockerfile', result.stdout)
        self.assertTrue(database.exists())
        result = self.run_command('db_manage', ['version'])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('0020_service_dockerfile', result.stdout)
        for suffix in (['downgrade'], ['upgrade', '-1'], []):
            result = self.run_command('db_manage', args + suffix)
            self.assertNotEqual(0, result.returncode)

    def test_db_upgrade_logs_revision_before_and_after(self):
        directory = Path(self.useFixture(fixtures.TempDir()).path)
        database = directory / 'coast.sqlite'
        log_dir = directory / 'logs'
        log_dir.mkdir()
        conf = directory / 'coast.conf'
        conf.write_text('[DEFAULT]\nlog_dir = ' + str(log_dir) + '\n'
                        '[database]\nconnection = sqlite:///' +
                        str(database) + '\n')
        result = self.run_command(
            'db_manage', ['--config-file', str(conf), 'upgrade'])
        self.assertEqual(0, result.returncode, result.stderr)
        before = 'Database revision before upgrade: none'
        after = 'Database revision after upgrade: 0020_service_dockerfile'
        self.assertLess(result.stdout.index(before),
                        result.stdout.index(after))
        log_file = log_dir / 'coast-db-manage.log'
        self.assertTrue(log_file.is_file(), list(log_dir.iterdir()))
        logged = log_file.read_text()
        self.assertIn(before, logged)
        self.assertIn(after, logged)
        self.assertLess(logged.index(before), logged.index(after))
        self.assertIn('INFO', logged)

    def test_db_upgrade_failure_exits_nonzero(self):
        directory = Path(self.useFixture(fixtures.TempDir()).path)
        conf = directory / 'coast.conf'
        conf.write_text('[database]\nconnection = sqlite:///' +
                        str(directory / 'missing' / 'coast.sqlite') + '\n')
        result = self.run_command(
            'db_manage', ['--config-file', str(conf), 'upgrade'])
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Database migration command failed',
                      result.stdout + result.stderr)

    def test_db_failure_logs_safe_traceback(self):
        command = mock.Mock(name='command')
        command.name = 'upgrade'
        command.revision = 'head'
        conf = mock.Mock()
        conf.command = command
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        db_manage.LOG.logger.addHandler(handler)
        self.addCleanup(db_manage.LOG.logger.removeHandler, handler)
        try:
            raise ValueError('mysql+pymysql://user:chain-secret@host/coast')
        except ValueError as cause:
            error = util.CommandError(
                "Cannot drop index: password='test-secret' "
                'mysql+pymysql://user:url-secret@host/coast')
            error.__cause__ = cause
        with mock.patch.object(db_manage, 'CONF', conf), \
                mock.patch.object(db_manage.config, 'parse_args'), \
                mock.patch.object(db_manage.config, 'setup_logging'), \
                mock.patch.object(db_manage.migration, 'get_config'), \
                mock.patch.object(db_manage, '_database_revision',
                                  return_value='0009'), \
                mock.patch.object(db_manage.command, 'upgrade',
                                  side_effect=error):
            with self.assertRaises(SystemExit) as caught:
                db_manage.main()
        self.assertEqual('Database migration command failed.',
                         str(caught.exception))
        logged = output.getvalue()
        self.assertIn('Traceback', logged)
        self.assertIn('CommandError', logged)
        self.assertIn('Cannot drop index', logged)
        for secret in ('test-secret', 'url-secret', 'chain-secret',
                       'mysql+pymysql://', 'host/coast'):
            self.assertNotIn(secret, logged)


class EngineTokenRotationCommandTest(base.TestCase):

    def setUp(self):
        super().setUp()
        self.command_conf = mock.Mock()
        self.command_conf.command.name = 'rotate-engine-token'
        self.command_conf.command.stack = 'requested-stack'
        self.patch(manage, 'CONF', self.command_conf)
        self.patch(manage.config, 'parse_args', mock.Mock())
        self.patch(manage.config, 'setup_logging', mock.Mock())
        self.lookup = mock.Mock(return_value=mock.Mock(
            id='resolved-stack', project_id='tenant-project'))
        self.patch(manage.objects.Stack, 'get_by_id', self.lookup)
        self.manager = mock.Mock()
        self.patch(manager, 'Manager', self.manager)
        self.rotate = mock.Mock()
        self.patch(engine_token_rotation, 'rotate', self.rotate)

    def test_dispatch_resolves_stack_and_scopes_rotation(self):
        self.assertEqual(0, manage.main())
        self.lookup.assert_called_once_with(
            mock.ANY, 'requested-stack', all_projects=True)
        admin = self.lookup.call_args.args[0]
        self.assertTrue(admin.is_admin)
        self.assertIsNone(admin.project_id)
        self.manager.assert_called_once_with(self.command_conf)
        self.rotate.assert_called_once_with(
            self.manager.return_value, mock.ANY, 'resolved-stack')
        ctx = self.rotate.call_args.args[1]
        self.assertTrue(ctx.is_admin)
        self.assertEqual('tenant-project', ctx.project_id)
        self.assertIsNot(admin, ctx)

    def test_dispatch_sanitizes_lookup_manager_and_rotation_failures(self):
        for target in (self.lookup, self.manager, self.rotate):
            with self.subTest(target=target):
                target.side_effect = RuntimeError('private-token-payload')
                with mock.patch('sys.stdout',
                                new_callable=io.StringIO) as out, \
                        mock.patch('sys.stderr',
                                   new_callable=io.StringIO) as err:
                    with self.assertRaises(SystemExit) as caught:
                        manage.main()
                self.assertEqual('Engine token rotation failed.',
                                 str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertIsNone(caught.exception.__cause__)
                self.assertEqual('', out.getvalue())
                self.assertEqual('', err.getvalue())
                target.side_effect = None


class DefaultDomainRefreshTest(base.TestCase):

    def test_parser_accepts_engine_token_rotation(self):
        parser = argparse.ArgumentParser()
        manage.add_command_parsers(parser.add_subparsers(dest='name'))
        args = parser.parse_args(['rotate-engine-token', 'stack-id'])
        self.assertEqual('rotate-engine-token', args.name)
        self.assertEqual('stack-id', args.stack)

    def test_parser_accepts_refresh_flags(self):
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest='name')
        manage.add_command_parsers(commands)
        args = parser.parse_args([
            'dns', 'refresh-default-domains', '--service', 'svc-1',
            '--dry-run'])
        self.assertEqual('dns', args.name)
        self.assertEqual('refresh-default-domains', args.dns_command)
        self.assertEqual('svc-1', args.service)
        self.assertTrue(args.dry_run)

    def test_refresh_prints_json_and_uses_the_exit_code(self):
        failed = [{'changed': False, 'default_domain': None, 'ok': False,
                   'service_id': 'svc-1'}]
        conf = mock.Mock()
        conf.command.dns_command = 'refresh-default-domains'
        conf.command.service = 'svc-1'
        conf.command.dry_run = True
        with mock.patch('coast.conductor.manager.Manager') as manager, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            refresh = manager.return_value.refresh_default_domains
            refresh.return_value = failed
            code = manage._dns(conf)
        self.assertEqual(1, code)
        self.assertEqual(failed, json.loads(out.getvalue()))
        self.assertEqual('svc-1', refresh.call_args.kwargs['service_id'])
        self.assertTrue(refresh.call_args.kwargs['dry_run'])
        with mock.patch('coast.conductor.manager.Manager') as manager, \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            manager.return_value.refresh_default_domains.return_value = [{
                'changed': True, 'default_domain': 'web.example.com',
                'ok': True, 'service_id': 'svc-1'}]
            self.assertEqual(0, manage._dns(conf))

    def test_refresh_hides_exception_text(self):
        conf = mock.Mock()
        conf.command.dns_command = 'other'
        with self.assertRaises(SystemExit) as caught:
            manage._dns(conf)
        self.assertEqual('An operator command is required.',
                         str(caught.exception))
        conf.command.dns_command = 'refresh-default-domains'
        conf.command.service = None
        conf.command.dry_run = False
        with mock.patch('coast.conductor.manager.Manager') as manager:
            manager.return_value.refresh_default_domains.side_effect = (
                RuntimeError('secret-token'))
            with self.assertRaises(SystemExit) as caught:
                manage._dns(conf)
        self.assertEqual('Default domain refresh failed.',
                         str(caught.exception))
        self.assertNotIn('secret-token', str(caught.exception))
