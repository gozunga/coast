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

"""Metrics through the worker, authenticated receiver, DB and API."""

import copy
import datetime
import json
import threading
from unittest import mock

import fixtures

from coast.agent import heartbeat
from coast.agent import metrics as agent_metrics
from coast.agent import namespace
from coast.common import context
from coast.common import exception
from coast.db import api
from coast.db import metrics
from coast.db import models
from coast.health_manager import receiver
from coast.health_manager import service as health_service
from coast.tests import base
from coast.tests import deployments


class ServiceMetricsTest(deployments.DeploymentTest):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.21'
        self.path = '/v1/services/' + self.service.id + '/metrics'
        self.now = datetime.datetime(2026, 10, 19, 5)
        self.worker.mgmt_ip = '192.0.2.10'
        self.worker.save()
        self.sample = dict(engine_ref='app-uuid', container_name='web',
                           cpu_percent=12.5, memory_bytes=104857600,
                           memory_limit_bytes=536870912)
        self.useFixture(fixtures.MonkeyPatch(
            'coast.db.metrics.timeutils.utcnow', lambda: self.now))

    def ingest(self, when=None, **changes):
        metrics.ingest(self.ctx, self.worker,
                       dict(self.sample, **changes), when or self.now)

    def rows(self):
        with api.reader.using(self.ctx) as session:
            return session.query(models.ServiceMetric).order_by(
                models.ServiceMetric.timestamp).all()

    def test_default_contract_sorted_containers_and_null(self):
        self.ingest(self.now - datetime.timedelta(seconds=30),
                    memory_limit_bytes=None)
        self.ingest()
        self.ingest(container_name='db')
        result = self.request('get', self.path).json['metrics']
        self.assertEqual(self.service.id, result['service_id'])
        self.assertEqual('1h', result['range'])
        self.assertEqual(30, result['step_seconds'])
        self.assertEqual(['db', 'web'],
                         [r['name'] for r in result['containers']])
        points = result['containers'][1]['points']
        self.assertEqual(['2026-10-19T04:59:30Z', '2026-10-19T05:00:00Z'],
                         [p['timestamp'] for p in points])
        self.assertIsNone(points[0]['memory_limit_bytes'])
        self.assertEqual(dict(timestamp='2026-10-19T05:00:00Z',
                              cpu_percent=12.5, memory_bytes=104857600,
                              memory_limit_bytes=536870912), points[1])

    def test_ranges_and_coarse_read_includes_recent_detail(self):
        self.ingest()
        self.ingest(self.now - datetime.timedelta(seconds=30),
                    cpu_percent=37.5)
        for name, (_, step) in metrics.RANGES.items():
            result = self.request('get', self.path + '?range=' + name).json[
                'metrics']
            self.assertEqual(step, result['step_seconds'])
            self.assertTrue(result['containers'][0]['points'])
        result = self.request('get', self.path + '?range=7d').json['metrics']
        self.assertEqual(2, len(result['containers'][0]['points']))
        # Place both observations into one five-minute bucket.
        self.ingest(self.now - datetime.timedelta(seconds=60),
                    cpu_percent=12.5)
        result = self.request('get', self.path + '?range=7d').json['metrics']
        self.assertEqual(
            25, result['containers'][0]['points'][0]['cpu_percent'])

    def test_auth_and_microversion_and_validation(self):
        self.ingest()
        self.client.get(self.path, headers={
            'OpenStack-API-Version': 'app-hosting 1.21'}, status=401)
        for changes, status in [({'X-Roles': 'unrelated'}, 403),
                                ({'X-Project-Id': 'other'}, 404),
                                ({'OpenStack-API-Version':
                                  'app-hosting 1.20'}, 404)]:
            self.request('get', self.path,
                         headers=dict(self.headers, **changes), status=status)
        self.request('get', self.path, headers=dict(self.headers, **{
            'X-Roles': 'reader'}))
        self.request('get', self.path, headers=dict(self.headers, **{
            'X-Roles': 'admin', 'X-Project-Id': 'other'}))
        for query in ('range=bad', 'range=', 'other=1', 'range=1h&range=7d'):
            self.request('get', self.path + '?' + query, status=400)
        for method in ('post', 'put', 'patch', 'delete'):
            self.request(method, self.path, status=405)
        missing = self.path.replace(self.service.id,
                                    '00000000-0000-0000-0000-000000000000')
        self.request('get', missing, status=404)
        other = context.RequestContext(project_id='other')
        self.assertEqual([], metrics.read(other, self.service.id, '1h')[
            'containers'])

    def test_ingestion_duplicate_and_service_delete(self):
        self.ingest()
        self.ingest(cpu_percent=99)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual(12.5, self.rows()[0].cpu_percent)
        api.services.delete(self.ctx, self.service.id)
        self.assertEqual([], self.rows())
        self.assertRaises(exception.NotFound, self.ingest)
        self.request('get', self.path, status=404)

    def test_rollup_boundaries_weighted_averages_and_expiry(self):
        day = datetime.timedelta(days=1)
        for offset, cpu in [(0, 10), (30, 40), (300, 70)]:
            when = self.now - 2 * day + datetime.timedelta(seconds=offset)
            self.ingest(when,
                        cpu_percent=cpu)
        # Exact cutoff detail is retained until the complete bucket is old.
        self.ingest(self.now - day)
        self.ingest(self.now - 8 * day, memory_limit_bytes=None)
        self.ingest(self.now - 31 * day)
        metrics.maintain(self.conf, self.now)
        rows = self.rows()
        self.assertEqual([3600, 300, 300, 30], [r.resolution for r in rows])
        self.assertIsNone(rows[0].memory_limit_bytes)
        self.assertEqual(25, rows[1].cpu_percent)
        self.assertEqual(2, rows[1].sample_count)
        self.assertEqual(70, rows[2].cpu_percent)
        before = [(r.id, r.sample_count) for r in rows]
        metrics.maintain(self.conf, self.now)
        self.assertEqual(before, [(r.id, r.sample_count) for r in self.rows()])
        # 2+1 observations must yield 40, rather than the unweighted 47.5.
        metrics.maintain(self.conf, self.now + 6 * day)
        hourly = next(r for r in self.rows() if r.sample_count == 3)
        self.assertEqual(40, hourly.cpu_percent)
        self.assertEqual(3600, hourly.resolution)

    def test_bounded_pass_and_atomic_rollback(self):
        self.config.config(batch_size=1, group='metrics')
        for minute in (0, 5, 10):
            self.ingest(self.now - datetime.timedelta(days=2, minutes=minute))
        with mock.patch.object(metrics, 'average', side_effect=RuntimeError):
            self.assertRaises(RuntimeError, metrics.maintain,
                              self.conf, self.now)
        self.assertEqual([30, 30, 30], [r.resolution for r in self.rows()])
        metrics.maintain(self.conf, self.now)
        self.assertEqual(1, sum(r.resolution == 300 for r in self.rows()))
        self.assertEqual(2, sum(r.resolution == 30 for r in self.rows()))

    def test_receiver_scopes_hmac_replay_server_and_stack(self):
        self.config.config(state_path=self.useFixture(fixtures.TempDir()).path,
                           group='health_manager')
        now = self.now.replace(tzinfo=datetime.timezone.utc).timestamp()
        recv = receiver.Receiver(lambda _: b'k' * 32, self.conf,
                                 clock=lambda: now)
        self.addCleanup(recv.close)
        payload = dict(server_id=self.worker.id, env_id=self.env.id, seq=1,
                       ts=now, docker_ok=True, engine_ok=False, disk_pct=1,
                       metric=self.sample)

        def accept(data, key=b'k' * 32, source='192.0.2.10'):
            return recv.accept(heartbeat.encode(data, key), source)
        self.assertFalse(accept(payload, key=b'x' * 32))
        self.assertFalse(accept(payload, source='192.0.2.11'))
        self.assertTrue(accept(payload))
        self.assertFalse(accept(payload))
        payload['seq'] = 2
        payload['metric'] = dict(self.sample, engine_ref='foreign-resource')
        self.assertFalse(accept(payload))
        payload['seq'] = 3
        payload['metric'] = dict(self.sample, memory_bytes=-1)
        self.assertFalse(accept(payload))
        payload['metric'] = self.sample
        # A matching engine ref on a different worker is insufficient.
        with api.writer.using(self.ctx) as session:
            service = session.get(models.Service, self.service.id)
            service.server_id = None
        self.assertFalse(accept(payload))
        other = api.stacks.create(self.ctx, dict(
            name='other-metrics-stack', plan='test', driver='noop',
            engine_version='test', status='PENDING'))
        with api.writer.using(self.ctx) as session:
            service = session.get(models.Service, self.service.id)
            service.server_id = self.worker.id
            service.stack_id = other.id
        payload['seq'] = 4
        self.assertFalse(accept(payload))
        with api.writer.using(self.ctx) as session:
            service = session.get(models.Service, self.service.id)
            service.stack_id = self.env.id
            service.project_id = 'another-project'
        payload['seq'] = 5
        self.assertFalse(accept(payload))
        self.assertEqual(1, len(self.rows()))

    def test_large_byte_values_remain_exact(self):
        maximum = 2 ** 63 - 1
        self.ingest(memory_bytes=maximum, memory_limit_bytes=maximum)
        point = self.request('get', self.path).json['metrics'][
            'containers'][0]['points'][0]
        self.assertEqual(maximum, point['memory_bytes'])
        self.assertEqual(maximum, point['memory_limit_bytes'])
        result = metrics.average(self.rows())
        self.assertEqual(maximum, result['memory_bytes'])
        self.assertEqual(maximum, result['memory_limit_bytes'])

    def test_invalid_metric_values(self):
        for changes in [dict(cpu_percent=float('nan')),
                        dict(cpu_percent=True), dict(cpu_percent=-1),
                        dict(cpu_percent=1e100),
                        dict(memory_bytes=2 ** 63), dict(memory_bytes=True),
                        dict(memory_limit_bytes=-1), dict(extra=1),
                        dict(container_name='../host'), dict(engine_ref='')]:
            self.assertRaises(ValueError, metrics.validate,
                              dict(self.sample, **changes))
        self.worker.role = 'build'
        self.assertRaises(ValueError, self.ingest)

    def test_retention_timer_hides_exception(self):
        logs = self.useFixture(fixtures.FakeLogger())
        manager = health_service.Service(self.conf)
        with mock.patch.object(metrics, 'maintain',
                               side_effect=RuntimeError('private-value')):
            manager._maintain_metrics()
        self.assertNotIn('private-value', logs.output)


