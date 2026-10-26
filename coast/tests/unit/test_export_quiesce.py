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

"""Quiesce lease, watchdog and snapshot-revert regressions."""

import json
import tempfile
from types import SimpleNamespace
from unittest import mock

from coast.agent.engines import coolify
from coast.agent import export
from coast.common import exception
from coast.conductor import exports
from coast.tests import base


EXPORT = '11111111-1111-1111-1111-111111111111'
VOLUME = '22222222-2222-2222-2222-222222222222'
OTHER = '33333333-3333-3333-3333-333333333333'
DATABASE = 'a' * 64
WEB = 'b' * 64


class Docker:
    def __init__(self):
        self.running = {DATABASE, WEB}
        self.actions = []
        self.fail_stop = False
        self.fail_start = False

    def __call__(self, *args, **kwargs):
        if args[0] == 'ps':
            return '\n'.join(sorted(self.running)).encode()
        if args[0] == 'inspect':
            mounts = [{'Type': 'volume', 'Source': '/data/database'}] if (
                args[-1] == DATABASE) else []
            return json.dumps(mounts).encode()
        self.actions.append((args[0], args[-1]))
        if args[0] == 'stop':
            self.running.discard(args[-1])
            if self.fail_stop:
                raise RuntimeError('sensitive engine output')
        else:
            if self.fail_start:
                raise RuntimeError('sensitive engine output')
            self.running.add(args[-1])
        return b''


