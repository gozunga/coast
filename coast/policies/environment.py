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
        name='coast:stack:resize_workers',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Resize workers serially to a larger flavor.',
        operations=[{'method': 'POST',
                     'path': '/v1/stacks/{stack_id}/action'}],
        scope_types=['project']),
    *[policy.DocumentedRuleDefault(
        name='coast:stack:ingress_rules:' + action,
        check_str=(base.PROJECT_READER_OR_ADMIN if method == 'GET'
                   else base.PROJECT_MEMBER_OR_ADMIN),
        description='Manage worker ingress rules.' if method != 'GET'
        else 'List worker ingress rules.',
        operations=[{'method': method, 'path':
                     '/v1/stacks/{stack_id}/ingress_rules' + suffix}],
        scope_types=['project'])
      for action, method, suffix in (
          ('list', 'GET', ''), ('create', 'POST', ''),
          ('delete', 'DELETE', '/{rule_id}'))],
    policy.DocumentedRuleDefault(
        name='coast:stack:events:get',
        check_str='rule:coast:stack:get_one',
        description='Read the stack event timeline.',
        operations=[{'method': 'GET',
                     'path': '/v1/stacks/{stack_id}/events'}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:stack:get_all',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List stacks.',
        operations=[
            {'method': 'GET',
             'path': '/v1/stacks'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:stack:get_one',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read a stack.',
        operations=[
            {'method': 'GET',
             'path': '/v1/stacks/{env_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:stack:create',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Create a stack as a project member.',
        operations=[
            {'method': 'POST',
             'path': '/v1/stacks'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:stack:update',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Update stack.',
        operations=[
            {'method': 'PATCH',
             'path': '/v1/stacks/{env_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:stack:delete',
        check_str=base.PROJECT_MANAGER_OR_ADMIN,
        description='Delete stack.',
        operations=[
            {'method': 'DELETE',
             'path': '/v1/stacks/{env_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:stack:upgrade',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Upgrade stack.',
        operations=[
            {'method': 'POST',
             'path': '/v1/stacks/{env_id}/action'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:stack:upgrade_rollback',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Restore the last engine upgrade rollback point.',
        operations=[{'method': 'POST',
                     'path': '/v1/stacks/{env_id}/action'}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:stack:get_detail',
        check_str=base.ADMIN,
        description='Read admin-only stack infrastructure and counts.',
        operations=[
            {'method': 'GET',
             'path': '/v1/stacks'},
            {'method': 'GET',
             'path': '/v1/stacks/{env_id}'},
        ],
        scope_types=['project']),
]


def list_rules():
    return rules
