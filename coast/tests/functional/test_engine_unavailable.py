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

"""API to conductor log reads when the engine cannot be reached."""

from coast.tests.unit import test_live_logs


class EngineUnavailableLogTest(test_live_logs.LiveLogsTest):
    """Functional run of engine log faults and unavailable output.

    The base class also carries unrelated API cases. ``load_tests`` keeps
    this module to the engine-fault reads.
    """


_CASES = (
    'test_service_logs_return_503_when_engine_is_unreachable',
    'test_deployment_log_returns_503_when_engine_is_unreachable',
    'test_service_logs_return_503_when_engine_rejects_credential',
    'test_service_logs_return_503_when_engine_token_is_missing',
    'test_service_logs_return_500_for_invalid_engine_response',
    'test_missing_engine_log_resources_return_empty_complete',
    'test_old_agent_template_log_rejection_is_empty_complete_once',
    'test_template_deployment_logs_are_complete_during_rollout',
)


def load_tests(loader, tests, pattern):
    suite = loader.suiteClass()
    for name in _CASES:
        suite.addTest(EngineUnavailableLogTest(name))
    return suite
