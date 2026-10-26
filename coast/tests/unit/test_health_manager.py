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

import contextlib
import datetime
import hashlib
import hmac
import json
import socket
import threading
from unittest import mock
import uuid

import fixtures

from coast.agent import heartbeat
from coast.api import secret_store
from coast.common import context
from coast.common import exception
from coast.common import management
from coast.db import api
from coast.db import base_models
from coast.db import models
from coast.health_manager import keys
from coast.health_manager import receiver
from coast.health_manager import service
from coast.tests import base


class ReceiverTest(base.TestCase):
    def setUp(self):
        super().setUp()
        for name in ('reader', 'writer'):
            self.useFixture(fixtures.MonkeyPatch(
                'coast.db.api.' + name, getattr(self.db.facade, name)))
        self.useFixture(fixtures.MonkeyPatch('coast.db.api.CONF', self.conf))
        base_models.Base.metadata.create_all(self.db.engine)
        self.owner = context.RequestContext(project_id='tenant')
        self.env = api.stacks.create(self.owner, dict(
            id=str(uuid.uuid4()), name='env', plan='test', driver='noop',
            engine_version='test', status='PENDING'))
        self.server = self.make_server()
        self.key = b'k' * 32
        self.lookup = mock.Mock(return_value=self.key)
        directory = self.useFixture(fixtures.TempDir()).path
        self.config.config(state_path=directory, group='health_manager',
                           max_packets_per_second=10000)
        self.now = 1800000000.0
        self.receiver = receiver.Receiver(self.lookup, self.conf,
                                          clock=lambda: self.now)
        self.addCleanup(self.receiver.close)
        self.payload = dict(server_id=self.server.id, env_id=self.env.id,
                            seq=1, ts=self.now, docker_ok=True, disk_pct=12.5,
                            engine_ok=False)

    def make_server(self, role='worker'):
        return api.servers.create(self.owner, dict(
            id=str(uuid.uuid4()), stack_id=self.env.id, name=role,
            kind='vm', role=role, status='PENDING', task_state='NONE',
            mgmt_ip='192.0.2.10'))

    def accept(self, **changes):
        payload = dict(self.payload, **changes)
        return self.receiver.accept(heartbeat.encode(payload, self.key),
                                    self.server.mgmt_ip)

    def test_sender_round_trip_batched_for_all_roles(self):
        for role in ('worker', 'build', 'control'):
            server = self.make_server(role)
            self.assertTrue(self.accept(server_id=server.id))
            self.assertIsNone(api.servers.get(
                self.owner, server.id).last_heartbeat_at)
        self.receiver.flush()
        expected = datetime.datetime.fromtimestamp(
            self.now, datetime.timezone.utc).replace(tzinfo=None)
        for row in api.servers.list(self.owner):
            if row.id != self.server.id:
                self.assertEqual(expected, row.last_heartbeat_at)
                self.assertEqual('PENDING', row.status)

    def test_bad_hmac_and_key_failure_do_not_write(self):
        packet = heartbeat.encode(self.payload, b'x' * 32)
        self.assertFalse(self.receiver.accept(packet, self.server.mgmt_ip))
        for key in (None, b'short', 'hex-string'):
            self.lookup.return_value = key
            self.assertFalse(self.accept())
        self.lookup.side_effect = RuntimeError('secret-do-not-log')
        self.assertFalse(self.accept())
        self.assertEqual({}, self.receiver.pending)
        self.assertFalse((self.receiver.state / self.server.id).exists())

    def test_builtin_lookup_accepts_and_drops_secret_failure(self):
        api.stacks.update(self.owner, self.env.id,
                          {'heartbeat_key_ref': 'opaque-ref'})
        self.receiver.key_lookup = keys.StackKeyLookup()
        logs = self.useFixture(fixtures.FakeLogger())
        with mock.patch.object(secret_store, 'call',
                               return_value=self.key.hex()):
            self.assertTrue(self.accept())
        self.receiver.key_lookup.cache.clear()
        with mock.patch.object(secret_store, 'call',
                               side_effect=RuntimeError(self.key.hex())):
            self.assertFalse(self.accept(seq=2))
        self.assertEqual(
            '1', (self.receiver.state / self.server.id).read_text())
        self.assertNotIn(self.key.hex(), logs.output)

    def test_expired_key_authenticates_heartbeats_during_store_outage(self):
        api.stacks.update(self.owner, self.env.id,
                          {'heartbeat_key_ref': 'opaque-ref'})
        self.receiver.key_lookup = keys.StackKeyLookup(self.conf)
        with mock.patch.object(keys.time, 'monotonic', return_value=100):
            with mock.patch.object(secret_store, 'call',
                                   return_value=self.key.hex()):
                self.assertTrue(self.accept())
        with mock.patch.object(keys.time, 'monotonic', return_value=131):
            with mock.patch.object(
                    secret_store, 'call',
                    side_effect=exception.SecretStoreUnavailable()):
                self.assertTrue(self.accept(seq=2))

    def test_slow_secret_outage_does_not_block_heartbeat_accept_or_flush(self):
        api.stacks.update(self.owner, self.env.id,
                          {'heartbeat_key_ref': 'opaque-ref'})
        lookup = keys.StackKeyLookup(self.conf)
        self.receiver.key_lookup = lookup
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(keys.time, 'monotonic', return_value=100):
            with mock.patch.object(secret_store, 'call',
                                   return_value=self.key.hex()):
                self.assertTrue(self.accept())

        def slow_get(*args):
            release.wait(2)
            release.set()
            raise exception.SecretStoreUnavailable()

        with mock.patch.object(keys.time, 'monotonic', return_value=131):
            with mock.patch.object(secret_store, 'call',
                                   side_effect=slow_get) as fetch:
                for sequence in range(2, 12):
                    self.assertTrue(self.accept(seq=sequence))
                self.receiver.flush()
                self.assertIsNotNone(api.servers.get(
                    self.owner, self.server.id).last_heartbeat_at)
                # A real backend request is still blocked after accepted
                # heartbeats have reached the database.
                self.assertFalse(release.is_set())
                self.assertEqual(1, fetch.call_count)
                pending = next(iter(lookup.refreshes.values()))
                release.set()
                self.assertRaises(exception.SecretStoreUnavailable,
                                  pending.result, timeout=1)
                self.assertTrue(self.accept(seq=12))
        with mock.patch.object(keys.time, 'monotonic', return_value=161):
            with mock.patch.object(secret_store, 'call',
                                   return_value=self.key.hex()) as fetch:
                self.assertTrue(self.accept(seq=13))
                self.assertEqual(1, fetch.call_count)

    def test_duplicate_and_lower_sequence_rejected_after_restart(self):
        self.assertTrue(self.accept(seq=10))
        self.assertFalse(self.accept(seq=10))
        self.assertFalse(self.accept(seq=9))
        self.receiver.close()
        self.receiver = receiver.Receiver(self.lookup, self.conf,
                                          clock=lambda: self.now)
        self.addCleanup(self.receiver.close)
        self.assertFalse(self.accept(seq=10))
        self.assertTrue(self.accept(seq=11))

    def test_corrupt_checkpoint_and_write_failure_fail_closed(self):
        path = self.receiver.state / self.server.id
        path.write_text('invalid')
        self.assertFalse(self.accept())
        path.unlink()
        with mock.patch.object(receiver.os, 'replace', side_effect=OSError):
            self.assertFalse(self.accept())
        self.assertEqual({}, self.receiver.pending)
        self.assertEqual(['.lock'],
                         [p.name for p in self.receiver.state.iterdir()])

    def test_schema_and_freshness_boundaries(self):
        invalid = [dict(seq=value) for value in (True, 0, -1, 1.5, 2 ** 64)]
        invalid += [dict(ts=value) for value in
                    (True, self.now - 60, self.now + 60, float('nan'))]
        invalid += [dict(disk_pct=value) for value in
                    (-1, 101, True, float('inf'))]
        invalid += [dict(server_id='bad'), dict(env_id=None),
                    dict(docker_ok=1), dict(engine_ok=None), dict(extra=1)]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertFalse(self.accept(**changes))
        self.assertTrue(self.accept(ts=self.now - 59.9))
        self.receiver.flush()
        seen = api.servers.get(self.owner, self.server.id).last_heartbeat_at
        self.assertEqual(self.now, seen.replace(
            tzinfo=datetime.timezone.utc).timestamp())

    def test_malformed_envelopes_and_size_limit(self):
        for packet in (b'', b'x' * 32, b'\xff' * 40,
                       b'x' * (self.conf.health_manager.max_packet_size + 1),
                       b'[]' + b'x' * 32,
                       b'{"seq":1,"seq":2}' + b'x' * 32):
            self.assertFalse(self.receiver.accept(packet, self.server.mgmt_ip))
        self.lookup.assert_not_called()
        payload = dict(self.payload)
        payload.pop('engine_ok')
        self.assertFalse(self.receiver.accept(
            heartbeat.encode(payload, self.key), self.server.mgmt_ip))

    def test_noncanonical_and_non_utf8_envelopes_rejected(self):
        for message in (json.dumps(self.payload).encode('utf-8'),
                        json.dumps(self.payload).encode('utf-16')):
            digest = hmac.new(self.key, message, hashlib.sha256).digest()
            self.assertFalse(self.receiver.accept(message + digest,
                                                  self.server.mgmt_ip))
        self.lookup.assert_not_called()

    def test_wrong_source_stack_missing_and_deleted_resources(self):
        packet = heartbeat.encode(self.payload, self.key)
        self.assertFalse(self.receiver.accept(packet, '192.0.2.11'))
        self.assertFalse(self.accept(env_id=str(uuid.uuid4())))
        self.assertFalse(self.accept(server_id=str(uuid.uuid4())))
        other = api.stacks.create(self.owner, dict(
            id=str(uuid.uuid4()), name='other', plan='test', driver='noop',
            engine_version='test', status='PENDING'))
        self.assertFalse(self.accept(env_id=other.id))
        api.servers.delete(self.owner, self.server.id)
        self.assertFalse(self.accept())

    def test_deleted_stack_rejected(self):
        api.stacks.delete(self.owner, self.env.id)
        self.assertFalse(self.accept())

    def test_flush_rechecks_identity_and_never_moves_last_seen_back(self):
        self.assertTrue(self.accept())
        later = datetime.datetime.fromtimestamp(
            self.now + 10, datetime.timezone.utc).replace(tzinfo=None)
        api.servers.update(self.owner, self.server.id,
                           {'last_heartbeat_at': later})
        self.receiver.flush()
        self.assertEqual(later, api.servers.get(
            self.owner, self.server.id).last_heartbeat_at)
        self.assertTrue(self.accept(seq=2))
        api.servers.update(self.owner, self.server.id,
                           {'mgmt_ip': '192.0.2.11',
                            'last_heartbeat_at': None})
        self.receiver.flush()
        self.assertIsNone(api.servers.get(
            self.owner, self.server.id).last_heartbeat_at)

    def test_flush_keeps_updated_at(self):
        stamped = datetime.datetime(2026, 1, 2, 3, 4, 5)
        with api.writer.using(self.owner) as session:
            session.query(models.Server).filter_by(
                id=self.server.id).update(
                    {'updated_at': stamped}, synchronize_session=False)
        before = api.servers.get(self.owner, self.server.id)
        self.assertEqual(stamped, before.updated_at)
        self.assertIsNone(before.last_heartbeat_at)
        self.assertTrue(self.accept())
        self.receiver.flush()
        row = api.servers.get(self.owner, self.server.id)
        self.assertEqual(stamped, row.updated_at)
        self.assertIsNotNone(row.last_heartbeat_at)

    def test_flush_skips_deleted_rows(self):
        self.assertTrue(self.accept())
        api.servers.delete(self.owner, self.server.id)
        self.receiver.flush()
        row = api.servers.get(self.owner, self.server.id, show_deleted=True)
        self.assertIsNone(row.last_heartbeat_at)

    def test_batch_bound_and_failed_transaction_retains_pending(self):
        self.config.config(batch_size=1, group='health_manager')
        self.assertTrue(self.accept())
        second = self.make_server('build')
        self.assertTrue(self.accept(server_id=second.id))
        self.assertEqual(1, len(self.receiver.pending))
        self.assertIsNotNone(api.servers.get(
            self.owner, self.server.id).last_heartbeat_at)
        with mock.patch.object(api.writer, 'using', side_effect=RuntimeError):
            self.assertRaises(RuntimeError, self.receiver.flush)
        self.assertEqual(1, len(self.receiver.pending))
        self.receiver.flush()
        self.assertEqual({}, self.receiver.pending)

    def test_global_rate_limit_before_lookup(self):
        self.config.config(max_packets_per_second=1, group='health_manager')
        with mock.patch.object(receiver.time, 'monotonic', return_value=100):
            self.assertTrue(self.accept())
            self.lookup.reset_mock()
            self.assertFalse(self.accept(seq=2))
            self.lookup.assert_not_called()
        with mock.patch.object(receiver.time, 'monotonic', return_value=101):
            self.assertTrue(self.accept(seq=2))

    def test_state_directory_has_one_receiver_owner(self):
        self.assertRaisesRegex(ValueError, 'already in use', receiver.Receiver,
                               self.lookup, self.conf)

    def test_state_directory_permissions(self):
        self.receiver.state.chmod(0o755)
        self.assertRaises(ValueError, receiver.Receiver, self.lookup,
                          self.conf)
        self.receiver.state.chmod(0o700)


