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

"""Small, sanitized transport for the documented Coolify REST API."""

from contextlib import nullcontext
import datetime
from email.utils import parsedate_to_datetime
import random
import time
from urllib import parse

from oslo_log import log as logging
from oslo_utils import strutils
import requests

from coast.agent import namespace
from coast.common import safe_log
from coast.drivers import base

LOG = logging.getLogger(__name__)


# Seconds before the deadline that a held final rate-limit retry wakes,
# so scheduler oversleep cannot push it past the budget. The engine's
# window opened before the request's rejected first attempt, so it has
# closed by then unless the engine was flooded in these last seconds.
FINAL_LEAD = 0.5


class Client:
    def __init__(self, ep, conf):
        self.ep = ep
        self.namespace = conf.controller_worker.mgmt_netns
        self.conf = conf.coolify
        direct = self.conf.insecure_direct_url
        self.url = ep.base_url.rstrip("/")
        url = parse.urlsplit(self.url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise base.DriverError(
                "Coolify requires a credential-free HTTPS "
                "endpoint without query or fragment"
            )
        if direct and not url.path.strip("/"):
            self.url += "/api/v1"
        if not ep.token:
            raise base.EngineAuthError("Coolify API token is required")
        if not direct and not all((ep.client_cert, ep.client_key, ep.ca_cert)):
            raise base.DriverError("Coolify requires agent mTLS credentials")
        self.session = requests.Session()
        self.session.trust_env = False  # No netrc auth or ambient proxies.
        self.session.headers.update(
            {
                "Authorization": "Bearer " + ep.token,
                "Accept": "application/json",
            }
        )
        self.session.verify = ep.ca_cert or True
        if not direct:
            self.session.cert = (ep.client_cert, ep.client_key)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def request(self, method, path, *, data=None, params=None, text=False):
        # Only explicit rejection (429) makes a POST safe to replay.
        deadline = time.monotonic() + self.conf.rate_limit_timeout
        rate_attempts = 0
        transient_attempts = 0
        # The engine's throttle window opened before this request started,
        # so it has closed by the deadline. Without a Retry-After, the last
        # allowed retry is held until just before the deadline instead of
        # giving up while the engine is still throttling.
        final = False
        last = None
        while True:
            # No retry starts at or after the deadline, even after a late
            # wake-up. Re-raise the kind of failure that caused the retry.
            if last is not None and time.monotonic() >= deadline:
                if isinstance(last, base.EngineRateLimited):
                    raise base.EngineRateLimited(
                        "Coolify API rate limit (HTTP 429) persisted; "
                        "retry budget exhausted")
                raise base.EngineUnreachable("Coolify API is unreachable")
            try:
                return self._request(method, path, data, params, text)
            except base.EngineRateLimited as exc:
                last = exc
                rate_attempts += 1
                if final or rate_attempts >= self.conf.rate_limit_attempts:
                    raise base.EngineRateLimited(
                        "Coolify API rate limit (HTTP 429) persisted; "
                        "retry budget exhausted") from None
                remaining = deadline - time.monotonic()
                delay = exc.retry_after
                if delay is None:
                    ceiling = min(10.0, self.conf.retry_wait *
                                  2 ** min(rate_attempts - 1, 10))
                    delay = random.SystemRandom().uniform(ceiling / 2, ceiling)
                    if (rate_attempts == self.conf.rate_limit_attempts - 1 or
                            delay >= remaining):
                        delay = max(0.0, remaining - FINAL_LEAD)
                        final = True
                if remaining <= 0 or (delay >= remaining and not final):
                    raise base.EngineRateLimited(
                        "Coolify API rate limit (HTTP 429) persisted; "
                        "retry budget exhausted") from None
            except base.EngineUnreachable as exc:
                last = exc
                transient_attempts += 1
                if (method not in ("GET", "DELETE", "PATCH") or
                        transient_attempts >= self.conf.retry_attempts):
                    raise
                delay = self.conf.retry_wait * 2 ** (transient_attempts - 1)
                if time.monotonic() + delay >= deadline:
                    raise
            time.sleep(delay)

    def _request(self, method, path, data, params, text):
        try:
            scope = (nullcontext() if self.conf.insecure_direct_url else
                     namespace.control_management_namespace(self.namespace))
            with scope:
                response = self.session.request(
                    method,
                    self.url + path,
                    json=data,
                    params=params,
                    timeout=min(self.ep.timeout, self.conf.api_timeout),
                    allow_redirects=False,
                )
        except (requests.RequestException, OSError):
            # Never include a requests exception: it can contain credentials,
            # request bodies, a response body, or a credential-bearing URL.
            raise base.EngineUnreachable(
                "Coolify API is unreachable"
            ) from None
        try:
            status = response.status_code
            LOG.debug(strutils.mask_password("Coolify API HTTP %s"), status)
            # Stock releases queue server validation and return 201.
            # SSH that is not ready is not an HTTP 400. Log the 4xx.
            # Do not retry it.
            if 400 <= status < 500 and status != 429:
                _log_4xx(
                    method, path, status, response,
                    sensitive=(method == 'GET' and
                               path.startswith('/applications/') or
                               '/storages' in path or
                               path.startswith('/security/keys') or
                               isinstance(data, dict) and
                               bool({'docker_compose_raw', 'dockerfile'} &
                                    data.keys())))
            error = {
                401: base.EngineAuthError,
                403: base.EngineAuthError,
                404: base.EngineNotFound,
                409: base.EngineConflict,
                422: base.EngineConflict,
            }.get(status, base.DriverError)
            if status == 429:
                failure = base.EngineRateLimited(
                    "Coolify API rate limited (HTTP 429)")
                failure.retry_after = _retry_after(response.headers.get(
                    'Retry-After'))
                failure.status_code = status
                raise failure
            if status >= 500:
                error = base.EngineUnreachable
            if not 200 <= status < 300:
                failure = error("Coolify API returned HTTP %d" % status)
                failure.status_code = status
                raise failure
            if text:
                return response.text.strip()
            if status == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                raise base.DriverError(
                    "Coolify API returned invalid JSON"
                ) from None
        finally:
            response.close()


def _json_object(response):
    try:
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _log_4xx(method, path, status, response, sensitive=False):
    payload = _json_object(response)
    if sensitive and isinstance(payload, dict):
        errors = payload.get('errors')
        payload = {'errors': dict.fromkeys(errors)} if isinstance(
            errors, dict) else None
    fields = safe_log.http_fault(method, path, status, payload)
    LOG.info(
        'Coolify API fault method=%(method)s path=%(path)s '
        'status=%(status)s message=%(message)s '
        'error_keys=%(error_keys)s detail=%(detail)s',
        fields)


def _retry_after(value):
    """Return a nonnegative delay; never expose an untrusted header."""
    if value is None:
        return None
    if value.strip().isascii() and value.strip().isdigit():
        try:
            return float(int(value))
        except (ValueError, OverflowError):
            return None
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            return None
        return max(0.0, (when - datetime.datetime.now(
            datetime.timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
