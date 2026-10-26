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

import json
from types import SimpleNamespace

import fixtures
from keystoneauth1.exceptions import http as ks_http
from openstack import exceptions as sdk_exceptions
import requests

from coast.common import exception
from coast.common import safe_log
from coast.conductor import stack_create
from coast.conductor import volume_delete
from coast.tests import base


SECRET = 'password=secret'
SDK_URL = 'https://cloud.example.com:8774/v2.1/servers?token=x'
SDK_SAFE = 'https://cloud.example.com:8774/v2.1/servers'


def _response(url, method='POST', request_id='req-conflict', status=409):
    response = requests.Response()
    response.status_code = status
    response.headers['x-openstack-request-id'] = request_id
    response.request = requests.Request(method, url).prepare()
    return response


def _raise_conflict():
    raise sdk_exceptions.ConflictException(
        message=SECRET, response=_response(SDK_URL), details=SECRET)


def _raise_keystone():
    raise ks_http.Conflict(
        message=SECRET, details='token=body',
        url='https://user:password=secret@cloud.example.com/v3/auth/tokens'
            '?token=x#fragment-secret',
        method='POST', request_id='req-ks', http_status=409)


def _raise_plain():
    password = SECRET
    raise Exception(password)


def _decode_secret():
    token = SECRET
    json.loads(token)


class SafeLogTest(base.TestCase):

    def test_sdk_conflict_drops_query_and_message(self):
        try:
            _raise_conflict()
        except sdk_exceptions.ConflictException as exc:
            cause, where = safe_log.describe(exc)
            rendered = str(exc)
        self.assertEqual(
            'openstack.exceptions.ConflictException status_code=409 '
            'method=POST url=%s request_id=req-conflict' % SDK_SAFE, cause)
        self.assertNotIn('token', cause)
        self.assertNotIn('?', cause)
        self.assertNotIn(SECRET, cause)
        self.assertNotIn(SECRET, where)
        self.assertIn('token=x', rendered)
        self.assertRegex(
            where, r'^coast/tests/unit/test_safe_log.py:\d+ _raise_conflict$')

    def test_keystoneauth_http_error_drops_userinfo_query_and_body(self):
        try:
            _raise_keystone()
        except ks_http.Conflict as exc:
            cause, where = safe_log.describe(exc)
        self.assertEqual(
            'keystoneauth1.exceptions.http.Conflict status_code=409 '
            'method=POST url=https://cloud.example.com/v3/auth/tokens '
            'request_id=req-ks', cause)
        for leaked in (SECRET, 'token=x', 'token=body', 'fragment-secret',
                       'user:'):
            self.assertNotIn(leaked, cause)
            self.assertNotIn(leaked, where)

    def test_unsafe_request_id_is_omitted(self):
        error = ks_http.Conflict(
            message=SECRET, url=SDK_URL, method='POST password',
            request_id=SECRET, http_status=409)
        cause, where = safe_log.describe(error)
        self.assertEqual(
            'keystoneauth1.exceptions.http.Conflict status_code=409 '
            'url=%s' % SDK_SAFE, cause)
        self.assertNotIn(SECRET, cause)
        self.assertNotIn('POST password', cause)
        self.assertEqual('-', where)

    def test_generic_exception_is_class_name_only(self):
        try:
            _raise_plain()
        except Exception as exc:
            cause, where = safe_log.describe(exc)
        self.assertEqual('builtins.Exception', cause)
        self.assertNotIn(SECRET, cause)
        self.assertNotIn(SECRET, where)
        self.assertRegex(
            where, r'^coast/tests/unit/test_safe_log.py:\d+ _raise_plain$')

    def test_library_frame_stays_at_coast_caller(self):
        try:
            _decode_secret()
        except Exception as exc:
            cause, where = safe_log.describe(exc)
        self.assertEqual('json.decoder.JSONDecodeError', cause)
        self.assertNotIn(SECRET, cause)
        self.assertNotIn(SECRET, where)
        self.assertNotIn('Expecting', cause)
        self.assertNotIn('decoder.py', where)
        self.assertRegex(
            where, r'^coast/tests/unit/test_safe_log.py:\d+ _decode_secret$')

    def test_missing_traceback_has_no_location(self):
        cause, where = safe_log.describe(Exception(SECRET))
        self.assertEqual('builtins.Exception', cause)
        self.assertEqual('-', where)
        self.assertNotIn(SECRET, cause)


