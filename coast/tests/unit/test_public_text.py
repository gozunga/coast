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


"""Project users never read the app engine or proxy name."""

import ast
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest import mock

from webob import Response

from coast.api import errors
from coast.api import hooks
from coast.api import microversions
from coast.common import public_text
from coast.common import states
from coast import objects
from coast.tests import base
from coast.tests.unit import test_controllers


ENGINE = re.compile('coolify|traefik', re.IGNORECASE)
LEAK = ('Coolify API returned HTTP 502 \u2014 TRAEFIK router failed '
        'password=private-value')
ROOT = Path(__file__).resolve().parents[2]


class ScrubTest(base.TestCase):
    def test_scrub_replaces_names_and_masks_secrets(self):
        text = public_text.scrub(LEAK)
        self.assertIsNone(ENGINE.search(text))
        self.assertNotIn('private-value', text)
        self.assertEqual(
            'application engine API returned HTTP 502 - application '
            'routing router failed password=***', text)
        self.assertIsNone(public_text.scrub(None))
        self.assertEqual('', public_text.scrub(''))

    def test_scrub_tree_touches_only_coast_text(self):
        body = {'events': [{'message': LEAK, 'name': 'traefik-demo',
                            'detail': {'reason': LEAK}}],
                'fault': LEAK, 'status_reason': 'Coolify',
                'logs': {'note': LEAK, 'unavailable': LEAK,
                         'lines': [{'message': 'traefik line'}]},
                'env': {'message': 'COOLIFY_FQDN'},
                'description': 'Traefik dashboard template'}
        result = public_text.scrub_tree(body)
        self.assertFalse(public_text.contains_engine_name(
            str([result['events'][0]['message'],
                 result['events'][0]['detail'], result['fault'],
                 result['status_reason'], result['logs']['note'],
                 result['logs']['unavailable']])))
        # Customer names, env values, log lines and template text stay.
        self.assertEqual('traefik-demo', result['events'][0]['name'])
        self.assertEqual(body['logs']['lines'], result['logs']['lines'])
        self.assertEqual(body['env'], result['env'])
        self.assertEqual(body['description'], result['description'])

    def test_scrub_tree_covers_errors_and_warnings(self):
        body = {'backup': {'error': LEAK, 'restores': [{'error': LEAK}]},
                'errors': [LEAK], 'warning': LEAK, 'warnings': [LEAK],
                'last_error': LEAK, 'detail': {'failover_warnings': [LEAK]},
                'logs': {'lines': [{'error': 'traefik line'}]},
                'env': {'error': 'COOLIFY_FQDN'}}
        result = public_text.scrub_tree(body)
        scrubbed = dict(result, logs=None, env=None)
        self.assertFalse(public_text.contains_engine_name(str(scrubbed)))
        self.assertNotIn('private-value', str(scrubbed))
        self.assertEqual(public_text.scrub(LEAK), result['backup']['error'])
        # Raw log lines and customer env values stay as they are.
        self.assertEqual(body['logs'], result['logs'])
        self.assertEqual(body['env'], result['env'])

    def test_error_faultstring_never_names_engine(self):
        response = errors.error_response(409, LEAK)
        self.assertFalse(public_text.contains_engine_name(response.body))
        self.assertNotIn(b'private-value', response.body)


class HookTest(base.TestCase):
    def after(self, response, admin=False, method='GET'):
        state = SimpleNamespace(response=response, request=SimpleNamespace(
            context={}, method=method, path='/v1/test'))
        with mock.patch.object(hooks, '_is_admin', return_value=admin):
            hooks.PublicTextHook().after(state)
        return response

    def json(self, body, etag=None):
        response = Response(json.dumps(body).encode(),
                            content_type='application/json')
        response.etag = etag
        return response

    def test_scrubs_json_and_drops_stale_etag(self):
        response = self.after(self.json(
            {'reconcile_fault': LEAK, 'message': LEAK}, etag='abc'))
        self.assertFalse(public_text.contains_engine_name(response.body))
        self.assertFalse(public_text.contains_engine_name(
            json.loads(response.body)['message']))
        self.assertIsNone(response.etag)

    def test_escaped_name_is_found(self):
        response = self.after(Response(
            b'{"fault": "\\u0043oolify down"}',
            content_type='application/json'))
        self.assertEqual('application engine down',
                         response.json['fault'])

    def test_leaves_admin_unchanged_bodies_and_other_types(self):
        body = {'fault': LEAK}
        self.assertEqual(body, self.after(
            self.json(body), admin=True).json)
        clean = self.json({'fault': 'ok'}, etag='e')
        self.assertEqual('"e"', self.after(clean).headers['ETag'])
        text = Response(LEAK, content_type='text/plain', charset='utf-8')
        self.assertEqual(LEAK, self.after(text).text)
        for status in (204, 304):
            response = Response(status=status)
            self.after(response)
            self.assertEqual(b'', response.body)
        head = self.after(self.json(body), method='HEAD')
        self.assertFalse(public_text.contains_engine_name(head.body))

    def test_streamed_body_is_not_read(self):
        read = []

        def chunks():
            read.append(True)
            yield json.dumps({'fault': LEAK}).encode()
        response = Response(app_iter=chunks(),
                            content_type='application/json')
        self.after(response)
        self.assertEqual([], read)


