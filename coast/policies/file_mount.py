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

"""File mount rules for both workload routes."""

from oslo_policy import policy

from coast.policies import base


rules = []
for action, method, item, check, description in (
        ('get_all', 'GET', False, base.PROJECT_READER_OR_ADMIN,
         'List container file mount metadata.'),
        ('get_one', 'GET', True, base.PROJECT_READER_OR_ADMIN,
         'Read container file mount metadata.'),
        ('create', 'POST', False, base.PROJECT_MEMBER_OR_ADMIN,
         'Create a container file mount.'),
        ('update', 'PATCH', True, base.PROJECT_MEMBER_OR_ADMIN,
         'Update a container file mount.'),
        ('delete', 'DELETE', True, base.PROJECT_MANAGER_OR_ADMIN,
         'Delete a container file mount.')):
    paths = ['/v1/%s/{service_id}/file_mounts' % kind
             for kind in ('apps', 'services')]
    if item:
        paths = [path + '/{file_mount_id}' for path in paths]
    rules.append(policy.DocumentedRuleDefault(
        name='coast:file_mount:' + action, check_str=check,
        description=description,
        operations=[{'method': method, 'path': path} for path in paths],
        scope_types=['project']))


def list_rules():
    return rules
