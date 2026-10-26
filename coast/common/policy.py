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

"""Project-scoped policy authorization."""
import threading

from oslo_config import cfg
from oslo_policy import policy

from coast.common import exception
from coast.common import opts
from coast import policies


_ENFORCER = None
_ENFORCER_LOCK = threading.Lock()


def get_enforcer():
    """Return the shared enforcer, including operator policy overrides."""
    global _ENFORCER
    with _ENFORCER_LOCK:
        if _ENFORCER is None:
            opts.set_defaults(cfg.CONF)
            enforcer = policy.Enforcer(cfg.CONF)
            enforcer.register_defaults(policies.list_rules())
            # Publish only after every default rule is registered.
            _ENFORCER = enforcer
        return _ENFORCER


def reset():
    """Clear cached policy state."""
    global _ENFORCER
    with _ENFORCER_LOCK:
        if _ENFORCER is not None:
            _ENFORCER.clear()
            _ENFORCER = None


def authorize(context, rule, target):
    """Authorize a project target without disclosing foreign resources.

    API callers must also filter queries by project and check ownership.
    Admin access is determined by the ADMIN policy, not context.is_admin.
    """
    enforcer = get_enforcer()
    credentials = context.to_policy_values()
    try:
        try:
            is_admin = enforcer.authorize(
                'ADMIN', target, credentials, do_raise=True)
        except policy.PolicyNotAuthorized:
            is_admin = False
        if ('project_id' in target and
                target['project_id'] != context.project_id and not is_admin):
            raise exception.NotFound()
        return enforcer.authorize(
            rule, target, credentials, do_raise=True,
            exc=exception.NotAuthorized)
    except policy.InvalidScope:
        raise exception.NotAuthorized()