class ResponseBoundaryTest(test_controllers.ControllerFixture):
    """Stored driver text is scrubbed on every project read."""

    def setUp(self):
        super().setUp()
        self.headers[microversions.HEADER] = (
            'app-hosting ' + microversions.MAX_VERSION)
        source = objects.Source(context=self.ctx, type='git')
        source.create()
        self.service = objects.Service(
            context=self.ctx, stack_id=self.env.id, name='web',
            source_id=source.id, server_id=self.server.id, ports='3000',
            replicas=1, auto_deploy=False, status=states.ERROR,
            task_state=states.NONE)
        self.service.create()
        objects.Deployment(
            context=self.ctx, service_id=self.service.id, spec={},
            trigger='api', status=states.FAILED, fault=LEAK).create()
        for resource_type, resource_id in (
                ('service', self.service.id), ('stack', self.env.id)):
            objects.Event(
                context=self.ctx, stack_id=self.env.id,
                resource_type=resource_type, resource_id=resource_id,
                action='deploy', outcome='failure', message=LEAK).create()
        self.service.reconcile_fault = LEAK
        self.service.save()
        self.server.fault = LEAK
        self.server.save()
        self.env.fault = LEAK
        self.env.save()
        service = '/v1/services/' + self.service.id
        self.paths = (
            self.path, '/v1/stacks', self.server_path,
            self.path + '/servers', self.path + '/events',
            service, '/v1/services', service + '/deployments',
            '/v1/events?resource_id=' + self.service.id)

    def test_project_reads_never_name_engine(self):
        for roles in ('reader', 'reader,member,manager'):
            headers = dict(self.headers, **{'X-Roles': roles})
            for path in self.paths:
                response = self.client.get(path, headers=headers)
                self.assertFalse(
                    public_text.contains_engine_name(response.body),
                    '%s as %s: %s' % (path, roles, response.text))
                self.assertNotIn(b'private-value', response.body)

    def test_admin_reads_keep_detail(self):
        admin = dict(self.headers, **{'X-Roles': 'admin'})
        shown = self.client.get(
            '/v1/services/%s/deployments' % self.service.id, headers=admin)
        self.assertIn('Coolify API returned HTTP 502', shown.text)


def _literals(node):
    """Yield the literal string parts of a message expression."""
    for item in ast.walk(node):
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            yield item.value


TEXT_KWARGS = public_text.TEXT_KEYS | {'msg_fmt'}
NAME = re.compile(r'(FAULT|REASON|MESSAGE|NOTE|UNAVAILABLE)')


class CoastTextTest(base.TestCase):
    """Coast-authored customer text never names the engine.

    The response boundary is the safety net. This test keeps Coast's own
    wording clean so a new path cannot depend on the net alone. Engine
    driver modules (coast/drivers/coolify) keep the name for operator
    logs; the boundary scrubs it.
    """

    def sources(self):
        for path in sorted(ROOT.rglob('*.py')):
            parts = path.relative_to(ROOT).parts
            if parts[0] == 'tests' or parts[:2] == ('drivers', 'coolify'):
                continue
            yield path, ast.parse(path.read_text(), str(path))

    def test_no_engine_names_in_coast_text(self):
        leaks = []
        for path, tree in self.sources():
            for node in ast.walk(tree):
                found = []
                if isinstance(node, ast.Assign):
                    names = [t.id for t in node.targets
                             if isinstance(t, ast.Name)]
                    names += [t.attr for t in node.targets
                              if isinstance(t, ast.Attribute)]
                    if any(n in TEXT_KWARGS or NAME.search(n) or
                           public_text.is_text_key(n) for n in names):
                        found = list(_literals(node.value))
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and (
                                public_text.is_text_key(key.value)):
                            found.extend(_literals(value))
                elif isinstance(node, ast.keyword) and (
                        node.arg in TEXT_KWARGS or
                        public_text.is_text_key(node.arg)):
                    found = list(_literals(node.value))
                elif isinstance(node, ast.Raise) and node.exc is not None:
                    call = node.exc
                    if isinstance(call, ast.Call):
                        for arg in call.args:
                            found.extend(_literals(arg))
                leaks.extend('%s:%d %r' % (path.relative_to(ROOT),
                                           node.lineno, text)
                             for text in found if ENGINE.search(text))
        self.assertEqual([], leaks)
