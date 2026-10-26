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

"""Read workload containers through the documented Docker Engine API 1.44.

https://docs.docker.com/reference/api/engine/version/v1.44/
Only labels select ownership. Never accept a caller-supplied Docker id.
"""

import datetime
import json
import re
import struct
import urllib.parse

from coast.agent import docker_exec
from coast.agent import namespace


NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z')


def timestamp(value):
    parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=datetime.timezone.utc) if (
        parsed.tzinfo is None) else parsed


class Reader(docker_exec.DockerClient):
    def inventory(self, kind, resource):
        kind = kind if kind in ('service', 'database') else 'application'
        key = 'coolify.' + kind + 'Uuid'
        engine_kind = kind
        labels = ['coolify.managed=true', 'coolify.type=' + engine_kind,
                  key + '=' + resource]
        query = urllib.parse.urlencode({
            'all': 'true', 'filters': json.dumps({'label': labels})})
        rows = self._json('GET', docker_exec._API + '/containers/json?' +
                          query, None)
        if not isinstance(rows, list) or len(rows) > 100:
            raise OSError('Container response invalid')
        found = {}
        for row in rows:
            labels = row.get('Labels') or {}
            if (labels.get('coolify.managed') != 'true' or
                    labels.get('coolify.type') != engine_kind or
                    labels.get(key) != resource):
                continue
            name = labels.get('com.docker.compose.service')
            if not isinstance(name, str) or not NAME.fullmatch(name):
                continue
            if name in found:
                raise OSError('Container target ambiguous')
            found[name] = docker_exec._safe_id(row.get('Id'))
        return found

    def inspect(self, cid, kind, resource):
        kind = kind if kind in ('service', 'database') else 'application'
        row = self._json('GET', docker_exec._API + '/containers/%s/json' %
                         docker_exec._safe_id(cid), None)
        labels = row.get('Config', {}).get('Labels') or {}
        key = 'coolify.' + kind + 'Uuid'
        if (labels.get('coolify.managed') != 'true' or
                labels.get('coolify.type') != kind or
                labels.get(key) != resource):
            raise OSError('Container ownership changed')
        return row

    def logs(self, cid, tty, tail, since):
        if tail == 0:
            return []
        query = urllib.parse.urlencode({
            'stdout': 'true', 'stderr': 'true', 'timestamps': 'true',
            'tail': tail})
        _, raw = self._http('GET', docker_exec._API +
                            '/containers/%s/logs?' %
                            docker_exec._safe_id(cid) + query, None)
        chunks = []
        if tty:
            chunks.append(('stdout', raw))
        else:
            while raw:
                if len(raw) < 8 or raw[0] not in (1, 2):
                    raise OSError('Container log format invalid')
                stream, size = struct.unpack('>BxxxI', raw[:8])
                if len(raw) < size + 8:
                    raise OSError('Container log format invalid')
                chunks.append(('stdout' if stream == 1 else 'stderr',
                               raw[8:8 + size]))
                raw = raw[8 + size:]
        lines = []
        pending = {'stdout': b'', 'stderr': b''}

        def append(stream, raw_line):
            line = raw_line.decode('utf-8', errors='replace')
            ts, _, content = line.partition(' ')
            observed_at = timestamp(ts)
            if not since or observed_at > timestamp(since):
                lines.append({'ts': ts, 'stream': stream, 'line': content})

        for stream, chunk in chunks:
            parts = (pending[stream] + chunk).split(b'\n')
            pending[stream] = parts.pop()
            for line in parts:
                append(stream, line)
        for stream, chunk in pending.items():
            if chunk:
                append(stream, chunk)
        return lines[-tail:]


def view(name, row, detail):
    state = row.get('State') or {}
    config = row.get('Config') or {}
    host = row.get('HostConfig') or {}
    result = {'id': 'c-' + name, 'name': name, 'image': config.get('Image'),
              'state': state.get('Status'),
              'health': (state.get('Health') or {}).get('Status'),
              'restart_count': row.get('RestartCount')}
    if detail:
        env = {}
        for item in config.get('Env') or []:
            key, _, value = item.partition('=')
            if not key.startswith(('COOLIFY_', 'COAST_')):
                env[key] = value
        cpus = host.get('NanoCpus', 0) / 1000000000
        if not cpus and host.get('CpuQuota', 0) > 0:
            cpus = host['CpuQuota'] / (host.get('CpuPeriod') or 100000)
        result.update(environment=env, limits={
            'cpu': cpus or None, 'memory_bytes': host.get('Memory') or None})
    return result


def read(conf, body, docker=None):
    if (not isinstance(body, dict) or set(body) - {
            'project_id', 'resource_type', 'resource_uuid', 'container',
            'logs', 'tail', 'since'} or
            not namespace.same_project(conf, body.get('project_id')) or
            body.get('resource_type') not in ('app', 'service') or
            not isinstance(body.get('resource_uuid'), str) or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', body['resource_uuid'])):
        raise ValueError('Container request invalid')
    target = body.get('container')
    tail = body.get('tail', 200)
    since = body.get('since')
    bad_target = target is not None and (
        not isinstance(target, str) or len(target) > 130 or
        not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,129}', target))
    if (bad_target or
            type(tail) is not int or not 0 <= tail <= 10000 or
            type(body.get('logs', False)) is not bool or
            body.get('logs') and target is None):
        raise ValueError('Container request invalid')
    if since is not None:
        if not isinstance(since, str):
            raise ValueError('Container request invalid')
        timestamp(since)
    docker = docker or Reader()
    kind, resource = body['resource_type'], body['resource_uuid']
    inventory = docker.inventory(kind, resource)
    if target is not None and (not target.startswith('c-') or
                               target[2:] not in inventory):
        return {'not_found': True}
    selected = [target[2:]] if target is not None else sorted(inventory)
    result = []
    for name in selected:
        row = docker.inspect(inventory[name], kind, resource)
        if body.get('logs'):
            lines = docker.logs(inventory[name],
                                row.get('Config', {}).get('Tty'), tail, since)
            return {'lines': lines,
                    'environment': view(name, row, True)['environment']}
        result.append(view(name, row, target is not None))
    return {'container': result[0]} if target is not None else {
        'containers': result}
