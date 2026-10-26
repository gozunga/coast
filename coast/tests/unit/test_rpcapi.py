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

import datetime
from unittest import mock

from oslo_config import cfg
import oslo_messaging

from coast.api import errors
from coast.common import exception
from coast.conductor import rpcapi
from coast.drivers import base as driver_base
from coast.tests import base


class _Resource:
    def __init__(self, updated_at, created_at=None):
        self.id = 'resource-1'
        self.updated_at = updated_at
        self.created_at = created_at


class RevisionTest(base.TestCase):
    def test_same_instant_matches_across_tz(self):
        naive = _Resource(datetime.datetime(
            2026, 10, 13, 9, 37, 37, 100000))
        aware = _Resource(datetime.datetime(
            2026, 10, 13, 9, 37, 37, 100000,
            tzinfo=datetime.timezone.utc))
        offset = datetime.timezone(datetime.timedelta(hours=2))
        shifted = _Resource(datetime.datetime(
            2026, 10, 13, 11, 37, 37, 100000, tzinfo=offset))
        self.assertEqual('2026-10-13T09:37:37.100000',
                         rpcapi.revision(naive))
        self.assertEqual(rpcapi.revision(naive), rpcapi.revision(aware))
        self.assertEqual(rpcapi.revision(naive), rpcapi.revision(shifted))
        self.assertEqual(rpcapi.receipt_id('delete', naive),
                         rpcapi.receipt_id('delete', aware))

    def test_receipts_identify_claim_and_deduplicate_redelivery(self):
        resource = _Resource(datetime.datetime(2026, 10, 20))
        claim = {'resource': 'service', 'id': 'service-id',
                 'state': 'RECONCILING', 'owner': 'domain_change',
                 'started_at': '2026-10-20T00:00:00'}
        first = rpcapi.receipt_id('delete_domain', resource, task_claim=claim)
        reordered = dict(reversed(list(claim.items())))
        self.assertEqual(first, rpcapi.receipt_id(
            'delete_domain', resource, task_claim=reordered))
        claim['started_at'] = '2026-10-20T00:00:01'
        self.assertNotEqual(first, rpcapi.receipt_id(
            'delete_domain', resource, task_claim=claim))

    def test_different_microseconds_differ(self):
        first = _Resource(datetime.datetime(
            2026, 10, 13, 9, 37, 37, 100000))
        second = _Resource(datetime.datetime(
            2026, 10, 13, 9, 37, 37, tzinfo=datetime.timezone.utc))
        self.assertNotEqual(rpcapi.revision(first), rpcapi.revision(second))

    def test_different_seconds_differ(self):
        early = _Resource(datetime.datetime(
            2026, 10, 13, 9, 37, 37, 100000))
        late = _Resource(datetime.datetime(
            2026, 10, 13, 9, 37, 38, 100000,
            tzinfo=datetime.timezone.utc))
        self.assertNotEqual(rpcapi.revision(early), rpcapi.revision(late))


class SyncCallFaultTest(base.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cfg, 'CONF', self.conf)
        patch.start()
        self.addCleanup(patch.stop)

    def _api(self, error):
        api = rpcapi.ConductorAPI(transport=mock.Mock())
        client = mock.Mock()
        api._client = client
        client.prepare.return_value.call.side_effect = error
        return api

    def _assert_clean(self, error, secret='password=secret'):
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_remote_engine_unreachable_is_clean(self):
        secret = 'password=secret'
        api = self._api(oslo_messaging.RemoteError(
            'EngineUnreachable', secret, 'Traceback ' + secret))
        with self.assertRaises(driver_base.EngineUnreachable) as caught:
            api.get_logs(mock.sentinel.context, 'service', 'svc')
        self._assert_clean(caught.exception, secret)

    def test_remote_engine_auth_on_export_urls_is_clean(self):
        secret = 'token=secret'
        api = self._api(oslo_messaging.RemoteError(
            'coast.drivers.base.EngineAuthError', secret, secret))
        with self.assertRaises(driver_base.EngineAuthError) as caught:
            api.get_export_urls(mock.sentinel.context, 'export-1')
        self._assert_clean(caught.exception, secret)

    def test_local_engine_fault_drops_its_message(self):
        secret = 'password=secret'
        api = self._api(driver_base.EngineUnreachable(secret))
        with self.assertRaises(driver_base.EngineUnreachable) as caught:
            api.get_logs(mock.sentinel.context, 'service', 'svc')
        self._assert_clean(caught.exception, secret)
        self.assertEqual('', str(caught.exception))

    def test_other_remote_errors_stay_generic(self):
        secret = 'password=secret'
        api = self._api(oslo_messaging.RemoteError(
            'EngineNotFound', secret, 'Traceback ' + secret))
        with self.assertRaises(exception.CoastException) as caught:
            api.get_logs(mock.sentinel.context, 'service', 'svc')
        self.assertEqual('Conductor log request failed.',
                         str(caught.exception))
        self._assert_clean(caught.exception, secret)

    def test_storage_read_failure_is_service_unavailable(self):
        for error in (oslo_messaging.MessagingTimeout('password=secret'),
                      oslo_messaging.RemoteError(
                          'ValueError', 'password=secret', 'remote traceback'),
                      cfg.DuplicateOptError('heartbeat_in_pthread')):
            api = self._api(error)
            with self.assertRaises(errors.StorageUnavailable) as caught:
                api.list_volume_mounts(mock.sentinel.context, 'service-1')
            self._assert_clean(caught.exception)

    def test_cast_forwards_accepted_claim_without_other_options(self):
        api = self._api(None)
        resource = _Resource(datetime.datetime(2026, 10, 19))
        claim = {'resource': 'service', 'id': 'service-1',
                 'state': 'RECONCILING', 'owner': 'domain_change',
                 'started_at': '2026-10-19T00:00:00.123456'}
        api.cast(mock.sentinel.context, 'delete_domain', resource,
                 {'_task_claim': claim, 'password': 'secret'})
        sent = api._client.prepare.return_value.cast.call_args.kwargs
        self.assertEqual({'_task_claim': claim}, sent['options'])
        self.assertIsNot(claim, sent['options']['_task_claim'])

    def test_failover_cast_forwards_only_valid_admission_status(self):
        api = self._api(None)
        resource = _Resource(datetime.datetime(2026, 10, 19))
        for status in ('ACTIVE', 'UNHEALTHY', 'ERROR', 'FAILOVER', None):
            api.cast(mock.sentinel.context, 'failover_server', resource,
                     {'admission_status': status, 'fault': 'private fault',
                      'password': 'secret'})
            sent = api._client.prepare.return_value.cast.call_args.kwargs
            expected = ({'admission_status': status} if status in (
                'ACTIVE', 'UNHEALTHY', 'ERROR') else {})
            self.assertEqual(expected, sent['options'])

    def test_cast_failure_logs_cause_without_credentials(self):
        api = self._api(None)
        api._client.prepare.return_value.cast.side_effect = RuntimeError(
            'rabbit://user:secret@example.com password=secret')
        resource = _Resource(datetime.datetime(2026, 10, 19))
        with mock.patch.object(rpcapi.LOG, 'error') as log:
            with self.assertRaises(exception.CoastException) as caught:
                api.cast(mock.sentinel.context, 'create_stack', resource)
        self._assert_clean(caught.exception)
        self.assertEqual('Conductor handoff failed.', str(caught.exception))
        values = log.call_args.args[1]
        self.assertEqual('builtins.RuntimeError', values['cause'])
        self.assertEqual('create_stack', values['operation'])
        self.assertNotIn('secret', str(log.call_args))
