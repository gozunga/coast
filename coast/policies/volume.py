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


rules = [
    policy.DocumentedRuleDefault(
        name='coast:volume:get_all',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List volumes.',
        operations=[
            {'method': 'GET',
             'path': '/v1/stacks/{env_id}/volumes'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:volume:get_one',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read a volume.',
        operations=[
            {'method': 'GET',
             'path': '/v1/stacks/{env_id}/volumes/{volume_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:volume:create',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Create volume.',
        operations=[
            {'method': 'POST',
             'path': '/v1/stacks/{env_id}/volumes'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:volume:update',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Update volume.',
        operations=[
            {'method': 'PATCH',
             'path': '/v1/stacks/{env_id}/volumes/{volume_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:volume:delete',
        check_str=base.PROJECT_MANAGER_OR_ADMIN,
        description='Delete detached retained data volume.',
        operations=[
            {'method': 'DELETE',
             'path': '/v1/stacks/{env_id}/volumes/{volume_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:volume:snapshot',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Snapshot volume.',
        operations=[
            {'method': 'POST',
             'path': '/v1/stacks/{env_id}/volumes/{volume_id}/action'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:volume:get_detail',
        check_str=base.ADMIN,
        description='Read admin-only volume fields.',
        operations=[
            {'method': 'GET',
             'path': '/v1/stacks/{env_id}/volumes'},
            {'method': 'GET',
             'path': '/v1/stacks/{env_id}/volumes/{volume_id}'},
        ],
        scope_types=['project']),
]


def list_rules():
    return rules
