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

import concurrent.futures
from importlib import metadata
from pathlib import Path
import threading
from unittest import mock

import fixtures
from oslo_config import cfg
from oslo_policy import policy as oslo_policy

from coast.common import context
from coast.common import exception
from coast.common import policy
from coast import policies
from coast.policies import base as policy_base
from coast.tests import base


EXPECTED_RULES = [
    'coast:database:get_all',
    'coast:database:get_one',
    'coast:database:create',
    'coast:database:update',
    'coast:database:delete',
    'coast:database:start',
    'coast:database:stop',
    'coast:database:restart',
    'coast:database:get_logs',
    'coast:database:get_credentials',

    'coast:flavor:get_all',
    'coast:stack:resize_workers',
    'coast:server:resize',
    'coast:stack:ingress_rules:list',
    'coast:stack:ingress_rules:create',
    'coast:stack:ingress_rules:delete',
    'coast:dns:zones:list',
    'coast:backup:get_policy',
    'coast:backup:set_policy',
    'coast:backup:get_all',
    'coast:backup:get_one',
    'coast:backup:create',
    'coast:backup:delete',
    'coast:backup:download',
    'coast:backup:restore',

    'coast:stack:events:get',
    'coast:stack:get_all',
    'coast:stack:get_one',
    'coast:stack:create',
    'coast:stack:update',
    'coast:stack:delete',
    'coast:stack:upgrade',
    'coast:stack:upgrade_rollback',
    'coast:stack:get_detail',
    'coast:export:get_all',
    'coast:export:get_one',
    'coast:export:create',
    'coast:export:renew_url',
    'coast:export:delete',
    'coast:appgroup:get_all',
    'coast:appgroup:get_one',
    'coast:appgroup:create',
    'coast:appgroup:update',
    'coast:appgroup:delete',
    'coast:environment:get_all',
    'coast:environment:get_one',
    'coast:environment:create',
    'coast:environment:update',
    'coast:environment:delete',
    'coast:server:get_all',
    'coast:server:get_one',
    'coast:server:create',
    'coast:server:delete',
    'coast:server:failover',
    'coast:server:get_detail',
    'coast:volume:get_all',
    'coast:volume:get_one',
    'coast:volume:create',
    'coast:volume:update',
    'coast:volume:delete',
    'coast:volume:snapshot',
    'coast:volume:get_detail',
    'coast:storage:get_all',
    'coast:storage:get_one',
    'coast:storage:create',
    'coast:storage:attach',
    'coast:storage:detach',
    'coast:storage:delete',
    'coast:file_mount:get_all',
    'coast:file_mount:get_one',
    'coast:file_mount:create',
    'coast:file_mount:update',
    'coast:file_mount:delete',
    'coast:service:get_all',
    'coast:service:get_one',
    'coast:service:create',
    'coast:service:update',
    'coast:service:delete',
    'coast:service:start',
    'coast:service:stop',
    'coast:service:restart',
    'coast:service:get_webhooks',
    'coast:service:regenerate_webhooks',
    'coast:service:get_logs',
    'coast:service:get_containers',
    'coast:service:get_container',
    'coast:service:get_container_logs',
    'coast:service:get_dockerfile',
    'coast:service:update_dockerfile',
    'coast:service:get_compose',
    'coast:service:update_compose',
    'coast:app:console',
    'coast:service:console',
    'coast:deployment:get_all',
    'coast:deployment:get_one',
    'coast:deployment:create',
    'coast:deployment:rollback',
    'coast:deployment:cancel',
    'coast:deployment:get_log',
    'coast:domain:get_all',
    'coast:domain:get_one',
    'coast:domain:create',
    'coast:domain:delete',
    'coast:domain:update',
    'coast:domain:verify',
    'coast:secret:get_all',
    'coast:secret:update',
    'coast:event:get_all',
    'coast:quota:get_all',
    'coast:quota:get_one',
    'coast:quota:update',
    'coast:quota:delete',
    'coast:quota:get_defaults',
    'coast:plan:get_all',
    'coast:template:list',
    'coast:template:get',
]


