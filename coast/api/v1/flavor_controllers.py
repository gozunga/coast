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

"""Project-visible VM sizes backed by the operator plan catalog."""

from oslo_config import cfg
import pecan

from coast.api import microversions
from coast.api.v1 import plan_controllers
from coast.api.v1 import stack_controllers as common
from coast.api.v1 import validation
from coast.conductor import controller


def resolve(reference):
    # The cache belongs to this request, including misses. Lists never perform
    # more than one Nova lookup per distinct flavor reference.
    cache = getattr(pecan.request, 'coast_flavors', None)
    if cache is None:
        cache = pecan.request.coast_flavors = {}
    if reference not in cache:
        try:
            nova = controller.ControllerCloud(cfg.CONF).proxy('nova')
            flavor = nova.find_flavor(reference)
        except Exception:
            pecan.abort(503)
        cache[reference] = (None if flavor is None else {
            key: getattr(flavor, key) for key in
            ('id', 'name', 'vcpus', 'ram', 'disk')})
    return cache[reference]


def server_flavor(row):
    if row.kind != 'vm' or not row.flavor_id:
        return None
    return resolve(row.flavor_id) or {'id': row.flavor_id, 'name': None}


class Flavors:
    @pecan.expose('json')
    def index(self, **kwargs):
        common.authorize('flavor', 'get_all')
        if not microversions.at_least(pecan.request.version,
                                      microversions.FLAVOR_VERSION):
            pecan.abort(404)
        if pecan.request.method != 'GET':
            pecan.abort(405)
        validation.parameters(pecan.request.params, ())
        candidates = []
        for plan in plan_controllers.load_plans():
            if plan['kind'] != 'vm' or not plan['roles']:
                continue
            flavor = resolve(plan['flavor'])
            if flavor:
                candidates.append(dict(flavor, plan=plan['name'],
                                       roles=plan['roles']))
        # The mapping must be unique for the selected role, including when
        # two configured references resolve to the same Nova flavor.
        result = []
        for item in candidates:
            roles = [role for role in item['roles'] if sum(
                other['id'] == item['id'] and role in other['roles']
                for other in candidates) == 1]
            if roles:
                result.append(dict(item, roles=roles))
        return {'flavors': sorted(result, key=lambda item: item['plan'])}
