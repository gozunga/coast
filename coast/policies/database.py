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

"""Project database policies, including reserved action hooks."""

from oslo_policy import policy

from coast.policies import base


rules = []
for action, method, suffix in (
        ('get_all', 'GET', ''), ('get_one', 'GET', '/{database_id}'),
        ('create', 'POST', ''), ('update', 'PATCH', '/{database_id}'),
        ('delete', 'DELETE', '/{database_id}'),
        ('get_credentials', 'GET', '/{database_id}/credentials'),
        ('start', 'POST', '/{database_id}/action'),
        ('stop', 'POST', '/{database_id}/action'),
        ('restart', 'POST', '/{database_id}/action'),
        ('get_logs', 'GET', '/{database_id}/logs')):
    operations = [{'method': method, 'path': '/v1/databases' + suffix}]
    if action == 'get_all':
        operations.append({'method': 'GET', 'path': '/v1/database_engines'})
    rules.append(policy.DocumentedRuleDefault(
        name='coast:database:' + action,
        check_str=(base.PROJECT_READER_OR_ADMIN if action in (
            'get_all', 'get_one', 'get_logs') else
            base.PROJECT_MEMBER_OR_ADMIN),
        description='Authorize database ' + action.replace('_', ' ') + '.',
        operations=operations, scope_types=['project']))


def list_rules():
    return rules
