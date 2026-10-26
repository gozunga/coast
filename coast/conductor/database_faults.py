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

"""Fixed public database faults; never interpolate exception details."""

import requests

from coast.common import exception
from coast.drivers import base


UNSUPPORTED = ("This stack can't run databases yet. Create a new stack, or "
               "contact support if it already is new.")
UNREACHABLE = ("The stack's control server did not respond. Check that the "
               "stack is ACTIVE, then retry.")
CAPACITY = ("The selected server could not start the database. Check its "
            "status and free resources, then retry.")
VERBS = {'create': 'created', 'start': 'started', 'stop': 'stopped',
         'restart': 'restarted', 'rename': 'updated', 'update': 'updated',
         'delete': 'deleted'}


def public_fault(error=None, action='start'):
    if (isinstance(error, base.EngineNotFound) or
            getattr(error, 'status_code', None) in (404, 405)):
        return UNSUPPORTED
    if isinstance(error, (base.EngineUnreachable, requests.Timeout,
                          requests.ConnectionError, TimeoutError,
                          ConnectionError)):
        return UNREACHABLE
    if isinstance(error, (base.EngineConflict, exception.OverQuota)):
        return CAPACITY
    return ("The database could not be %s. Retry, or contact support with "
            "the database ID." % VERBS.get(action, 'updated'))
