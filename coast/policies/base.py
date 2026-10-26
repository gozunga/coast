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


PROJECT_READER = 'role:reader and project_id:%(project_id)s'
PROJECT_MEMBER = 'role:member and project_id:%(project_id)s'
PROJECT_MANAGER = 'role:manager and project_id:%(project_id)s'
ADMIN = 'role:admin'
AGENT = 'role:coast_agent'
PROJECT_READER_OR_ADMIN = 'rule:PROJECT_READER or rule:ADMIN'
PROJECT_MEMBER_OR_ADMIN = 'rule:PROJECT_MEMBER or rule:ADMIN'
PROJECT_MANAGER_OR_ADMIN = 'rule:PROJECT_MANAGER or rule:ADMIN'


rules = [
    policy.RuleDefault(
        name='PROJECT_READER', check_str=PROJECT_READER,
        description='Check the project reader persona.',
        scope_types=['project']),
    policy.RuleDefault(
        name='PROJECT_MEMBER', check_str=PROJECT_MEMBER,
        description='Check the project member persona.',
        scope_types=['project']),
    policy.RuleDefault(
        name='PROJECT_MANAGER', check_str=PROJECT_MANAGER,
        description='Check the project manager persona.',
        scope_types=['project']),
    policy.RuleDefault(
        name='ADMIN', check_str=ADMIN,
        description='Check the admin persona.',
        scope_types=['project']),
    policy.RuleDefault(
        name='AGENT', check_str=AGENT,
        description='Limited agent credential. No Coast API rule uses '
                    'this check.',
        scope_types=['project']),
    policy.RuleDefault(
        name='PROJECT_READER_OR_ADMIN', check_str=PROJECT_READER_OR_ADMIN,
        description='Check the project reader or admin persona.',
        scope_types=['project']),
    policy.RuleDefault(
        name='PROJECT_MEMBER_OR_ADMIN', check_str=PROJECT_MEMBER_OR_ADMIN,
        description='Check the project member or admin persona.',
        scope_types=['project']),
    policy.RuleDefault(
        name='PROJECT_MANAGER_OR_ADMIN', check_str=PROJECT_MANAGER_OR_ADMIN,
        description='Check the project manager or admin persona.',
        scope_types=['project']),
]


def list_rules():
    return rules
