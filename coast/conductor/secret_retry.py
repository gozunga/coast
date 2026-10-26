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

"""Bounded process-local backoff for pre-fence secret-store outages."""

from collections import OrderedDict
import threading
import time

from oslo_log import log as logging

LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_RETRIES = OrderedDict()
MAX_ENTRIES = 1024
INITIAL_DELAY = 10
MAX_DELAY = 300


def ready(identifier):
    with _LOCK:
        retry = _RETRIES.get(identifier)
        return retry is None or time.monotonic() >= retry[0]


def defer(identifier, stack_id):
    with _LOCK:
        previous = _RETRIES.pop(identifier, None)
        delay = min(previous[1] * 2, MAX_DELAY) if previous else INITIAL_DELAY
        if len(_RETRIES) >= MAX_ENTRIES:
            _RETRIES.popitem(last=False)
        _RETRIES[identifier] = (time.monotonic() + delay, delay)
    LOG.warning('Failover: secret storage unavailable for stack %s; '
                'attempt deferred for %s seconds.', stack_id, delay)


def clear(identifier):
    with _LOCK:
        _RETRIES.pop(identifier, None)