class PolicyTest(base.TestCase):

    def setUp(self):
        super().setUp()
        self.enforcer = oslo_policy.Enforcer(self.conf)
        self.enforcer.register_defaults(policies.list_rules())
        self.enforcer_patch = mock.patch.object(
            policy, 'get_enforcer', return_value=self.enforcer)
        self.enforcer_patch.start()
        self.addCleanup(self.enforcer_patch.stop)
        self.addCleanup(self.enforcer.clear)
        self.target = {'project_id': 'project-a'}

    def authorize(self, rule, roles, project='project-a', **kwargs):
        ctx = context.RequestContext(
            project_id=project, roles=roles, **kwargs)
        return policy.authorize(ctx, rule, self.target)

    def test_registered_rules(self):
        rules = list(policies.list_rules())
        endpoints = [r for r in rules
                     if isinstance(r, oslo_policy.DocumentedRuleDefault)]
        self.assertCountEqual(EXPECTED_RULES, [r.name for r in endpoints])
        self.assertEqual(len(rules), len({r.name for r in rules}))
        for rule in rules:
            self.assertEqual(['project'], rule.scope_types)
        for rule in endpoints:
            self.assertTrue(rule.description)
            self.assertTrue(rule.operations)
            for operation in rule.operations:
                self.assertIn(operation['method'],
                              ['GET', 'POST', 'PATCH', 'PUT', 'DELETE'])
                self.assertTrue(operation['path'].startswith('/v1/'))

    def test_exact_persona_checks(self):
        for persona, role in [('PROJECT_READER', 'reader'),
                              ('PROJECT_MEMBER', 'member'),
                              ('PROJECT_MANAGER', 'manager')]:
            self.assertEqual(
                'role:%s and project_id:%%(project_id)s' % role,
                getattr(policy_base, persona))
            self.assertEqual(
                'rule:%s or rule:ADMIN' % persona,
                getattr(policy_base, persona + '_OR_ADMIN'))
        self.assertEqual('role:admin', policy_base.ADMIN)

    def test_persona_matrix(self):
        cases = {
            'coast:flavor:get_all': ['reader', 'admin'],
            'coast:stack:get_one': ['reader', 'admin'],
            'coast:stack:create': ['member', 'admin'],
            'coast:stack:update': ['member', 'admin'],
            'coast:stack:upgrade': ['member', 'admin'],
            'coast:stack:upgrade_rollback': ['member', 'admin'],
            'coast:appgroup:get_one': ['reader', 'admin'],
            'coast:appgroup:create': ['manager', 'admin'],
            'coast:appgroup:update': ['member', 'admin'],
            'coast:appgroup:delete': ['manager', 'admin'],
            'coast:environment:get_all': ['reader', 'admin'],
            'coast:environment:create': ['manager', 'admin'],
            'coast:environment:update': ['member', 'admin'],
            'coast:environment:delete': ['manager', 'admin'],
            'coast:export:get_one': ['member', 'admin'],
            'coast:export:renew_url': ['manager', 'admin'],
            'coast:volume:delete': ['manager', 'admin'],
            'coast:volume:get_detail': ['admin'],
            'coast:deployment:create': ['member', 'admin'],
            'coast:app:console': ['member', 'admin'],
            'coast:service:console': ['member', 'admin'],
            'coast:service:get_dockerfile': ['member', 'admin'],
            'coast:service:update_dockerfile': ['member', 'admin'],
            'coast:service:get_compose': ['member', 'admin'],
            'coast:service:update_compose': ['member', 'admin'],
            'coast:dns:zones:list': ['reader', 'member', 'manager', 'admin'],
            'coast:domain:get_all': ['reader', 'admin'],
            'coast:domain:update': ['member', 'admin'],
            'coast:domain:verify': ['member', 'admin'],
            'coast:secret:get_all': ['member', 'admin'],
            'coast:quota:get_defaults': ['reader', 'admin'],
            'coast:quota:update': ['admin'],
            'coast:plan:get_all': ['reader', 'member', 'manager',
                                   'admin', 'unrelated'],
        }
        # Roles are literal checks; Keystone supplies implied roles.
        for rule, allowed in cases.items():
            for role in ['reader', 'member', 'manager', 'admin', 'unrelated']:
                with self.subTest(rule=rule, role=role):
                    if role in allowed:
                        self.assertTrue(self.authorize(rule, [role]))
                    else:
                        self.assertRaises(exception.NotAuthorized,
                                          self.authorize, rule, [role])

    def test_reader_cannot_write(self):
        for rule in list(policies.list_rules()):
            if isinstance(rule, oslo_policy.DocumentedRuleDefault):
                if any(op['method'] != 'GET' for op in rule.operations):
                    self.assertRaises(exception.NotAuthorized,
                                      self.authorize, rule.name, ['reader'])

    def test_wrong_project_hidden_for_every_endpoint(self):
        for rule in EXPECTED_RULES:
            for role in ['reader', 'member', 'manager']:
                with self.subTest(rule=rule, role=role):
                    self.assertRaises(exception.NotFound, self.authorize,
                                      rule, [role], project='project-b')

    def test_project_admin_cross_project(self):
        for rule in EXPECTED_RULES:
            self.assertTrue(self.authorize(
                rule, ['admin'], project='project-b'))

    def test_agent_role_has_no_api_grant(self):
        self.assertEqual('role:coast_agent', policy_base.AGENT)
        for rule in policies.list_rules():
            if isinstance(rule, oslo_policy.DocumentedRuleDefault):
                self.assertNotIn('coast_agent', rule.check_str)
                self.assertNotIn('rule:AGENT', rule.check_str)
        for rule in ('coast:secret:get_all', 'coast:stack:get_one',
                     'coast:stack:get_detail', 'coast:app:console',
                     'coast:service:console', 'coast:volume:get_detail'):
            self.assertRaises(exception.NotAuthorized, self.authorize,
                              rule, ['coast_agent'])
        self.assertTrue(self.authorize('coast:plan:get_all', ['coast_agent']))
        self.assertRaises(exception.NotFound, self.authorize,
                          'coast:plan:get_all', ['coast_agent'],
                          project='project-b')
        self.assertRaises(exception.NotAuthorized, self.authorize,
                          'coast:plan:get_all', ['coast_agent'],
                          project=None, system_scope='all')

    def test_system_scope_rejected(self):
        for rule in EXPECTED_RULES:
            self.assertRaises(
                exception.NotAuthorized, self.authorize, rule, ['admin'],
                project=None, system_scope='all')

    def test_domain_scope_rejected(self):
        ctx = context.RequestContext(
            domain_id='domain-a', roles=['admin'])
        self.assertRaises(
            exception.NotAuthorized, policy.authorize, ctx,
            'coast:plan:get_all', {})

    def test_operator_policy_file_override(self):
        directory = self.useFixture(fixtures.TempDir()).path
        path = Path(directory) / 'policy.yaml'
        path.write_text('"coast:service:create": "role:reader"\n')
        self.config.config(policy_file=str(path), policy_dirs=[],
                           group='oslo_policy')
        enforcer = oslo_policy.Enforcer(self.conf)
        enforcer.register_defaults(policies.list_rules())
        self.addCleanup(enforcer.clear)
        with mock.patch.object(policy, 'get_enforcer', return_value=enforcer):
            self.assertTrue(self.authorize('coast:service:create', ['reader']))
            self.assertRaises(exception.NotAuthorized, self.authorize,
                              'coast:service:create', ['member'])

    def test_is_admin_flag_does_not_grant_role(self):
        self.assertRaises(
            exception.NotAuthorized, self.authorize,
            'coast:stack:upgrade', ['reader'], is_admin=True)
        self.assertRaises(
            exception.NotFound, self.authorize,
            'coast:stack:get_one', ['reader'],
            project='project-b', is_admin=True)

    def test_policy_override_cannot_bypass_project_guard(self):
        self.enforcer.set_rules(oslo_policy.Rules.from_dict({
            'coast:stack:get_one': '@'}), overwrite=False)
        self.assertRaises(
            exception.NotFound, self.authorize,
            'coast:stack:get_one', ['reader'], project='project-b')

    def test_unknown_policy_fails_closed(self):
        self.assertRaises(
            oslo_policy.PolicyNotRegistered, self.authorize,
            'coast:service:unknown', ['admin'])

    def test_secret_update_operations(self):
        rule = self.enforcer.registered_rules['coast:secret:update']
        self.assertEqual(
            [{'method': 'PUT', 'path': '/v1/services/{service_id}/env'},
             {'method': 'PATCH', 'path': '/v1/services/{service_id}/env'}],
            rule.operations)


