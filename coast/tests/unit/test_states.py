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

from coast.common import exception
from coast.common import states
from coast.tests import base


DIRECT_DELETE_RESOURCES = frozenset(['domain', 'storage', 'backup_job'])
SOFT_DELETE_WITHOUT_STATUS_RESOURCES = frozenset(['deployment'])
TERMINALS = {
    'backup': frozenset([states.DELETED]),
    'backup_job': frozenset([states.ACTIVE, states.ERROR]),
    'stack': frozenset([states.DELETED]),
    'appgroup': frozenset([states.DELETED]),
    'app_environment': frozenset([states.DELETED]),
    'server': frozenset(),
    'volume': frozenset(),
    'service': frozenset(),
    'deployment': frozenset([states.SUPERSEDED, states.FAILED,
                             states.CANCELLED, states.ROLLED_BACK]),
    'domain': frozenset(),
    'export': frozenset(),
    'storage': frozenset(),
}
INITIAL = {
    'backup': states.PENDING,
    'backup_job': states.PENDING,
    'stack': states.PENDING,
    'appgroup': states.PENDING,
    'app_environment': states.PENDING,
    'server': states.PENDING,
    'volume': states.CREATING,
    'service': states.PENDING,
    'deployment': states.PENDING,
    'domain': states.PENDING_VERIFICATION,
    'export': states.PENDING,
    'storage': states.AVAILABLE,
}
# Independent vocabulary: catch accidental removal as well as missing rows.
VOCABULARY = {
    'backup': 'PENDING BUILDING ACTIVE ERROR DELETING DELETED',
    'backup_job': 'PENDING BUILDING ACTIVE ERROR',
    'stack': 'PENDING BUILDING ACTIVE DEGRADED UPGRADING ROLLING_BACK ERROR '
    'DELETING DELETED',
    'appgroup': 'PENDING ACTIVE ERROR DELETING DELETED',
    'app_environment': 'PENDING ACTIVE ERROR DELETING DELETED',
    'server': 'PENDING BUILDING ACTIVE UNHEALTHY FAILOVER ERROR DELETING',
    'volume': 'CREATING AVAILABLE IN_USE SNAPSHOTTING ERROR DELETING',
    'service': 'PENDING ACTIVE STOPPED DEGRADED ERROR DELETING STARTING '
               'DEPLOYING',
    'deployment': 'PENDING BUILDING DEPLOYING ACTIVE SUPERSEDED FAILED '
                  'CANCELLED ROLLED_BACK',
    'domain': 'PENDING_VERIFICATION VERIFIED PENDING_DNS ACTIVE ERROR',
    'export': 'PENDING SNAPSHOTTING UPLOADING ACTIVE EXPIRED ERROR DELETING',
    'storage': 'AVAILABLE IN_USE',
}


def reachable(transitions, initial):
    seen = set()
    pending = [initial]
    while pending:
        state = pending.pop()
        if state not in seen:
            seen.add(state)
            pending.extend(transitions[state] - seen)
    return seen


class TransitionTest(base.TestCase):

    def test_complete_known_vocabulary(self):
        self.assertEqual(set(VOCABULARY), set(states.TRANSITIONS))
        for resource, vocabulary in VOCABULARY.items():
            with self.subTest(resource=resource):
                expected = frozenset(vocabulary.split())
                self.assertEqual(expected, frozenset(
                    states.TRANSITIONS[resource]))
                self.assertEqual(expected, getattr(
                    states, resource.upper() + '_STATES'))
                for source, targets in states.TRANSITIONS[resource].items():
                    self.assertIsInstance(targets, frozenset)
                    self.assertLessEqual(targets, expected)
                    self.assertNotIn(source, targets)

    def test_terminal_states_have_no_outgoing_edges(self):
        for resource, transitions in states.TRANSITIONS.items():
            self.assertEqual(TERMINALS[resource], frozenset(
                state for state, targets in transitions.items()
                if not targets))

    def test_all_states_reachable_from_initial(self):
        for resource, transitions in states.TRANSITIONS.items():
            with self.subTest(resource=resource):
                self.assertEqual(set(transitions), reachable(
                    transitions, INITIAL[resource]))

    def test_deletion_reachable_with_explicit_exemptions(self):
        for resource, transitions in states.TRANSITIONS.items():
            if resource in (DIRECT_DELETE_RESOURCES |
                            SOFT_DELETE_WITHOUT_STATUS_RESOURCES):
                self.assertNotIn(states.DELETING, transitions)
                continue
            for source in transitions.keys() - TERMINALS[resource]:
                with self.subTest(resource=resource, source=source):
                    self.assertIn(states.DELETING,
                                  reachable(transitions, source))

    def test_deleted_only_entered_through_deleting(self):
        predecessors = []
        for resource, transitions in states.TRANSITIONS.items():
            for source, targets in transitions.items():
                if states.DELETED in targets:
                    predecessors.append((resource, source))
        self.assertEqual([
            ('backup', states.DELETING),
            ('stack', states.DELETING),
            ('appgroup', states.DELETING),
            ('app_environment', states.DELETING),
        ], predecessors)

    def test_helper_matches_every_edge_and_known_noop(self):
        for resource, transitions in states.TRANSITIONS.items():
            for source, targets in transitions.items():
                for target in transitions:
                    with self.subTest(resource=resource, source=source,
                                      target=target):
                        if source == target or target in targets:
                            self.assertIsNone(states.check_transition(
                                resource, source, target))
                        else:
                            self.assertRaises(
                                exception.InvalidStateTransition,
                                states.check_transition,
                                resource, source, target)

    def test_unknown_resource_or_state_including_noop(self):
        for resource, source, target in [
                ('unknown', states.ACTIVE, states.ACTIVE),
                ('service', 'unknown', 'unknown'),
                ('service', 'unknown', states.ACTIVE),
                ('service', states.ACTIVE, 'unknown'),
                ('service', states.BUILDING, states.ACTIVE),
                ('service', states.NONE, states.NONE)]:
            self.assertRaises(exception.InvalidStateTransition,
                              states.check_transition, resource,
                              source, target)

    def test_exception_is_conflict_and_names_transition(self):
        error = self.assertRaises(exception.InvalidStateTransition,
                                  states.check_transition,
                                  'stack', states.ERROR, states.ACTIVE)
        self.assertIsInstance(error, exception.Conflict)
        self.assertEqual(409, error.code)
        self.assertEqual('Invalid stack state transition from ERROR '
                         'to ACTIVE.', str(error))

    def test_recovery_and_immutable_deployments(self):
        for resource, source, target in [
                ('stack', states.DEGRADED, states.ACTIVE),
                ('stack', states.ERROR, states.DELETING),
                ('server', states.UNHEALTHY, states.FAILOVER),
                ('server', states.FAILOVER, states.ACTIVE),
                ('server', states.FAILOVER, states.ERROR),
                ('service', states.ERROR, states.ACTIVE)]:
            self.assertIsNone(states.check_transition(
                resource, source, target))
        for source in TERMINALS['deployment']:
            self.assertRaises(exception.InvalidStateTransition,
                              states.check_transition,
                              'deployment', source, states.PENDING)

    def test_failed_deletes_can_be_retried(self):
        for resource in ('stack', 'appgroup', 'app_environment',
                         'server', 'volume', 'service', 'export'):
            with self.subTest(resource=resource):
                self.assertIsNone(states.check_transition(
                    resource, states.DELETING, states.ERROR))
                self.assertIsNone(states.check_transition(
                    resource, states.ERROR, states.DELETING))
