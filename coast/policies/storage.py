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

from oslo_policy import policy

from coast.policies import base


_COLLECTION = '/v1/stacks/{stack_id}/storages'
_ITEM = _COLLECTION + '/{storage_id}'


rules = [
    policy.DocumentedRuleDefault(
        name='coast:storage:get_all',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List storage mounts.',
        operations=[{'method': 'GET', 'path': _COLLECTION}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:storage:get_one',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read a storage mount.',
        operations=[{'method': 'GET', 'path': _ITEM}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:storage:create',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Create storage or attach a service mount.',
        operations=[{'method': 'POST', 'path': _COLLECTION}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:storage:attach',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Attach storage or edit a service mount.',
        operations=[{'method': 'POST', 'path': _ITEM + '/attach'},
                    {'method': 'PATCH', 'path': _ITEM}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:storage:detach',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Detach a storage volume.',
        operations=[{'method': 'POST', 'path': _ITEM + '/detach'}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:storage:delete',
        check_str=base.PROJECT_MANAGER_OR_ADMIN,
        description='Delete a user storage mount.',
        operations=[{'method': 'DELETE', 'path': _ITEM}],
        scope_types=['project']),
]


def list_rules():
    return rules
