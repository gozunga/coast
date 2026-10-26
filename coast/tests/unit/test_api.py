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

from pathlib import Path
from unittest import mock

import fixtures
from keystonemiddleware import auth_token
from oslo_config import cfg
import oslo_messaging
from oslo_middleware import cors
from oslo_middleware import http_proxy_to_wsgi
import pecan
from webob import exc
import webtest

from coast.api import app
from coast.api import errors
from coast.api import microversions
from coast.api import wsgi
from coast.cmd import api as api_cmd
from coast.common import context
from coast.common import exception
from coast.common import policy
from coast.drivers import base as driver_base
from coast.tests import base


PASTE_FILE = str(Path('etc/coast/api-paste.ini').resolve())


class ProbeController:
    @pecan.expose('json')
    def index(self):
        request = pecan.request
        ctx = request.context['coast_context']
        return {'project_id': ctx.project_id, 'user_id': ctx.user_id,
                'roles': ctx.roles, 'request_id': ctx.request_id,
                'global_request_id': ctx.global_request_id,
                'is_admin': ctx.is_admin, 'version': request.version,
                'policy_allowed': request.context['policy'].authorize(
                    ctx, 'coast:plan:get_all', {})}


class RaisingController:
    def __init__(self, error):
        self.error = error

    @pecan.expose('json')
    def index(self):
        raise self.error


