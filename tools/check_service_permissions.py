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

"""Check service credentials without modifying cloud resources.

Run with the installed Coast environment and --config-file. Only fixed
PASS/FAIL labels are printed. No write probe is implemented.
"""

import argparse
import logging
import sys

from coast.common import clients
from coast.common import config
from coast.common import service_permissions
from coast.conductor import backup_storage
from coast.network import dns_credentials


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant-project-id',
                        help='Tenant UUID for cross-project Swift/DNS reads.')
    parser.add_argument('--dns-trust-id',
                        help='Use this linked-zone trust for the DNS read.')
    args, remaining = parser.parse_known_args(argv)
    # Disable all Python logging, including keystoneauth HTTP debug logs.
    logging.disable(logging.CRITICAL)
    try:
        config.parse_args(remaining)
        conf = config.CONF
        swift = dns = None
        if args.tenant_project_id:
            def swift_proxy():
                return backup_storage.proxy_for(conf, args.tenant_project_id)

            def dns_proxy():
                return dns_credentials.get_provider(
                    conf, args.dns_trust_id).proxy(
                        args.tenant_project_id, trust_id=args.dns_trust_id)
            swift, dns = swift_proxy, dns_proxy
        elif args.dns_trust_id:
            print('FAIL configuration/tenant-project-required')
            return 1
        return service_permissions.check(
            lambda service: clients.get_connection(service, conf),
            project_id=clients.get_session(conf).get_project_id(),
            swift=swift, dns=dns)
    except Exception:
        print('FAIL configuration/authentication')
        return 1


if __name__ == '__main__':
    sys.exit(main())