class QuiesceTest(base.TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config.config(role='controller', state_path=directory.name,
                           group='agent')
        self.now = 1000
        self.docker = Docker()
        self.agent = export.Quiesce(self.conf, self.docker,
                                    lambda: self.now)
        self.body = {'export_id': EXPORT, 'volume_ids': [VOLUME]}

    def test_idempotence_and_resume_only_selected_running_containers(self):
        first = self.agent.quiesce(self.body)
        self.now += 10
        self.assertEqual(first, self.agent.quiesce(self.body))
        self.assertEqual({WEB}, self.docker.running)
        self.agent.resume({'export_id': EXPORT})
        self.agent.resume({'export_id': EXPORT})
        self.assertEqual({DATABASE, WEB}, self.docker.running)
        self.assertEqual([('stop', DATABASE), ('start', DATABASE)],
                         self.docker.actions)
        self.assertRaises(coolify.Conflict, self.agent.quiesce, self.body)

    def test_restart_retains_deadline_and_watchdog_resumes(self):
        lease = self.agent.quiesce(self.body)
        restarted = export.Quiesce(self.conf, self.docker, lambda: self.now)
        restarted.recover()
        self.assertEqual({WEB}, self.docker.running)
        self.now = lease['deadline']
        restarted.recover()
        restarted.recover()
        self.assertEqual({DATABASE, WEB}, self.docker.running)
        self.assertEqual('resumed', restarted.read(EXPORT)['state'])

    def test_stop_failure_resumes_uncertain_outcome(self):
        self.docker.fail_stop = True
        error = self.assertRaises(coolify.PreconditionFailed,
                                  self.agent.quiesce, self.body)
        self.assertNotIn('sensitive', str(error))
        self.assertEqual({DATABASE, WEB}, self.docker.running)
        self.assertEqual('resumed', self.agent.read(EXPORT)['state'])

    def test_failed_resume_keeps_pending_ids_for_retry(self):
        self.agent.quiesce(self.body)
        self.docker.fail_start = True
        self.now += 900
        self.agent.recover()
        self.assertEqual([DATABASE], self.agent.read(EXPORT)['containers'])
        self.docker.fail_start = False
        self.agent.recover()
        self.assertEqual('resumed', self.agent.read(EXPORT)['state'])
        self.assertEqual([], self.agent.read(EXPORT)['containers'])

    def test_restart_recovers_write_before_uncertain_stop(self):
        self.agent.write(EXPORT, {'volume_ids': [VOLUME],
                                  'containers': [DATABASE],
                                  'state': 'stopping',
                                  'deadline': self.now + 900})
        self.docker.running.discard(DATABASE)
        export.Quiesce(self.conf, self.docker, lambda: self.now).recover()
        self.assertIn(DATABASE, self.docker.running)

    def test_resume_overtakes_quiesce_and_blocks_late_stop(self):
        self.agent.resume({'export_id': EXPORT})
        self.assertRaises(coolify.Conflict, self.agent.quiesce, self.body)
        self.assertEqual([], self.docker.actions)

    def test_changed_selection_and_overlapping_lease_rejected(self):
        self.agent.quiesce(self.body)
        self.assertRaises(coolify.Conflict, self.agent.quiesce,
                          {'export_id': EXPORT, 'volume_ids': [OTHER]})
        self.assertRaises(coolify.Conflict, self.agent.quiesce,
                          {'export_id': OTHER, 'volume_ids': [VOLUME]})
        self.assertEqual([('stop', DATABASE)], self.docker.actions)

    def test_roles_and_request_validation_do_not_stop_containers(self):
        for body in ({}, {'export_id': '../invalid', 'volume_ids': [VOLUME]},
                     {'export_id': EXPORT, 'volume_ids': []},
                     {'export_id': EXPORT, 'volume_ids': [VOLUME, VOLUME]},
                     dict(self.body, unexpected='secret')):
            self.assertRaises(ValueError, self.agent.quiesce, body)
        for role in ('worker', 'build', 'controller'):
            self.config.config(role=role, group='agent')
            self.assertEqual(
                EXPORT, self.agent.validate(self.body, quiesce=True))
            self.assertEqual(
                EXPORT, self.agent.validate({'export_id': EXPORT}))
        self.assertEqual([], self.docker.actions)

    def test_worker_and_build_quiesce_and_recover(self):
        self.config.config(role='worker', group='agent')
        lease = self.agent.quiesce(self.body)
        self.assertEqual({WEB}, self.docker.running)
        self.now = lease['deadline']
        self.agent.recover()
        self.assertEqual('resumed', self.agent.read(EXPORT)['state'])
        self.assertEqual({DATABASE, WEB}, self.docker.running)
        self.config.config(role='build', group='agent')
        self.agent.quiesce({'export_id': OTHER, 'volume_ids': [VOLUME]})
        self.assertEqual({WEB}, self.docker.running)
        self.agent.resume({'export_id': OTHER})
        self.assertEqual({DATABASE, WEB}, self.docker.running)

    def test_deadline_is_not_extended_by_duplicate_quiesce(self):
        self.agent.quiesce(self.body)
        self.now += 900
        self.assertRaises(coolify.Conflict, self.agent.quiesce, self.body)
        self.assertEqual({DATABASE, WEB}, self.docker.running)

    def test_state_permissions_and_symlink_refusal(self):
        self.agent.quiesce(self.body)
        path = self.agent.root / EXPORT
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.agent.root.stat().st_mode & 0o777)
        path.unlink()
        path.symlink_to(self.agent.root / '.lock')
        self.assertRaises(OSError, self.agent.read, EXPORT)

    def test_shutdown_recovers_unexpired_lease(self):
        self.agent.quiesce(self.body)
        self.agent.recover(force=True)
        self.assertEqual({DATABASE, WEB}, self.docker.running)

    def test_deadline_during_stop_reverts_without_success(self):
        docker = self.docker

        def late(*args, **kwargs):
            result = docker(*args, **kwargs)
            if args[0] == 'stop':
                self.now += 900
            return result
        self.agent.command = late
        self.assertRaises(coolify.PreconditionFailed, self.agent.quiesce,
                          self.body)
        self.assertEqual({DATABASE, WEB}, self.docker.running)


class SnapshotTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.controller = SimpleNamespace(
            id='controller', mgmt_ip='192.0.2.10', role='control',
            project_id='project', stack_id='stack')
        self.export = SimpleNamespace(id=EXPORT, volume_ids=[VOLUME],
                                      project_id='project',
                                      stack_id='stack')
        self.volumes = [SimpleNamespace(
            id=VOLUME, cinder_volume_id=OTHER, project_id='project',
            stack_id='stack', server_id='controller',
            purpose='data')]
        self.order = []
        self.agent = mock.Mock()
        self.agent.post.side_effect = self.post
        self.cloud = mock.Mock()
        self.snapshot = SimpleNamespace(id='snapshot')
        self.cloud.snapshots.return_value = []
        self.cloud.create_snapshot.return_value = self.snapshot
        self.cloud.wait_for_status.side_effect = self.wait

    def post(self, controller, path, body):
        self.order.append(path)
        if path.endswith('/quiesce'):
            return {'status': 'quiesced', 'deadline': 1000}
        return {'status': 'resumed'}

    def wait(self, *args, **kwargs):
        self.order.append('snapshot-ready')

    def run_flow(self):
        with mock.patch.object(exports.time, 'time', return_value=100):
            return exports.snapshot_volumes(
                self.conf, self.controller, self.export, self.volumes,
                agent=self.agent, block_storage=self.cloud)

    def test_snapshot_wait_completes_before_resume(self):
        result = self.run_flow()
        self.assertEqual([self.snapshot], result)
        self.assertEqual(['/v1/export/quiesce', 'snapshot-ready',
                          '/v1/export/resume'], self.order)
        attrs = self.cloud.create_snapshot.call_args.kwargs
        self.assertEqual(OTHER, attrs['volume_id'])
        self.assertEqual(EXPORT, attrs['metadata']['export_id'])
        self.assertEqual('project', attrs['metadata']['project_id'])
        self.assertEqual('stack', attrs['metadata']['stack_id'])

    def test_failed_snapshot_deletes_partial_resources_and_resumes(self):
        self.cloud.wait_for_status.side_effect = RuntimeError('cloud failed')
        self.assertRaises(RuntimeError, self.run_flow)
        self.assertEqual(['/v1/export/quiesce', '/v1/export/resume',
                          '/v1/export/resume'], self.order)
        self.cloud.delete_snapshot.assert_called_once_with(
            self.snapshot, ignore_missing=True)

    def test_unknown_quiesce_outcome_always_attempts_resume(self):
        def failed(controller, path, body):
            self.order.append(path)
            if path.endswith('/quiesce'):
                raise exception.CoastException('Unknown outcome')
            return {'status': 'resumed'}
        self.agent.post.side_effect = failed
        self.assertRaises(exception.CoastException, self.run_flow)
        self.assertEqual(['/v1/export/quiesce', '/v1/export/resume'],
                         self.order)
        self.assertFalse(self.cloud.create_snapshot.called)

    def test_expired_lease_prevents_snapshot_and_resumes(self):
        def expired(controller, path, body):
            self.order.append(path)
            return {'status': 'quiesced', 'deadline': 99} if (
                path.endswith('/quiesce')) else {'status': 'resumed'}
        self.agent.post.side_effect = expired
        self.assertRaises(exception.CoastException, self.run_flow)
        self.assertFalse(self.cloud.create_snapshot.called)
        self.assertEqual('/v1/export/resume', self.order[-1])

    def test_cleanup_failure_does_not_skip_resume(self):
        self.cloud.wait_for_status.side_effect = RuntimeError(
            'snapshot failed')
        self.cloud.delete_snapshot.side_effect = RuntimeError('delete failed')
        self.assertRaises(RuntimeError, self.run_flow)
        self.assertEqual('/v1/export/resume', self.order[-1])

    def test_failed_resume_does_not_prevent_snapshot_revert(self):
        def failing_resume(controller, path, body):
            self.order.append(path)
            if path.endswith('/quiesce'):
                return {'status': 'quiesced', 'deadline': 1000}
            raise exception.CoastException('Resume failed')
        self.agent.post.side_effect = failing_resume
        self.assertRaises(exception.CoastException, self.run_flow)
        self.assertEqual(['/v1/export/quiesce', 'snapshot-ready',
                          '/v1/export/resume', '/v1/export/resume',
                          '/v1/export/resume', '/v1/export/resume'],
                         self.order)
        self.cloud.delete_snapshot.assert_called_once_with(
            self.snapshot, ignore_missing=True)

    def test_tls_configuration_is_required_before_any_network_call(self):
        client = exports.AgentClient(self.conf)
        with mock.patch.object(exports.management, 'socket_for_agent') as sock:
            self.assertRaises(exception.CoastException, client.post,
                              self.controller, '/v1/export/quiesce', {})
        self.assertFalse(sock.called)

    def test_owned_snapshot_is_adopted_after_interrupted_attempt(self):
        self.snapshot.name = 'coast-export-%s-%s' % (EXPORT, VOLUME)
        self.snapshot.volume_id = OTHER
        self.snapshot.metadata = {'coast': 'true', 'project_id': 'project',
                                  'stack_id': 'stack',
                                  'export_id': EXPORT, 'volume_id': VOLUME}
        self.cloud.snapshots.return_value = [self.snapshot]
        self.assertEqual([self.snapshot], self.run_flow())
        self.assertFalse(self.cloud.create_snapshot.called)

    def test_owned_snapshot_with_configured_prefix_is_adopted(self):
        self.config.config(resource_name_prefix='operator')
        self.snapshot.name = 'operator-export-%s-%s' % (EXPORT, VOLUME)
        self.snapshot.volume_id = OTHER
        self.snapshot.metadata = {'coast': 'true', 'project_id': 'project',
                                  'stack_id': 'stack',
                                  'export_id': EXPORT, 'volume_id': VOLUME}
        self.cloud.snapshots.return_value = [self.snapshot]
        self.assertEqual([self.snapshot], self.run_flow())
        self.assertFalse(self.cloud.create_snapshot.called)

    def test_foreign_snapshot_is_not_adopted_or_deleted(self):
        self.snapshot.name = 'coast-export-%s-%s' % (EXPORT, VOLUME)
        self.snapshot.volume_id = OTHER
        self.snapshot.metadata = {'project_id': 'foreign'}
        self.cloud.snapshots.return_value = [self.snapshot]
        self.assertRaises(exception.CoastException, self.run_flow)
        self.assertFalse(self.cloud.create_snapshot.called)
        self.assertFalse(self.cloud.delete_snapshot.called)
        self.assertEqual('/v1/export/resume', self.order[-1])

    def test_foreign_or_unlisted_owner_never_quiesces_or_snapshots(self):
        volume = self.volumes[0]
        for name, value in (('project_id', 'foreign'),
                            ('stack_id', 'foreign'),
                            ('server_id', 'worker'), ('purpose', 'scratch'),
                            ('id', OTHER), ('cinder_volume_id', None)):
            original = getattr(volume, name)
            setattr(volume, name, value)
            self.assertRaises(exception.CoastException, self.run_flow)
            setattr(volume, name, original)
        self.assertEqual([], self.order)
        self.assertFalse(self.cloud.create_snapshot.called)

    def test_resume_precedes_cloud_cleanup(self):
        self.cloud.wait_for_status.side_effect = RuntimeError(
            'snapshot failed')

        def cleanup(*args, **kwargs):
            self.assertEqual('/v1/export/resume', self.order[-1])
            self.order.append('delete-snapshot')
        self.cloud.delete_snapshot.side_effect = cleanup
        self.assertRaises(RuntimeError, self.run_flow)
        self.assertEqual(['/v1/export/quiesce', '/v1/export/resume',
                          'delete-snapshot', '/v1/export/resume'], self.order)

    def test_multi_owner_fan_out_quiesces_before_any_snapshot(self):
        worker_id = '44444444-4444-4444-4444-444444444444'
        build_id = '55555555-5555-5555-5555-555555555555'
        worker = SimpleNamespace(
            id='worker', mgmt_ip='192.0.2.11', role='worker',
            project_id='project', stack_id='stack')
        build = SimpleNamespace(
            id='build', mgmt_ip='192.0.2.12', role='build',
            project_id='project', stack_id='stack')
        volumes = self.volumes + [
            SimpleNamespace(
                id=worker_id, cinder_volume_id='cinder-worker',
                project_id='project', stack_id='stack',
                server_id='worker', purpose='data'),
            SimpleNamespace(
                id=build_id, cinder_volume_id='cinder-build',
                project_id='project', stack_id='stack',
                server_id='build', purpose='docker')]
        self.export.volume_ids = [volume.id for volume in volumes]
        calls = []

        def post(server, path, body):
            calls.append((server.id, path, list(body.get('volume_ids', []))))
            self.order.append((server.id, path))
            if path.endswith('/quiesce'):
                return {'status': 'quiesced', 'deadline': 1000}
            return {'status': 'resumed'}

        self.agent.post.side_effect = post
        with mock.patch.object(exports.time, 'time', return_value=100):
            exports.snapshot_volumes(
                self.conf, [worker, build, self.controller], self.export,
                volumes, agent=self.agent, block_storage=self.cloud)
        quiesced = [item[0] for item in calls if item[1].endswith('/quiesce')]
        resumed = [item[0] for item in calls if item[1].endswith('/resume')]
        self.assertEqual(['build', 'controller', 'worker'], quiesced)
        self.assertEqual(['build', 'controller', 'worker'], resumed)
        self.assertEqual(
            self.export.volume_ids, calls[0][2])
        first_snapshot = self.order.index('snapshot-ready')
        last_quiesce = max(
            index for index, item in enumerate(self.order)
            if item != 'snapshot-ready' and item[1].endswith('/quiesce'))
        self.assertLess(last_quiesce, first_snapshot)

    def test_partial_resume_failure_attempts_every_owner(self):
        worker_id = '44444444-4444-4444-4444-444444444444'
        worker = SimpleNamespace(
            id='worker', mgmt_ip='192.0.2.11', role='worker',
            project_id='project', stack_id='stack')
        volumes = self.volumes + [SimpleNamespace(
            id=worker_id, cinder_volume_id='cinder-worker',
            project_id='project', stack_id='stack',
            server_id='worker', purpose='data')]
        self.export.volume_ids = [volume.id for volume in volumes]
        resumes = []

        def post(server, path, body):
            self.order.append((server.id, path))
            if path.endswith('/quiesce'):
                return {'status': 'quiesced', 'deadline': 1000}
            resumes.append(server.id)
            if server.id == 'controller':
                raise exception.CoastException('sensitive resume detail')
            return {'status': 'resumed'}

        self.agent.post.side_effect = post
        with mock.patch.object(exports.time, 'time', return_value=100):
            error = self.assertRaises(
                exception.CoastException, exports.snapshot_volumes,
                self.conf, [worker, self.controller], self.export, volumes,
                agent=self.agent, block_storage=self.cloud)
        self.assertEqual('Export resume failed.', str(error))
        self.assertNotIn('sensitive', str(error))
        self.assertEqual(['controller', 'worker'], resumes[:2])
        self.assertLess(
            self.order.index(('worker', '/v1/export/quiesce')),
            self.order.index('snapshot-ready'))

    def test_later_quiesce_failure_resumes_every_owner(self):
        worker_id = '44444444-4444-4444-4444-444444444444'
        worker = SimpleNamespace(
            id='worker', mgmt_ip='192.0.2.11', role='worker',
            project_id='project', stack_id='stack')
        volumes = self.volumes + [SimpleNamespace(
            id=worker_id, cinder_volume_id='cinder-worker',
            project_id='project', stack_id='stack',
            server_id='worker', purpose='data')]
        self.export.volume_ids = [volume.id for volume in volumes]

        def post(server, path, body):
            self.order.append((server.id, path))
            if path.endswith('/quiesce') and server.id == 'worker':
                raise exception.CoastException('Quiesce failed')
            if path.endswith('/quiesce'):
                return {'status': 'quiesced', 'deadline': 1000}
            return {'status': 'resumed'}

        self.agent.post.side_effect = post
        with mock.patch.object(exports.time, 'time', return_value=100):
            self.assertRaises(
                exception.CoastException, exports.snapshot_volumes,
                self.conf, [self.controller, worker], self.export, volumes,
                agent=self.agent, block_storage=self.cloud)
        self.assertIn(('controller', '/v1/export/quiesce'), self.order)
        self.assertIn(('worker', '/v1/export/quiesce'), self.order)
        self.assertIn(('controller', '/v1/export/resume'), self.order)
        self.assertIn(('worker', '/v1/export/resume'), self.order)
        self.assertNotIn('snapshot-ready', self.order)
        self.assertFalse(self.cloud.create_snapshot.called)


class AgentClientTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(client_certificate='/client.pem',
                           client_key='/key.pem', server_ca='/ca.pem',
                           group='export')
        self.controller = SimpleNamespace(role='control',
                                          mgmt_ip='192.0.2.10')

    def request(self, status=200, content=b'{"status":"resumed"}'):
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = status
        response.read.return_value = content
        with mock.patch.object(exports.ssl, 'create_default_context'), \
                mock.patch.object(exports.management, 'socket_for_agent'), \
                mock.patch.object(exports.http.client, 'HTTPSConnection',
                                  return_value=connection):
            return exports.AgentClient(self.conf).post(
                self.controller, '/v1/export/resume', {'export_id': EXPORT})

    def test_parses_successful_agent_result(self):
        self.assertEqual({'status': 'resumed'}, self.request())

    def test_worker_and_build_roles_reach_the_agent(self):
        for role in ('worker', 'build', 'control'):
            self.controller.role = role
            self.assertEqual({'status': 'resumed'}, self.request())
        self.controller.role = 'controller'
        with mock.patch.object(exports.management, 'socket_for_agent') as sock:
            self.assertRaises(
                exception.CoastException, exports.AgentClient(self.conf).post,
                self.controller, '/v1/export/resume', {'export_id': EXPORT})
        self.assertFalse(sock.called)

    def test_rejects_failed_malformed_and_oversized_responses(self):
        for status, content in ((412, b'sensitive upstream output'),
                                (200, b'not-json'), (200, b'[]'),
                                (200, b'x' * (
                                    self.conf.agent.max_body_size + 1))):
            error = self.assertRaises(exception.CoastException, self.request,
                                      status, content)
            self.assertNotIn('sensitive', str(error))
            self.assertEqual('Export agent request failed.', str(error))

    def test_network_failure_does_not_expose_upstream_exception(self):
        with mock.patch.object(exports.ssl, 'create_default_context'), \
                mock.patch.object(exports.management, 'socket_for_agent',
                                  side_effect=OSError('sensitive detail')):
            error = self.assertRaises(
                exception.CoastException, exports.AgentClient(self.conf).post,
                self.controller, '/v1/export/resume', {'export_id': EXPORT})
        self.assertNotIn('sensitive', str(error))

    def test_docker_failures_never_expose_command_output(self):
        result = SimpleNamespace(returncode=1, stdout=b'sensitive detail')
        with mock.patch.object(export.subprocess, 'run', return_value=result):
            error = self.assertRaises(coolify.PreconditionFailed,
                                      export.docker, 'ps')
        self.assertNotIn('sensitive', str(error))
