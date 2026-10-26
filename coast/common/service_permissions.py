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

"""Read-only representatives of service-project and cross-project access.

These checks establish read access only, never write authorization. Results
contain fixed labels only; SDK objects and exceptions may contain secrets.
"""

import sys


OWN_READS = (
    ('compute', 'servers'),
    ('network', 'networks'),
    ('block_storage', 'volumes'),
    ('image', 'images'),
    ('key_manager', 'secrets'),
    ('dns', 'zones'),
)
_SERVICES = ('nova', 'neutron', 'cinder', 'glance', 'barbican', 'designate')


def check(factory, project_id, swift=None, dns=None, output=None):
    """Consume lists to catch lazy authorization failures without logging data.

    ``swift`` and ``dns`` are optional proxy factories. Swift must address
    a tenant account, and DNS must carry the
    configured sudo header or use a tenant trust. Neither is inferred from
    resource labels. No all-projects inventory or secret payload is read.
    """
    output = sys.stdout if output is None else output
    failed = False
    if not project_id:
        print('FAIL configuration/service-project-required', file=output)
        return 1

    def probe(label, call, listing=False):
        nonlocal failed
        try:
            result = call()
            if listing:
                # Exhaust pagination without retaining resource contents.
                for item in result:
                    pass
        except Exception:
            failed = True
            print('FAIL ' + label, file=output)
        else:
            print('PASS ' + label, file=output)

    for service, (attribute, method) in zip(_SERVICES, OWN_READS):
        def read(service=service, attribute=attribute, method=method):
            proxy = getattr(factory(service), attribute)
            query = {}
            if service in ('nova', 'cinder'):
                query['all_projects'] = False
            elif service == 'neutron':
                query['project_id'] = project_id
            elif service == 'glance':
                query['owner'] = project_id
            return getattr(proxy, method)(**query)
        probe('own/' + service + '/' + method, read, listing=True)
    if swift is not None:
        probe('cross/swift/account-head',
              lambda: swift().get_account_metadata())
    if dns is not None:
        probe('cross/designate/zones', lambda: dns().zones(), listing=True)
    return int(failed)
