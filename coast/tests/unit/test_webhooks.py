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

import functools
import hashlib
import hmac
import json
from unittest import mock

import webtest

from coast.api import webhooks
from coast.common import states
from coast import objects
from coast.tests import app_api


class WebhookTest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.config.config(secret_key='unit-test-key', group='webhooks')
        self.source.branch = 'main'
        self.source.save()
        self.handler = mock.Mock()
        self.hooks = webtest.TestApp(
            functools.partial(webhooks.handle, handler=self.handler))
        self.token = webhooks.token_for(self.service.id)
        self.url = '/hooks/%s/%s' % (self.service.id, self.token)

    def post(self, body, status, url=None, sign=True, **headers):
        raw = json.dumps(body).encode()
        if sign:
            headers['X-Hub-Signature-256'] = 'sha256=' + hmac.new(
                self.token.encode(), raw, hashlib.sha256).hexdigest()
        return self.hooks.post(url or self.url, raw, headers=headers,
                               status=status)

    def test_token_is_stable_and_per_service(self):
        self.assertEqual(self.token, webhooks.token_for(self.service.id))
        self.assertNotEqual(self.token, webhooks.token_for(
            '00000000-0000-0000-0000-000000000000'))
        self.assertTrue(webhooks.url_for(self.service.id, 'http://h/')
                        .startswith('http://h/v1/hooks/' + self.service.id))

    def test_signed_push_queues_webhook_deployment(self):
        result = self.post({'ref': 'refs/heads/main'}, 202,
                           **{'X-GitHub-Event': 'push'}).json
        self.assertEqual('queued', result['result'])
        row = objects.Deployment.get_by_id(self.ctx, result['deployment_id'])
        self.assertEqual('webhook', row.trigger)
        self.handler.create_deployment.assert_called_once()
        self.assertEqual(states.DEPLOYING, objects.Service.get_by_id(
            self.ctx, self.service.id).task_state)
        self.post({'ref': 'refs/heads/main'}, 409)

    def test_other_branch_and_tags_are_ignored(self):
        for ref in ('refs/heads/dev', 'refs/tags/v1'):
            self.assertEqual('ignored', self.post({'ref': ref}, 202)
                             .json['result'])
        self.handler.create_deployment.assert_not_called()

    def test_unset_branch_uses_repository_default(self):
        self.source.branch = None
        self.source.save()
        body = {'ref': 'refs/heads/dev',
                'repository': {'default_branch': 'main'}}
        self.assertEqual('ignored', self.post(body, 202).json['result'])
        body['ref'] = 'refs/heads/main'
        self.assertEqual('queued', self.post(body, 202).json['result'])

    def test_ping(self):
        self.assertEqual('pong', self.post(
            {'zen': 'x'}, 200, **{'X-GitHub-Event': 'ping'}).json['result'])
        self.handler.create_deployment.assert_not_called()

    def test_bad_signature_and_gitlab_token(self):
        self.post({'ref': 'refs/heads/main'}, 401, sign=False,
                  **{'X-Hub-Signature-256': 'sha256=' + '0' * 64})
        self.post({'ref': 'refs/heads/main'}, 401, sign=False,
                  **{'X-Gitlab-Token': 'wrong'})
        self.post({'ref': 'refs/heads/main'}, 202, sign=False,
                  **{'X-Gitlab-Token': self.token})

    def test_wrong_token_unknown_service_and_disabled_are_404(self):
        self.post({}, 404, url='/hooks/%s/%s' % (self.service.id, 'x' * 43))
        other = '00000000-0000-0000-0000-000000000000'
        self.post({}, 404, url='/hooks/%s/%s' % (
            other, webhooks.token_for(other)))
        self.config.config(secret_key=None, group='webhooks')
        self.post({}, 404)
        self.assertIsNone(webhooks.url_for(self.service.id))
        self.handler.create_deployment.assert_not_called()

    def test_body_limit(self):
        self.config.config(max_body_size=1024, group='webhooks')
        self.post({'pad': 'x' * 2000}, 413)

    def raw(self, url, body=b'{}', status=202, **headers):
        return self.hooks.post(url, body, headers=headers, status=status)

    def test_deploy_url_needs_token_and_respects_lock(self):
        url = '/apps/%s/deploy' % self.service.id
        self.raw(url, status=404)
        self.raw(url + '?token=' + 'x' * 43, status=404)
        result = self.raw(url + '?token=' + self.token).json
        self.assertEqual('queued', result['result'])
        self.raw(url + '?token=' + self.token, status=409)

    def test_provider_urls(self):
        base = '/hooks/%s/' % self.service.id
        body = json.dumps({'ref': 'refs/heads/main'}).encode()
        sig = hmac.new(self.token.encode(), body, hashlib.sha256).hexdigest()
        q = '?token=' + self.token
        self.raw(base + 'github' + q, body, 200, **{'X-GitHub-Event': 'ping'})
        self.assertEqual('ignored', self.raw(
            base + 'github' + q, body,
            **{'X-GitHub-Event': 'issues'}).json['result'])
        self.raw(base + 'github' + q, body, 401, **{
            'X-GitHub-Event': 'push', 'X-Hub-Signature-256': 'sha256=00'})
        self.raw(base + 'gitlab' + q, body, 401, **{
            'X-Gitlab-Event': 'Push Hook', 'X-Gitlab-Token': 'wrong'})
        self.raw(base + 'github', body, 404, **{'X-GitHub-Event': 'push'})
        self.assertEqual('queued', self.raw(
            base + 'gitea' + q, body, **{'X-Gitea-Event': 'push',
                                         'X-Gitea-Signature': sig}
        ).json['result'])
        self.raw(base + 'github' + q, body, 409, **{
            'X-GitHub-Event': 'push', 'X-Hub-Signature-256': 'sha256=' + sig})

    def test_bitbucket_branch_from_changes(self):
        url = '/hooks/%s/bitbucket?token=%s' % (self.service.id, self.token)
        body = {'push': {'changes': [{'new': {'name': 'dev',
                                              'type': 'branch'}}]}}
        self.assertEqual('ignored', self.raw(
            url, json.dumps(body).encode(),
            **{'X-Event-Key': 'repo:push'}).json['result'])
        body['push']['changes'][0]['new']['name'] = 'main'
        self.assertEqual('queued', self.raw(
            url, json.dumps(body).encode(),
            **{'X-Event-Key': 'repo:push'}).json['result'])

    def test_webhooks_endpoint_and_regenerate(self):
        path = self.path + '/webhooks'
        self.request('get', path, version='1.2', status=404)
        shown = self.request('get', path, version='1.3').json['webhooks']
        self.assertTrue(shown['enabled'])
        self.assertEqual(self.token, shown['token'])
        self.assertIn('/v1/apps/%s/deploy?token=%s' % (
            self.service.id, self.token), shown['deploy'])
        for provider in ('github', 'gitlab', 'bitbucket', 'gitea'):
            self.assertIn('/v1/hooks/%s/%s?token=' % (
                self.service.id, provider), shown[provider])
        self.request('get', path, version='1.3', status=403, headers=dict(
            self.headers, **{'X-Roles': 'reader'}))
        new = self.request('post', path + '/regenerate', version='1.3'
                           ).json['webhooks']
        self.assertNotEqual(self.token, new['token'])
        self.post({'ref': 'refs/heads/main'}, 404)
        self.raw('/apps/%s/deploy?token=%s' % (self.service.id,
                                               new['token']))
        self.assertNotIn('webhook_url', self.request(
            'get', self.path, version='1.3').json['service'])
