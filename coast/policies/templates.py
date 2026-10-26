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


rules = [
    policy.DocumentedRuleDefault(
        name='coast:template:' + action,
        check_str=('rule:PROJECT_READER_OR_ADMIN or '
                   'rule:PROJECT_MEMBER_OR_ADMIN or '
                   'rule:PROJECT_MANAGER_OR_ADMIN'),
        description=description,
        operations=[{'method': 'GET', 'path': path}],
        scope_types=['project'])
    for action, description, path in (
        ('list', 'List permitted templates for a stack.', '/v1/templates'),
        ('get', 'Read a permitted template for a stack.',
         '/v1/templates/{template_id}'))
]


def list_rules():
    return rules
