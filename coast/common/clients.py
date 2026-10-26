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

"""Cached OpenStack clients for the Coast service project.

The factory follows the Octavia service-client pattern and uses one
keystoneauth session from ``[service_auth]``. Each service gets an
openstacksdk Connection. The Connection reads that service group from
oslo.config. Construction does not contact a cloud.

``[agent_auth]`` is a separate control-plane credential. This module
does not load it. Cloud calls stay on ``[service_auth]``.

Coast does not build a user-scoped connection or accept a request context.
The API validates tenant tokens; RPC removes them before conductor handoff.
Cloud resources normally use the configured service project. Database
backups use this service session with Swift reseller rights to address the
recorded customer account; see coast.conductor.backup_storage.
"""

import logging as std_logging
import threading
import weakref

from keystoneauth1 import exceptions as ks_exceptions
from keystoneauth1 import loading as ks_loading
from openstack import connection
from oslo_config import cfg
from oslo_log import log as logging

from coast.common import exception
from coast.common import opts
from coast import version

LOG = logging.getLogger(__name__)

_ENDPOINT_TYPES = {
    'public': 'public',
    'publicurl': 'public',
    'internal': 'internal',
    'internalurl': 'internal',
    'admin': 'admin',
    'adminurl': 'admin',
}
_INTERFACES = frozenset(('public', 'internal', 'admin'))
# RLock: a weak-reference callback can run while this lock is held.
_LOCK = threading.RLock()
# ConfigOpts is not hashable, so it cannot be a WeakKeyDictionary key.
# The key is the object id. The weak reference drops that entry when
# the config object is gone, including when the id is reused.
_CACHES = {}


class _Cache:
    def __init__(self):
        self.auth = None
        self.sessions = {}
        self.connections = {}


def _release(conf_id, ref):
    with _LOCK:
        current = _CACHES.get(conf_id)
        if current is not None and current[0] is ref:
            _CACHES.pop(conf_id, None)


def reset(conf=None):
    """Drop cached sessions and connections."""
    with _LOCK:
        if conf is None:
            _CACHES.clear()
        else:
            _CACHES.pop(id(conf), None)


def _conf(conf):
    return cfg.CONF if conf is None else conf


def _cache(conf):
    key = id(conf)
    found = _CACHES.get(key)
    if found is not None and found[0]() is conf:
        return found[1]
    cache = _Cache()

    def _drop(dead, conf_id=key):
        _release(conf_id, dead)

    _CACHES[key] = (weakref.ref(conf, _drop), cache)
    return cache


def _fail(message, *args):
    # Log only the fixed phrase and the service name. Do not log values.
    LOG.error(message, *args)
    raise exception.ServiceConfigError()


def _prepare_service(conf, service):
    """Require a region and one catalog interface."""
    group = conf[service]
    if not group.region_name:
        _fail('Service %s region_name is not set.', service)
    endpoint_type = group.endpoint_type
    interfaces = list(group.valid_interfaces or [])
    mapped = None
    if endpoint_type:
        mapped = _ENDPOINT_TYPES.get(str(endpoint_type).strip().lower())
        if mapped is None:
            _fail('Service %s endpoint_type is not valid.', service)
    if endpoint_type and interfaces and interfaces != [mapped]:
        _fail('Service %s interface options conflict.', service)
    if endpoint_type and not interfaces:
        conf.set_override('valid_interfaces', [mapped], group=service)
        interfaces = [mapped]
    if not interfaces:
        _fail('Service %s catalog interface is not set.', service)
    for interface in interfaces:
        if interface not in _INTERFACES:
            _fail('Service %s catalog interface is not valid.', service)


def _verify_override(conf, service):
    """Return session kwargs for a per-service TLS override."""
    insecure = conf[service].insecure
    ca_file = conf[service].ca_certificates_file
    if insecure and ca_file:
        _fail('Service %s TLS options conflict.', service)
    if insecure:
        return {'verify': False}
    if ca_file:
        return {'verify': ca_file}
    return {}


def _session_key(verify_kwargs):
    if not verify_kwargs:
        return 'service-auth'
    return ('verify', verify_kwargs['verify'])


def _load_auth(conf, cache):
    if cache.auth is not None:
        return cache.auth
    try:
        auth = ks_loading.load_auth_from_conf_options(
            conf, 'service_auth')
    except ks_exceptions.auth_plugins.MissingRequiredOptions as exc:
        names = []
        for option in getattr(exc, 'options', []) or []:
            name = getattr(option, 'dest', None) or getattr(
                option, 'name', None)
            if name:
                names.append(str(name).replace('-', '_'))
        LOG.error('Service authentication is missing options: %s',
                  ', '.join(names) or 'unknown')
        raise exception.ServiceConfigError() from None
    # An unset auth_type makes the loader return None. Do not cache it.
    if auth is None:
        LOG.error('Service authentication option auth_type is not set.')
        raise exception.ServiceConfigError() from None
    cache.auth = auth
    return cache.auth


def _load_session(conf, cache, verify_kwargs):
    auth = _load_auth(conf, cache)
    session = ks_loading.load_session_from_conf_options(
        conf, 'service_auth', auth=auth, **verify_kwargs)
    if 'verify' in verify_kwargs:
        # Keep the service CA or insecure flag if the loader prefers
        # the [service_auth] session value.
        session.verify = verify_kwargs['verify']
    return session


def get_session(conf=None):
    """Return the cached ``[service_auth]`` session.

    The same session object is returned until :func:`reset`.
    """
    conf = _conf(conf)
    with _LOCK:
        cache = _cache(conf)
        session = cache.sessions.get('service-auth')
        if session is None:
            session = _load_session(conf, cache, {})
            cache.sessions['service-auth'] = session
        return session


def get_connection(service, conf=None):
    """Return the cached openstacksdk Connection for one service.

    ``service`` is ``nova``, ``neutron``, ``cinder``, ``glance``,
    ``barbican`` or ``designate``. Services with the same TLS settings
    share one session.
    This function does not contact a cloud.
    """
    conf = _conf(conf)
    if service not in opts.SERVICE_TYPE_DEFAULTS:
        _fail('OpenStack service %s is not a Coast client.', service)
    _prepare_service(conf, service)
    verify_kwargs = _verify_override(conf, service)
    with _LOCK:
        cache = _cache(conf)
        cached = cache.connections.get(service)
        if cached is not None:
            return cached
        key = _session_key(verify_kwargs)
        session = cache.sessions.get(key)
        if session is None:
            session = _load_session(conf, cache, verify_kwargs)
            cache.sessions[key] = session
        conn = connection.Connection(
            session=session,
            oslo_conf=conf,
            app_name='coast',
            app_version=version.version_info.version_string(),
            service_types=[conf[service].service_type])
        cache.connections[service] = conn
        return conn


def get_sensitive_proxy(service, conf=None):
    """Suppress SDK HTTP payload logs for config drives and secret storage.

    The public keystoneauth Adapter.logger controls request/response logging.
    A private disabled logger cannot be enabled by operator debug settings.
    Normal service logs and sanitized Coast failures remain available.
    """
    attribute = {'nova': 'compute', 'barbican': 'key_manager'}[service]
    proxy = getattr(get_connection(service, conf), attribute)
    logger = std_logging.Logger('coast.sdk.sensitive')
    logger.disabled = True
    proxy.logger = logger
    return proxy