class EntryPointTest(base.TestCase):

    def test_concurrent_initialization_does_not_publish_partial_rules(self):
        self.addCleanup(policy.reset)
        policy.reset()
        registering = threading.Event()
        release = threading.Event()
        list_rules = policies.list_rules

        def paused_rules():
            registering.set()
            if not release.wait(timeout=10):
                raise AssertionError('Policy registration was not released')
            return list_rules()

        ctx = context.RequestContext(project_id='project', roles=['member'])
        target = {'project_id': 'project'}

        def authorize():
            allowed = policy.authorize(ctx, 'coast:volume:update', target)
            return allowed, policy.get_enforcer()

        with mock.patch.object(cfg, 'CONF', self.conf), mock.patch.object(
                policies, 'list_rules', side_effect=paused_rules):
            with concurrent.futures.ThreadPoolExecutor(2) as executor:
                first = executor.submit(authorize)
                try:
                    self.assertTrue(registering.wait(timeout=10))
                    # A request must not see an enforcer without defaults.
                    self.assertIsNone(policy._ENFORCER)
                    second = executor.submit(authorize)
                finally:
                    release.set()
                first_allowed, first_enforcer = first.result(timeout=10)
                second_allowed, second_enforcer = second.result(timeout=10)
        self.assertTrue(first_allowed)
        self.assertTrue(second_allowed)
        self.assertIs(first_enforcer, second_enforcer)

    def test_failed_initialization_can_be_retried(self):
        self.addCleanup(policy.reset)
        policy.reset()
        with mock.patch.object(cfg, 'CONF', self.conf):
            with mock.patch.object(policies, 'list_rules',
                                   side_effect=RuntimeError('Registration')):
                self.assertRaises(RuntimeError, policy.get_enforcer)
            ctx = context.RequestContext(project_id='project',
                                         roles=['member'])
            self.assertTrue(policy.authorize(
                ctx, 'coast:volume:update', {'project_id': 'project'}))

    def test_entry_points_and_singleton(self):
        self.addCleanup(policy.reset)
        policy.reset()
        with mock.patch.object(cfg, 'CONF', self.conf):
            for group, expected in [
                    ('oslo.policy.policies', policies.list_rules),
                    ('oslo.policy.enforcer', policy.get_enforcer)]:
                entry = metadata.entry_points(
                    group=group, name='coast')
                self.assertEqual(1, len(entry))
                self.assertIs(expected, next(iter(entry)).load())
            first = policy.get_enforcer()
            self.assertIs(first, policy.get_enforcer())
            self.assertTrue(self.conf.oslo_policy.enforce_scope)
            self.assertTrue(self.conf.oslo_policy.enforce_new_defaults)
            self.assertIn('coast:service:create', first.registered_rules)
            policy.reset()
            self.assertIsNot(first, policy.get_enforcer())
