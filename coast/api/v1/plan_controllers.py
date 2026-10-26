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

"""Read-only operator plans; the public Q34 contract is revisable."""

import json
import re

import jsonschema
from oslo_config import cfg
import pecan

from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers as common
from coast.api.v1 import validation
from coast.common import exception
from coast.common import policy


PLAN = schemas.document({
    'name': schemas.text(64),
    'kind': {'enum': ['vm', 'baremetal']},
    'roles': {'type': 'array', 'uniqueItems': True,
              'items': {'enum': ['control', 'worker', 'build']}},
    'description': {'type': ['string', 'null']},
    'flavor': schemas.text(255)}, ('name', 'kind', 'roles', 'flavor'))
PUBLIC_FIELDS = ('name', 'kind', 'roles', 'description')


class Plans:
    @pecan.expose('json')
    def index(self, **kwargs):
        common.authorize('plan', 'get_all')
        if pecan.request.method != 'GET':
            pecan.abort(405)
        validation.parameters(pecan.request.params, ())
        plans = load_plans()
        fields = PUBLIC_FIELDS
        if pecan.request.version == '1.1':
            # Q150 interim: the dashboard offers Nova flavors and must know
            # which flavor each plan uses to preselect usable ones.
            fields = PUBLIC_FIELDS + ('flavor',)
        ctx = common.context()
        try:
            policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
            admin = True
        except exception.NotAuthorized:
            admin = False
        result = []
        for plan in sorted(plans, key=lambda item: item['name']):
            item = {key: plan.get(key) for key in fields}
            if not admin and item['description']:
                item['description'] = re.sub(
                    'coolify|traefik', 'App engine server',
                    item['description'], flags=re.IGNORECASE)
            result.append(item)
        return {'plans': result}


def load_plans():
    """Load the validated operator catalog without exposing flavor data."""
    try:
        plans = json.loads(cfg.CONF.plans.definitions)
        jsonschema.validate(plans, {'type': 'array', 'items': PLAN})
        if len({plan['name'] for plan in plans}) != len(plans):
            raise ValueError()
    except (ValueError, jsonschema.ValidationError):
        # Operator config errors must not disclose internal flavor data.
        raise exception.CoastException() from None
    return plans
