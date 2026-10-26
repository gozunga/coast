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

import datetime
import io
from types import SimpleNamespace
from unittest import mock
import uuid

import fixtures

from coast.cmd import manage
from coast.common import context
from coast.common import exception
from coast.common import janitor
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast.tests import base


LIVE = '11111111-1111-4111-8111-111111111111'
DEAD = '22222222-2222-4222-8222-222222222222'


def resource(**values):
    return SimpleNamespace(**dict({'id': DEAD, 'name': '', 'tags': [],
                                  'metadata': {}}, **values))


class JanitorTest(base.TestCase):
    def test_exact_markers_and_configured_prefix(self):
        for row in (resource(tags=['coast']),
                    resource(metadata={'coast': 'true'}),
                    resource(name='operator-' + DEAD)):
            self.assertTrue(
                janitor.is_orphan('server', row, set(), 'operator'))
        for row in (resource(tags=['coastal']),
                    resource(metadata={'coast': 'false'}),
                    resource(name='coast-' + DEAD),
                    resource(name='operatorium-' + DEAD),
                    resource(name='not-operator-' + DEAD)):
            self.assertFalse(janitor.is_orphan('server', row, set(),
                                               'operator'))

    def test_live_direct_owner_and_related_references(self):
        cases = [resource(id=LIVE),
                 resource(tags=['coast', 'environment_id=' + LIVE]),
                 resource(metadata={'coast': 'true', 'stack_id': LIVE}),
                 resource(name='coast-' + LIVE + '-mgmt'),
                 resource(name='coast-export-' + DEAD + '-' + LIVE)]
        for key in ('device_id', 'network_id', 'router_id', 'port_id',
                    'volume_id', 'snapshot_id'):
            cases.append(resource(tags=['coast'], **{key: LIVE}))
        cases.append(resource(tags=['coast'],
                              attachments=[{'server_id': LIVE}]))
        for row in cases:
            self.assertFalse(janitor.is_orphan('volume', row, {LIVE}, 'coast'))

    def test_short_named_guest_is_owned_and_live_stack_protects_it(self):
        guest = resource(name='coast-shop-5bad06-worker-2',
                         tags=['coast', 'stack_id=' + LIVE])
        self.assertFalse(janitor.is_orphan('server', guest, {LIVE}, 'coast'))
        self.assertTrue(janitor.is_orphan('server', guest, set(), 'coast'))

    def test_uuid_substrings_are_not_live_owners(self):
        row = resource(name='coast-a' + LIVE + 'f')
        self.assertTrue(janitor.is_orphan('port', row, {LIVE}, 'coast'))

    def test_secret_identity_uses_uuid_not_url(self):
        row = resource(name='coast-credential',
                       secret_ref='https://secrets.example.com/v1/secrets/' +
                       DEAD)
        self.assertEqual(DEAD, janitor._identity('secret', row))
        self.assertFalse(janitor.is_orphan('secret', row, {DEAD}, 'coast'))
        self.assertTrue(janitor.is_orphan('secret', row, set(), 'coast'))

    def test_colon_secret_requires_reference_and_live_owners(self):
        row = resource(secret_id=DEAD, name='coast:%s:env:%s' % (LIVE, LIVE))
        self.assertTrue(janitor.is_orphan('secret', row, {LIVE}, 'coast'))
        self.assertFalse(janitor.is_orphan(
            'secret', row, {LIVE, DEAD}, 'coast'))
        self.assertTrue(janitor.is_orphan('secret', row, {DEAD}, 'coast'))
        row.name = 'unmarked'
        self.assertFalse(janitor.is_orphan('secret', row, set(), 'coast'))

    def test_only_reference_uuids_enter_missing_report(self):
        self.assertEqual({LIVE}, janitor._secret_ids({
            'https://secrets.example.com/v1/secrets/' + LIVE,
            'https://example.com/secrets/arbitrary-spec-value',
            'https://example.com/secrets/' + DEAD + '?value=private',
            '/secrets/' + DEAD, LIVE}))

    def test_reference_read_rejects_non_admin(self):
        self.assertRaises(exception.NotAuthorized, janitor.references,
                          SimpleNamespace(is_admin=False))

    def test_missing_service_project_fails_closed(self):
        session = SimpleNamespace(get_project_id=lambda: None)
        with mock.patch.object(janitor.clients, 'get_session',
                               return_value=session):
            self.assertRaises(exception.NotAuthorized, janitor.report,
                              self.conf)

    def test_command_masks_failure_and_has_no_partial_output(self):
        output, errors = io.StringIO(), io.StringIO()
        conf = SimpleNamespace(
            command=SimpleNamespace(name='janitor-report'),
            register_cli_opt=lambda opt: None,
            set_override=lambda name, value: None)
        with mock.patch.object(manage, 'CONF', conf), mock.patch.object(
                manage.config, 'parse_args'), mock.patch.object(
                manage.config, 'setup_logging'), mock.patch.object(
                janitor, 'report', side_effect=RuntimeError('token-secret')), \
                mock.patch('sys.stdout', output), \
                mock.patch('sys.stderr', errors):
            self.assertEqual(2, manage.main())
        self.assertEqual('', output.getvalue())
        self.assertNotIn('token-secret', errors.getvalue())
        self.assertIn('no complete report', errors.getvalue())

    def test_empty_name_prefix_fails_closed(self):
        self.config.config(resource_name_prefix='')
        session = SimpleNamespace(get_project_id=lambda: 'service-project')
        with mock.patch.object(janitor.clients, 'get_session',
                               return_value=session):
            self.assertRaises(ValueError, janitor.report, self.conf)


class RequiredReferencesTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        base_models.Base.metadata.create_all(self.db.engine)
        self.ctx = context.RequestContext(is_admin=True)
        self.refs = {}

        def ref(name):
            value = ('https://secrets.example.com/v1/secrets/' +
                     str(uuid.uuid4()))
            self.refs[name] = value
            return value

        with api.writer.using(self.ctx) as session:
            self.stack = models.Stack(
                project_id='tenant', name='stack', driver='noop',
                engine_version='test', plan='test', status='ERROR',
                engine_token_ref=ref('stack'))
            self.source = models.Source(
                project_id='tenant', type='git', credentials_ref=ref('source'),
                deploy_key_rotation={'previous_ref': ref('rotation')})
            session.add_all([self.stack, self.source])
            session.flush()
            self.service = models.Service(
                project_id='tenant', stack_id=self.stack.id,
                source_id=self.source.id, name='app', auto_deploy=False,
                status='ERROR', task_state='NONE', compose_ref=ref('compose'),
                dockerfile_ref=ref('dockerfile'), password_ref=ref('password'),
                file_mount_secret_refs=[ref('file_refs')])
            session.add(self.service)
            session.flush()
            self.deployment = models.Deployment(
                project_id='tenant', service_id=self.service.id, number=1,
                spec={'value_ref': ref('deployment')}, trigger='manual',
                status='SUCCEEDED')
            session.add_all([
                self.deployment,
                models.Secret(
                    project_id='tenant', service_id=self.service.id,
                    key='KEY', is_secret=True, build_time=False, version=1,
                    value_ref=ref('secret')),
                models.FileMount(
                    project_id='tenant', service_id=self.service.id,
                    container_path='/config', content_ref=ref('file')),
                models.BackupPolicy(
                    project_id='tenant', service_id=self.service.id,
                    engine='test', container='test', key_ref=ref('policy'),
                    signing_key_ref=ref('signer')),
                models.Backup(
                    project_id='tenant', service_id=self.service.id,
                    kind='manual', status='AVAILABLE', engine='test',
                    container='test', key_ref=ref('backup')),
                models.BackupAccount(
                    project_id='tenant', key_ref=ref('account'))])
        self.useFixture(fixtures.MockPatchObject(
            janitor.clients, 'get_session', return_value=SimpleNamespace(
                get_project_id=lambda: 'service-project')))
        self.useFixture(fixtures.MockPatchObject(
            janitor, '_LISTS',
            [('secret', 'barbican', 'key_manager', 'secrets')]))
        self.proxy = self.useFixture(fixtures.MockPatchObject(
            janitor.clients, 'get_sensitive_proxy')).mock.return_value
        self.proxy.secrets.return_value = []

    def missing(self):
        return janitor.report(self.conf)['missing_secrets']

    def assert_required(self, names):
        expected = {self.refs[name] for name in names}
        self.assertEqual(expected, janitor.required_references(self.ctx))
        self.assertEqual(sorted(janitor._secret_ids(expected)), self.missing())
        # Historical refs continue to protect resources from orphan detection.
        protected = janitor.references(self.ctx)
        self.assertTrue(set(self.refs.values()).issubset(protected))
        for identifier in janitor._secret_ids(self.refs.values()):
            self.assertFalse(janitor.is_orphan(
                'secret', resource(secret_id=identifier, name='coast-saved'),
                protected, 'coast'))

    def delete(self, model, identifier):
        with api.writer.using(self.ctx) as session:
            session.query(model).filter_by(id=identifier).update({
                'deleted_at': datetime.datetime.now(datetime.timezone.utc)})

    def test_live_owners_require_all_secret_locations(self):
        self.assert_required(self.refs)

    def test_deleted_service_and_children_are_not_required(self):
        self.delete(models.Service, self.service.id)
        self.assert_required(['stack', 'account'])

    def test_deleted_stack_and_children_are_not_required(self):
        self.delete(models.Stack, self.stack.id)
        self.assert_required(['account'])

    def test_deleted_deployment_spec_is_not_required(self):
        self.delete(models.Deployment, self.deployment.id)
        self.assert_required(set(self.refs) - {'deployment'})

    def test_unlinked_source_is_not_required(self):
        with api.writer.using(self.ctx) as session:
            session.add(models.Source(
                project_id='tenant', type='git',
                credentials_ref='https://secrets.example.com/v1/secrets/' +
                DEAD))
        self.assert_required(self.refs)

    def test_owner_deleted_during_scan_is_not_missing(self):
        self.proxy.secrets.side_effect = lambda: (
            self.delete(models.Stack, self.stack.id) or [])
        self.assertEqual(
            sorted(janitor._secret_ids([self.refs['account']])),
            self.missing())

    def test_owner_created_during_scan_is_not_missing(self):
        self.delete(models.Stack, self.stack.id)

        def revive():
            with api.writer.using(self.ctx) as session:
                session.query(models.Stack).filter_by(
                    id=self.stack.id).update({'deleted_at': None})
            return []

        self.proxy.secrets.side_effect = revive
        self.assertEqual(
            sorted(janitor._secret_ids([self.refs['account']])),
            self.missing())

    def test_required_reference_read_rejects_non_admin(self):
        self.assertRaises(exception.NotAuthorized, janitor.required_references,
                          SimpleNamespace(is_admin=False))
