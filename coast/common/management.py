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

"""Control-side sockets: enter management, bind, then restore the thread."""

import ipaddress
import socket

from coast.agent import namespace


def socket_for_agent(conf, kind=socket.SOCK_STREAM, port=0):
    """Return a bound socket for a conductor client or heartbeat listener.

    Only socket creation/bind uses the configured management namespace.
    DB, RPC and other service traffic stays in the original namespace.
    The caller owns the socket.
    """
    address = ipaddress.IPv4Address(conf.health_manager.bind_ip)
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ValueError('An explicit management interface IP is required')
    with namespace.control_management_namespace(
            conf.controller_worker.mgmt_netns):
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.bind((str(address), port))
            return sock
        except Exception:
            sock.close()
            raise
