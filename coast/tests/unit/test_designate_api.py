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

"""Automatic DNS through WSGI, project policy, fake RPC and conductor."""

import datetime
import time
from unittest import mock

from keystoneauth1 import exceptions as ks_exceptions
from openstack import exceptions as sdk_exceptions
from oslo_db import exception as db_exception
from oslo_utils import timeutils

from coast.api import handlers
from coast.api import microversions
from coast.api.v1 import dns_controllers
from coast.common import states
from coast.conductor import dns as managed_dns
from coast.db import api as db_api
from coast.network import dns_credentials
from coast import objects
from coast.tests import conductor
from coast.tests import dns


class DesignateAPITest(conductor.ConductorTest):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = 'app-hosting 1.22'
        self.proxy = dns.Proxy()
        self.provider = dns.Provider(self.proxy)
        patcher = mock.patch.object(dns_credentials, 'get_provider',
                                    return_value=self.provider)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stack_path = self.stack()
        self.stack_id = self.stack_path.split('/')[-1]
        self.worker = next(s for s in objects.ServerList.get_all(self.ctx)
                           if s.role == 'worker')
        self.worker.floating_ip = '203.0.113.10'
        self.worker.save()

    def test_portless_pasted_compose_gets_customer_dns_and_routing(self):
        self.headers['OpenStack-API-Version'] = (
            'app-hosting ' + microversions.PASTED_SOURCE_VERSION)
        self.request('patch', self.stack_path, {'stack': {
            'default_domain': 'apps.example.com'}}, status=200)
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        driver = self.manager._driver(stack)
        body = dict(self.service_parents(self.stack_id), name='compose',
                    server_id=self.worker.id, deploy=False,
                    source={'type': 'compose', 'compose':
                            'services: {web: {image: nginx}}'})
        with mock.patch.object(driver, 'set_domains',
                               wraps=driver.set_domains) as route:
            view = self.request('post', '/v1/services', {'service': body},
                                status=202, complete=True).json['service']
        service = objects.Service.get_by_id(self.ctx, view['id'])
        self.assertFalse(service.ports)
        self.assertTrue(service.is_engine_service)
        hostname = 'compose-' + service.id[:4] + '.apps.example.com'
        self.assertEqual(hostname, service.default_domain)
        self.assertEqual(stack.dns_zone_id, service.default_dns_zone_id)
        self.assertEqual(stack.dns_trust_id, service.default_dns_trust_id)
        self.assertIn(hostname, view['default_url'])
        records = [r for r in self.proxy.items.values()
                   if r.name.rstrip('.') == hostname and r.type == 'A']
        self.assertEqual(1, len(records))
        self.assertEqual(stack.dns_zone_id, records[0].zone_id)
        self.assertEqual([self.worker.floating_ip], records[0].records)
        route.assert_called_once()
        self.assertEqual(service.id, route.call_args.args[2].id)
        self.assertEqual([view['default_url']], route.call_args.args[3])

    def test_conductor_domain_load_locks_service_before_domain(self):
        self.app()
        domain = self.domain()
        original = db_api._lock_query
        locks = []

        def record_lock(session, query, model):
            locks.append(model.__tablename__)
            return original(session, query, model)

        with db_api.writer.using(self.ctx):
            with mock.patch.object(db_api, '_lock_query',
                                   side_effect=record_lock):
                current = self.manager._load_current(
                    self.ctx, 'create_domain', domain, 'domain', 'create',
                    None, 'unused-receipt')
        self.assertEqual(domain.id, current.id)
        self.assertEqual(['services', 'domains'], locks)

    def test_domain_delete_deadlock_retries_whole_claim(self):
        self.app()
        domain = self.domain()
        original = db_api.domains._get
        attempts = []

        def deadlock_once(ctx, id, *args, **kwargs):
            if kwargs.get('lock'):
                attempts.append(id)
                if len(attempts) == 1:
                    # Service claim already succeeded; retry must undo it.
                    raise db_exception.DBDeadlock('sensitive-reference')
            return original(ctx, id, *args, **kwargs)

        with mock.patch.object(db_api.domains, '_get',
                               side_effect=deadlock_once):
            with mock.patch('oslo_db.api.time.sleep'), mock.patch.object(
                    handlers.Handler, 'delete_domain') as send:
                self.request('delete', self.domain_path + '/' + domain.id,
                             status=202)
        send.assert_called_once()
        self.assertEqual(2, len(attempts))

    def test_domain_delete_deadlock_exhaustion_returns_409(self):
        self.app()
        domain = self.domain()
        original = db_api.domains._get
        attempts = []

        def deadlock(ctx, id, *args, **kwargs):
            if kwargs.get('lock'):
                attempts.append(id)
                raise db_exception.DBDeadlock('sensitive-reference')
            return original(ctx, id, *args, **kwargs)

        with mock.patch.object(db_api.domains, '_get', side_effect=deadlock):
            with mock.patch('oslo_db.api.time.sleep'), mock.patch.object(
                    handlers.Handler, 'delete_domain') as send:
                response = self.request(
                    'delete', self.domain_path + '/' + domain.id, status=409)
        self.assertEqual(4, len(attempts))
        send.assert_not_called()
        self.assertNotIn('sensitive-reference', response.text)
        self.assertEqual(states.NONE, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.assertIsNotNone(objects.Domain.get_by_id(self.ctx, domain.id))

    def test_disabling_default_removes_only_its_records(self):
        self.request('patch', self.stack_path, {'stack': {
            'default_domain': 'apps.example.com'}}, status=200)
        self.app()
        domain = self.domain(record_type='CNAME')
        # Both kinds share a zone and ownership marker: cleanup must be named.
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        host = service.default_domain
        self.assertIsNotNone(host)
        service.default_domain_enabled = False
        service.save()
        managed_dns.ensure_default(
            self.manager, self.ctx, service, stack, None)
        managed_dns.ensure_default(
            self.manager, self.ctx, service, stack, None)
        managed_dns.ensure_domain(
            self.manager, self.ctx, service, stack, domain)
        records = self.proxy.items.values()
        self.assertFalse(any(r.name.rstrip('.') == host for r in records))
        self.assertTrue(any(r.name.rstrip('.') == domain.fqdn and r.type == 'A'
                            for r in records))
        self.assertTrue(any(r.name.rstrip('.') == '_coast-challenge.' +
                            domain.fqdn for r in records))
        service.default_domain_enabled = True
        service.save()
        managed_dns.ensure_default(
            self.manager, self.ctx, service, stack, host)
        self.assertTrue(any(r.name.rstrip('.') == host for r in records))
        self.assertTrue(managed_dns.ensure_domain(
            self.manager, self.ctx, service, stack, domain))
        self.assertTrue(any(r.name.rstrip('.') == domain.fqdn and
                            r.type == 'CNAME' for r in records))

    def test_type_replacement_recovers_after_observation_save_failure(self):
        self.app()
        domain = self.domain(record_type='CNAME')
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        original = self.manager._save

        def fail_observation(row, **values):
            if 'managed_records' in values:
                raise RuntimeError('observation save failed')
            return original(row, **values)

        service.default_domain_enabled = False
        service.save()
        with mock.patch.object(self.manager, '_save',
                               side_effect=fail_observation):
            self.assertFalse(managed_dns.ensure_domain(
                self.manager, self.ctx, service, stack, domain))
        # Simulate a fresh pass with durable observations still saying CNAME.
        domain = objects.Domain.get_by_id(self.ctx, domain.id)
        self.assertEqual('CNAME', domain.managed_records[-1]['type'])
        self.assertTrue(any(r.type == 'A' and r.name == domain.fqdn + '.'
                            for r in self.proxy.items.values()))
        service.default_domain_enabled = True
        service.save()
        for attempt in range(2):
            self.assertTrue(managed_dns.ensure_domain(
                self.manager, self.ctx, service, stack, domain))
        records = [r for r in self.proxy.items.values()
                   if r.name == domain.fqdn + '.']
        self.assertEqual(['CNAME'], [r.type for r in records])
        self.assertEqual('CNAME', objects.Domain.get_by_id(
            self.ctx, domain.id).managed_records[-1]['type'])

    def app(self, name='web'):
        data = dict(self.service_parents(self.stack_id), name=name,
                    server_id=self.worker.id, deploy=False, ports='80',
                    source=dict(type='git', repo_url='https://example.com/web',
                                build_pack='nixpacks', base_directory='.'))
        created = self.request('post', '/v1/services', {'service': data},
                               status=202, complete=True).json['service']
        self.service = objects.Service.get_by_id(self.ctx, created['id'])
        self.domain_path = '/v1/services/' + self.service.id + '/domains'
        return created

    def domain(self, **options):
        result = self.request('post', self.domain_path, {'domain': {
            'fqdn': 'shop.example.com', 'auto_dns': True, 'tls': 'none',
            **options}}, status=201, complete=True).json['domain']
        return objects.Domain.get_by_id(self.ctx, result['id'])

    def test_periodic_dns_network_calls_leave_service_available(self):
        self._periodic_dns_network_calls_leave_service_available(False)

    def test_skip_verification_dns_network_calls_leave_service_available(self):
        self._periodic_dns_network_calls_leave_service_available(True)

    def _periodic_dns_network_calls_leave_service_available(self, skip):
        self.config.config(skip_verification=skip, group='domains')
        self.request('patch', self.stack_path, {'stack': {
            'default_domain': 'apps.example.com'}}, status=200)
        self.app()
        domain = self.domain()
        ensure = managed_dns.customer_dns.CustomerDNS.ensure

        def slow_ensure(client, *args, **kwargs):
            time.sleep(0.01)
            self.assertEqual(states.NONE, objects.Service.get_by_id(
                self.ctx, self.service.id).task_state)
            return ensure(client, *args, **kwargs)

        with mock.patch.object(managed_dns.customer_dns.CustomerDNS, 'ensure',
                               autospec=True, side_effect=slow_ensure), \
                mock.patch('coast.conductor.manager.domain_check.txt_matches',
                           return_value=False):
            self.manager._drive_default_dns(self.service)
            self.manager._drive_domain(domain)
        self.request('delete', self.domain_path + '/' + domain.id, status=202,
                     complete=True)

    def test_unrelated_task_preserves_valid_dns_records(self):
        self.app()
        domain = self.domain()
        ensure = managed_dns.customer_dns.CustomerDNS.ensure

        def concurrent(client, *args, **kwargs):
            current = objects.Service.get_by_id(self.ctx, self.service.id)
            current.acquire_task(states.RECONCILING, owner='restart')
            try:
                return ensure(client, *args, **kwargs)
            finally:
                current.release_task(states.RECONCILING)

        with mock.patch.object(managed_dns.customer_dns.CustomerDNS, 'ensure',
                               autospec=True, side_effect=concurrent):
            self.manager._drive_domain(domain)
        self.assertEqual(set(managed_dns.names(domain.fqdn)), {
            r.name.rstrip('.') for r in self.proxy.items.values()})

    def test_domain_delete_finishes_before_delayed_dns_create(self):
        self._domain_delete_dns_race(False)

    def test_domain_delete_cleans_partial_delayed_dns_create(self):
        self._domain_delete_dns_race(True)

    def _domain_delete_dns_race(self, partial):
        self.app()
        domain = self.domain()
        sibling = self.domain(fqdn='keep.example.com')
        retained = {r.id for r in self.proxy.items.values() if
                    r.name.rstrip('.') in managed_dns.names(sibling.fqdn)}
        names = managed_dns.names(domain.fqdn)
        for key, record in list(self.proxy.items.items()):
            if record.name.rstrip('.') in names:
                del self.proxy.items[key]
        create = self.proxy.create_recordset
        deleted = False

        def delayed(zone, **attrs):
            nonlocal deleted
            if not deleted:
                deleted = True
                self.request('delete', self.domain_path + '/' + domain.id,
                             status=202, complete=True)
            record = create(zone, **attrs)
            if partial:
                raise RuntimeError('DNS response lost after create')
            return record

        with mock.patch.object(self.proxy, 'create_recordset',
                               side_effect=delayed):
            self.manager._drive_domain(domain)
        self.assertTrue(deleted)
        self.assertTrue(retained.issubset(self.proxy.items))
        self.assertFalse(any(r.name.rstrip('.') in names for r in
                             self.proxy.items.values()))

    def test_service_delete_finishes_before_delayed_default_dns_create(self):
        self._service_delete_dns_race(False)

    def test_service_delete_cleans_partial_delayed_default_dns_create(self):
        self._service_delete_dns_race(True)

    def test_operator_dns_cleanup_after_concurrent_service_delete(self):
        self.config.config(use_designate=True, designate_zone_id='zone',
                           group='dns')
        self.config.config(default_suffix='example.com', group='domains')
        with mock.patch.object(dns_credentials, 'ServiceProjectProvider',
                               return_value=self.provider), \
                mock.patch.object(self.proxy, 'get_zone', create=True,
                                  return_value=self.proxy.zone_items[0]):
            self._service_delete_dns_race(False, operator=True)

    def _service_delete_dns_race(self, partial, operator=False):
        if not operator:
            self.request('patch', self.stack_path, {'stack': {
                'default_domain': 'apps.example.com'}}, status=200)
        self.app()
        hostname = self.service.default_domain
        for key, record in list(self.proxy.items.items()):
            if record.name.rstrip('.') == hostname:
                del self.proxy.items[key]
        create = self.proxy.create_recordset
        deleted = False

        def delayed(zone, **attrs):
            nonlocal deleted
            if not deleted:
                deleted = True
                self.request('delete', '/v1/services/' + self.service.id,
                             status=202, complete=True)
            record = create(zone, **attrs)
            if partial:
                raise RuntimeError('DNS response lost after create')
            return record

        with mock.patch.object(self.proxy, 'create_recordset',
                               side_effect=delayed):
            if partial:
                self.assertRaises(
                    managed_dns.customer_dns.DNSFailure,
                    self.manager._drive_default_dns, self.service)
            else:
                self.manager._drive_default_dns(self.service)
        self.assertTrue(deleted)
        self.assertFalse(any(r.name.rstrip('.') == hostname for r in
                             self.proxy.items.values()))

    def test_other_domain_change_preserves_current_dns_link(self):
        self.app()
        domain = self.domain()
        create = self.proxy.create_recordset
        names = managed_dns.names(domain.fqdn)
        for key, record in list(self.proxy.items.items()):
            if record.name.rstrip('.') in names:
                del self.proxy.items[key]
        held = objects.Service.get_by_id(self.ctx, self.service.id)

        def delayed(zone, **attrs):
            if held.task_state == states.NONE:
                held.acquire_task(states.RECONCILING, owner='domain_change')
            return create(zone, **attrs)

        with mock.patch.object(self.proxy, 'create_recordset',
                               side_effect=delayed):
            self.manager._drive_domain(domain)
        self.assertTrue(any(r.name.rstrip('.') in names for r in
                            self.proxy.items.values()))
        self.assertEqual(states.RECONCILING, objects.Service.get_by_id(
            self.ctx, held.id).task_state)
        held.release_task(states.RECONCILING)

    def test_default_dns_compensates_during_stack_teardown(self):
        self.request('patch', self.stack_path, {'stack': {
            'default_domain': 'apps.example.com'}}, status=200)
        self.app()
        hostname = self.service.default_domain
        for key, record in list(self.proxy.items.items()):
            if record.name.rstrip('.') == hostname:
                del self.proxy.items[key]
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        create = self.proxy.create_recordset

        def delayed(zone, **attrs):
            stack.acquire_task(states.DELETING)
            stack.status = states.DELETING
            stack.save()
            managed_dns.delete_service(self.conf, self.ctx, self.service)
            return create(zone, **attrs)

        with mock.patch.object(self.proxy, 'create_recordset',
                               side_effect=delayed):
            self.manager._drive_default_dns(self.service)
        self.assertFalse(any(r.name.rstrip('.') == hostname for r in
                             self.proxy.items.values()))
        stack.release_task(states.DELETING)

    def test_zone_list_project_policy_and_microversion(self):
        result = self.request('get', '/v1/dns/zones').json['zones']
        self.assertEqual(['zone', 'deep'], [z['id'] for z in result])
        for changes, status in [({'X-Roles': 'unrelated'}, 403),
                                ({'OpenStack-API-Version':
                                  'app-hosting 1.21'}, 404)]:
            self.request('get', '/v1/dns/zones', status=status,
                         headers=dict(self.headers, **changes))
        self.request('get', '/v1/dns/zones', headers=dict(
            self.headers, **{'X-Roles': 'reader'}))
        result = self.request('get', '/v1/dns/zones', headers=dict(
            self.headers, **{'X-Project-Id': 'other'})).json['zones']
        self.assertEqual(['foreign'], [z['id'] for z in result])
        self.client.get('/v1/dns/zones', status=401, headers={
            'OpenStack-API-Version': 'app-hosting 1.22'})
        self.request('get', '/v1/dns/zones?all_projects=true', status=400)

    def test_zone_list_without_catalog_endpoint(self):
        self.config.config(group='dns', use_designate=False)
        provider = dns_credentials.ServiceAccountProvider(self.conf)
        for error in (ks_exceptions.EndpointNotFound,
                      sdk_exceptions.EndpointNotFound):
            with self.subTest(error=error), mock.patch.object(
                    dns_credentials, 'get_provider', return_value=provider), \
                    mock.patch.object(
                        dns_credentials.clients, 'get_connection',
                        side_effect=error('secret')) as get, \
                    mock.patch.object(
                        dns_credentials.clients, 'reset') as reset:
                result = self.request('get', '/v1/dns/zones', status=404)
            self.assertIn('DNS zones are not available on this cloud.',
                          result.text)
            self.assertNotIn('secret', result.text)
            get.assert_called_once_with('designate', conf=self.conf)
            reset.assert_not_called()

    def test_zone_list_designate_failure(self):
        unavailable = sdk_exceptions.HttpException('secret')
        unavailable.status_code = 503
        for error in (unavailable, RuntimeError('secret')):
            with self.subTest(error=error), mock.patch.object(
                    self.proxy, 'zones', side_effect=error):
                result = self.request('get', '/v1/dns/zones', status=409)
            self.assertIn('DNS service unavailable.', result.text)
            self.assertNotIn('secret', result.text)

    def test_unexpected_failures_log_class_and_request_id_only(self):
        self.app()
        calls = [
            ('get', '/v1/dns/zones', None, 409),
            ('patch', self.stack_path,
             {'stack': {'default_domain': 'example.com'}}, 400),
            ('post', self.domain_path,
             {'domain': {'fqdn': 'shop.example.com', 'auto_dns': True}},
             400)]
        for method, path, body, status in calls:
            with self.subTest(path=path), mock.patch.object(
                    self.proxy, 'zones',
                    side_effect=RuntimeError('secret TXT token')), \
                    mock.patch.object(dns_controllers.LOG, 'warning') as log:
                result = self.request(method, path, body, status=status)
            log.assert_called_once()
            args = log.call_args.args
            self.assertEqual('RuntimeError', args[1])
            self.assertTrue(args[2].startswith('req-'))
            self.assertNotIn('secret', str(log.call_args))
            self.assertNotIn('secret', result.text)

    def test_stack_default_deeper_domain_and_new_apps_only(self):
        initial = self.app()
        self.assertIn('sslip.io', initial['default_url'])
        self.request('patch', self.stack_path, {'stack': {
            'default_domain': 'app.sd.example.com'}})
        stack = objects.Stack.get_by_id(self.ctx, self.stack_id)
        self.assertEqual('deep', stack.dns_zone_id)
        second = self.app('shop')
        self.assertTrue(second['default_url'].endswith('.app.sd.example.com'))
        self.assertEqual('deep', self.service.default_dns_zone_id)
        managed_dns.ensure_default(self.manager, self.ctx, self.service,
                                   stack, self.service.default_domain)
        record = next(iter(self.proxy.items.values()))
        self.assertEqual([self.worker.floating_ip], record.records)
        self.request('patch', self.stack_path, {'stack': {
            'default_domain': None, 'dns_zone_id': None}})
        self.assertEqual('deep', objects.Service.get_by_id(
            self.ctx, self.service.id).default_dns_zone_id)
        self.assertEqual(initial['default_url'], 'https://' +
                         objects.Service.get_by_id(
                             self.ctx, initial['id']).default_domain)
        third = self.app('fallback')
        self.assertIn('sslip.io', third['default_url'])

    def test_auto_dns_verifies_activates_updates_and_domain_delete(self):
        self.app()
        domain = self.domain()
        self.assertEqual(2, len(self.proxy.items))
        self.assertEqual('zone', domain.zone_id)
        self.assertEqual(2, len(domain.managed_records))
        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        return_value=True), mock.patch(
                'coast.conductor.manager.domain_check.routes_to',
                return_value=True):
            self.manager.drive_domains()
        domain = objects.Domain.get_by_id(self.ctx, domain.id)
        self.assertEqual(states.ACTIVE, domain.status)
        self.worker.floating_ip = '203.0.113.99'
        self.worker.save()
        self.manager.drive_domains()
        route = next(r for r in self.proxy.items.values() if r.type == 'A')
        self.assertEqual(['203.0.113.99'], route.records)
        self.request('delete', self.domain_path + '/' + domain.id,
                     status=202, complete=True)
        self.assertEqual({}, self.proxy.items)

    def test_edit_host_zone_cleans_old_records_and_rotates_challenge(self):
        self.app()
        domain = self.domain()
        self.request('patch', self.domain_path + '/' + domain.id,
                     {'domain': {'host': 'new', 'zone_id': 'deep'}},
                     status=202, complete=True)
        current = objects.Domain.get_by_id(self.ctx, domain.id)
        self.assertEqual('new.sd.example.com', current.fqdn)
        self.assertNotEqual(domain.verification_token,
                            current.verification_token)
        self.assertIsNone(current.status_reason)
        self.assertEqual(2, len(self.proxy.items))
        self.assertEqual(
            {'deep'}, {r.zone_id for r in self.proxy.items.values()})
        self.assertIsNone(current.dns_previous)
        result = self.request('get', self.domain_path + '/' + domain.id).json[
            'domain']
        self.assertEqual('designate', result['dns_mode'])
        self.assertNotIn('dns_trust_id', result)
        self.request('get', self.domain_path + '/' + domain.id,
                     headers=dict(self.headers, **{'X-Project-Id': 'other'}),
                     status=404)

    def test_foreign_zone_apex_cname_and_old_schema_rejected(self):
        self.app()
        for values in (dict(host='shop', zone_id='foreign', auto_dns=True),
                       dict(fqdn='example.com', auto_dns=True,
                            record_type='CNAME'),
                       dict(host='shop', zone_id='zone'),
                       dict(auto_dns=True)):
            self.request('post', self.domain_path,
                         {'domain': values}, status=400)
        self.request('post', self.domain_path, {'domain': {
            'host': 'shop', 'zone_id': 'zone', 'auto_dns': True}}, status=400,
            headers=dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting 1.21'}))

    def test_conflicts_persist_safe_reason_and_retry(self):
        self.app()
        foreign = self.proxy.create_recordset(
            'zone', name='shop.example.com.', type='A',
            records=['203.0.113.8'],
            description='customer', ttl=300)
        domain = self.domain()
        self.assertIn('conflict', domain.status_reason)
        self.assertEqual(1, len(self.proxy.items))
        self.proxy.items.pop(foreign.id)
        self.manager.drive_domains()
        current = objects.Domain.get_by_id(self.ctx, domain.id)
        self.assertIsNone(current.status_reason)
        self.assertEqual(2, len(self.proxy.items))

    def test_manual_older_microversion_does_not_write(self):
        self.app()
        result = self.request('post', self.domain_path, {'domain': {
            'fqdn': 'shop.example.com', 'auto_dns': True}}, status=201,
            complete=True, headers=dict(self.headers, **{
                'OpenStack-API-Version': 'app-hosting 1.21'})).json['domain']
        self.assertNotIn('managed_records', result)
        self.assertEqual({}, self.proxy.items)

    def test_service_and_stack_cleanup_preserve_customer_records(self):
        self.app()
        self.domain()
        foreign = self.proxy.create_recordset(
            'zone', name='keep.example.com.', type='A',
            records=['203.0.113.8'],
            description='customer', ttl=300)
        self.request('delete', '/v1/services/' + self.service.id,
                     status=202, complete=True)
        self.assertEqual([foreign.id], list(self.proxy.items))
        self.app('another')
        self.domain()
        self.request('delete', self.stack_path, status=409)
        self.request('delete', '/v1/services/' + self.service.id,
                     status=202, complete=True)
        stack_id = self.stack_path.rsplit('/', 1)[1]
        for app in objects.AppgroupList.get_all(
                self.ctx, filters={'stack_id': stack_id}):
            for env in objects.EnvironmentList.get_all(
                    self.ctx, filters={'appgroup_id': app.id}):
                self.request('delete', '/v1/environments/' + env.id,
                             status=202, complete=True)
            self.request('delete', '/v1/appgroups/' + app.id,
                         status=202, complete=True)
        self.request('delete', self.stack_path, status=202, complete=True)
        self.assertEqual([foreign.id], list(self.proxy.items))

    def test_pending_dns_keeps_tls_gate(self):
        self.app()
        domain = self.domain(tls='letsencrypt')
        with mock.patch('coast.conductor.manager.domain_check.txt_matches',
                        return_value=True), mock.patch(
                'coast.conductor.manager.domain_check.routes_to',
                return_value=True), mock.patch(
                'coast.conductor.manager.domain_check.tls_ready',
                return_value=False):
            self.manager.drive_domains()
        self.assertEqual(states.PENDING_DNS, objects.Domain.get_by_id(
            self.ctx, domain.id).status)

    def test_cname_and_switch_to_manual_cleanup(self):
        self.app()
        domain = self.domain(record_type='CNAME')
        cname = next(r for r in self.proxy.items.values() if r.type == 'CNAME')
        self.assertEqual([self.service.default_domain + '.'], cname.records)
        path = (self.domain_path + '/' + domain.id +
                '?fields=zone_id,managed_records,dns_records')
        shown = self.request('get', path).json['domain']
        self.assertEqual(['TXT', 'CNAME'], [r['type']
                         for r in shown['dns_records']])
        self.request('patch', self.domain_path + '/' + domain.id,
                     {'domain': {'auto_dns': False}},
                     status=202, complete=True)
        current = objects.Domain.get_by_id(self.ctx, domain.id)
        self.assertEqual('manual', current.dns_mode)
        self.assertIsNone(current.zone_id)
        self.assertEqual({}, self.proxy.items)

    def test_edit_resets_timeout_and_reselects_longest_zone(self):
        self.app()
        domain = self.domain()
        domain.verification_started_at = (
            timeutils.utcnow() - datetime.timedelta(days=2))
        domain.save()
        self.request('patch', self.domain_path + '/' + domain.id,
                     {'domain': {'fqdn': 'new.sd.example.com'}},
                     status=202, complete=True)
        current = objects.Domain.get_by_id(self.ctx, domain.id)
        self.assertEqual('deep', current.zone_id)
        self.manager.drive_domains()
        self.assertEqual(states.PENDING_VERIFICATION,
                         objects.Domain.get_by_id(self.ctx, domain.id).status)

    def test_stopped_default_dns_is_reconciled_and_cleanup_retries(self):
        self.request('patch', self.stack_path, {'stack': {
            'default_domain': 'app.sd.example.com'}})
        self.app()
        self.worker.floating_ip = '203.0.113.55'
        self.worker.save()
        self.manager.drive_domains()
        route = next(iter(self.proxy.items.values()))
        self.assertEqual(['203.0.113.55'], route.records)
        with mock.patch.object(self.proxy, 'delete_recordset',
                               side_effect=RuntimeError('secret')):
            self.assertRaisesRegex(
                Exception, 'DNS service unavailable',
                managed_dns.Cleanup(self.conf).delete,
                self.ctx.project_id, self.stack_id, self.service.id)
        self.assertEqual(self.service.id, objects.Service.get_by_id(
            self.ctx, self.service.id).id)
        managed_dns.Cleanup(self.conf).delete(
            self.ctx.project_id, self.stack_id, self.service.id)
        self.assertEqual({}, self.proxy.items)

    def test_tls_edit_keeps_dns_verification_and_records(self):
        self.app()
        domain = self.domain()
        before = {key: vars(value).copy()
                  for key, value in self.proxy.items.items()}
        self.request('patch', self.domain_path + '/' + domain.id,
                     {'domain': {'tls': 'letsencrypt'}},
                     status=202, complete=True)
        current = objects.Domain.get_by_id(self.ctx, domain.id)
        self.assertEqual(domain.verification_token, current.verification_token)
        self.assertEqual(domain.status, current.status)
        self.assertIsNone(current.dns_previous)
        self.assertEqual(before, {key: vars(value).copy()
                                  for key, value in self.proxy.items.items()})

    def test_service_cleanup_paginates_domains(self):
        self.app()
        self.domain()
        self.domain(fqdn='second.example.com')
        self.config.config(max_limit=1, group='api')
        managed_dns.delete_service(self.conf, self.ctx, self.service)
        self.assertEqual({}, self.proxy.items)