class APITest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.patch = mock.patch.object(cfg, 'CONF', self.conf)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(policy.reset)
        policy.reset()
        self.config.config(auth_strategy='noauth',
                           api_paste_config=PASTE_FILE, group='api')
        self.client = webtest.TestApp(app.load_app())

    def assert_fault(self, response, code, message=None):
        self.assertEqual(code, response.status_int)
        self.assertEqual('application/json', response.content_type)
        self.assertEqual({'faultstring', 'faultcode', 'debuginfo'},
                         set(response.json))
        self.assertIsNone(response.json['debuginfo'])
        self.assertEqual('Server' if code >= 500 else 'Client',
                         response.json['faultcode'])
        if message is not None:
            self.assertEqual(message, response.json['faultstring'])

    def assert_version(self, response, version='1.0'):
        self.assertEqual('app-hosting ' + version, response.headers[
            microversions.HEADER])
        self.assertIn(microversions.HEADER, response.headers['Vary'])
        self.assertRegex(response.headers['X-OpenStack-Request-Id'],
                         r'^req-[0-9a-f-]{36}$')

    def test_discovery(self):
        response = self.client.get('/', headers={'Host': 'api.example.com'})
        self.assertEqual({'versions': [{
            'id': 'v1', 'status': 'CURRENT', 'min_version': '1.0',
            'max_version': microversions.MAX_VERSION, 'links': [{
                'rel': 'self', 'href': 'http://api.example.com/v1/'}]}]},
            response.json)
        self.assert_version(response)

    def test_v1_discovery(self):
        self.config.config(auth_strategy='keystone', group='api')
        client = webtest.TestApp(app.load_app())
        for endpoint in (None, 'https://api.example.com/coast/'):
            self.config.config(public_endpoint=endpoint, group='api')
            headers = {'Host': 'api.example.com',
                       microversions.HEADER: 'app-hosting latest'}
            version = client.get('/', headers=headers).json['versions'][0]
            for path in ('/v1', '/v1/'):
                response = client.get(path, headers=headers, status=200)
                self.assertEqual({'version': version}, response.json)
                self.assert_version(response, microversions.MAX_VERSION)
                response = client.get(path, headers={
                    microversions.HEADER: 'app-hosting 1.99'}, status=406)
                self.assert_version(response)

    def test_public_endpoint(self):
        self.config.config(public_endpoint='https://api.example.com/coast/',
                           group='api')
        response = self.client.get('/')
        self.assertEqual('https://api.example.com/coast/v1/',
                         response.json['versions'][0]['links'][0]['href'])

    def test_proxy_endpoint(self):
        self.conf.register_opts(http_proxy_to_wsgi.OPTS,
                                group='oslo_middleware')
        self.config.config(enable_proxy_headers_parsing=True,
                           group='oslo_middleware')
        response = self.client.get('/', headers={
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'api.example.com',
            'X-Forwarded-Prefix': '/coast'})
        self.assertEqual('https://api.example.com/coast/v1/',
                         response.json['versions'][0]['links'][0]['href'])

    def test_supported_versions(self):
        expected = {'app-hosting 1.0': '1.0', 'app-hosting 1.1': '1.1',
                    'app-hosting 1.2': '1.2', 'app-hosting 1.3': '1.3',
                    'app-hosting 1.4': '1.4',
                    'app-hosting 1.5': '1.5',
                    'app-hosting 1.6': '1.6',
                    'app-hosting 1.7': '1.7',
                    'app-hosting 1.8': '1.8',
                    'app-hosting 1.9': '1.9',
                    'app-hosting 1.10': '1.10',
                    'app-hosting 1.11': '1.11',
                    'app-hosting 1.12': '1.12',
                    'app-hosting 1.13': '1.13',
                    'app-hosting 1.14': '1.14',
                    'app-hosting 1.15': '1.15',
                    'app-hosting 1.16': '1.16',
                    'app-hosting 1.17': '1.17',
                    'app-hosting 1.18': '1.18',
                    'app-hosting latest': microversions.MAX_VERSION}
        for version, selected in expected.items():
            response = self.client.get('/', headers={
                microversions.HEADER: version})
            self.assert_version(response, selected)

    def test_unsupported_versions(self):
        for version in ('0.9', '1.99', '2.0', '999999999999999999999.0'):
            response = self.client.get('/', headers={
                microversions.HEADER: 'app-hosting ' + version}, status=406)
            self.assert_fault(response, 406)
            self.assert_version(response)

    def test_malformed_versions(self):
        for value in ('', '1.0', 'compute 1.0', 'app-hosting',
                      'app-hosting 1', 'app-hosting 1.a',
                      'app-hosting -1.0', 'app-hosting 1.0 trailing'):
            response = self.client.get('/', headers={
                microversions.HEADER: value}, status=400)
            self.assert_fault(response, 400)
            self.assert_version(response)

    def test_unknown_paths(self):
        for path in ('/unknown', '/v1/not-implemented',
                     '/healthcheck/unknown'):
            response = self.client.get(path, status=404)
            self.assert_fault(response, 404)
            self.assert_version(response)

    def test_context(self):
        probe = webtest.TestApp(microversions.MicroversionMiddleware(
            errors.ErrorMiddleware(app.setup_app(ProbeController()))))
        request_id = 'req-11111111-1111-1111-1111-111111111111'
        global_id = 'req-22222222-2222-2222-2222-222222222222'
        response = probe.get('/', headers={
            'X-User-Id': 'user-a', 'X-Project-Id': 'project-a',
            'X-Roles': 'reader, member',
            microversions.HEADER: 'app-hosting latest'}, extra_environ={
                'openstack.request_id': request_id,
                'openstack.global_request_id': global_id})
        self.assertEqual({'user_id': 'user-a', 'project_id': 'project-a',
                          'roles': ['reader', 'member'],
                          'request_id': request_id,
                          'global_request_id': global_id,
                          'is_admin': False,
                          'version': microversions.MAX_VERSION,
                          'policy_allowed': True}, response.json)

    def test_context_without_identity(self):
        seen = []
        original = context.RequestContext.from_environ

        def capture(environ, **kwargs):
            ctx = original(environ, **kwargs)
            seen.append(ctx)
            return ctx

        with mock.patch.object(context.RequestContext, 'from_environ',
                               side_effect=capture):
            response = self.client.get('/')
        self.assertIsNone(seen[0].project_id)
        self.assertEqual([], seen[0].roles)
        self.assertFalse(seen[0].is_admin)
        self.assertEqual(response.headers['X-OpenStack-Request-Id'],
                         seen[0].request_id)

    def test_healthcheck(self):
        response = self.client.get('/healthcheck', headers={
            microversions.HEADER: 'malformed'})
        self.assertEqual(200, response.status_int)
        self.assertIn('X-OpenStack-Request-Id', response.headers)
        self.assertNotIn(microversions.HEADER, response.headers)

    def test_exception_mapping_and_sanitization(self):
        cases = [(exception.NotFound(), 404),
                 (exception.NotAuthorized(), 403),
                 (exception.OverQuota(), 403),
                 (exception.Conflict(), 409), (exception.Invalid(), 400),
                 (exception.CoastException('private-password'), 500),
                 (RuntimeError('private-password'), 500),
                 (exc.HTTPUnauthorized('private-password'), 401)]
        for error, code in cases:
            client = webtest.TestApp(microversions.MicroversionMiddleware(
                errors.ErrorMiddleware(app.setup_app(
                    RaisingController(error)))))
            response = client.get('/', status=code)
            self.assert_fault(response, code)
            self.assertNotIn('private-password', response.text)
            self.assertEqual('app-hosting 1.0',
                             response.headers[microversions.HEADER])

    def test_engine_faults_return_503_without_remote_text(self):
        secret = 'password=secret'
        remote_cls = type('EngineUnreachable_Remote',
                          (driver_base.EngineUnreachable,), {})
        auth_remote = type('EngineAuthError_Remote',
                           (driver_base.EngineAuthError,), {})
        cases = (
            (driver_base.EngineUnreachable(secret),
             'coast.drivers.base.EngineUnreachable'),
            (driver_base.EngineAuthError(secret),
             'coast.drivers.base.EngineAuthError'),
            (remote_cls(secret), 'coast.drivers.base.EngineUnreachable'),
            (auth_remote(secret), 'coast.drivers.base.EngineAuthError'),
            (oslo_messaging.RemoteError(
                'EngineUnreachable', secret, 'Traceback ' + secret),
             'coast.drivers.base.EngineUnreachable'),
            (oslo_messaging.RemoteError(
                'coast.drivers.base.EngineAuthError_Remote', secret, secret),
             'coast.drivers.base.EngineAuthError'))
        for error, cause in cases:
            client = webtest.TestApp(errors.ErrorMiddleware(app.setup_app(
                RaisingController(error))))
            logs = self.useFixture(fixtures.FakeLogger())
            response = client.get('/', status=503)
            self.assert_fault(response, 503, 'Service Unavailable')
            self.assertNotIn(secret, response.text)
            self.assertNotIn('Traceback', response.text)
            self.assertNotIn(secret, logs.output)
            self.assertIn(cause, logs.output)

    def test_other_driver_faults_stay_500(self):
        secret = 'password=secret'
        for error in (driver_base.EngineNotFound(secret),
                      driver_base.DriverError(secret),
                      driver_base.EngineConflict(secret),
                      oslo_messaging.RemoteError(
                          'EngineNotFound', secret, 'Traceback ' + secret)):
            client = webtest.TestApp(errors.ErrorMiddleware(app.setup_app(
                RaisingController(error))))
            logs = self.useFixture(fixtures.FakeLogger())
            response = client.get('/', status=500)
            self.assert_fault(response, 500, 'Internal Server Error')
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, logs.output)

    def test_coast_client_message_masks_password(self):
        client = webtest.TestApp(errors.ErrorMiddleware(app.setup_app(
            RaisingController(exception.Invalid('password=secret-value')))))
        response = client.get('/', status=400)
        self.assertNotIn('secret-value', response.text)

    def test_keystone_pipeline(self):
        self.config.config(auth_strategy='keystone', group='api')
        client = webtest.TestApp(app.load_app())
        client.get('/')
        client.get('/healthcheck')
        response = client.get('/v1/services', headers={
            'X-Project-Id': 'spoofed-project', 'X-Roles': 'admin'}, status=401)
        self.assert_fault(response, 401)
        self.assertIn('WWW-Authenticate', response.headers)
        self.assert_version(response)

    def test_keystone_validated_context(self):
        self.config.config(auth_strategy='keystone', group='api')
        seen = []

        def validate(instance, environ, start_response):
            # Model validated headers, replacing all client identity claims.
            environ.update(HTTP_X_IDENTITY_STATUS='Confirmed',
                           HTTP_X_PROJECT_ID='validated-project',
                           HTTP_X_USER_ID='validated-user',
                           HTTP_X_ROLES='reader')
            return instance._app(environ, start_response)

        original = context.RequestContext.from_environ

        def capture(environ, **kwargs):
            ctx = original(environ, **kwargs)
            seen.append(ctx)
            return ctx

        with mock.patch.object(auth_token.AuthProtocol, '__call__', validate):
            client = webtest.TestApp(app.load_app())
            with mock.patch.object(context.RequestContext, 'from_environ',
                                   side_effect=capture):
                client.get('/v1/not-implemented', headers={
                    'X-Project-Id': 'spoofed-project', 'X-Roles': 'admin'},
                    status=404)
        self.assertEqual('validated-project', seen[0].project_id)
        self.assertEqual(['reader'], seen[0].roles)

    def test_cors_preserves_vary(self):
        self.conf.register_opts(cors.OPTS, group='cors')
        self.config.config(allowed_origin=['https://portal.example.com'],
                           group='cors')
        client = webtest.TestApp(app.load_app())
        response = client.get('/', headers={
            'Origin': 'https://portal.example.com'})
        self.assertEqual('https://portal.example.com', response.headers[
            'Access-Control-Allow-Origin'])
        self.assertIn('Origin', response.headers['Vary'])
        self.assert_version(response)

    def test_missing_paste_file(self):
        self.config.config(api_paste_config='/missing/api-paste.ini',
                           group='api')
        self.assertRaises(RuntimeError, app.load_app)

    def test_wsgi_entry_point(self):
        with mock.patch.object(wsgi.config, 'parse_args'), \
                mock.patch.object(wsgi.config, 'setup_logging'):
            client = webtest.TestApp(wsgi.init_application())
        client.get('/')
        client.get('/v1')

    def test_dev_server_application(self):
        responses = []

        class Server:
            def __init__(server, host, port, appgroup, **kwargs):
                server.appgroup = appgroup

            def __enter__(server):
                return server

            def serve_forever(server):
                responses.append(webtest.TestApp(server.appgroup).get('/'))

            def __exit__(server, *args):
                pass

        with mock.patch.object(api_cmd.config, 'CONF', self.conf), \
                mock.patch.object(api_cmd.config, 'parse_args'), \
                mock.patch.object(api_cmd.config, 'setup_logging'), \
                mock.patch.object(api_cmd.simple_server,
                                  'make_server', Server):
            self.assertEqual(0, api_cmd.main())
        self.assertEqual('v1', responses[0].json['versions'][0]['id'])

    def test_dev_access_log_omits_request_secrets(self):
        logger = self.useFixture(fixtures.FakeLogger(name='coast.cmd.api'))
        handler = api_cmd.RequestHandler.__new__(api_cmd.RequestHandler)
        handler.requestline = 'GET /?token=private-value HTTP/1.1'
        handler.log_request(200, 100)
        self.assertIn('status=200', logger.output)
        self.assertNotIn('private-value', logger.output)


class ErrorMiddlewareLogTest(base.TestCase):
    """Safe log for one unhandled API error."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cfg, 'CONF', self.conf)
        patch.start()
        self.addCleanup(patch.stop)

    def test_unhandled_exception_is_logged_and_safe(self):
        secret = 'password=secret'
        client = webtest.TestApp(errors.ErrorMiddleware(app.setup_app(
            RaisingController(Exception(secret)))))
        logs = self.useFixture(fixtures.FakeLogger())
        response = client.get('/', status=500, extra_environ={
            'openstack.request_id': 'req-safe-500'})
        self.assertEqual(500, response.status_int)
        self.assertEqual('application/json', response.content_type)
        self.assertEqual({
            'faultstring': 'Internal Server Error',
            'faultcode': 'Server',
            'debuginfo': None}, response.json)
        self.assertNotIn(secret, response.text)
        self.assertNotIn('password', response.text)
        self.assertIn('builtins.Exception', logs.output)
        self.assertIn('req-safe-500', logs.output)
        self.assertNotIn(secret, logs.output)
        self.assertNotIn('password', logs.output)
