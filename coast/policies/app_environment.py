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

"""Q94 Coolify environment rules.

``coast.policies.environment`` is the stack rule module. The state
machine key for this resource is ``app_environment``.
"""

from oslo_policy import policy

from coast.policies import base


rules = [
    policy.DocumentedRuleDefault(
        name='coast:environment:get_all',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List environments.',
        operations=[
            {'method': 'GET',
             'path': '/v1/appgroups/{appgroup_id}/environments'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:environment:get_one',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read an environment.',
        operations=[
            {'method': 'GET',
             'path': '/v1/environments/{environment_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:environment:create',
        check_str=base.PROJECT_MANAGER_OR_ADMIN,
        description='Create environment.',
        operations=[
            {'method': 'POST',
             'path': '/v1/appgroups/{appgroup_id}/environments'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:environment:update',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Update environment.',
        operations=[
            {'method': 'PATCH',
             'path': '/v1/environments/{environment_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:environment:delete',
        check_str=base.PROJECT_MANAGER_OR_ADMIN,
        description='Delete environment.',
        operations=[
            {'method': 'DELETE',
             'path': '/v1/environments/{environment_id}'},
        ],
        scope_types=['project']),
]


def list_rules():
    return rules
