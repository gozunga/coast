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

"""Operator command against SQLite and the SDK with read-only fake clouds."""

import datetime
import io
import json
import logging as std_logging
from types import SimpleNamespace
from unittest import mock
import uuid

import fixtures
from keystoneauth1 import noauth
from keystoneauth1 import session as ks_session
from openstack import connection
from requests_mock.contrib import fixture as requests_fixture
import sqlalchemy as sa

from coast.cmd import manage
from coast.common import clients
from coast.common import context
from coast.common import janitor
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast.tests import base


class JanitorReportTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(is_admin=True)
        self.stack_id, self.dead_id, self.retained_id, self.secret_id = (
            str(uuid.uuid4()) for _ in range(4))
        with api.writer.using(self.ctx) as session:
            session.add_all([
                models.Stack(id=self.stack_id, project_id='tenant-a',
                             name='live', driver='noop', engine_version='test',
                             plan='small', status='ACTIVE', task_state='NONE',
                             engine_token_ref=self.secret_ref(self.secret_id)),
                models.Stack(id=self.dead_id, project_id='tenant-b',
                             name='deleted', driver='noop',
                             engine_version='test', plan='small',
                             status='DELETED', task_state='NONE',
                             deleted_at=datetime.datetime.now(
                                 datetime.timezone.utc))])
            session.flush()
            session.add(models.StackDeleteSnapshot(
                project_id='tenant-b', stack_id=self.dead_id,
                volume_id=str(uuid.uuid4()),
                cinder_volume_id=str(uuid.uuid4()),
                cinder_snapshot_id=self.retained_id, status='available'))
        self.http = self.useFixture(requests_fixture.Fixture())
        for url, version, maximum in (
                ('https://compute.example.com/v2.1', 'v2.1', '2.95'),
                ('https://block.example.com/v3', 'v3.0', '3.71'),
                ('https://block.example.com/v3/service-project',
                 'v3.0', '3.71')):
            self.http.get(url, json={'version': {
                'id': version, 'status': 'CURRENT',
                'min_version': '2.1' if version == 'v2.1' else '3.0',
                'version': maximum, 'links': [{'rel': 'self', 'href': url}]}})
        for url, version in (
                ('https://network.example.com/v2.0', 'v2.0'),
                ('https://secrets.example.com/v1', 'v1.0')):
            self.http.get(url, json={'version': {
                'id': version, 'status': 'CURRENT',
                'links': [{'rel': 'self', 'href': url}]}})
        session = ks_session.Session(auth=noauth.NoAuth())
        cloud = connection.Connection(
            session=session,
            compute_endpoint_override='https://compute.example.com/v2.1',
            network_endpoint_override='https://network.example.com/v2.0',
            block_storage_endpoint_override='https://block.example.com/v3/'
            'service-project',
            key_manager_endpoint_override='https://secrets.example.com/v1')
        self.addCleanup(cloud.close)
        self.useFixture(fixtures.MockPatchObject(
            clients, 'get_connection', return_value=cloud))
        self.useFixture(fixtures.MockPatchObject(
            clients, 'get_sensitive_proxy', return_value=cloud.key_manager))
        self.useFixture(fixtures.MockPatchObject(
            clients, 'get_session', return_value=SimpleNamespace(
                get_project_id=lambda: 'service-project')))
        self.urls = {
            'server': ('https://compute.example.com/v2.1/servers/detail',
                       'servers'),
            'volume': ('https://block.example.com/v3/service-project/'
                       'volumes/detail', 'volumes'),
            'snapshot': ('https://block.example.com/v3/service-project/'
                         'snapshots/detail', 'snapshots'),
            'secret': ('https://secrets.example.com/v1/secrets', 'secrets')}
        for kind, path, key in (
                ('port', 'ports', 'ports'),
                ('network', 'networks', 'networks'),
                ('subnet', 'subnets', 'subnets'),
                ('router', 'routers', 'routers'),
                ('security_group', 'security-groups', 'security_groups'),
                ('floating_ip', 'floatingips', 'floatingips')):
            self.urls[kind] = ('https://network.example.com/v2.0/' + path, key)
        for kind in self.urls:
            self.cloud_list(kind, [])
        self.conf.clear()
        self.useFixture(fixtures.MonkeyPatch(
            'coast.cmd.manage.CONF', self.conf))
        self.useFixture(fixtures.MockPatchObject(
            manage.config, 'parse_args', side_effect=lambda: self.conf(
                ['janitor-report'], default_config_files=[])))
        self.useFixture(fixtures.MockPatchObject(
            manage.config, 'setup_logging'))

    def secret_ref(self, identifier):
        return 'https://secrets.example.com/v1/secrets/' + identifier

    def cloud_list(self, kind, rows, **kwargs):
        if kind == 'secret':
            rows = rows + [{'secret_ref': self.secret_ref(self.secret_id),
                            'name': 'coast:%s:engine-token:%s' % (
                                self.stack_id, self.stack_id)}]
        url, key = self.urls[kind]
        self.http.get(url, json={key: rows}, **kwargs)

    def run_command(self):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch('sys.stdout', output), \
                mock.patch('sys.stderr', errors):
            code = manage.main()
        return code, output.getvalue(), errors.getvalue()

    def test_full_report_is_read_only_scoped_and_protects_live_owners(self):
        expected = []
        for kind in self.urls:
            identifier = str(uuid.uuid4())
            orphan = {'id': identifier, 'name': 'coast-' + self.dead_id,
                      'tags': ['coast', 'environment_id=' + self.dead_id]}
            live = {'id': str(uuid.uuid4()),
                    'name': 'coast-' + self.stack_id,
                    'tags': ['coast', 'environment_id=' + self.stack_id]}
            if kind == 'secret':
                orphan['secret_ref'] = self.secret_ref(identifier)
                live['secret_ref'] = self.secret_ref(self.secret_id)
                live['name'] = 'coast-saved-credential'
            rows = [orphan, live,
                    {'id': str(uuid.uuid4()), 'name': 'unmarked'}]
            if kind == 'snapshot':
                rows.append({'id': self.retained_id,
                             'name': 'coast-' + self.dead_id})
            if kind == 'secret':
                rows[-1]['secret_ref'] = self.secret_ref(rows[-1]['id'])
            self.cloud_list(kind, rows)
            expected.append({'resource_type': kind, 'id': identifier})

        def read_only(conn, cursor, statement, parameters, ctx, many):
            self.assertTrue(statement.lstrip().upper().startswith(
                ('SELECT', 'BEGIN')))

        sa.event.listen(self.db.engine, 'before_cursor_execute', read_only)
        try:
            code, output, errors = self.run_command()
        finally:
            sa.event.remove(self.db.engine, 'before_cursor_execute', read_only)
        self.assertEqual(1, code)
        self.assertEqual('', errors)
        result = json.loads(output)
        self.assertTrue(result['complete'])
        self.assertEqual(
            sorted(expected, key=lambda row: row['resource_type']),
            result['orphans'])
        self.assertEqual(set(self.urls), set(result['scanned']))
        self.assertNotIn('coast-saved-credential', output)
        self.assertNotIn('https://secrets.example.com', output)
        for request in self.http.request_history:
            self.assertEqual('GET', request.method)
            self.assertNotIn('/payload', request.path)
            self.assertNotIn('all_tenants=1', request.url)
            if (request.hostname == 'network.example.com' and
                    request.path != '/v2.0'):
                self.assertEqual(['service-project'],
                                 request.qs.get('project_id'))

    def test_secret_orphans_and_missing_refs(self):
        unreferenced, dead_owner = (str(uuid.uuid4()) for _ in range(2))
        with api.writer.using(self.ctx) as session:
            session.query(models.Stack).filter_by(id=self.stack_id).update({
                'engine_token_previous_ref': self.secret_ref(dead_owner)})
        self.cloud_list('secret', [
            {'secret_ref': self.secret_ref(unreferenced),
             'name': 'coast:%s:env:%s' % (self.stack_id, self.stack_id)},
            {'secret_ref': self.secret_ref(dead_owner),
             'name': 'coast:%s:env:%s' % (self.stack_id, self.dead_id)}])
        code, output, errors = self.run_command()
        self.assertEqual(1, code)
        self.assertEqual('', errors)
        result = json.loads(output)
        self.assertEqual(sorted([unreferenced, dead_owner]),
                         [row['id'] for row in result['orphans']])
        self.assertEqual([], result['missing_secrets'])

    def test_missing_secret_refs(self):
        # Complete inventory can expose missing refs without fetching payloads.
        self.http.get(self.urls['secret'][0], json={'secrets': []})
        code, output, errors = self.run_command()
        self.assertEqual(1, code)
        self.assertEqual([self.secret_id],
                         json.loads(output)['missing_secrets'])

    def test_all_secret_locations_and_project_signer_are_protected(self):
        service_id, source_id, policy_id = (
            str(uuid.uuid4()) for _ in range(3))
        keys = [str(uuid.uuid4()) for _ in range(7)]
        refs = [self.secret_ref(key) for key in keys]
        with api.writer.using(self.ctx) as session:
            session.add(models.Source(
                id=source_id, project_id='tenant-a', type='git',
                credentials_ref=refs[0]))
            session.flush()
            session.add(models.Service(
                id=service_id, stack_id=self.stack_id, project_id='tenant-a',
                name='app', source_id=source_id, auto_deploy=False,
                status='ACTIVE', task_state='NONE', compose_ref=refs[1],
                file_mount_secret_refs=[refs[2]]))
            session.flush()
            session.add_all([
                models.FileMount(project_id='tenant-a', service_id=service_id,
                                 container_path='/config',
                                 content_ref=refs[3]),
                models.BackupPolicy(
                    id=policy_id, project_id='tenant-a', service_id=service_id,
                    engine='test', container='test', key_ref=refs[4],
                    signing_key_ref=refs[5]),
                models.Backup(project_id='tenant-a', service_id=service_id,
                              kind='manual', status='AVAILABLE',
                              engine='test', container='test',
                              key_ref=refs[6]),
                models.BackupAccount(project_id='tenant-a', key_ref=refs[5])])
        names = [
            'coast:%s:env:%s' % (self.stack_id, service_id),
            'coast:%s:compose:%s' % (self.stack_id, service_id),
            'coast:%s:file-mount:%s' % (self.stack_id, service_id),
            'coast:%s:file-mount:%s' % (self.stack_id, service_id),
            'coast:%s:backup-key:%s' % (self.stack_id, policy_id),
            'coast:tenant-a:backup-signing-key:tenant-a',
            'coast:%s:backup-key:%s' % (self.stack_id, policy_id)]
        self.cloud_list('secret', [
            {'secret_ref': ref, 'name': name}
            for ref, name in zip(refs, names)])
        code, output, errors = self.run_command()
        self.assertEqual(0, code)
        result = json.loads(output)
        self.assertEqual([], result['orphans'])
        self.assertEqual([], result['missing_secrets'])
        # The project account survives its last stack and still owns its key.
        with api.writer.using(self.ctx) as session:
            session.query(models.Stack).filter_by(id=self.stack_id).update({
                'deleted_at': datetime.datetime.now(datetime.timezone.utc)})
        owners = janitor.secret_owners(self.ctx)
        self.assertIn('tenant-a', owners)
        self.assertNotIn(self.stack_id, owners)

    def test_clean_report_returns_zero(self):
        code, output, errors = self.run_command()
        self.assertEqual(0, code)
        self.assertEqual('', errors)
        self.assertEqual([], json.loads(output)['orphans'])

    def test_failed_last_list_never_prints_partial_report(self):
        self.cloud_list(
            'server', [{'id': str(uuid.uuid4()), 'tags': ['coast']}])
        self.http.get(self.urls['secret'][0], status_code=403,
                      text='credential-must-not-leak')
        code, output, errors = self.run_command()
        self.assertEqual(2, code)
        self.assertEqual('', output)
        self.assertNotIn('credential-must-not-leak', errors)

    def test_reference_created_during_cloud_scan_is_protected(self):
        identifier = str(uuid.uuid4())

        def create_during_scan(request, ctx):
            with api.writer.using(self.ctx) as session:
                session.query(models.Stack).filter_by(id=self.stack_id).update(
                    {'network_id': identifier})
            return {'networks': [{'id': identifier, 'tags': ['coast']}]}

        self.http.get(self.urls['network'][0], json=create_during_scan)
        code, output, errors = self.run_command()
        self.assertEqual(0, code)
        self.assertEqual('', errors)
        self.assertEqual([], json.loads(output)['orphans'])

    def test_reference_read_failure_has_no_output(self):
        with mock.patch.object(janitor, 'references',
                               side_effect=RuntimeError('database-password')):
            code, output, errors = self.run_command()
        self.assertEqual(2, code)
        self.assertEqual('', output)
        self.assertNotIn('database-password', errors)

    def test_direct_references_and_historical_secrets_across_projects(self):
        server_id, compute_id, volume_id, cinder_id, source_id, service_id = (
            str(uuid.uuid4()) for _ in range(6))
        history_secret, env_secret = (str(uuid.uuid4()) for _ in range(2))
        with api.writer.using(self.ctx) as session:
            session.add(models.Source(id=source_id, project_id='tenant-b',
                                      type='git'))
            session.add(models.Server(
                id=server_id, stack_id=self.stack_id, project_id='tenant-b',
                name='server', kind='vm', role='worker', status='ERROR',
                task_state='NONE', compute_id=compute_id))
            session.flush()
            session.add(models.Volume(
                id=volume_id, stack_id=self.dead_id, project_id='tenant-b',
                name='retained', size_gb=1, mount_path='/data', purpose='data',
                cinder_volume_id=cinder_id, status='AVAILABLE'))
            session.add(models.Service(
                id=service_id, stack_id=self.stack_id, project_id='tenant-b',
                name='service', source_id=source_id, ports='80', replicas=1,
                auto_deploy=False, status='ACTIVE', task_state='NONE'))
            session.flush()
            session.add(models.Secret(
                project_id='tenant-b', service_id=service_id, key='KEY',
                is_secret=True, build_time=False, version=1,
                value_ref=self.secret_ref(env_secret)))
            session.add(models.Deployment(
                project_id='tenant-b', service_id=service_id, number=1,
                spec={'env': [{'value_ref': self.secret_ref(history_secret)}]},
                trigger='manual', status='SUCCEEDED',
                deleted_at=datetime.datetime.now(datetime.timezone.utc)))
        self.cloud_list('server', [{'id': compute_id, 'tags': ['coast']}])
        self.cloud_list('volume', [{'id': cinder_id,
                                   'metadata': {'coast': 'true'}}])
        self.cloud_list('secret', [
            {'secret_ref': self.secret_ref(identifier), 'name': 'coast-saved'}
            for identifier in (env_secret, history_secret)])
        code, output, errors = self.run_command()
        self.assertEqual(0, code)
        self.assertEqual('', errors)
        self.assertEqual([], json.loads(output)['orphans'])

    def test_all_sdk_pages_are_included(self):
        identifiers = [str(uuid.uuid4()), str(uuid.uuid4())]
        url = self.urls['server'][0]
        next_url = url + '?marker=' + identifiers[0]
        self.http.get(url, json={
            'servers': [{'id': identifiers[0], 'tags': ['coast']}],
            'servers_links': [{'rel': 'next', 'href': next_url}]},
            complete_qs=True)
        self.http.get(next_url, json={
            'servers': [{'id': identifiers[1], 'tags': ['coast']}]},
            complete_qs=True)
        code, output, errors = self.run_command()
        self.assertEqual(1, code)
        self.assertEqual('', errors)
        result = json.loads(output)
        self.assertEqual(2, result['scanned']['server'])
        self.assertEqual(sorted(identifiers),
                         [row['id'] for row in result['orphans']])

    def test_secret_checkpoint_during_scan_is_not_missing_this_run(self):
        identifier = str(uuid.uuid4())

        def create_during_scan(request, ctx):
            with api.writer.using(self.ctx) as session:
                session.query(models.Stack).filter_by(id=self.stack_id).update(
                    {'engine_token_previous_ref': self.secret_ref(identifier)})
            return {'secrets': [{
                'secret_ref': self.secret_ref(self.secret_id),
                'name': 'coast:%s:engine-token:%s' % (
                    self.stack_id, self.stack_id)}]}

        self.http.get(self.urls['secret'][0], json=create_during_scan)
        code, output, errors = self.run_command()
        self.assertEqual(0, code)
        self.assertEqual([], json.loads(output)['missing_secrets'])

    def test_reference_removed_during_scan_stays_protected_this_run(self):
        identifier = str(uuid.uuid4())
        with api.writer.using(self.ctx) as session:
            session.query(models.Stack).filter_by(id=self.stack_id).update(
                {'network_id': identifier})

        def remove_during_scan(request, ctx):
            with api.writer.using(self.ctx) as session:
                session.query(models.Stack).filter_by(id=self.stack_id).update(
                    {'network_id': None})
            return {'networks': [{'id': identifier, 'tags': ['coast']}]}

        self.http.get(self.urls['network'][0], json=remove_during_scan)
        code, output, errors = self.run_command()
        self.assertEqual(0, code)
        self.assertEqual('', errors)
        self.assertEqual([], json.loads(output)['orphans'])

    def test_final_reference_read_failure_never_prints_a_report(self):
        protected = janitor.references(self.ctx)
        with mock.patch.object(janitor, 'references', side_effect=[
                protected, RuntimeError('database-password')]):
            code, output, errors = self.run_command()
        self.assertEqual(2, code)
        self.assertEqual('', output)
        self.assertNotIn('database-password', errors)

    def test_logged_failure_does_not_contaminate_json_stdout(self):
        root = std_logging.getLogger()
        handlers, level = root.handlers[:], root.level

        def fail(conf):
            clients.LOG.error('Service authentication is not configured.')
            raise RuntimeError('credential-must-not-leak')

        try:
            with mock.patch.object(manage.config, 'setup_logging',
                                   side_effect=lambda:
                                   manage.config.logging.setup(
                                       self.conf, 'coast')), \
                    mock.patch.object(clients, 'get_session',
                                      side_effect=fail):
                code, output, errors = self.run_command()
        finally:
            root.handlers = handlers
            root.setLevel(level)
        self.assertEqual(2, code)
        self.assertEqual('', output)
        self.assertIn('Service authentication is not configured.', errors)
        self.assertNotIn('credential-must-not-leak', errors)
