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
        name='coast:deployment:get_all',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List deployments.',
        operations=[
            {'method': 'GET',
             'path': '/v1/services/{service_id}/deployments'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:deployment:get_one',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read a deployment.',
        operations=[
            {'method': 'GET',
             'path': '/v1/services/{service_id}/deployments/{dep_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:deployment:create',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Create deployment.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/deployments'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:deployment:rollback',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Rollback deployment.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/deployments/{dep_id}/action'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:deployment:cancel',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Cancel deployment.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/deployments/{dep_id}/action'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:deployment:get_log',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read the deployment build log.',
        operations=[
            {'method': 'GET',
             'path': '/v1/services/{service_id}/deployments/{dep_id}/log'},
        ],
        scope_types=['project']),
]


def list_rules():
    return rules