class HttpFaultTest(base.TestCase):

    def test_keeps_message_and_drops_path_ids(self):
        payload = {
            'message': 'A server with this IP/Domain is already in use.',
            'errors': {
                'ip': ['The ip field is required.'],
                'private_key': ['-----BEGIN OPENSSH PRIVATE KEY-----\nsecret'],
                'token=engine-secret': ['skipped'],
            },
            'private_key': '-----BEGIN OPENSSH PRIVATE KEY----- secret',
            'docs': 'https://user:engine-secret@example.com/secret',
        }
        fields = safe_log.http_fault(
            'POST', '/servers/server-uuid/validate?token=engine-secret',
            400, payload)
        # A query makes the whole path unsafe.
        self.assertEqual('-', fields['path'])
        fields = safe_log.http_fault(
            'post', '/servers/server-uuid/validate', 400, payload)
        self.assertEqual('POST', fields['method'])
        self.assertEqual('/servers/{id}/validate', fields['path'])
        self.assertEqual('400', fields['status'])
        self.assertEqual(
            'A server with this IP/Domain is already in use.',
            fields['message'])
        self.assertEqual('ip,private_key', fields['error_keys'])
        self.assertEqual('The ip field is required.', fields['detail'])
        rendered = ' '.join(fields.values())
        for leaked in ('server-uuid', 'secret', 'BEGIN', 'engine-secret',
                       'user:'):
            self.assertNotIn(leaked, rendered)

    def test_drops_secret_message_and_non_object_body(self):
        secret = 'password=response-secret token=engine-secret'
        fields = safe_log.http_fault(
            'POST', '/security/keys', 400, {'message': secret})
        self.assertEqual('/security/keys', fields['path'])
        self.assertEqual('-', fields['message'])
        self.assertNotIn('secret', ' '.join(fields.values()))
        fields = safe_log.http_fault(
            'GET', '/appgroups/app-uuid/logs', 401,
            {'message': 'engine-secret'})
        self.assertEqual('/appgroups/{id}/logs', fields['path'])
        self.assertEqual('-', fields['message'])
        self.assertNotIn('engine-secret', ' '.join(fields.values()))
        fields = safe_log.http_fault(
            'POST', '/security/keys', 422,
            {'message': 'Invalid private key.'})
        self.assertEqual('Invalid private key.', fields['message'])
        fields = safe_log.http_fault(
            'POST', '/servers', 400,
            {'message': 'Invalid request.', 'error': 'Empty JSON.'})
        self.assertEqual('Invalid request.', fields['message'])
        self.assertEqual('Empty JSON.', fields['detail'])
        fields = safe_log.http_fault('POST', '/servers', 400, secret)
        self.assertEqual('-', fields['message'])
        self.assertNotIn('secret', ' '.join(fields.values()))
        fields = safe_log.http_fault(
            'NO SPACE', '/servers/../secret', '400', None)
        self.assertEqual('-', fields['method'])
        self.assertEqual('-', fields['path'])
        self.assertEqual('-', fields['status'])


class StepLogTest(base.TestCase):

    def test_stack_step_logs_http_cause_and_hides_secret(self):
        class Flow:
            def network(self):
                _raise_conflict()

        flow = Flow()
        flow.stack = SimpleNamespace(id='stack-id')
        flow.ctx = SimpleNamespace(request_id='req-step')
        logs = self.useFixture(fixtures.FakeLogger())
        step = stack_create.Step(flow, 'network')
        error = self.assertRaises(exception.CoastException, step.execute)
        self.assertEqual(stack_create.FAULT, str(error))
        self.assertIsNone(error.__cause__)
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, logs.output)
        self.assertNotIn('token=x', logs.output)
        self.assertIn('openstack.exceptions.ConflictException', logs.output)
        self.assertIn('status_code=409', logs.output)
        self.assertIn(SDK_SAFE, logs.output)
        self.assertIn('stack-id', logs.output)
        self.assertIn('network', logs.output)
        self.assertIn('req-step', logs.output)

    def test_server_step_names_the_server(self):
        class Flow:
            failure_kind = 'server'

            def boot(self):
                raise Exception(SECRET)

        flow = Flow()
        flow.stack = SimpleNamespace(id='stack-id')
        flow.server = SimpleNamespace(id='server-id')
        flow.ctx = SimpleNamespace(request_id='req-server')
        logs = self.useFixture(fixtures.FakeLogger())
        self.assertRaises(
            exception.CoastException, stack_create.Step(flow, 'boot').execute)
        self.assertNotIn(SECRET, logs.output)
        self.assertIn('Server server-id stack stack-id step boot failed',
                      logs.output)
        self.assertIn('builtins.Exception', logs.output)
        self.assertIn('req-server', logs.output)

    def test_volume_step_logs_class_only(self):
        class Flow:
            def verify(self):
                raise Exception(SECRET)

        flow = Flow()
        flow.volume = SimpleNamespace(id='volume-id')
        flow.stack = SimpleNamespace(id='stack-id')
        flow.ctx = SimpleNamespace(request_id='req-volume')
        logs = self.useFixture(fixtures.FakeLogger())
        error = self.assertRaises(
            exception.CoastException,
            volume_delete.Step(flow, 'verify').execute)
        self.assertEqual(volume_delete.FAULT, str(error))
        self.assertIsNone(error.__cause__)
        self.assertNotIn(SECRET, logs.output)
        self.assertIn(
            'Volume volume-id stack stack-id step verify failed', logs.output)
        self.assertIn('builtins.Exception', logs.output)
        self.assertIn('req-volume', logs.output)
