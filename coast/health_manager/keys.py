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

"""Resolve stack heartbeat keys without exposing secret payloads."""

from concurrent import futures
import threading
import time

from oslo_config import cfg
from oslo_log import log as logging

from coast.api import secret_store
from coast.common import context
from coast.common import exception
from coast.db import api as db_api


LOG = logging.getLogger(__name__)


class StackKeyLookup:
    """Shared receiver and stale-check lookup with a bounded key cache."""

    CACHE_TTL = 30
    CACHE_SIZE = 1024
    REFRESH_RETRY = 30
    REFRESH_TIMEOUT = 0.05
    MAX_REFRESHES = 4

    def __init__(self, conf=cfg.CONF):
        self.lock = threading.Lock()
        self.conf = conf
        self.warnings = {}
        self.admin = context.RequestContext(is_admin=True)
        self.cache = {}
        self.refreshes = {}
        self.retries = {}

    def __call__(self, env_id):
        with self.lock:
            return self._lookup(env_id)

    def _lookup(self, env_id):
        try:
            # Check liveness and reference even on a cache hit.
            stack = db_api.stacks.get(self.admin, env_id, all_projects=True)
            reference = stack.heartbeat_key_ref
            if not reference:
                raise ValueError('Heartbeat key is unavailable')
            now = time.monotonic()
            cached = self.cache.get(env_id)
            if cached and cached[0] == reference and now < cached[1]:
                return cached[2]
            if cached and cached[0] != reference:
                self.cache.pop(env_id, None)
                cached = None
            try:
                payload = self._refresh(env_id, reference, now)
            except exception.SecretStoreUnavailable:
                now = time.monotonic()
                self._warn(env_id, now)
                if (cached and now < cached[1] +
                        self.conf.health_manager.key_cache_max_stale):
                    return cached[2]
                # Keep the dedicated error for the stale check, but the
                # receiver still sees only ValueError (no backend details).
                raise KeyUnavailable() from None
            self.warnings.pop(env_id, None)
            if not isinstance(payload, str) or len(payload) != 64:
                raise ValueError('Invalid heartbeat key')
            key = bytes.fromhex(payload)
            if len(key) != 32:
                raise ValueError('Invalid heartbeat key')
            if len(self.cache) >= self.CACHE_SIZE:
                self.cache.pop(next(iter(self.cache)))
            self.cache[env_id] = (reference,
                                  time.monotonic() + self.CACHE_TTL, key)
            return key
        except KeyUnavailable:
            raise
        except Exception:
            self.cache.pop(env_id, None)
            # Receiver drops ValueError. Never expose backend exceptions,
            # secret references or payloads, including decoding errors.
            raise ValueError('Heartbeat key lookup failed') from None

    def _refresh(self, env_id, reference, now):
        identity = (env_id, reference)
        pending = self.refreshes.get(identity)
        if pending is None:
            retry = self.retries.get(env_id)
            if retry and retry[0] == reference and now < retry[1]:
                raise exception.SecretStoreUnavailable()
            # Completed results belong to their stack until consumed. Bound
            # active calls separately, retaining results across other stacks'
            # refreshes and evicting the oldest completed entries only at cap.
            completed = [key for key, result in self.refreshes.items()
                         if result.done()]
            if len(self.refreshes) - len(completed) >= self.MAX_REFRESHES:
                raise exception.SecretStoreUnavailable()
            for key in completed:
                if len(self.refreshes) < self.CACHE_SIZE + self.MAX_REFRESHES:
                    break
                del self.refreshes[key]
                retry = self.retries.get(key[0])
                if retry and retry[0] == key[1]:
                    # Eviction must allow a fresh fetch, rather than deferring
                    # a stack whose successful result could not be retained.
                    del self.retries[key[0]]
            pending = futures.Future()
            self.refreshes[identity] = pending
            threading.Thread(target=self._fetch,
                             args=(pending, reference), daemon=True).start()
            timeout = self.REFRESH_TIMEOUT
        else:
            # Subsequent packets never wait on an outstanding request.
            timeout = 0
        try:
            payload = pending.result(timeout=timeout)
        except (futures.TimeoutError, exception.SecretStoreUnavailable):
            failed = pending.done() and pending.exception() is not None
            if failed:
                self.refreshes.pop(identity, None)
            if len(self.retries) >= self.CACHE_SIZE:
                self.retries.pop(next(iter(self.retries)))
            # Do not slide the retry deadline on packets received while a
            # request is pending. Start the interval when failure is observed.
            if failed or env_id not in self.retries:
                self.retries[env_id] = (
                    reference, time.monotonic() + self.REFRESH_RETRY)
            raise exception.SecretStoreUnavailable() from None
        self.refreshes.pop(identity, None)
        self.retries.pop(env_id, None)
        return payload

    def _fetch(self, pending, reference):
        try:
            pending.set_result(secret_store.call('get', self.admin, reference))
        except Exception:
            pending.set_exception(exception.SecretStoreUnavailable())

    def _warn(self, env_id, now):
        if now >= self.warnings.get(env_id, 0):
            LOG.warning('Secret storage unavailable for stack %s; '
                        'heartbeat health checks deferred when no key is '
                        'usable.', env_id)
            if len(self.warnings) >= self.CACHE_SIZE:
                self.warnings.pop(next(iter(self.warnings)))
            self.warnings[env_id] = now + 60


class KeyUnavailable(ValueError):
    """No usable heartbeat key while secret storage is unavailable."""
