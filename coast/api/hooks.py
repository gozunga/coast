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

"""Pecan request hooks."""

import json

from oslo_log import log as logging
from pecan import hooks

from coast.api import microversions
from coast.common import context
from coast.common import exception
from coast.common import policy
from coast.common import public_text


LOG = logging.getLogger(__name__)


class ContextHook(hooks.PecanHook):
    def on_route(self, state):
        request = state.request
        ctx = context.RequestContext.from_environ(
            request.environ, request_id=request.environ.get(
                'openstack.request_id'), global_request_id=request.environ.get(
                'openstack.global_request_id'))
        request.context['coast_context'] = ctx
        request.version = request.environ.get(
            'coast.api_version', microversions.MIN_VERSION)
        request.context['policy'] = policy


def _is_admin(ctx):
    if ctx is None:
        return False
    try:
        policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
    except exception.CoastException:
        return False
    return True


class PublicTextHook(hooks.PecanHook):
    """Keep engine names out of Coast text in project user responses.

    Driver errors name the engine so operators can debug them. Stored
    faults, event messages and log notes can carry that text. This is the
    one place where it becomes customer text, so scrub it here. Admin
    reads keep the detail.
    """

    def after(self, state):
        response = state.response
        if response is None or response.status_int >= 300:
            return
        if (response.content_type or '').split(';')[0].strip() != (
                'application/json'):
            return
        # Leave streamed bodies alone; reading them here would buffer or
        # block. Pecan JSON views set a buffered body.
        if not isinstance(response.app_iter, (list, tuple)):
            return
        body = response.body
        if not public_text.may_need_scrub(body):
            return
        if _is_admin(state.request.context.get('coast_context')):
            return
        try:
            decoded = json.loads(body)
        except ValueError:
            return
        scrubbed = public_text.scrub_tree(decoded)
        if scrubbed == decoded:
            return
        LOG.debug('Scrubbed engine names from %s %s',
                  state.request.method, state.request.path)
        response.body = json.dumps(scrubbed).encode('utf-8')
        # The view's ETag describes the unscrubbed body.
        response.etag = None
