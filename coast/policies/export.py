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
        name='coast:export:get_all',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='List exports.',
        operations=[
            {'method': 'GET',
             'path': '/v1/stacks/{env_id}/exports'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:export:get_one',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Read an export.',
        operations=[
            {'method': 'GET',
             'path': '/v1/stacks/{env_id}/exports/{export_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:export:create',
        check_str=base.PROJECT_MANAGER_OR_ADMIN,
        description='Create export.',
        operations=[
            {'method': 'POST',
             'path': '/v1/stacks/{env_id}/exports'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:export:renew_url',
        check_str=base.PROJECT_MANAGER_OR_ADMIN,
        description='Renew export download URLs while the objects exist.',
        operations=[
            {'method': 'POST',
             'path': '/v1/stacks/{env_id}/exports/{export_id}/action'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:export:delete',
        check_str=base.PROJECT_MANAGER_OR_ADMIN,
        description='Delete export.',
        operations=[
            {'method': 'DELETE',
             'path': '/v1/stacks/{env_id}/exports/{export_id}'},
        ],
        scope_types=['project']),
]


def list_rules():
    return rules