class AgentMetricsTest(base.TestCase):
    def stats(self):
        return dict(cpu_stats=dict(cpu_usage=dict(total_usage=200),
                                   system_cpu_usage=1000, online_cpus=4),
                    precpu_stats=dict(cpu_usage=dict(total_usage=100),
                                      system_cpu_usage=200),
                    memory_stats=dict(usage=1000,
                                      stats=dict(inactive_file=200)))

    def test_cpu_limits_cpuset_cache_and_unlimited_memory(self):
        stats = self.stats()
        self.assertEqual(dict(cpu_percent=12.5, memory_bytes=800,
                              memory_limit_bytes=None),
                         agent_metrics.values(stats, {}))
        for host in (dict(NanoCpus=500000000),
                     dict(CpuQuota=50000, CpuPeriod=100000)):
            host['Memory'] = 2048
            self.assertEqual(dict(cpu_percent=100, memory_bytes=800,
                                  memory_limit_bytes=2048),
                             agent_metrics.values(stats, host))
        self.assertEqual(25, agent_metrics.values(stats, dict(
            CpusetCpus='0-1'))['cpu_percent'])
        self.assertEqual(200, agent_metrics.values(
            stats, dict(NanoCpus=250000000))['cpu_percent'])
        stats['memory_stats']['stats']['total_inactive_file'] = 300
        self.assertEqual(700, agent_metrics.values(stats, {})['memory_bytes'])
        stats['precpu_stats'] = copy.deepcopy(stats['cpu_stats'])
        self.assertEqual(0, agent_metrics.values(stats, {})['cpu_percent'])

    def test_parallel_reports_keep_signed_sequences_and_heartbeats(self):
        directory = self.useFixture(fixtures.TempDir()).path
        key = b'k' * 32
        self.config.config(
            state_path=directory, heartbeat_key=key.hex(),
            server_id='00000000-0000-0000-0000-000000000001',
            stack_id='00000000-0000-0000-0000-000000000002',
            health_manager_ips=['192.0.2.20'], group='agent')
        sock = mock.Mock()
        with mock.patch.object(namespace, 'management_socket',
                               return_value=sock), \
                mock.patch.object(heartbeat, 'docker_ok', return_value=True), \
                mock.patch.object(heartbeat.shutil, 'disk_usage',
                                  return_value=mock.Mock(used=1, total=4)):
            sender = heartbeat.Sender(self.conf)
            threads = [threading.Thread(target=sender.send, kwargs=dict(
                metric={'cpu_percent': 12.5}, sampled_at=123))
                for unused in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            sender.send()
        packets = [call.args[0] for call in sock.sendto.call_args_list]
        self.assertEqual(9, len(packets))
        payloads = [json.loads(packet[:-32]) for packet in packets]
        sequences = [payload['seq'] for payload in payloads]
        self.assertEqual(sorted(set(sequences)), sequences)
        for packet, payload in zip(packets, payloads):
            self.assertEqual(packet, heartbeat.encode(payload, key))
        for payload in payloads[:-1]:
            self.assertEqual({'cpu_percent': 12.5}, payload['metric'])
            self.assertEqual(123, payload['ts'])
        self.assertNotIn('metric', payloads[-1])
        self.assertEqual(sender.sequence,
                         int(sender.sequence_path.read_text()))

    def test_collection_app_and_compose_and_disappearing_container(self):
        docker = mock.Mock()
        sender = mock.Mock(conf=self.conf)
        rows = []
        for kind, name in [('application', 'web'), ('service', 'db')]:
            rows.append(dict(Id=name, Labels={
                'coolify.type': kind, 'coolify.managed': 'true',
                'coolify.' + ('serviceUuid' if kind == 'service' else
                              'applicationUuid'): 'app-uuid',
                'com.docker.compose.service': name}))
        docker.list_running.return_value = rows
        docker.inspect.return_value = {'HostConfig': {}}
        self.config.config(metrics_max_workers=2, group='agent')
        barrier = threading.Barrier(2)

        def stats(*args):
            barrier.wait(timeout=10)
            return self.stats()

        docker._json.side_effect = stats
        agent_metrics.collect(sender, docker)
        docker._json.side_effect = None
        docker._json.return_value = self.stats()
        samples = [call.kwargs['metric']
                   for call in sender.send.call_args_list]
        self.assertEqual(['db', 'web'],
                         sorted(r['container_name'] for r in samples))
        self.assertEqual([12.5, 12.5], [r['cpu_percent'] for r in samples])
        self.assertTrue(all('stream=false' in call.args[1]
                            for call in docker._json.call_args_list))
        sender.send.reset_mock()
        rows[0]['Names'] = ['/web-1']
        replica = copy.deepcopy(rows[0])
        replica.update(Id='replica', Names=['/web-2'])
        rows.append(replica)
        agent_metrics.collect(sender, docker)
        self.assertEqual(['db', 'web-1', 'web-2'], sorted([
            call.kwargs['metric']['container_name']
            for call in sender.send.call_args_list]))
        sender.send.reset_mock()
        docker.inspect.side_effect = OSError('container gone')
        agent_metrics.collect(sender, docker)
        self.assertEqual(0, sender.send.call_count)
        docker.list_running.side_effect = OSError('Docker unavailable')
        agent_metrics.collect(sender, docker)
        self.config.config(role='controller', group='agent')
        docker.list_running.reset_mock()
        agent_metrics.collect(sender, docker)
        self.assertEqual(0, docker.list_running.call_count)
