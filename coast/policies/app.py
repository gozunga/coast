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
        name='coast:service:get_all',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List services.',
        operations=[
            {'method': 'GET',
             'path': '/v1/services'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:get_one',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read a service or its CPU and memory metrics.',
        operations=[
            {'method': 'GET',
             'path': '/v1/services/{service_id}'},
            {'method': 'GET',
             'path': '/v1/services/{service_id}/metrics'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:create',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Create service.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:update',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Update service metadata, settings or image source.',
        operations=[
            {'method': 'PATCH',
             'path': '/v1/services/{service_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:delete',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Delete service.',
        operations=[
            {'method': 'DELETE',
             'path': '/v1/services/{service_id}'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:start',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Start service.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/action'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:stop',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Stop service.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/action'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:get_webhooks',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Show the app deploy and Git webhook URLs (tokens).',
        operations=[
            {'method': 'GET',
             'path': '/v1/services/{service_id}/webhooks'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:regenerate_webhooks',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Regenerate the app webhook token.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/webhooks/regenerate'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:restart',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Restart service.',
        operations=[
            {'method': 'POST',
             'path': '/v1/services/{service_id}/action'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:get_logs',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read live application logs.',
        operations=[
            {'method': 'GET',
             'path': '/v1/services/{service_id}/logs'},
        ],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:get_compose',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Read compose YAML (can contain secrets).',
        operations=[{'method': 'GET',
                     'path': '/v1/services/{service_id}/compose'}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:update_compose',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Replace desired compose YAML.',
        operations=[{'method': 'PUT',
                     'path': '/v1/services/{service_id}/compose'}],
        scope_types=['project']),

    policy.DocumentedRuleDefault(
        name='coast:service:get_dockerfile',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Read dockerfile text (can contain secrets).',
        operations=[{'method': 'GET',
                     'path': '/v1/services/{service_id}/dockerfile'}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:update_dockerfile',
        check_str=base.PROJECT_MEMBER_OR_ADMIN,
        description='Replace desired dockerfile text.',
        operations=[{'method': 'PUT',
                     'path': '/v1/services/{service_id}/dockerfile'}],
        scope_types=['project']),

    policy.DocumentedRuleDefault(
        name='coast:service:get_containers',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List compose service containers.',
        operations=[{'method': 'GET',
                     'path': '/v1/services/{service_id}/containers'}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:get_container',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read container environment and limits.',
        operations=[{'method': 'GET',
                     'path': ('/v1/services/{service_id}/containers/'
                              '{container_id}')}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:service:get_container_logs',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='Read recent container logs.',
        operations=[{'method': 'GET',
                     'path': ('/v1/services/{service_id}/containers/'
                              '{container_id}/logs')}],
        scope_types=['project']),

]


def list_rules():
    return rules