class ManagementSocketTest(base.TestCase):
    def test_socket_created_bound_and_restored_for_udp_and_tcp(self):
        self.config.config(bind_ip='192.0.2.20', group='health_manager')
        events = []

        @contextlib.contextmanager
        def entered(name):
            events.append('enter:' + name)
            yield
            events.append('restore')

        sock = mock.Mock()
        sock.bind.side_effect = lambda address: events.append('bind')
        with mock.patch.object(management.namespace, 'management_namespace',
                               side_effect=entered), mock.patch.object(
                                   management.socket, 'socket',
                                   side_effect=lambda *args: (
                                       events.append('socket') or sock)):
            for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
                events.clear()
                result = management.socket_for_agent(self.conf, kind)
                self.assertIs(sock, result)
                self.assertEqual(['enter:coast-mgmt', 'socket', 'bind',
                                  'restore'], events)

    def test_udp_5565_binds_only_the_management_address(self):
        self.config.config(bind_ip='192.0.2.20', group='health_manager')
        sock = mock.Mock()
        with mock.patch.object(management.namespace,
                               'management_namespace'), \
                mock.patch.object(management.socket, 'socket',
                                  return_value=sock) as create:
            result = management.socket_for_agent(
                self.conf, socket.SOCK_DGRAM, 5565)
        self.assertIs(sock, result)
        create.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind.assert_called_once_with(('192.0.2.20', 5565))

    def test_empty_namespace_still_binds_the_management_address(self):
        self.config.config(bind_ip='192.0.2.20', group='health_manager')
        self.config.config(mgmt_netns='', group='controller_worker')
        sock = mock.Mock()
        with mock.patch.object(management.namespace,
                               'management_namespace') as setns, \
                mock.patch.object(management.socket, 'socket',
                                  return_value=sock):
            management.socket_for_agent(
                self.conf, socket.SOCK_DGRAM, 5565)
        setns.assert_not_called()
        sock.bind.assert_called_once_with(('192.0.2.20', 5565))

    def test_invalid_bind_ip_and_failed_bind(self):
        for ip in (None, '0.0.0.0', '127.0.0.1', '224.0.0.1', 'bad'):
            self.config.config(bind_ip=ip, group='health_manager')
            self.assertRaises((ValueError, TypeError),
                              management.socket_for_agent, self.conf)
        self.config.config(bind_ip='192.0.2.20', group='health_manager')
        sock = mock.Mock()
        sock.bind.side_effect = OSError
        with mock.patch.object(management.namespace, 'management_namespace'), \
                mock.patch.object(management.socket, 'socket',
                                  return_value=sock):
            self.assertRaises(OSError, management.socket_for_agent, self.conf)
        self.assertTrue(sock.close.called)


