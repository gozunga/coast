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

"""Public deploy webhooks.

* POST /v1/apps/<service-id>/deploy?token=<token>: deploy now.
* POST /v1/hooks/<service-id>/<provider>?token=<token>: Git push webhook for
  github, gitlab, bitbucket or gitea.
* POST /v1/hooks/<service-id>/<token>: generic push webhook (M10).

The per-app token is derived from [webhooks] secret_key, the service ID and
the app's webhook nonce, so regenerating the nonce revokes old URLs.
Coast checks the token, optional provider signatures
(GitHub/Gitea HMAC-SHA256 with the token as the secret, GitLab token header)
and the pushed branch, then starts a normal Coast deployment. The app engine
gets no new exposure and every webhook deploy is a Coast deployment.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets

from oslo_config import cfg

from coast.common import context as context_module
from coast.common import exception
from coast.common import states
from coast import objects

LOG = logging.getLogger(__name__)

UUID = (r'(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-'
        r'[0-9a-f]{12})')
PROVIDERS = ('github', 'gitlab', 'bitbucket', 'gitea')
PATH = re.compile(r'^/hooks/' + UUID + r'/(?P<token>[A-Za-z0-9_-]{20,128})/?$')
PROVIDER_PATH = re.compile(
    r'^/hooks/' + UUID + r'/(?P<provider>' + '|'.join(PROVIDERS) + r')/?$')
DEPLOY_PATH = re.compile(r'^/apps/' + UUID + r'/deploy/?$')
TOKEN_QUERY = re.compile(r'(?:^|&)token=([A-Za-z0-9_-]{20,128})(?:&|$)')
# Push event names per provider; any other event is acknowledged and ignored.
PUSH_EVENTS = {'github': ('HTTP_X_GITHUB_EVENT', ('push',)),
               'gitea': ('HTTP_X_GITEA_EVENT', ('push',)),
               'gitlab': ('HTTP_X_GITLAB_EVENT', ('push hook',
                                                  'tag push hook')),
               'bitbucket': ('HTTP_X_EVENT_KEY', ('repo:push',))}
BUSY = (states.DEPLOYING, states.ROLLING_BACK, states.PROVISIONING)


def enabled():
    return bool(cfg.CONF.webhooks.secret_key)


def token_for(service_id, nonce=None):
    """Return the URL-safe webhook token for one service."""
    message = 'coast-webhook:' + service_id
    if nonce:
        message += ':' + nonce
    digest = hmac.new(cfg.CONF.webhooks.secret_key.encode('utf-8'),
                      message.encode('ascii'), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


def new_nonce():
    return secrets.token_hex(16)


def _base(host_url):
    return (cfg.CONF.webhooks.public_url or host_url or '').rstrip('/')


def url_for(service_id, host_url=None, nonce=None):
    if not enabled():
        return None
    return '%s/v1/hooks/%s/%s' % (_base(host_url), service_id,
                                  token_for(service_id, nonce))


def urls_for(service, host_url=None):
    """All webhook URLs for one app, or None when webhooks are off."""
    if not enabled():
        return None
    nonce = getattr(service, 'webhook_nonce', None)
    token = token_for(service.id, nonce)
    base = _base(host_url)
    result = {'token': token,
              'deploy': '%s/v1/apps/%s/deploy?token=%s' % (
                  base, service.id, token)}
    for provider in PROVIDERS:
        result[provider] = '%s/v1/hooks/%s/%s?token=%s' % (
            base, service.id, provider, token)
    return result


def matches(environ):
    path = environ.get('PATH_INFO', '')
    return (environ.get('REQUEST_METHOD') == 'POST' and
            (path.startswith('/hooks/') or bool(DEPLOY_PATH.match(path))))


def _reply(start_response, status, body):
    payload = json.dumps(body).encode('utf-8')
    start_response(status, [('Content-Type', 'application/json'),
                            ('Content-Length', str(len(payload)))])
    return [payload]


def _not_found(start_response):
    # Unknown service, bad token and disabled feature look the same.
    return _reply(start_response, '404 Not Found',
                  {'error': 'Not found.'})


def _signature_ok(environ, token, body):
    secret = token.encode('ascii')
    for header in ('HTTP_X_HUB_SIGNATURE_256', 'HTTP_X_GITEA_SIGNATURE',
                   'HTTP_X_HUB_SIGNATURE'):
        sent = environ.get(header)
        if sent and not sent.startswith('sha1='):
            sent = sent.split('=', 1)[1] if sent.startswith('sha256=') \
                else sent
            expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(sent.strip().lower(), expected)
    gitlab = environ.get('HTTP_X_GITLAB_TOKEN')
    if gitlab is not None:
        return hmac.compare_digest(gitlab, token)
    return True


def _load(body):
    try:
        data = json.loads(body.decode('utf-8')) if body else {}
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _default_branch(data):
    """Repository default branch named by GitHub, Gitea or GitLab."""
    for key in ('repository', 'project'):
        value = data.get(key)
        if isinstance(value, dict) and isinstance(
                value.get('default_branch'), str):
            return value['default_branch']
    return None


def _pushed_branch(body):
    """Return the pushed branch, '' for no ref, or None for a non-branch."""
    data = _load(body)
    ref = data.get('ref') if isinstance(data, dict) else None
    if not ref:
        changes = (data.get('push') or {}).get('changes') if isinstance(
            data, dict) else None
        try:
            ref = 'refs/heads/' + changes[0]['new']['name']
        except (TypeError, KeyError, IndexError):
            return ''
    if not isinstance(ref, str) or not ref.startswith('refs/heads/'):
        return None
    return ref[len('refs/heads/'):]


def _deploy(ctx, service, handler):
    # Imported here so the WSGI wrapper does not load pecan controllers.
    from coast.api.v1 import service_controllers as services
    from coast.api.v1 import stack_controllers as common
    from coast.db import api as db_api
    with db_api.writer.using(ctx):
        common.quota_hook(ctx, 'deployment', {})
        services.claim_service(ctx, service, states.DEPLOYING)
        if service.status == states.DELETING:
            raise exception.Conflict()
        row = services.create_deployment(ctx, service, {},
                                         trigger='webhook')
    handler.create_deployment(ctx, row, {})
    return row


def _route(environ):
    """Return (service_id, token, kind) or None.

    kind is deploy, generic or a provider name.
    """
    path = environ.get('PATH_INFO', '')
    query = TOKEN_QUERY.search(environ.get('QUERY_STRING', ''))
    match = DEPLOY_PATH.match(path)
    if match:
        return (match.group('id'), query and query.group(1), 'deploy')
    match = PROVIDER_PATH.match(path)
    if match:
        return (match.group('id'), query and query.group(1),
                match.group('provider'))
    match = PATH.match(path)
    if match:
        return match.group('id'), match.group('token'), 'generic'
    return None


def _event(environ, kind):
    """Return 'ping', 'push' or 'other' for the delivered event."""
    names = ('HTTP_X_GITHUB_EVENT', 'HTTP_X_GITEA_EVENT',
             'HTTP_X_GITLAB_EVENT', 'HTTP_X_EVENT_KEY')
    event = ''
    for name in names:
        if environ.get(name):
            event = environ[name].strip().lower()
            break
    if event in ('ping', 'diagnostics:ping'):
        return 'ping'
    if kind in PUSH_EVENTS:
        header, pushes = PUSH_EVENTS[kind]
        value = (environ.get(header) or '').strip().lower()
        if value and value not in pushes:
            return 'other'
    return 'push'


def handle(environ, start_response, handler=None):
    route = _route(environ)
    if not enabled() or not route or not route[1]:
        return _not_found(start_response)
    service_id, token, kind = route
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    if length > cfg.CONF.webhooks.max_body_size:
        return _reply(start_response, '413 Request Entity Too Large',
                      {'error': 'Payload too large.'})
    admin = context_module.RequestContext(is_admin=True)
    try:
        found = objects.Service.get_by_id(admin, service_id,
                                          all_projects=True)
    except exception.NotFound:
        found = None
    expected = token_for(service_id, found and found.webhook_nonce)
    # Unknown app and wrong token answer the same way.
    if not hmac.compare_digest(token, expected) or found is None:
        return _not_found(start_response)
    body = environ['wsgi.input'].read(length) if length else b''
    if kind != 'deploy' and not _signature_ok(environ, token, body):
        return _reply(start_response, '401 Unauthorized',
                      {'error': 'Signature mismatch.'})
    ctx = context_module.RequestContext(project_id=found.project_id,
                                        is_admin=True)
    service = objects.Service.get_by_id(ctx, service_id)
    if kind != 'deploy':
        event = _event(environ, kind)
        if event == 'ping':
            return _reply(start_response, '200 OK', {'result': 'pong'})
        if event == 'other':
            return _reply(start_response, '202 Accepted',
                          {'result': 'ignored',
                           'reason': 'Not a push event.'})
        source = objects.Source.get_by_id(ctx, service.source_id)
        branch = _pushed_branch(body)
        # An app without a branch builds the repository default branch.
        wanted = source.branch or _default_branch(_load(body))
        if branch is None or (branch and source.type == 'git' and
                              wanted and branch != wanted):
            return _reply(start_response, '202 Accepted',
                          {'result': 'ignored',
                           'reason': 'Push is not for the app branch.'})
    # The per-app lock: one deployment at a time.
    if service.task_state in BUSY:
        return _reply(start_response, '409 Conflict',
                      {'error': 'A deployment is already running.'})
    try:
        if handler is None:
            from coast.api import handlers
            handler = handlers.Handler()
        row = _deploy(ctx, service, handler)
    except exception.Conflict:
        return _reply(start_response, '409 Conflict',
                      {'error': 'A deployment is already running.'})
    except exception.CoastException:
        LOG.exception('Webhook deploy failed for %s', service_id)
        return _reply(start_response, '503 Service Unavailable',
                      {'error': 'Deployment could not be started.'})
    LOG.info('Webhook deployment %s queued for %s via %s', row.id,
             service_id, kind)
    return _reply(start_response, '202 Accepted',
                  {'result': 'queued', 'deployment_id': row.id})
