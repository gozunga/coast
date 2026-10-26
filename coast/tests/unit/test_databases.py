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

"""Offline database API, secret, isolation and conductor coverage."""

import datetime
import json

import fixtures
from openstack.network.v2 import security_group_rule
from oslo_utils import timeutils
from pathlib import Path
from unittest import mock

import requests_mock

from coast.api import handlers
from coast.common import engine_versions
from coast.common import exception
from coast.common import quota
from coast.common import states
from coast.conductor import database_faults
from coast.conductor import deployments
from coast.conductor import manager
from coast.conductor import reconcile
from coast.conductor import runtime_logs
from coast.conductor import stack_teardown
from coast.db import api as db_api
from coast.drivers import base as driver_base
from coast.drivers.coolify import v4
from coast.network import stack as network_stack
from coast import objects
from coast.tests import app_api
from coast.tests import database_registry


class DatabaseTest(app_api.AppAPITest):
    def call(self, method, path='/v1/databases', body=None, status=200, **kw):
        return self.request(method, path, body, status=status, version='1.29',
                            **kw)

    def body(self, **kw):
        values = dict(
            name='orders', engine=next(iter(engine_versions.DATABASES)),
            appgroup_id=self.application.id,
            environment_id=self.environment.id, server_id=self.server.id)
        values.update(kw)
        return {'database': values}

    def create(self, **kw):
        with mock.patch.object(handlers.Handler, 'create_service'):
            response = self.call('post', body=self.body(**kw), status=202)
        return objects.Service.get_by_id(
            self.ctx, response.json['database']['id'])

    def test_fault_render_and_read_only(self):
        row = self.unlock(self.create().id)
        path = '/v1/databases/' + row.id
        self.assertIsNone(self.call('get', path).json['database']['fault'])
        row.fault = database_faults.UNSUPPORTED
        row.status = states.ERROR
        row.save()
        self.assertEqual(row.fault, self.call(
            'get', path).json['database']['fault'])
        self.assertEqual(row.fault, self.call(
            'get').json['databases'][0]['fault'])
        self.assertEqual(row.fault, self.call(
            'get', path + '?fields=fault').json['database']['fault'])
        self.call('post', body=self.body(fault='injected'), status=400)
        self.call('patch', path, {'database': {'fault': None}}, status=400)
        with mock.patch.object(handlers.Handler, 'rename_service'):
            response = self.call('patch', path, {'database': {}})
        self.assertIsNone(response.json['database']['fault'])
        self.assertIsNone(objects.Service.get_by_id(self.ctx, row.id).fault)

    def test_database_create_failure_mappings(self):
        detail = 'coolify traefik https://control.example.com secret-image'
        method_error = driver_base.DriverError(detail)
        method_error.status_code = 405
        cases = (
            (driver_base.EngineNotFound(detail), database_faults.UNSUPPORTED),
            (method_error, database_faults.UNSUPPORTED),
            (driver_base.EngineUnreachable(detail),
             database_faults.UNREACHABLE),
            (TimeoutError(detail), database_faults.UNREACHABLE),
            (driver_base.EngineConflict(detail), database_faults.CAPACITY),
            (exception.OverQuota(detail), database_faults.CAPACITY),
            (RuntimeError(detail),
             database_faults.public_fault(None, 'create')))
        owner = manager.Manager(self.conf)
        for number, (error, expected) in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                row = self.create()
                with mock.patch.object(owner, '_driver', side_effect=error):
                    owner.execute(self.ctx, 'create_service', row, {})
                current = objects.Service.get_by_id(self.ctx, row.id)
                self.assertEqual(states.ERROR, current.status)
                self.assertEqual(expected, current.fault)
                self.assertNotIn('coolify', current.fault.lower())
                self.assertNotIn('traefik', current.fault.lower())
                self.assertNotIn(detail, current.fault)
                current.name = 'failed-%s' % number
                current.save()

    def test_database_action_failures_and_fault_clearing(self):
        owner = manager.Manager(self.conf)
        for action in ('start', 'stop', 'restart', 'rename', 'delete'):
            with self.subTest(action=action):
                row = self.unlock(self.create().id)
                row.status = (states.STOPPED if action == 'start'
                              else states.ACTIVE)
                row.fault = database_faults.UNSUPPORTED
                row.engine_ref = 'database-host'
                row.save()
                path = '/v1/databases/' + row.id
                handler = action + '_service'
                with mock.patch.object(handlers.Handler, handler):
                    if action == 'rename':
                        response = self.call('patch', path, {'database': {}})
                    elif action == 'delete':
                        response = self.call('delete', path, status=202)
                    else:
                        response = self.call('post', path + '/action',
                                             {action: {}}, status=202)
                self.assertIsNone(response.json['database']['fault'])
                current = objects.Service.get_by_id(self.ctx, row.id)
                with mock.patch.object(owner, '_' + handler,
                                       side_effect=RuntimeError('private')):
                    owner.execute(self.ctx, handler, current, {})
                current = objects.Service.get_by_id(self.ctx, row.id)
                self.assertEqual(states.ERROR, current.status)
                self.assertEqual(database_faults.public_fault(None, action),
                                 current.fault)
                self.assertNotIn('coolify', current.fault.lower())
                self.assertNotIn('traefik', current.fault.lower())
                current.name = 'failed-' + action
                current.save()

    def test_success_clears_database_fault(self):
        owner = manager.Manager(self.conf)
        self.stack.engine_version = 'noop-1.0'
        self.stack.save()
        row = self.create()
        row.fault = database_faults.CAPACITY
        row.save()
        owner.execute(self.ctx, 'create_service', row, {})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ACTIVE, current.status)
        self.assertIsNone(current.fault)
        current.fault = database_faults.UNREACHABLE
        current.save()
        current.acquire_task(states.RECONCILING, owner='stop')
        owner.execute(self.ctx, 'stop_service', current, {})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.STOPPED, current.status)
        self.assertIsNone(current.fault)

    def test_database_failure_before_task_dispatch(self):
        row = self.create()
        owner = manager.Manager(self.conf)
        with mock.patch.object(owner, '_run', side_effect=(
                driver_base.EngineNotFound('private'))):
            self.assertRaises(driver_base.EngineNotFound, owner.execute,
                              self.ctx, 'create_service', row, {})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(database_faults.UNSUPPORTED, current.fault)

    def public_rules(self):
        path = '/v1/stacks/' + self.stack.id + '/ingress_rules'
        return [r for r in self.call('get', path).json['ingress_rules']
                if r['id'].startswith('managed-database-')]

    def patch_public(self, row, public, status=200):
        with mock.patch.object(handlers.Handler, 'rename_service'):
            return self.call('patch', '/v1/databases/' + row.id,
                             {'database': {'public': public}}, status=status)

    def _cloud_firewall(self):
        # Run the real rule diff against an offline Neutron proxy. The repair
        # sweep is disabled: only conductor operations may change these rules.
        self.config.config(network_repair_interval=0,
                           mgmt_net_id='management',
                           mgmt_sg_id='management-sg',
                           group='controller_worker')
        self.stack.driver = 'coolify_v4'
        self.stack.engine_version = engine_versions.DEFAULT
        self.stack.save()
        objects.Server(
            context=self.ctx, name='controller', stack_id=self.stack.id,
            kind='vm', role='control', private_ip='192.0.2.10',
            status=states.ACTIVE, task_state=states.NONE).create()
        connection = mock.Mock()
        network = connection.network
        module = network_stack.EnvironmentNetwork(self.conf, connection)
        module._find = mock.Mock(side_effect=lambda kind, name, tags:
                                 mock.Mock(id=name, cidr='192.0.2.0/24'))
        module.management_cidrs = mock.Mock(return_value=['198.51.100.0/24'])
        module.ensure_egress_group = mock.Mock(return_value=mock.Mock(id='eg'))
        module.repair_peers = mock.Mock()
        stored = {}
        sequence = iter(range(1000))

        def create(**attrs):
            rule = security_group_rule.SecurityGroupRule(
                id=str(next(sequence)), **attrs)
            stored[rule.id] = rule
            return rule

        network.create_security_group_rule.side_effect = create
        network.delete_security_group_rule.side_effect = stored.pop
        network.security_group_rules.side_effect = lambda **query: [
            rule for rule in stored.values()
            if rule.security_group_id == query['security_group_id']]
        self.useFixture(fixtures.MockPatchObject(
            network_stack, 'EnvironmentNetwork', return_value=module))
        owner = manager.Manager(self.conf, notifier=mock.Mock())
        driver = mock.Mock()
        driver.create_database.return_value = driver_base.DriverResult(
            engine_ref='database-host')
        driver.update_database.return_value = True
        self.useFixture(fixtures.MockPatchObject(
            owner, '_driver', return_value=driver))
        self.useFixture(fixtures.MockPatchObject(
            owner, '_endpoint', return_value=None))
        return owner, driver, network, stored

    def _published_cloud_rules(self, stored):
        return [rule for rule in stored.values()
                if rule.direction == 'ingress' and
                rule.port_range_min == 16000]

    def _create_published(self, owner):
        row = self.create(public={'enabled': True, 'port': 16000})
        owner.execute(self.ctx, 'create_service', row, {})
        row = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.STARTING, row.status)
        return row

    def _recover_abandoned_database_firewall(self, transient_failure=False):
        owner, driver, network, stored = self._cloud_firewall()
        row = self.create(public={'enabled': True, 'port': 16000})
        driver.create_database.side_effect = RuntimeError('Interrupted create')
        # Interrupt the create transaction after its cloud rule is installed,
        # bypassing execute's abort handler just as a process exit would.
        with self.assertRaises(RuntimeError):
            with db_api.writer.using(self.ctx):
                owner._create_database(
                    self.ctx, row, self.stack, driver, {}, [])
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.PENDING, current.status)
        self.assertIsNone(current.engine_ref)
        original, = self._published_cloud_rules(stored)
        self.assertTrue(any(call.kwargs.get('port_range_min') == 16000
                            for call in
                            network.create_security_group_rule.call_args_list))
        with db_api.writer.using(self.ctx):
            current.repository._get(self.ctx, row.id).task_started_at = (
                timeutils.utcnow() - datetime.timedelta(minutes=10))
        if transient_failure:
            network.delete_security_group_rule.side_effect = RuntimeError(
                'Network temporarily unavailable')
        restarted = manager.Manager(self.conf, notifier=mock.Mock())
        restarted.settle_deployments()
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        checkpoint, = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'action': 'firewall_cleanup'})
        if transient_failure:
            self.assertEqual('start', checkpoint.outcome)
            self.assertIn(original.id, stored)
            network.delete_security_group_rule.side_effect = stored.pop
            restarted = manager.Manager(self.conf, notifier=mock.Mock())
            restarted.settle_deployments()
        network.delete_security_group_rule.assert_any_call(original.id)
        self.assertEqual([], self._published_cloud_rules(stored))
        self.assertEqual('success', objects.Event.get_by_id(
            self.ctx, checkpoint.id).outcome)

    def test_abandoned_database_create_removes_cloud_rule_without_sweep(self):
        self._recover_abandoned_database_firewall()

    def test_abandoned_database_cleanup_retries_after_network_failure(self):
        self._recover_abandoned_database_firewall(transient_failure=True)

    def test_public_enable_installs_cloud_rule_without_sweep(self):
        owner, driver, network, stored = self._cloud_firewall()
        self._create_published(owner)
        rule, = self._published_cloud_rules(stored)
        self.assertEqual('0.0.0.0/0', rule.remote_ip_prefix)
        self.assertEqual(16000, rule.port_range_max)
        self.assertEqual('tcp', rule.protocol)
        self.assertTrue(any(call.kwargs.get('port_range_min') == 16000
                            for call in
                            network.create_security_group_rule.call_args_list))

    def _change_public_cloud_rule(self, public):
        owner, driver, network, stored = self._cloud_firewall()
        row = self._create_published(owner)
        original, = self._published_cloud_rules(stored)
        row.status = states.ACTIVE
        row.save()
        self.patch_public(row, public)
        current = objects.Service.get_by_id(self.ctx, row.id)
        network.reset_mock()
        owner.execute(self.ctx, 'rename_service', current, {})
        network.delete_security_group_rule.assert_any_call(original.id)
        return network, stored

    def test_public_source_change_replaces_cloud_rule_without_sweep(self):
        network, stored = self._change_public_cloud_rule(
            {'source': '203.0.113.0/24'})
        rule, = self._published_cloud_rules(stored)
        self.assertEqual('203.0.113.0/24', rule.remote_ip_prefix)
        self.assertTrue(any(
            call.kwargs.get('remote_ip_prefix') == '203.0.113.0/24'
            for call in network.create_security_group_rule.call_args_list))

    def test_public_port_change_replaces_cloud_rule_without_sweep(self):
        network, stored = self._change_public_cloud_rule({'port': 16001})
        self.assertEqual([], self._published_cloud_rules(stored))
        self.assertTrue(any(rule.direction == 'ingress' and
                            rule.port_range_min == 16001 and
                            rule.port_range_max == 16001
                            for rule in stored.values()))
        self.assertTrue(any(call.kwargs.get('port_range_min') == 16001
                            for call in
                            network.create_security_group_rule.call_args_list))

    def test_public_disable_removes_cloud_rule_without_sweep(self):
        network, stored = self._change_public_cloud_rule({'enabled': False})
        self.assertEqual([], self._published_cloud_rules(stored))
        network.create_security_group_rule.assert_not_called()

    def test_database_delete_removes_cloud_rule_without_sweep(self):
        owner, driver, network, stored = self._cloud_firewall()
        row = self._create_published(owner)
        original, = self._published_cloud_rules(stored)
        with mock.patch.object(handlers.Handler, 'delete_service'):
            self.call('delete', '/v1/databases/' + row.id, status=202)
        row = objects.Service.get_by_id(self.ctx, row.id)
        owner.execute(self.ctx, 'delete_service', row, {})
        network.delete_security_group_rule.assert_any_call(original.id)
        self.assertEqual([], self._published_cloud_rules(stored))
        self.call('get', '/v1/databases/' + row.id, status=404)

    def test_public_patch_from_error_installs_cloud_rule_without_sweep(self):
        owner, driver, network, stored = self._cloud_firewall()
        row = self.unlock(self.create().id)
        row.status = states.ERROR
        row.save()
        self.patch_public(row, {'enabled': True, 'port': 16000})
        current = objects.Service.get_by_id(self.ctx, row.id)
        owner.execute(self.ctx, 'rename_service', current, {})
        self.assertEqual(states.STARTING, objects.Service.get_by_id(
            self.ctx, row.id).status)
        rule, = self._published_cloud_rules(stored)
        self.assertEqual('0.0.0.0/0', rule.remote_ip_prefix)
        self.assertTrue(any(call.kwargs.get('port_range_min') == 16000
                            for call in
                            network.create_security_group_rule.call_args_list))

    def test_failed_delete_firewall_cleanup_survives_neutron_failure(self):
        owner, driver, network, stored = self._cloud_firewall()
        row = self._create_published(owner)
        original, = self._published_cloud_rules(stored)
        with mock.patch.object(handlers.Handler, 'delete_service'):
            self.call('delete', '/v1/databases/' + row.id, status=202)
        network.delete_security_group_rule.side_effect = RuntimeError(
            'Network temporarily unavailable')
        current = objects.Service.get_by_id(self.ctx, row.id)
        owner.execute(self.ctx, 'delete_service', current, {})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertIn(original.id, stored)
        pending, = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'action': 'firewall_cleanup',
            'outcome': 'start'})
        network.delete_security_group_rule.side_effect = stored.pop
        network.delete_security_group_rule.reset_mock()
        restarted = manager.Manager(self.conf, notifier=mock.Mock())
        restarted.settle_deployments()
        network.delete_security_group_rule.assert_any_call(original.id)
        self.assertEqual([], self._published_cloud_rules(stored))
        self.assertEqual('success', objects.Event.get_by_id(
            self.ctx, pending.id).outcome)
        # Firewall recovery does not destroy the row needed for delete retry.
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, row.id).status)

    def test_runtime_error_and_recovery_reconcile_cloud_rules(self):
        owner, driver, network, stored = self._cloud_firewall()
        row = self._create_published(owner)
        row.status = states.ACTIVE
        row.save()
        original, = self._published_cloud_rules(stored)
        driver.update_database.return_value = None
        driver.get_database_status.return_value = driver_base.ObservedStatus(
            status=states.ERROR)
        owner.service_reconciler.run(row)
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, row.id).status)
        network.delete_security_group_rule.assert_any_call(original.id)
        self.assertEqual([], self._published_cloud_rules(stored))
        self.config.config(group='conductor', stable_seconds=0)
        driver.get_database_status.return_value = driver_base.ObservedStatus(
            status=states.ACTIVE)
        network.reset_mock()
        owner.service_reconciler.run(objects.Service.get_by_id(
            self.ctx, row.id))
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, row.id).status)
        self.assertEqual(1, len(self._published_cloud_rules(stored)))
        self.assertTrue(any(call.kwargs.get('port_range_min') == 16000
                            for call in
                            network.create_security_group_rule.call_args_list))

    def test_start_timeout_removes_cloud_rule_without_sweep(self):
        owner, driver, network, stored = self._cloud_firewall()
        row = self._create_published(owner)
        original, = self._published_cloud_rules(stored)
        driver.get_app_status.return_value = driver_base.ObservedStatus(
            status=states.STOPPED)
        expired = timeutils.utcnow() - datetime.timedelta(hours=1)
        for event in objects.EventList.get_all(
                self.ctx, filters={'resource_id': row.id, 'action': 'create'}):
            with db_api.writer.using(self.ctx):
                event.repository._get(self.ctx, event.id).created_at = expired
        deployments.settle_starting(owner, self.ctx, row.id)
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, row.id).status)
        network.delete_security_group_rule.assert_any_call(original.id)
        self.assertEqual([], self._published_cloud_rules(stored))

    def test_stop_start_reconciles_cloud_rule_without_sweep(self):
        owner, driver, network, stored = self._cloud_firewall()
        row = self._create_published(owner)
        row.status = states.ACTIVE
        row.save()
        for action in ('stop', 'start'):
            with mock.patch.object(handlers.Handler, action + '_service'):
                self.call('post', '/v1/databases/' + row.id + '/action',
                          {action: {}}, status=202)
            current = objects.Service.get_by_id(self.ctx, row.id)
            # Simulate drift; the action must restore desired exposure itself.
            stored.clear()
            network.reset_mock()
            owner.execute(self.ctx, action + '_service', current, {})
            self.assertEqual(1, len(self._published_cloud_rules(stored)))
            self.assertTrue(any(call.kwargs.get('port_range_min') == 16000
                                for call in network.create_security_group_rule
                                .call_args_list))

    def _failed_action_cleanup(self, action):
        owner, driver, network, stored = self._cloud_firewall()
        row = self._create_published(owner)
        row.status = states.STOPPED if action == 'start' else states.ACTIVE
        row.save()
        original, = self._published_cloud_rules(stored)
        with mock.patch.object(handlers.Handler, action + '_service'):
            self.call('post', '/v1/databases/' + row.id + '/action',
                      {action: {}}, status=202)
        getattr(driver, action + '_app').side_effect = (
            driver_base.EngineUnreachable('Engine unavailable'))
        network.delete_security_group_rule.side_effect = RuntimeError(
            'Network temporarily unavailable')
        owner.execute(self.ctx, action + '_service',
                      objects.Service.get_by_id(self.ctx, row.id), {})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        pending, = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'action': 'firewall_cleanup',
            'outcome': 'start'})
        self.assertIn(original.id, stored)
        network.delete_security_group_rule.side_effect = stored.pop
        network.delete_security_group_rule.reset_mock()
        restarted = manager.Manager(self.conf, notifier=mock.Mock())
        restarted.settle_deployments()
        network.delete_security_group_rule.assert_any_call(original.id)
        self.assertEqual([], self._published_cloud_rules(stored))
        self.assertEqual('success', objects.Event.get_by_id(
            self.ctx, pending.id).outcome)

    def test_failed_start_retries_firewall_cleanup_without_sweep(self):
        self._failed_action_cleanup('start')

    def test_failed_stop_retries_firewall_cleanup_without_sweep(self):
        self._failed_action_cleanup('stop')

    def test_failed_restart_retries_firewall_cleanup_without_sweep(self):
        self._failed_action_cleanup('restart')

    def test_database_failed_create_removes_cloud_rule_without_sweep(self):
        owner, driver, network, stored = self._cloud_firewall()
        exposed = []

        def fail(*args):
            rule, = self._published_cloud_rules(stored)
            exposed.append(rule.id)
            raise driver_base.EngineUnreachable('Engine unavailable')

        driver.create_database.side_effect = fail
        row = self.create(public={'enabled': True, 'port': 16000})
        owner.execute(self.ctx, 'create_service', row, {})
        created = [call for call in
                   network.create_security_group_rule.call_args_list
                   if call.kwargs.get('port_range_min') == 16000]
        self.assertEqual(1, len(created))
        self.assertEqual([], self._published_cloud_rules(stored))
        network.delete_security_group_rule.assert_any_call(exposed[0])
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        # Retrying ERROR must restore the rule once STARTING is checkpointed.
        current.acquire_task(states.PROVISIONING, owner='create_database')
        driver.create_database.side_effect = None
        owner.execute(self.ctx, 'create_service', current, {})
        self.assertEqual(1, len(self._published_cloud_rules(stored)))

    def test_failed_create_firewall_cleanup_survives_neutron_failure(self):
        owner, driver, network, stored = self._cloud_firewall()
        exposed = []

        def fail_create(*args):
            rule, = self._published_cloud_rules(stored)
            exposed.append(rule.id)
            raise driver_base.EngineUnreachable('Engine unavailable')

        attempts = []

        def delete(rule_id):
            attempts.append(rule_id)
            if len(attempts) <= 2:
                raise RuntimeError('Network temporarily unavailable')
            return stored.pop(rule_id)

        driver.create_database.side_effect = fail_create
        network.delete_security_group_rule.side_effect = delete
        row = self.create(public={'enabled': True, 'port': 16000})
        owner.execute(self.ctx, 'create_service', row, {},
                      reservation={'apps': 1})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertIsNone(current.task_owner)
        usage = quota.usage(self.ctx)
        self.assertEqual(0, usage['reserved']['apps'])
        self.assertEqual([exposed[0]], attempts)
        self.assertEqual(1, len(self._published_cloud_rules(stored)))
        pending = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'action': 'firewall_cleanup',
            'outcome': 'start'})
        self.assertEqual(1, len(pending))
        failures = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'action': 'create', 'outcome': 'failure'})
        self.assertTrue(failures)
        # A fresh manager has no in-memory retry state. Exercise the actual
        # settle entry point with optional network repair still disabled.
        restarted = manager.Manager(self.conf, notifier=mock.Mock())
        with mock.patch.object(restarted.network_reconciler, 'sweep') as sweep:
            restarted.settle_deployments()
            sweep.assert_not_called()
        self.assertEqual([exposed[0], exposed[0]], attempts)
        self.assertEqual(1, len(self._published_cloud_rules(stored)))
        self.assertEqual('start', objects.Event.get_by_id(
            self.ctx, pending[0].id).outcome)
        restarted.settle_deployments()
        self.assertEqual([exposed[0]] * 3, attempts)
        self.assertEqual([], self._published_cloud_rules(stored))
        self.assertEqual('success', objects.Event.get_by_id(
            self.ctx, pending[0].id).outcome)
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertIsNone(current.task_owner)
        # Completed cleanup is not retried on subsequent ticks.
        restarted.settle_deployments()
        self.assertEqual(3, len(attempts))

    def test_delayed_firewall_cleanup_preserves_retried_database(self):
        owner, driver, network, stored = self._cloud_firewall()
        driver.create_database.side_effect = driver_base.EngineUnreachable(
            'Engine unavailable')
        network.delete_security_group_rule.side_effect = RuntimeError(
            'Network temporarily unavailable')
        row = self.create(public={'enabled': True, 'port': 16000})
        owner.execute(self.ctx, 'create_service', row, {})
        pending, = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'action': 'firewall_cleanup',
            'outcome': 'start'})
        network.delete_security_group_rule.side_effect = stored.pop
        driver.create_database.side_effect = None
        current = objects.Service.get_by_id(self.ctx, row.id)
        current.acquire_task(states.PROVISIONING, owner='create_database')
        owner.execute(self.ctx, 'create_service', current, {})
        rule, = self._published_cloud_rules(stored)
        network.delete_security_group_rule.reset_mock()
        restarted = manager.Manager(self.conf, notifier=mock.Mock())
        restarted.settle_deployments()
        current_rule, = self._published_cloud_rules(stored)
        self.assertEqual(rule.id, current_rule.id)
        network.delete_security_group_rule.assert_not_called()
        self.assertEqual('success', objects.Event.get_by_id(
            self.ctx, pending.id).outcome)

    def test_failed_source_update_retries_old_cidr_rule_deletion(self):
        owner, driver, network, stored = self._cloud_firewall()
        row = self._create_published(owner)
        old_rule, = self._published_cloud_rules(stored)
        self.assertEqual('0.0.0.0/0', old_rule.remote_ip_prefix)
        row.status = states.ACTIVE
        row.save()
        attempts = []

        def delete(rule_id):
            if rule_id == old_rule.id:
                attempts.append(rule_id)
                # Fail the update and its synchronous abort cleanup. Recovery
                # must be durable while optional network repair is disabled.
                if len(attempts) <= 2:
                    raise RuntimeError('Network temporarily unavailable')
            return stored.pop(rule_id)

        network.delete_security_group_rule.side_effect = delete
        self.patch_public(row, {'source': '203.0.113.0/24'})
        current = objects.Service.get_by_id(self.ctx, row.id)
        owner.execute(self.ctx, 'rename_service', current, {})
        self.assertEqual({'0.0.0.0/0', '203.0.113.0/24'}, {
            rule.remote_ip_prefix
            for rule in self._published_cloud_rules(stored)})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertIsNone(current.task_owner)
        pending, = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'action': 'firewall_cleanup',
            'outcome': 'start'})
        restarted = manager.Manager(self.conf, notifier=mock.Mock())
        with mock.patch.object(restarted.network_reconciler, 'sweep') as sweep:
            restarted.settle_deployments()
            sweep.assert_not_called()
        self.assertEqual([old_rule.id] * 3, attempts)
        self.assertNotIn(old_rule.id, stored)
        self.assertEqual([], self._published_cloud_rules(stored))
        self.assertEqual('success', objects.Event.get_by_id(
            self.ctx, pending.id).outcome)
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ERROR, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertIsNone(current.task_owner)

    def test_public_validation_and_allocation(self):
        for public in ({'port': 1023}, {'port': 9443}, {'port': 8000},
                       {'source': '::/0'}, {'source': 'invalid'},
                       {'enabled': True, 'unknown': 1}):
            self.call('post', body=self.body(public=public), status=400)
        self.config.config(group='databases', public_port_min=16000,
                           public_port_max=16001)
        self.config.config(group='api', max_limit=1)
        first = self.create(public={'enabled': True})
        second = self.create(name='second', public={'enabled': True})
        self.assertEqual(16000, first.public_port)
        self.assertEqual(16001, second.public_port)
        response = self.call('post', body=self.body(
            name='third', public={'enabled': True}), status=409)
        self.assertIn(
            'No public port is available. Free a port, then try again.',
            response.text)
        self.assertEqual(2, len(self.public_rules()))

    def test_public_ports_isolate_sources_across_workers(self):
        from coast.network import stack as network_stack
        worker = objects.Server(
            context=self.ctx, name='second-worker', stack_id=self.stack.id,
            kind='vm', role='worker', status=states.ACTIVE,
            task_state=states.NONE)
        worker.create()
        first = self.create(public={
            'enabled': True, 'port': 16000, 'source': '192.0.2.1/32'})
        self.call('post', body=self.body(
            name='collision', server_id=worker.id, public={
                'enabled': True, 'port': 16000, 'source': '192.0.2.2/32'}),
                  status=409)
        second = self.unlock(self.create(
            name='second', server_id=worker.id, public={
                'port': 16000, 'source': '192.0.2.2/32'}).id)
        second.status = states.ACTIVE
        second.save()
        self.patch_public(second, {'enabled': True}, status=409)
        self.config.config(group='databases', public_port_min=16000,
                           public_port_max=16001)
        self.patch_public(second, {'enabled': True, 'port': None})
        network = network_stack.EnvironmentNetwork(self.conf,
                                                   connection=mock.Mock())
        rules = network_stack.customer_ingress(network.customer_rules(
            self.ctx.project_id, self.stack.id))
        self.assertEqual({(16000, '192.0.2.1/32'), (16001, '192.0.2.2/32')},
                         {(r['port_range_min'], r['remote_ip_prefix'])
                          for r in rules})
        self.assertEqual(first.public_port, 16000)

    def test_disabled_retained_port_can_be_updated_after_reallocation(self):
        row = self.unlock(self.create(public={'port': 16000}).id)
        row.status = states.ACTIVE
        row.save()
        self.create(name='enabled', public={'enabled': True, 'port': 16000})
        for public in ({'enabled': False}, {'source': '192.0.2.3/32'}):
            result = self.patch_public(row, public).json['database']
            self.assertFalse(result['public']['enabled'])
            self.assertEqual(16000, result['public']['port'])
            self.unlock(row.id)
        self.assertEqual('192.0.2.3/32', result['public']['source'])
        self.patch_public(row, {'enabled': True}, status=409)
        self.assertEqual(1, len(self.public_rules()))

    def test_public_rule_update_disable_delete_and_hosts(self):
        self.server.floating_ip = '192.0.2.10'
        self.server.private_ip = '192.0.2.20'
        self.server.save()
        row = self.unlock(self.create(public={'enabled': True}).id)
        row.status = states.ACTIVE
        row.save()
        path = '/v1/databases/' + row.id
        result = self.call('get', path).json['database']
        self.assertEqual(self.server.floating_ip, result['public']['host'])
        self.assertEqual(self.server.private_ip,
                         result['public']['private_host'])
        self.assertIn('@192.0.2.10:', result['public_url'])
        credentials = self.call(
            'get', path + '/credentials').json['credentials']
        self.assertIn(credentials['password'], credentials['public_url'])
        rule = self.public_rules()[0]
        self.assertTrue(rule['managed'])
        self.assertEqual('Database orders', rule['description'])
        rule_path = ('/v1/stacks/' + self.stack.id +
                     '/ingress_rules/' + rule['id'])
        response = self.call('delete', rule_path, status=409)
        self.assertIn('Database orders', response.text)
        self.patch_public(row, {'source': '192.0.2.7/24', 'port': 17000})
        updated = self.public_rules()[0]
        self.assertEqual(rule['id'], updated['id'])
        self.assertEqual('192.0.2.0/24', updated['source'])
        self.assertEqual(17000, updated['port_range_min'])
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertIsNotNone(current.settings_changed_at)
        self.unlock(row.id)
        self.patch_public(row, {'enabled': False})
        self.assertEqual([], self.public_rules())
        self.assertIsNone(
            self.call('get', path).json['database']['public_url'])
        self.unlock(row.id)
        self.patch_public(row, {'enabled': True})
        self.assertEqual(1, len(self.public_rules()))
        self.unlock(row.id)
        with mock.patch.object(handlers.Handler, 'delete_service'):
            self.call('delete', path, status=202)
        self.assertEqual([], self.public_rules())

    def test_public_collision_quota_and_network_desired_state(self):
        from coast.network import stack as network_stack
        ingress_path = '/v1/stacks/' + self.stack.id + '/ingress_rules'
        self.call('post', ingress_path, {'ingress_rule': {
            'protocol': 'tcp', 'port_range_min': 16000,
            'port_range_max': 16010}}, status=201)
        self.call('post', body=self.body(public={
            'enabled': True, 'port': 16005}), status=409)
        row = self.create(public={'enabled': True, 'port': 17000})
        response = self.call('post', body=self.body(
            name='duplicate', public={'enabled': True, 'port': 17000}),
            status=409)
        self.assertIn(
            'Public port is already in use. Choose another port.',
            response.text)
        self.call('post', ingress_path, {'ingress_rule': {
            'protocol': 'tcp', 'port_range_min': 17000}}, status=409)
        network = network_stack.EnvironmentNetwork(self.conf,
                                                   connection=mock.Mock())
        self.assertEqual(2, len(network.customer_rules(
            self.ctx.project_id, self.stack.id)))
        self.assertEqual([], network.customer_rules('other', self.stack.id))
        self.config.config(group='controller_worker',
                           ingress_rules_per_stack=2)
        self.call('post', body=self.body(name='overquota', public={
            'enabled': True, 'port': 18000}), status=403)
        self.call('post', ingress_path, {'ingress_rule': {
            'protocol': 'icmp'}}, status=403)
        self.assertEqual(1, len(self.public_rules()))
        row.status = states.ERROR
        row.save()
        self.assertEqual([], self.public_rules())
        self.assertEqual(1, len(network.customer_rules(
            self.ctx.project_id, self.stack.id)))
        self.call('post', ingress_path, {'ingress_rule': {
            'protocol': 'icmp'}}, status=403)
        self.call('post', ingress_path, {'ingress_rule': {
            'protocol': 'tcp', 'port_range_min': 17000}}, status=409)

    def test_public_failed_create_has_no_orphan_rule(self):
        with mock.patch('coast.api.v1.database_controllers.secret_store.call',
                        side_effect=RuntimeError('secret store unavailable')):
            self.call('post', body=self.body(public={'enabled': True}),
                      status=500)
        self.assertEqual([], self.public_rules())
        self.assertEqual([], self.call('get').json['databases'])

    def test_catalog_and_gating(self):
        catalog = self.call(
            'get', '/v1/database_engines').json['database_engines']
        self.assertEqual(list(engine_versions.DATABASES),
                         [item['engine'] for item in catalog])
        for path in ('/v1/database_engines', '/v1/databases'):
            self.request('get', path, version='1.28', status=404)
            self.call('get', path + '?unknown=value', status=400)
            self.call('get', path, headers={}, status=401)
            self.call('get', path, headers=dict(self.headers,
                      **{'X-Roles': 'unrelated'}), status=403)

    def test_all_engines_defaults_and_secret_separation(self):
        for number, (engine, info) in enumerate(
                engine_versions.DATABASES.items()):
            body = self.body()
            body['database'].update(name='db-%s' % number, engine=engine)
            with mock.patch.object(handlers.Handler, 'create_service'):
                response = self.call('post', body=body, status=202)
            result = response.json['database']
            row = objects.Service.get_by_id(self.ctx, result['id'])
            self.assertEqual(info['default_version'], result['version'])
            self.assertEqual(info['port'], result['port'])
            self.assertEqual(states.PENDING, result['status'])
            self.assertFalse(result['public']['enabled'])
            self.assertNotIn('password_ref', result)
            self.assertNotIn('engine_ref', result)
            credentials = self.call('get', '/v1/databases/' + row.id +
                                    '/credentials')
            self.assertEqual('no-store', credentials.headers['Cache-Control'])
            password = credentials.json['credentials']['password']
            self.assertNotIn(password, json.dumps(result))
            self.assertNotIn(password, str(row.obj_to_primitive()))
            self.assertEqual('root_password' in info['credentials'],
                             'root_password' in
                             credentials.json['credentials'])
            self.assertEqual('app' if info['has_username'] else None,
                             credentials.json['credentials']['username'])

    def test_services_hide_databases_and_names_are_shared(self):
        row = self.create()
        listed = self.call('get', '/v1/services').json['services']
        self.assertNotIn(row.id, [item['id'] for item in listed])
        self.call('get', '/v1/services/' + row.id, status=404)
        self.call('get', '/v1/services/' + row.id + '/env', status=404)
        self.call('get', '/v1/services?kind=database', status=400)
        self.assertEqual([row.id], [item['id'] for item in
                                    self.call('get').json['databases']])
        body = self.body()
        body['database']['name'] = self.service.name
        self.call('post', body=body, status=409)

    def test_credentials_reader_and_foreign_resources(self):
        row = self.create()
        path = '/v1/databases/' + row.id
        reader = dict(self.headers, **{'X-Roles': 'reader'})
        self.call('get', path, headers=reader)
        self.call('get', path + '/credentials', headers=reader, status=403)
        foreign = dict(self.headers, **{'X-Project-Id': 'other'})
        for method, suffix, body in (
                ('get', '', None), ('get', '/credentials', None),
                ('delete', '', None), ('patch', '', {'database': {}})):
            self.call(method, path + suffix, body, headers=foreign, status=404)
        self.assertEqual([], self.call(
            'get', headers=foreign).json['databases'])
        for method, body in (('delete', None), ('patch', {'database': {}})):
            self.call(method, path, body, headers=reader, status=403)

    def test_validation_quota_placement_and_filters(self):
        for key, value in (('password', 'caller-secret'), ('unknown', 1),
                           ('version', 'unknown'), ('server_id', None),
                           ('public', {'source': '::/0'})):
            body = self.body()
            body['database'][key] = value
            self.call('post', body=body, status=400)
        for engine, info in engine_versions.DATABASES.items():
            for field in ('database', 'username'):
                if not info['has_' + field]:
                    body = self.body()
                    body['database'].update(engine=engine, **{field: 'app'})
                    self.call('post', body=body, status=400)
        self.server.role = 'build'
        self.server.save()
        self.call('post', body=self.body(), status=400)
        self.server.role = 'worker'
        self.server.save()
        row = self.create()
        result = self.call('get', '/v1/databases?fields=id,name&engine=' +
                           row.engine).json['databases']
        self.assertEqual({'id', 'name', 'links'}, set(result[0]))
        self.call('get', '/v1/databases?password_ref=x', status=400)
        self.config.config(apps=0, group='quotas')
        body = self.body()
        body['database']['name'] = 'another'
        self.call('post', body=body, status=403)

    def test_create_delete_conductor_and_secret_cleanup(self):
        row = self.create(public={'enabled': True})
        self.assertEqual(1, len(self.public_rules()))
        self.stack.engine_version = 'noop-1.0'
        self.stack.save()
        endpoint = manager.Manager(self.conf)
        # Exercise the actual async operation runner, including quota receipts.
        endpoint.execute(self.ctx, 'create_service', row, {})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ACTIVE, current.status)
        self.assertEqual(states.NONE, current.task_state)
        password = self.store.get(self.ctx, current.password_ref)
        response = self.call('get', '/v1/databases/' + row.id)
        self.assertNotIn(password, response.text)
        credentials = self.call(
            'get', '/v1/databases/' + row.id + '/credentials')
        self.assertIn(
            password, credentials.json['credentials']['internal_url'])
        with mock.patch.object(handlers.Handler, 'delete_service'):
            response = self.call(
                'delete', '/v1/databases/' + row.id, status=202)
        self.assertEqual(states.DELETING, response.json['database']['status'])
        current = objects.Service.get_by_id(self.ctx, row.id)
        endpoint.execute(self.ctx, 'delete_service', current, {})
        self.call('get', '/v1/databases/' + row.id, status=404)
        self.assertNotIn(current.password_ref, self.store.values)
        self.assertEqual([], self.public_rules())
        events = objects.EventList.get_all(self.ctx)
        self.assertNotIn(password, str(
            [item.obj_to_primitive() for item in events]))

    def test_delete_standalone_database_without_source(self):
        row = self.create()
        self.stack.engine_version = 'noop-1.0'
        self.stack.save()
        endpoint = manager.Manager(self.conf)
        endpoint.execute(self.ctx, 'create_service', row, {})
        current = objects.Service.get_by_id(self.ctx, row.id)
        secret_refs = [current.password_ref, current.root_password_ref]
        with mock.patch.object(handlers.Handler, 'delete_service'):
            self.call('delete', '/v1/databases/' + row.id, status=202)
        current = objects.Service.get_by_id(self.ctx, row.id)
        driver = endpoint._driver(self.stack)
        # The shared schema currently supplies an internal source row.
        # Exercise the conductor with a source-less standalone database.
        with mock.patch.object(objects.Service, 'source_id',
                               new_callable=mock.PropertyMock,
                               return_value=None), \
                mock.patch.object(endpoint, '_driver', return_value=driver), \
                mock.patch.object(driver, 'delete_database') as delete:
            endpoint.execute(self.ctx, 'delete_service', current, {})
        delete.assert_called_once()
        self.call('get', '/v1/databases/' + row.id, status=404)
        for ref in secret_refs:
            self.assertNotIn(ref, self.store.values)

    def test_patch_and_parent_block(self):
        row = self.create()
        row = self.unlock(row.id)
        row.status = states.ACTIVE
        row.engine_ref = 'database-host'
        row.save()
        path = '/v1/databases/' + row.id
        for field in ('engine', 'version', 'server_id', 'password'):
            self.call('patch', path, {'database': {
                      field: 'other'}}, status=400)
        with mock.patch.object(handlers.Handler, 'rename_service'):
            result = self.call('patch', path, {'database': {
                'name': 'renamed', 'cpu_limit': 0.5, 'memory_limit_mb': 256}})
        self.assertEqual('renamed', result.json['database']['name'])
        self.assertIsNotNone(objects.Service.get_by_id(
            self.ctx, row.id).settings_changed_at)
        self.call('delete', '/v1/environments/' +
                  self.environment.id, status=409)

    def test_database_images_only_in_registry(self):
        root = Path(__file__).resolve().parents[3]
        paths = list((root / 'coast').rglob('*.py'))
        paths.extend((root / 'tools').rglob('*.py'))
        for path in paths:
            if path == root / 'coast/common/engine_versions.py':
                continue
            self.assertEqual([], database_registry.violations(
                path.read_text(), microversions=(
                    path == root / 'coast/api/microversions.py')), str(path))

    def test_registry_guard_rejects_unregistered_image_and_version(self):
        image = next(iter(database_registry.IMAGES)).rsplit(':', 1)[0]
        image += ':unreviewed-tag'
        self.assertNotIn(image, database_registry.IMAGES)
        for declaration in (
                'DATABASE_IMAGE = ' + repr(image),
                'settings = {"image": ' + repr('example.com/' + image) + '}',
                'DATABASE_VERSION = "999.123"',
                'DATABASE_VERSIONS = ["999.123"]',
                'DATABASE_IMAGE = "example.com/custom-engine:unreviewed"',
                'settings = {"engine": ' +
                repr(next(iter(engine_versions.DATABASES))) +
                ', "version": "999.123"}'):
            self.assertEqual([1], database_registry.violations(declaration))
        self.assertEqual([], database_registry.violations(
            'image = catalog[engine][version]'))

    def test_create_retries_reuse_checkpoint_and_mask_telemetry(self):
        logger = self.useFixture(fixtures.FakeLogger())
        row = self.create(public={'enabled': True})
        reference = row.password_ref
        password = self.store.get(self.ctx, reference)
        notifier = mock.Mock()
        endpoint = manager.Manager(self.conf, notifier=notifier)
        driver = mock.Mock()
        driver.create_database.side_effect = [
            driver_base.EngineUnreachable('Engine unavailable'),
            driver_base.DriverResult(engine_ref='database-host')]
        self.stack.engine_version = engine_versions.DEFAULT
        self.stack.driver = 'coolify_v4'
        self.stack.save()
        with mock.patch.object(endpoint, '_driver', return_value=driver), \
                mock.patch.object(endpoint, '_endpoint', return_value=None), \
                mock.patch.object(endpoint.network_reconciler, 'apply'), \
                mock.patch.object(self.store, 'put',
                                  wraps=self.store.put) as put:
            endpoint.execute(self.ctx, 'create_service', row, {})
            current = objects.Service.get_by_id(self.ctx, row.id)
            self.assertEqual(states.ERROR, current.status)
            self.assertEqual([], self.public_rules())
            current.acquire_task(states.PROVISIONING, owner='create_database')
            endpoint.execute(self.ctx, 'create_service', current, {})
            current = objects.Service.get_by_id(self.ctx, row.id)
            self.assertEqual(states.STARTING, current.status)
            self.assertEqual(1, len(self.public_rules()))
            self.assertEqual(reference, current.password_ref)
            put.assert_not_called()
            first, second = driver.create_database.call_args_list
            self.assertEqual(first.args[-1], second.args[-1])
            self.assertEqual(password, second.args[-1]['password'])
            driver.get_app_status.return_value = driver_base.ObservedStatus(
                status=states.ACTIVE)
            deployments.settle_starting(endpoint, self.ctx, current.id)
        self.assertEqual(states.ACTIVE, objects.Service.get_by_id(
            self.ctx, row.id).status)
        self.assertNotIn(password, logger.output)
        self.assertNotIn(password, str(notifier.mock_calls))
        self.assertTrue(any(call.args[1].startswith('coast.database.')
                            for call in notifier.info.call_args_list))

    def test_stack_cleanup_inventory_includes_database_passwords(self):
        row = self.create()
        refs = set(stack_teardown._secret_refs(self.ctx, self.stack))
        self.assertIn(row.password_ref, refs)
        clients = mock.Mock()
        stack_teardown._delete_engine(self.ctx, self.stack, clients)
        deleted = clients.driver.delete_app.call_args_list
        self.assertTrue(any(call.args[-1].id == row.id for call in deleted))

    def test_database_timeout_survives_metadata_update(self):
        row = self.create()
        endpoint = manager.Manager(self.conf)
        driver = mock.Mock()
        row = self.unlock(row.id)
        row.status = states.STARTING
        row.engine_ref = 'database-host'
        row.save()
        event = objects.Event(
            context=self.ctx, stack_id=self.stack.id, resource_type='app',
            resource_id=row.id, action='create', outcome='success')
        event.create()
        with db_api.writer.using(self.ctx):
            stored = event.repository._get(self.ctx, event.id)
            stored.created_at = timeutils.utcnow() - datetime.timedelta(
                seconds=10)
        row.name = 'metadata-updated'
        row.save()
        self.config.config(start_timeout=1, group='conductor')
        driver.get_app_status.return_value = driver_base.ObservedStatus(
            status=states.DEPLOYING)
        with mock.patch.object(endpoint, '_driver', return_value=driver), \
                mock.patch.object(endpoint, '_endpoint', return_value=None):
            deployments.settle_starting(endpoint, self.ctx, row.id)
        self.assertEqual(states.ERROR, objects.Service.get_by_id(
            self.ctx, row.id).status)

    def test_readiness_timeout_fault_uses_originating_action(self):
        owner = manager.Manager(self.conf)
        driver = mock.Mock()
        driver.get_app_status.return_value = driver_base.ObservedStatus(
            states.DEPLOYING)
        self.config.config(start_timeout=60, group='conductor')
        now = timeutils.utcnow()
        for action, verb in (('create', 'created'), ('start', 'started'),
                             ('restart', 'restarted')):
            with self.subTest(action=action):
                row = self.unlock(self.create().id)
                row.name = 'timeout-' + action
                row.status = states.STARTING
                row.engine_ref = 'database-host'
                row.save()
                # Historical actions must not determine the current fault.
                for previous in ('create', 'start', 'restart'):
                    event = objects.Event(
                        context=self.ctx, stack_id=self.stack.id,
                        resource_type='database', resource_id=row.id,
                        action=previous, outcome='start')
                    event.create()
                    with db_api.writer.using(self.ctx):
                        stored = event.repository._get(self.ctx, event.id)
                        stored.created_at = now - datetime.timedelta(hours=1)
                deployments.checkpoint_database_start(self.ctx, row, action)
                later = timeutils.utcnow() + datetime.timedelta(seconds=61)
                with mock.patch.object(
                        owner, '_driver', return_value=driver), \
                        mock.patch.object(owner, '_endpoint',
                                          return_value=None), \
                        mock.patch.object(timeutils, 'utcnow',
                                          return_value=later):
                    deployments.settle_starting(owner, self.ctx, row.id)
                current = objects.Service.get_by_id(self.ctx, row.id)
                self.assertEqual(states.ERROR, current.status)
                self.assertEqual(
                    'The database could not be %s. Retry, or contact support '
                    'with the database ID.' % verb, current.fault)

    def _action_start_checkpoint(self, action, status):
        row = self.unlock(self.create().id)
        row.status = status
        row.engine_ref = 'database-host'
        row.save()
        row.acquire_task(states.RECONCILING, owner=action + '_database')
        owner = manager.Manager(self.conf, notifier=mock.Mock())
        driver = mock.Mock()
        with mock.patch.object(owner, '_driver', return_value=driver), \
                mock.patch.object(owner, '_endpoint', return_value=None):
            owner.execute(self.ctx, action + '_service', row, {})
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.STARTING, current.status)
        events = objects.EventList.get_all(self.ctx, filters={
            'resource_id': row.id, 'resource_type': 'database',
            'action': action, 'outcome': 'start'})
        self.assertEqual(1, len(events))
        self.assertGreaterEqual(
            deployments._start_requested_at(self.ctx, current),
            events[0].created_at)

    def test_start_checkpoints_database_readiness(self):
        self._action_start_checkpoint('start', states.STOPPED)

    def test_restart_checkpoints_database_readiness(self):
        self._action_start_checkpoint('restart', states.ACTIVE)

    def _limit_restart_readiness(self, via_patch, healthy, settings=None):
        row = self.unlock(self.create().id)
        row.status = states.ACTIVE
        row.engine_ref = 'database-host'
        row.save()
        event = objects.Event(
            context=self.ctx, stack_id=self.stack.id, resource_type='database',
            resource_id=row.id, action='create', outcome='success')
        event.create()
        now = timeutils.utcnow()
        with db_api.writer.using(self.ctx):
            stored = event.repository._get(self.ctx, event.id)
            stored.created_at = now - datetime.timedelta(hours=1)
        self.config.config(start_timeout=60, stable_seconds=5,
                           group='conductor')
        owner = manager.Manager(self.conf, notifier=mock.Mock())
        driver = mock.Mock()
        public_update = bool(settings and 'public' in settings)
        driver.update_database.return_value = public_update
        driver.get_database_status.return_value = driver_base.ObservedStatus(
            states.ACTIVE)
        driver.get_app_status.return_value = driver_base.ObservedStatus(
            states.ACTIVE if healthy else states.DEPLOYING)
        deployments.RUNNING_SINCE[('start', row.id)] = -100
        with mock.patch.object(owner, '_driver', return_value=driver), \
                mock.patch.object(owner, '_endpoint', return_value=None):
            if via_patch:
                with mock.patch.object(handlers.Handler, 'rename_service'):
                    self.call('patch', '/v1/databases/' + row.id,
                              {'database': settings or {'cpu_limit': 2}})
                current = objects.Service.get_by_id(self.ctx, row.id)
                self.assertIsNotNone(current.settings_changed_at)
                owner.execute(self.ctx, 'rename_service', current, {})
                driver.update_database.assert_called_once()
                if settings and 'public' in settings:
                    applied = driver.update_database.call_args.args[-1]
                    self.assertTrue(applied.public_enabled)
                    self.assertEqual(16000, applied.public_port)
            else:
                row.cpu_limit = 2
                row.settings_changed_at = now
                row.save()
                self.assertFalse(owner.service_reconciler.run(row))
            current = objects.Service.get_by_id(self.ctx, row.id)
            if public_update:
                self.assertIsNone(current.settings_changed_at)
                driver.restart_app.assert_not_called()
            else:
                self.assertEqual(states.ACTIVE, current.status)
                self.assertIsNotNone(current.settings_changed_at)
                current.acquire_task(states.RECONCILING, owner='restart')
                owner.execute(self.ctx, 'restart_service', current, {})
                current = objects.Service.get_by_id(self.ctx, row.id)
            self.assertEqual(states.STARTING, current.status)
            checkpoint = timeutils.normalize_time(
                deployments._start_requested_at(self.ctx, current))
            self.assertGreaterEqual(checkpoint, now)
            self.assertNotIn(('start', row.id), deployments.RUNNING_SINCE)
            with mock.patch.object(deployments.time, 'monotonic') as monotonic:
                monotonic.return_value = 100
                with mock.patch.object(timeutils, 'utcnow', return_value=(
                        checkpoint + datetime.timedelta(seconds=59))):
                    deployments.settle_starting(owner, self.ctx, row.id)
                self.assertEqual(states.STARTING, objects.Service.get_by_id(
                    self.ctx, row.id).status)
                monotonic.return_value = 105
                with mock.patch.object(timeutils, 'utcnow', return_value=(
                        checkpoint + datetime.timedelta(seconds=61))):
                    deployments.settle_starting(owner, self.ctx, row.id)
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ACTIVE if healthy else states.ERROR,
                         current.status)
        if healthy:
            self.assertIsNone(current.fault)
        else:
            self.assertEqual(database_faults.public_fault(None, 'restart'),
                             current.fault)

    def test_patch_public_access_restarts_and_completes_readiness(self):
        self._limit_restart_readiness(
            via_patch=True, healthy=True,
            settings={'public': {'enabled': True, 'port': 16000}})

    def test_patch_limits_on_old_database_gets_fresh_healthy_window(self):
        self._limit_restart_readiness(via_patch=True, healthy=True)

    def test_patch_limits_on_old_database_gets_full_timeout(self):
        self._limit_restart_readiness(via_patch=True, healthy=False)

    def test_reconcile_limits_on_old_database_gets_fresh_healthy_window(self):
        self._limit_restart_readiness(via_patch=False, healthy=True)

    def test_reconcile_limits_on_old_database_gets_full_timeout(self):
        self._limit_restart_readiness(via_patch=False, healthy=False)

    def test_delete_wins_before_create_delivery(self):
        row = self.create()
        self.stack.engine_version = 'noop-1.0'
        self.stack.save()
        endpoint = manager.Manager(self.conf)
        with mock.patch.object(handlers.Handler, 'delete_service'):
            self.call('delete', '/v1/databases/' + row.id, status=202)
        current = objects.Service.get_by_id(self.ctx, row.id)
        endpoint.execute(self.ctx, 'create_service', row, {})
        self.assertEqual(states.DELETING, objects.Service.get_by_id(
            self.ctx, row.id).status)
        endpoint.execute(self.ctx, 'delete_service', current, {})
        self.call('get', '/v1/databases/' + row.id, status=404)

    def test_disabled_public_settings_are_retained_without_exposure(self):
        row = self.create(public={'enabled': False, 'port': 12345})
        result = self.call('get', '/v1/databases/' + row.id).json['database']
        self.assertEqual(12345, result['public']['port'])
        self.assertFalse(result['public']['enabled'])
        self.assertIsNone(result['public_url'])
        for port in (0, 65536, True):
            public = {'enabled': False, 'port': port}
            self.call('post', body=self.body(public=public), status=400)

    def test_database_reconciliation_observes_status_and_stability(self):
        row = self.unlock(self.create().id)
        row.status = states.ACTIVE
        row.engine_ref = 'database-host'
        row.save()
        owner = manager.Manager(self.conf, notifier=mock.Mock())
        driver = mock.Mock()
        driver.update_database.return_value = False
        self.config.config(stable_seconds=20, group='conductor')
        now = datetime.datetime(2026, 1, 1)
        with mock.patch.object(owner, '_driver', return_value=driver), \
                mock.patch.object(owner, '_endpoint', return_value=None), \
                mock.patch.object(reconcile.timeutils, 'utcnow') as clock:
            def observe(status, seconds):
                clock.return_value = now + datetime.timedelta(seconds=seconds)
                driver.get_database_status.return_value = (
                    driver_base.ObservedStatus(status))
                self.assertFalse(owner.service_reconciler.run(row))
                current = objects.Service.get_by_id(self.ctx, row.id)
                self.assertEqual(states.NONE, current.task_state)
                self.assertIsNone(current.task_owner)
                return current

            self.assertEqual(states.STOPPED, observe(states.STOPPED, 0).status)
            self.assertEqual(states.STOPPED, observe(states.ACTIVE, 1).status)
            self.assertEqual(states.STOPPED, observe(states.ACTIVE, 20).status)
            self.assertEqual(states.ACTIVE, observe(states.ACTIVE, 21).status)
            failed = observe(states.ERROR, 22)
            self.assertEqual(states.ERROR, failed.status)
            self.assertEqual(database_faults.public_fault(), failed.fault)
            self.assertNotIn('coolify', failed.fault.lower())
            self.assertNotIn('traefik', failed.fault.lower())
            self.assertEqual(states.ERROR, observe(states.ACTIVE, 23).status)
            self.assertEqual(
                states.ERROR, observe(states.DEPLOYING, 30).status)
            self.assertEqual(states.ERROR, observe(states.ACTIVE, 31).status)
            recovered = observe(states.ACTIVE, 51)
            self.assertEqual(states.ACTIVE, recovered.status)
            self.assertIsNone(recovered.fault)
        self.assertEqual(9, driver.update_database.call_count)
        driver.compare_settings.assert_not_called()
        driver.get_app_status.assert_not_called()

    def test_reconciliation_recovery_clears_stored_fault(self):
        owner = manager.Manager(self.conf, notifier=mock.Mock())
        driver = mock.Mock()
        driver.update_database.return_value = False
        self.config.config(stable_seconds=0, group='conductor')
        for status in (states.ACTIVE, states.STOPPED):
            with self.subTest(status=status):
                row = self.unlock(self.create().id)
                row.name = 'recover-' + status
                row.status = states.ERROR
                row.engine_ref = 'database-host'
                row.reconcile_fault = 'Runtime failure'
                row.fault = database_faults.UNREACHABLE
                row.save()
                driver.get_database_status.return_value = (
                    driver_base.ObservedStatus(status))
                with mock.patch.object(
                        owner, '_driver', return_value=driver), \
                        mock.patch.object(owner, '_endpoint',
                                          return_value=None):
                    self.assertFalse(owner.service_reconciler.run(row))
                current = objects.Service.get_by_id(self.ctx, row.id)
                self.assertEqual(status, current.status)
                self.assertIsNone(current.fault)
                self.assertIsNone(current.reconcile_fault)

    def test_reconciliation_restart_clears_stored_fault(self):
        row = self.unlock(self.create().id)
        row.status = states.ERROR
        row.engine_ref = 'database-host'
        row.fault = database_faults.UNREACHABLE
        row.settings_changed_at = timeutils.utcnow()
        row.save()
        owner = manager.Manager(self.conf, notifier=mock.Mock())
        driver = mock.Mock()
        driver.update_database.return_value = True
        driver.get_database_status.return_value = driver_base.ObservedStatus(
            states.STOPPED)
        with mock.patch.object(owner, '_driver', return_value=driver), \
                mock.patch.object(owner, '_endpoint', return_value=None):
            self.assertFalse(owner.service_reconciler.run(row))
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.STARTING, current.status)
        self.assertIsNone(current.fault)
        self.assertIsNone(current.settings_changed_at)
        self.assertEqual(states.NONE, current.task_state)
        driver.update_database.assert_called_once()

    def test_database_reconciliation_settings_and_failure_release(self):
        row = self.unlock(self.create().id)
        row.status = states.ACTIVE
        row.engine_ref = 'database-host'
        row.cpu_limit = 1.5
        row.memory_limit_mb = 512
        row.settings_changed_at = timeutils.utcnow()
        row.save()
        owner = manager.Manager(self.conf, notifier=mock.Mock())
        driver = mock.Mock()
        driver.get_database_status.return_value = driver_base.ObservedStatus(
            states.ACTIVE)

        def apply(endpoint, stack, database):
            current = objects.Service.get_by_id(self.ctx, database.id)
            self.assertEqual(states.RECONCILING, current.task_state)
            self.assertEqual(1.5, database.cpu_limit)
            self.assertEqual(512, database.memory_limit_mb)
            raise driver_base.EngineUnreachable('Unavailable')

        driver.update_database.side_effect = apply
        with mock.patch.object(owner, '_driver', return_value=driver), \
                mock.patch.object(owner, '_endpoint', return_value=None):
            self.assertTrue(owner.service_reconciler.run(row))
            current = objects.Service.get_by_id(self.ctx, row.id)
            self.assertEqual(states.NONE, current.task_state)
            self.assertIsNotNone(current.settings_changed_at)
            driver.update_database.side_effect = None
            driver.update_database.return_value = None
            self.assertFalse(owner.service_reconciler.run(row))
            current = objects.Service.get_by_id(self.ctx, row.id)
            self.assertIsNotNone(current.settings_changed_at)
            self.assertEqual(states.NONE, current.task_state)
            driver.update_database.return_value = None
            self.assertFalse(owner.service_reconciler.run(row))
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ACTIVE, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertIsNotNone(current.settings_changed_at)

    def test_actions_guards_validation_and_isolation(self):
        row = self.unlock(self.create().id)
        path = '/v1/databases/' + row.id + '/action'
        for action in ('start', 'stop', 'restart'):
            for status in (states.STOPPED, states.ACTIVE, states.ERROR):
                row = self.unlock(row.id)
                row.status = status
                row.save()
                valid = status == (states.STOPPED if action == 'start'
                                   else states.ACTIVE)
                with mock.patch.object(handlers.Handler,
                                       action + '_service') as dispatch:
                    self.call('post', path, {action: {}},
                              status=202 if valid else 409)
                self.assertEqual(int(valid), dispatch.call_count)
            self.call('post', path, {action: {}}, headers=dict(
                self.headers, **{'X-Roles': 'reader'}), status=403)
            self.call('post', path, {action: {}}, headers=dict(
                self.headers, **{'X-Project-Id': 'other'}), status=404)
        for body in ({}, {'unknown': {}}, {'start': {'x': 1}},
                     {'start': {}, 'stop': {}}):
            self.call('post', path, body, status=400)
        self.call('post', path + '?unknown=1', {'start': {}}, status=400)
        self.request('post', path, {'start': {}}, version='1.28', status=404)

    def test_logs_validation_and_policy(self):
        row = self.create()
        path = '/v1/databases/' + row.id + '/logs'
        with mock.patch.object(handlers.Handler, 'service_logs',
                               return_value={'lines': []}) as read:
            self.assertEqual({'lines': []}, self.call(
                'get', path + '?tail=42&since=2026-01-01T00:00:00Z'
            ).json['logs'])
            self.assertEqual(42, read.call_args.args[-1]['tail'])
        for query in ('tail=-1', 'tail=x', 'since=invalid', 'unknown=1'):
            self.call('get', path + '?' + query, status=400)
        self.call('get', path, headers=dict(
            self.headers, **{'X-Project-Id': 'other'}), status=404)
        self.call('get', path, headers=dict(
            self.headers, **{'X-Roles': 'unrelated'}), status=403)

    def test_limit_capacity_and_restart_required(self):
        row = self.unlock(self.create().id)
        row.status = states.ACTIVE
        row.save()
        path = '/v1/databases/' + row.id
        with mock.patch('coast.api.v1.flavor_controllers.server_flavor',
                        return_value={'vcpus': 2, 'ram': 512}):
            for limits in ({'cpu_limit': 3}, {'memory_limit_mb': 513}):
                self.call('patch', path, {'database': limits}, status=400)
            with mock.patch.object(handlers.Handler, 'rename_service'):
                result = self.call('patch', path, {'database': {
                    'cpu_limit': 1, 'memory_limit_mb': 256}})
        self.assertTrue(result.json['database']['restart_required'])
        owner = manager.Manager(self.conf)
        driver = mock.Mock()
        driver.update_database.return_value = False
        current = objects.Service.get_by_id(self.ctx, row.id)
        with mock.patch.object(owner, '_driver', return_value=driver), \
                mock.patch.object(owner, '_endpoint', return_value=None):
            owner.execute(self.ctx, 'rename_service', current, {})
            self.assertTrue(self.call('get', path).json[
                'database']['restart_required'])
            current = objects.Service.get_by_id(self.ctx, row.id)
            current.acquire_task(states.RECONCILING, owner='restart')
            owner.execute(self.ctx, 'restart_service', current, {})
        self.assertFalse(self.call('get', path).json[
            'database']['restart_required'])
        driver.restart_app.assert_called_once()

    def test_duplicate_create_has_database_message(self):
        self.create()
        with mock.patch.object(handlers.Handler, 'create_service') as dispatch:
            response = self.call('post', body=self.body(), status=409)
        self.assertIn(
            'A database or app named orders already exists in this stack. '
            'Choose a different name.', response.text)
        dispatch.assert_not_called()

    def test_duplicate_rename_has_database_message(self):
        self.create()
        row = self.unlock(self.create(name='inventory').id)
        row.status = states.ACTIVE
        row.save()
        with mock.patch.object(handlers.Handler, 'rename_service') as dispatch:
            response = self.call(
                'patch', '/v1/databases/' + row.id,
                {'database': {'name': 'orders'}}, status=409)
        self.assertIn(
            'A database or app named orders already exists in this stack. '
            'Choose a different name.', response.text)
        dispatch.assert_not_called()
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual('inventory', current.name)
        self.assertEqual(states.NONE, current.task_state)

    def test_busy_action_has_plain_message(self):
        row = self.create()
        response = self.call('post', '/v1/databases/' + row.id + '/action',
                             {'start': {}}, status=409)
        self.assertEqual(
            'Wait for the current operation on this database to finish, '
            'then try again.', response.json['faultstring'])
        self.assertNotIn('create_database', response.text)
        self.assertNotIn(states.PROVISIONING, response.text)

    def test_action_status_conflict_messages(self):
        row = self.unlock(self.create().id)
        path = '/v1/databases/' + row.id + '/action'
        cases = (
            ('stop', states.STARTING,
             'The database is starting. Try again when it is Active.'),
            ('restart', states.STARTING,
             'The database is starting. Try again when it is Active.'),
            ('start', states.ACTIVE, 'The database is already running.'),
            ('start', states.STARTING, 'The database is already running.'),
            ('start', states.ERROR,
             'Only a stopped database can be started.'),
            ('stop', states.STOPPED, 'The database is already stopped.'),
            ('restart', states.STOPPED, 'Start the database first.'),
            ('stop', states.ERROR,
             'Only an active database can be stopped or restarted.'))
        for action, status, message in cases:
            row.status = status
            row.save()
            response = self.call('post', path, {action: {}}, status=409)
            self.assertIn(message, response.text)
            current = objects.Service.get_by_id(self.ctx, row.id)
            self.assertEqual(status, current.status)
            self.assertEqual(states.NONE, current.task_state)

    def test_delete_while_task_running_has_plain_message(self):
        row = self.unlock(self.create().id)
        row.status = states.ACTIVE
        row.save()
        row.acquire_task(states.RECONCILING, owner='update_database')
        response = self.call('delete', '/v1/databases/' + row.id, status=409)
        self.assertIn(
            'Wait for the current operation on this database to finish, '
            'then try again.', response.text)
        self.assertNotIn('update_database', response.text)
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.ACTIVE, current.status)
        self.assertEqual(states.RECONCILING, current.task_state)

    def test_delete_already_deleting_has_plain_message(self):
        row = self.unlock(self.create().id)
        row.status = states.DELETING
        row.save()
        row.acquire_task(states.DELETING, owner='delete_database')
        with mock.patch.object(handlers.Handler, 'delete_service') as dispatch:
            response = self.call('delete', '/v1/databases/' + row.id,
                                 status=409)
        self.assertIn('This database is being deleted.', response.text)
        dispatch.assert_not_called()
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.DELETING, current.status)
        self.assertEqual(states.DELETING, current.task_state)
        self.assertEqual('delete_database', current.task_owner)

    def test_delete_failed_deploy_task_cas_has_plain_message(self):
        row = self.create()
        with mock.patch.object(db_api, 'cas_task_state',
                               side_effect=exception.Conflict()) as cas, \
                mock.patch.object(handlers.Handler,
                                  'delete_service') as dispatch:
            response = self.call('delete', '/v1/databases/' + row.id,
                                 status=409)
        self.assertIn(
            'Wait for the current operation on this database to finish, '
            'then try again.', response.text)
        cas.assert_called_once_with(
            mock.ANY, objects.Service.repository.model, row.id,
            new=states.DELETING, expected=states.PROVISIONING,
            owner='delete_database')
        dispatch.assert_not_called()
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(states.PENDING, current.status)
        self.assertEqual(states.PROVISIONING, current.task_state)
        self.assertEqual('create_database', current.task_owner)

    def test_stack_busy_has_plain_message(self):
        row = self.unlock(self.create().id)
        self.stack.acquire_task(states.UPGRADING)
        with mock.patch.object(handlers.Handler, 'stop_service') as dispatch:
            response = self.call(
                'post', '/v1/databases/' + row.id + '/action',
                {'stop': {}}, status=409)
        self.assertIn(
            'Wait for the current operation on this stack to finish, '
            'then try again.', response.text)
        dispatch.assert_not_called()
        current = objects.Service.get_by_id(self.ctx, row.id)
        self.assertEqual(row.status, current.status)
        self.assertEqual(states.NONE, current.task_state)
        self.assertIsNone(current.task_owner)
        self.assertEqual(states.UPGRADING, objects.Stack.get_by_id(
            self.ctx, self.stack.id).task_state)

    def test_create_parent_not_active_has_plain_message(self):
        for parent in (self.application, self.environment):
            parent.status = states.PENDING
            with mock.patch.object(type(parent), 'get_by_id',
                                   return_value=parent):
                response = self.call('post', body=self.body(), status=409)
            self.assertIn('Wait for the stack to finish setting up, '
                          'then try again.', response.text)
            parent.status = states.ACTIVE

    def test_patch_creation_and_deletion_conflict_messages(self):
        row = self.create()
        for status, message in (
                (states.PENDING, 'Wait for the database to finish being '
                 'created, then try again.'),
                (states.DELETING, 'This database is being deleted.')):
            row.status = status
            row.save()
            response = self.call('patch', '/v1/databases/' + row.id,
                                 {'database': {'description': 'updated'}},
                                 status=409)
            self.assertIn(message, response.text)

    def _database_failure_logs(self, starting):
        row = self.unlock(self.create().id)
        row.status = states.STARTING if starting else states.ACTIVE
        row.engine_ref = 'database-host'
        row.save()
        self.stack.driver = 'coolify_v4'
        self.stack.engine_version = engine_versions.DEFAULT
        self.stack.save()
        self.config.config(insecure_direct_url=True, group='coolify')
        self.config.config(start_timeout=1, group='conductor')
        password = self.store.get(self.ctx, row.password_ref)
        driver = v4.CoolifyV4Driver(self.conf)
        endpoint = driver_base.EngineEndpoint(
            'https://engine.example.com', '', '', '', 'test-token')
        owner = manager.Manager(self.conf)
        path = 'https://engine.example.com/api/v1/databases/database-host'
        self.addCleanup(runtime_logs.LAST.pop, row.id, None)
        self.assertIsNone(runtime_logs.latest(self.ctx, row.id))
        with requests_mock.Mocker() as http, \
                mock.patch.object(owner, '_driver', return_value=driver), \
                mock.patch.object(owner, '_endpoint', return_value=endpoint), \
                mock.patch.object(runtime_logs.time, 'sleep'), \
                mock.patch.object(timeutils, 'is_older_than',
                                  return_value=True):
            http.get(path, json=dict(driver._database_settings(row),
                                     status='restarting'))
            http.get(path + '/logs', [
                {'json': {'logs': ''}},
                {'json': {'logs': '2026-01-01T00:00:00Z failure ' + password}},
                {'json': {'logs': ''}}])
            if starting:
                deployments.settle_starting(owner, self.ctx, row.id)
                self.assertEqual(states.ERROR, objects.Service.get_by_id(
                    self.ctx, row.id).status)
            else:
                self.assertFalse(owner.service_reconciler.run(row))
            self.assertIsNotNone(runtime_logs.last(row.id))
            self.assertIsNone(runtime_logs.latest(self.ctx, row.id))

            def read(ctx, database, options):
                return owner.get_logs(ctx, 'service', database.id, options)

            with mock.patch.object(handlers.Handler, 'service_logs',
                                   side_effect=read):
                result = self.call('get', '/v1/databases/' + row.id + '/logs')
            lines = result.json['logs']['lines']
            self.assertEqual(1, len(lines))
            self.assertIn('failure', lines[0]['line'])
            self.assertNotIn(password, result.text)
            self.assertIn('captured_at', result.json['logs'])

    def test_reconcile_captures_database_logs_without_deployment(self):
        self._database_failure_logs(starting=False)

    def test_start_timeout_captures_database_logs_without_deployment(self):
        self._database_failure_logs(starting=True)

    def test_deferred_limits_preserve_restart_required(self):
        for action, status in (('start', states.STOPPED),
                               ('restart', states.ACTIVE)):
            row = self.unlock(self.create().id)
            row.status = status
            row.engine_ref = 'database-host'
            row.cpu_limit = 1.5
            row.settings_changed_at = timeutils.utcnow()
            row.save()
            checkpoint = row.settings_changed_at
            path = '/v1/databases/' + row.id
            with mock.patch.object(handlers.Handler, action + '_service'):
                self.call('post', path + '/action', {action: {}}, status=202)
            self.config.config(insecure_direct_url=True, group='coolify')
            driver = v4.CoolifyV4Driver(self.conf)
            endpoint = driver_base.EngineEndpoint(
                'https://engine.example.com', '', '', '', 'test-token')
            owner = manager.Manager(self.conf)
            current = objects.Service.get_by_id(self.ctx, row.id)
            with requests_mock.Mocker() as http, \
                    mock.patch.object(owner, '_driver', return_value=driver), \
                    mock.patch.object(owner, '_endpoint',
                                      return_value=endpoint):
                # The engine changed status after the API admitted the action.
                http.get('https://engine.example.com/api/v1/databases/' +
                         row.engine_ref, json={'status': 'starting'})
                http.post('https://engine.example.com/api/v1/databases/' +
                          row.engine_ref + '/' + action, json={})
                owner.execute(self.ctx, action + '_service', current, {})
                self.assertEqual(['GET', 'POST'], [
                    request.method for request in http.request_history])
            current = objects.Service.get_by_id(self.ctx, row.id)
            self.assertEqual(checkpoint, current.settings_changed_at)
            self.assertEqual(states.STARTING, current.status)
            self.assertEqual(states.NONE, current.task_state)
            self.assertTrue(self.call('get', path).json[
                'database']['restart_required'])
            # Once the engine settles, a later action applies the limits.
            current.status = status
            current.save()
            with mock.patch.object(handlers.Handler, action + '_service'):
                self.call('post', path + '/action', {action: {}}, status=202)
            current = objects.Service.get_by_id(self.ctx, row.id)
            with requests_mock.Mocker() as http, \
                    mock.patch.object(owner, '_driver', return_value=driver), \
                    mock.patch.object(owner, '_endpoint',
                                      return_value=endpoint):
                http.get('https://engine.example.com/api/v1/databases/' +
                         row.engine_ref, json=dict(
                             driver._database_settings(current),
                             status=('exited' if action == 'start'
                                     else 'running')))
                http.post('https://engine.example.com/api/v1/databases/' +
                          row.engine_ref + '/' + action, json={})
                owner.execute(self.ctx, action + '_service', current, {})
            current = objects.Service.get_by_id(self.ctx, row.id)
            self.assertIsNone(current.settings_changed_at)
            self.assertFalse(self.call('get', path).json[
                'database']['restart_required'])
            current.name = 'deferred-' + action
            current.save()
