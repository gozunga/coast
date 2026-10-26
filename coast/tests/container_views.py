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

"""Docker API fixtures shared by unit and functional container tests."""

import copy
import struct

from coast.agent import containers


def labels(name='web', kind='service', resource='app-uuid'):
    return {'coolify.managed': 'true', 'coolify.type': kind,
            'coolify.serviceUuid' if kind == 'service' else
            'coolify.applicationUuid': resource,
            'com.docker.compose.service': name}


def frame(text, stream=1):
    raw = text.encode()
    return struct.pack('>BxxxI', stream, len(raw)) + raw


class Docker(containers.Reader):
    def __init__(self):
        self.calls = []
        self.rows = [{'Id': 'abc123', 'Labels': labels()},
                     {'Id': 'def456', 'Labels': labels('db')}]
        self.detail = {
            'Config': {'Labels': labels(), 'Image': 'nginx:stable',
                       'Env': ['PASSWORD=plain', 'TOKEN=hidden-value',
                               'ALIAS=hidden-value', 'COOLIFY_URL=internal'],
                       'Tty': False},
            'State': {'Status': 'exited', 'Health': {'Status': 'unhealthy'}},
            'RestartCount': 3,
            'HostConfig': {'NanoCpus': 500000000, 'Memory': 134217728,
                           'Binds': ['/host:/data']},
            'Mounts': [{'Source': '/host'}], 'Name': '/engine-internal'}
        self.raw = frame('2026-10-10T01:00:00Z before\n') + frame(
            '2026-10-10T01:00:01Z hidden-value PASSWORD=plain\n', 2)

    def _json(self, method, path, body):
        self.calls.append((method, path))
        if '/containers/json?' in path:
            return copy.deepcopy(self.rows)
        return copy.deepcopy(self.detail)

    def _http(self, method, path, body):
        self.calls.append((method, path))
        return 200, self.raw
