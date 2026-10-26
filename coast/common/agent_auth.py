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

"""Control-plane Keystone credential for agent-facing checks.

The guest agent does not receive this password. Conductor, the health
manager and the console proxy load it when ``auth_type`` is set. Cloud
API calls stay on the ``[service_auth]`` session. Startup does not
request a token.
"""

import threading
import weakref

from keystoneauth1 import exceptions as ks_exceptions
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
from oslo_log import log as logging

from coast.common import exception

LOG = logging.getLogger(__name__)

GROUP = 'agent_auth'
ROLE = 'coast_agent'
_LOCK = threading.Lock()
_SESSIONS = {}


def _conf(conf):
    return cfg.CONF if conf is None else conf


def _register_password_options(conf, group):
    """Register password options so a file value can be read.

    Keystoneauth registers these options when it loads a plugin.
    Equivalent options can be registered again. A different schema
    raises DuplicateOptError; the existing option stays readable.
    """
    for opt in ks_loading.get_auth_plugin_conf_options('password'):
        try:
            conf.register_opt(opt, group=group)
        except cfg.DuplicateOptError:
            continue


def _text(conf, group, name):
    try:
        value = conf[group][name]
    except cfg.NoSuchOptError:
        return None
    return value


def _identity(value):
    if not isinstance(value, str):
        return ''
    return value.strip().casefold()


def _missing_names(exc):
    names = []
    for option in getattr(exc, 'options', []) or []:
        name = getattr(option, 'dest', None) or getattr(option, 'name', None)
        if name:
            names.append(str(name).replace('-', '_'))
    return ', '.join(names) or 'unknown'


def _release(conf_id, ref):
    with _LOCK:
        current = _SESSIONS.get(conf_id)
        if current is not None and current[0] is ref:
            _SESSIONS.pop(conf_id, None)


def _fail(message):
    # Fixed text only. Do not log option values or the plugin.
    LOG.error(message)
    raise exception.ServiceConfigError()


def _require_password_options(conf):
    """Require the password fields. Log names, never values."""
    missing = []
    for name in ('auth_url', 'password', 'project_name'):
        value = _text(conf, GROUP, name)
        if not isinstance(value, str) or not value.strip():
            missing.append(name)
    domain_name = _text(conf, GROUP, 'user_domain_name')
    domain_id = _text(conf, GROUP, 'user_domain_id')
    named = isinstance(domain_name, str) and domain_name.strip()
    identified = isinstance(domain_id, str) and domain_id.strip()
    if not named and not identified:
        missing.append('user_domain_name')
    if missing:
        LOG.error('Agent credential is missing options: %s',
                  ', '.join(missing))
        raise exception.ServiceConfigError()


def load_auth(conf=None):
    """Build the agent auth plugin. Do not contact Keystone."""
    conf = _conf(conf)
    if not conf.agent_auth.auth_type:
        _fail('Agent credential option auth_type is not set.')
    if conf.agent_auth.auth_section:
        _fail('Agent credential auth_section is not supported.')
    _register_password_options(conf, GROUP)
    _register_password_options(conf, 'service_auth')
    agent_name = _identity(_text(conf, GROUP, 'username'))
    service_name = _identity(_text(conf, 'service_auth', 'username'))
    if not agent_name or agent_name == service_name:
        _fail('Agent credential user name matches the service user.')
    _require_password_options(conf)
    try:
        auth = ks_loading.load_auth_from_conf_options(conf, GROUP)
    except ks_exceptions.auth_plugins.MissingRequiredOptions as exc:
        LOG.error('Agent credential is missing options: %s',
                  _missing_names(exc))
        raise exception.ServiceConfigError() from None
    except ks_exceptions.auth_plugins.NoMatchingPlugin:
        _fail('Agent credential auth_type is not supported.')
    except ks_exceptions.OptionError:
        _fail('Agent credential options are incomplete.')
    if auth is None:
        _fail('Agent credential option auth_type is not set.')
    return auth


def get_session(conf=None):
    """Return the cached agent session. Do not contact Keystone."""
    conf = _conf(conf)
    key = id(conf)
    with _LOCK:
        found = _SESSIONS.get(key)
        if found is not None and found[0]() is conf:
            return found[1]
        auth = load_auth(conf)
        session = ks_loading.load_session_from_conf_options(
            conf, GROUP, auth=auth)
        username = _text(conf, GROUP, 'username')
        project = _text(conf, GROUP, 'project_name')
        LOG.info('Agent credential user %s project %s role %s',
                 username, project, ROLE)

        def _drop(dead, conf_id=key):
            _release(conf_id, dead)

        _SESSIONS[key] = (weakref.ref(conf, _drop), session)
        return session


def prepare(conf=None):
    """Reject a shared service user when this credential is configured.

    An unset ``auth_type`` keeps existing deployments unchanged.
    """
    conf = _conf(conf)
    if not conf.agent_auth.auth_type:
        return None
    return get_session(conf)
