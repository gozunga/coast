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
        name='coast:domain:get_all',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List domains.',
        operations=[
            {'method': 'GET',
             'path': '/v1/services/{service_id}/domains'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:domain:get_one',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read a domain.',
        operations=[
            {'method': 'GET',
             'path': '/v1/services/{service_id}/domains/{domain_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:domain:create',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Create domain.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/domains'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:domain:delete',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Delete domain.',
        operations=[
            {'method': 'DELETE',
             'path': '/v1/services/{service_id}/domains/{domain_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:domain:update',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Update a domain and its primary address preference.',
        operations=[
            {'method': 'PATCH',
             'path': '/v1/services/{service_id}/domains/{domain_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:domain:verify',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Verify domain.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/domains/{domain_id}/action'},
        ],
        scope_types=['project']),
]


def list_rules():
    return rules
