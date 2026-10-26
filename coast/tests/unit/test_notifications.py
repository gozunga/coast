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

"""Lifecycle notification payloads, drivers, and API acceptance events."""

from unittest import mock

import fixtures
from oslo_messaging.notify import _impl_test as test_driver

from coast.common import context
from coast.common import notifications
from coast.common import states
from coast import objects
from coast.tests import app_api
from coast.tests import base


SECRET_VALUE = 'super-secret-value'
UPDATED_SECRET = 'super-secret-value-2'
TOKEN = 'token-must-stay-out'
SERVICE_TOKEN = 'service-token-must-stay-out'
BARBICAN_REF = 'barbican://secrets/engine-token'
VERIFY_TOKEN = 'domain-verification-token'


class Resource:
    def __init__(self):
        self.id = 'resource-id'
        self.project_id = 'project'
        self.status = states.ACTIVE
        self.stack_id = 'stack-id'
        self.service_id = 'service-id'
        self.server_id = 'server-id'
        self.verification_token = VERIFY_TOKEN
        self.value_ref = BARBICAN_REF
        self.engine_token_ref = BARBICAN_REF
        self.key = 'PASSWORD'
        self.value = SECRET_VALUE
        self.spec = {'env': {'PASSWORD': {'value_ref': BARBICAN_REF}}}


def captured():
    return list(test_driver.NOTIFICATIONS)


def blob(records=None):
    return str(records if records is not None else captured())


class NotificationTest(base.TestCase):

    def setUp(self):
        super().setUp()
        test_driver.reset()

    def context(self):
        return context.RequestContext(
            user_id='user', project_id='project', auth_token=TOKEN,
            service_token=SERVICE_TOKEN)

    def test_payload_allow_list_drops_secrets(self):
        ctx = self.context()
        payload = notifications.payload_for(Resource(), ctx)
        self.assertEqual({
            'resource_id': 'resource-id', 'project_id': 'project',
            'user_id': 'user', 'status': states.ACTIVE,
            'stack_id': 'stack-id', 'service_id': 'service-id',
            'server_id': 'server-id'}, payload)
        notifications.emit(
            ctx, notifications.API, 'environment', 'extend', 'start', {
                'resource_id': 'resource-id', 'project_id': 'project',
                'status': states.ACTIVE, 'verification_token': VERIFY_TOKEN,
                'value_ref': BARBICAN_REF, 'value': SECRET_VALUE,
                'auth_token': TOKEN, 'engine_token_ref': BARBICAN_REF,
                'env': {'PASSWORD': SECRET_VALUE}, 'key': 'PASSWORD'},
            self.conf)
        ctxt, message, priority, retry = captured()[-1]
        self.assertEqual('coast.environment.update.start',
                         message['event_type'])
        self.assertEqual(
            {'resource_id': 'resource-id', 'project_id': 'project',
             'status': states.ACTIVE}, message['payload'])
        self.assertEqual('INFO', priority)
        self.assertEqual(0, retry)
        self.assertTrue(message['publisher_id'].startswith('coast-api.'))
        text = blob()
        for secret in (SECRET_VALUE, TOKEN, SERVICE_TOKEN, BARBICAN_REF,
                       VERIFY_TOKEN, 'PASSWORD'):
            self.assertNotIn(secret, text)

    def test_noop_driver_drops_notifications(self):
        self.config.config(driver=['noop'],
                           group='oslo_messaging_notifications')
        notifications.reset(self.conf)
        notifications.emit(
            self.context(), notifications.CONDUCTOR, 'server', 'delete',
            'end', {'resource_id': 'server-id', 'value': SECRET_VALUE},
            self.conf)
        self.assertEqual([], captured())

    def test_delivery_failure_does_not_log_secrets(self):
        logs = self.useFixture(fixtures.FakeLogger())
        notifier = notifications.notifier_for(self.conf, notifications.API)
        with mock.patch.object(notifier, 'info',
                               side_effect=RuntimeError(TOKEN)):
            notifications.emit(
                self.context(), notifications.API, 'app', 'create', 'start',
                {'resource_id': 'service-id', 'value': SECRET_VALUE},
                self.conf)
        self.assertNotIn(TOKEN, logs.output)
        self.assertNotIn(SECRET_VALUE, logs.output)
        self.assertIn('coast.app.create.start', logs.output)


class ApiNotificationTest(app_api.AppAPITest):

    def setUp(self):
        super().setUp()
        test_driver.reset()
        self.headers = dict(self.headers, **{'X-Auth-Token': TOKEN})

    def types(self):
        return [message['event_type'] for unused, message, unused_priority,
                unused_retry in captured()]

    def test_service_domain_deployment_and_env_events_omit_secrets(self):
        created = self.request('post', '/v1/services', self.create_body(
            ports='8080',
            env={'PASSWORD': {'value': SECRET_VALUE, 'secret': True},
                 'PUBLIC': {'value': 'plain-value'}}),
            status=202).json['service']
        path = '/v1/services/' + created['id']
        self.unlock(created['id'])
        self.request('patch', path, {'service': {'name': 'renamed'}})
        self.request('patch', path + '/env', {'env': {
            'PASSWORD': {'value': UPDATED_SECRET, 'secret': True},
            'PUBLIC': None,
            'NEW_KEY': {'value': 'new-plain'}}})
        domain = self.request('post', path + '/domains', {'domain': {
            'fqdn': 'notify.example.com'}}, status=201).json['domain']
        stored = objects.Domain.get_by_id(self.ctx, domain['id'])
        self.request('delete', path + '/domains/' + domain['id'], status=202)
        self.unlock(created['id'])
        self.request('post', path + '/deployments', {'deployment': {
            'source_ref': 'release'}}, status=202)
        self.unlock(created['id'])
        self.request('delete', path, status=202)
        self.request('patch', '/v1/stacks/' + self.stack.id,
                     {'stack': {'description': 'renamed'}})
        kinds = self.types()
        for event_type in (
                'coast.app.create.start',
                'coast.app.update.start', 'coast.app.update.end',
                'coast.app.delete.start',
                'coast.env_var.create.start', 'coast.env_var.create.end',
                'coast.env_var.update.start', 'coast.env_var.update.end',
                'coast.env_var.delete.start', 'coast.env_var.delete.end',
                'coast.domain.create.start', 'coast.domain.delete.start',
                'coast.deployment.create.start',
                'coast.environment.update.start',
                'coast.environment.update.end'):
            self.assertIn(event_type, kinds)
        text = blob()
        self.assertNotIn(SECRET_VALUE, text)
        self.assertNotIn(UPDATED_SECRET, text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(stored.verification_token, text)
        self.assertNotIn('plain-value', text)
        for reference in self.store.values:
            self.assertNotIn(reference, text)
            self.assertNotIn(self.store.values[reference][1], text)
