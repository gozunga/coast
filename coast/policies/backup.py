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

"""Database backup access: project readers, members and administrators."""

from oslo_policy import policy

from coast.policies import base


_OPERATIONS = {
    'get_policy': [('GET', '/v1/services/{id}/backup_policy')],
    'set_policy': [('PUT', '/v1/services/{id}/backup_policy')],
    'get_all': [('GET', '/v1/services/{id}/backups')],
    'create': [('POST', '/v1/services/{id}/backups')],
    'get_one': [('GET', '/v1/backups/{id}')],
    'delete': [('DELETE', '/v1/backups/{id}')],
    'download': [('GET', '/v1/backups/{id}/download')],
    'restore': [('POST', '/v1/backups/{id}/restore')],
}


def list_rules():
    return [policy.DocumentedRuleDefault(
        name='coast:backup:' + name,
        check_str=(base.PROJECT_READER_OR_ADMIN if name in (
            'get_policy', 'get_all', 'get_one') else
            base.PROJECT_MEMBER_OR_ADMIN),
        description='Database backup ' + name.replace('_', ' ') + '.',
        operations=[{'method': method, 'path': path}
                    for method, path in operations] + [
                        {'method': method,
                         'path': path.replace('/services/', '/databases/')}
                        for method, path in operations
                        if '/services/' in path], scope_types=['project'])
            for name, operations in _OPERATIONS.items()]
