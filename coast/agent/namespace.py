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

"""Create management sockets, then restore the calling thread namespace."""

from contextlib import contextmanager
import ctypes
import os
import re
import socket


@contextmanager
def management_namespace(name):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('Invalid management namespace')
    # setns affects this native thread only. Do not yield to an event loop.
    libc = ctypes.CDLL(None, use_errno=True)
    with open('/proc/thread-self/ns/net', 'rb') as original:
        with open('/var/run/netns/' + name, 'rb') as target:
            if libc.setns(target.fileno(), 0x40000000):
                raise OSError(ctypes.get_errno(),
                              'Management namespace failed')
            try:
                yield
            finally:
                if libc.setns(original.fileno(), 0x40000000):
                    # Continuing in the wrong namespace is unsafe.
                    os._exit(1)


@contextmanager
def control_management_namespace(name):
    """Use the host namespace only when no control-side name is supplied."""
    if name is None or name == '':
        yield
    else:
        with management_namespace(name):
            yield


def same_project(conf, project_id):
    """Return True only when this guest is configured for that project.

    An empty project id fails closed. Agent startup does not call this.
    """
    configured = conf.agent.project_id or ''
    return bool(configured) and project_id == configured


def management_socket(conf, kind, port):
    sock = socket.socket(socket.AF_INET, kind)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((conf.agent.bind_ip, port))
        if kind == socket.SOCK_STREAM:
            sock.listen(conf.agent.max_connections)
        return sock
    except Exception:
        sock.close()
        raise