class ServiceTest(base.TestCase):
    def test_empty_lookup_fails_before_socket_creation(self):
        self.config.config(key_lookup='', group='health_manager')
        instance = service.Service(self.conf)
        with mock.patch.object(management, 'socket_for_agent') as factory:
            self.assertRaisesRegex(ValueError, 'Q86', instance.start)
        factory.assert_not_called()

    def test_start_with_default_builtin_lookup(self):
        directory = self.useFixture(fixtures.TempDir()).path
        self.config.config(
            state_path=directory, bind_ip='192.0.2.20',
            group='health_manager')
        instance = service.Service(self.conf)
        with mock.patch.object(management, 'socket_for_agent'):
            instance.prepare()
        self.addCleanup(instance.stop)
        self.assertIsInstance(instance.key_lookup, keys.StackKeyLookup)

    def test_shutdown_drains_batch_and_closes_socket(self):
        instance = service.Service(self.conf, key_lookup=lambda env: b'k' * 32)
        instance.sock = mock.Mock()
        instance.sock.recvfrom.side_effect = socket.timeout
        instance.receiver = mock.Mock()
        instance.thread = threading.Thread(target=instance._run)
        instance.thread.start()
        instance.stop()
        self.assertFalse(instance.thread.is_alive())
        self.assertTrue(instance.receiver.flush.called)
        self.assertTrue(instance.sock.close.called)

    def test_receive_failure_stops_loop_and_flush_failure_is_sanitized(self):
        instance = service.Service(self.conf, key_lookup=lambda env: None)
        instance.sock = mock.Mock()
        instance.sock.recvfrom.side_effect = OSError('secret')
        instance.receiver = mock.Mock()
        instance.receiver.flush.side_effect = RuntimeError('secret')
        instance._run()
        self.assertTrue(instance.stopping.is_set())

    def test_start_with_injected_lookup(self):
        directory = self.useFixture(fixtures.TempDir()).path
        self.config.config(state_path=directory, bind_ip='192.0.2.20',
                           group='health_manager')
        sock = mock.Mock()
        sock.recvfrom.side_effect = socket.timeout
        instance = service.Service(self.conf, key_lookup=lambda env: b'k' * 32)
        with mock.patch.object(management, 'socket_for_agent',
                               return_value=sock) as factory:
            instance.start()
            instance.stop()
        factory.assert_called_once_with(
            self.conf, socket.SOCK_DGRAM, 5565)
        self.assertFalse(instance.thread.is_alive())
