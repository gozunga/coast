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
        name='coast:flavor:get_all',
        check_str=base.PROJECT_READER_OR_ADMIN,
        description='List allowed VM flavors and their plans.',
        operations=[{'method': 'GET', 'path': '/v1/flavors'}],
        scope_types=['project']),
    policy.DocumentedRuleDefault(
        name='coast:plan:get_all',
        check_str='@',
        description='List plans.',
        operations=[
            {'method': 'GET',
             'path': '/v1/plans'},
        ],
        scope_types=['project']),
]


def list_rules():
    return rules
