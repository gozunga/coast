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

"""Raw template metadata cache, local to each API worker process."""

import copy
import threading
import time

from oslo_config import cfg

from coast.drivers import loader


_CACHE = {}
_LOCK = threading.Lock()


def read(stack):
    """Return caller-owned metadata; never cache request visibility rules."""
    key = (stack.driver, stack.engine_version)
    with _LOCK:
        ttl = cfg.CONF.templates.cache_ttl
        if not ttl:
            _CACHE.clear()
        cached = _CACHE.get(key)
        if cached is None or time.monotonic() - cached[0] >= ttl:
            items = loader.load_driver(stack.driver, cfg.CONF).list_templates(
                None, stack)
            if ttl:
                _CACHE[key] = (time.monotonic(), copy.deepcopy(items))
        else:
            items = cached[1]
        return copy.deepcopy(items)
