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

"""Sample Docker API 1.44 statistics without a streaming connection.

https://docs.docker.com/reference/api/engine/version/v1.44/#tag/Container/operation/ContainerStats
"""

import collections
import concurrent.futures
import time

from oslo_log import log as logging

from coast.agent import containers
from coast.agent import docker_exec

LOG = logging.getLogger(__name__)


def values(stats, host):
    current = stats['cpu_stats']
    previous = stats['precpu_stats']
    delta = (current['cpu_usage']['total_usage'] -
             previous.get('cpu_usage', {}).get('total_usage', 0))
    system = (current.get('system_cpu_usage', 0) -
              previous.get('system_cpu_usage', 0))
    online = current.get('online_cpus') or len(
        current['cpu_usage'].get('percpu_usage') or []) or 1
    available = float(online)
    if host.get('NanoCpus', 0) > 0:
        available = min(available, host['NanoCpus'] / 1000000000)
    elif host.get('CpuQuota', 0) > 0:
        available = min(available, host['CpuQuota'] /
                        (host.get('CpuPeriod') or 100000))
    if host.get('CpusetCpus'):
        cpus = set()
        for part in host['CpusetCpus'].split(','):
            first, _, last = part.partition('-')
            cpus.update(range(int(first), int(last or first) + 1))
        available = min(available, len(cpus))
    cpu = (delta / system * online / available * 100
           if system > 0 and delta >= 0 and available > 0 else 0.0)
    memory = stats.get('memory_stats') or {}
    usage = memory.get('usage', 0)
    cache = memory.get('stats') or {}
    cache = cache.get('total_inactive_file', cache.get('inactive_file', 0))
    return dict(cpu_percent=max(0.0, cpu),
                memory_bytes=max(0, usage - cache if cache < usage else usage),
                memory_limit_bytes=host.get('Memory') or None)


def collect(sender, docker=None):
    if sender.conf.agent.role != 'worker':
        return
    docker = docker or containers.Reader()
    try:
        rows = docker.list_running(['coolify.managed=true'])
    except Exception:
        LOG.warning('Container metrics inventory failed.')
        return
    names = collections.Counter(
        ((row.get('Labels') or {}).get('coolify.serviceUuid') or
         (row.get('Labels') or {}).get('coolify.applicationUuid'),
         (row.get('Labels') or {}).get('com.docker.compose.service'))
        for row in rows)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=sender.conf.agent.metrics_max_workers) as pool:
        for row in rows:
            pool.submit(_sample, sender, docker, row, names)


def _sample(sender, docker, row, names):
    try:
        labels = row.get('Labels') or {}
        kind = labels.get('coolify.type')
        key = ('coolify.serviceUuid' if kind == 'service' else
               'coolify.applicationUuid')
        resource = labels.get(key)
        if kind not in ('service', 'application') or not resource:
            return
        name = labels.get('com.docker.compose.service')
        if not name or names[(resource, name)] > 1:
            name = (row.get('Names') or [''])[0].lstrip('/')
        if not containers.NAME.fullmatch(name):
            return
        cid = docker_exec._safe_id(row['Id'])
        inspected = docker.inspect(
            cid, 'service' if kind == 'service' else 'app', resource)
        stats = docker._json('GET', docker_exec._API +
                             '/containers/%s/stats?stream=false' % cid,
                             None)
        sample = values(stats, inspected.get('HostConfig') or {})
        sample.update(engine_ref=resource, container_name=name)
        sender.send(metric=sample, sampled_at=time.time())
    except Exception:
        # Containers may stop or be replaced during a sample.
        LOG.warning('Container metrics sample failed.')
