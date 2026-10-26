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

"""Lifecycle and usage notifications with scalar allow-listed payloads."""

import socket

from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging


LOG = logging.getLogger(__name__)

API = 'coast-api'
CONDUCTOR = 'coast-conductor'
# Identifiers only. Names, tokens, env values, and secret refs stay out.
ALLOWED = ('resource_id', 'project_id', 'user_id', 'status',
           'stack_id', 'service_id', 'server_id', 'operation_id')
USAGE_ALLOWED = tuple(key for key in ALLOWED if key != 'user_id') + (
    'plan', 'flavor_id', 'size_gb', 'replicas', 'cpu_limit', 'memory_limit_mb',
    'created_at', 'service_id', 'audit_period_beginning',
    'audit_period_ending')
_STATES = {}


def public_action(action):
    """Map an internal operation name to the public notification action."""
    if action in ('extend', 'cancel'):
        return 'update'
    if action == 'rollback':
        return 'create'
    return action


class Serializer:
    """Drop Keystone tokens before a notification leaves the process."""

    def serialize_entity(self, context, entity):
        return entity

    def deserialize_entity(self, context, entity):
        return entity

    def serialize_context(self, context):
        if hasattr(context, 'to_dict'):
            data = context.to_dict()
        elif isinstance(context, dict):
            data = dict(context)
        else:
            data = {}
        for name in ('auth_token', 'service_token'):
            data.pop(name, None)
        return data

    def deserialize_context(self, context):
        return context


def _state(conf):
    state = _STATES.get(id(conf))
    if state is None:
        state = {'transport': None, 'notifiers': {}}
        _STATES[id(conf)] = state
    return state


def reset(conf=None):
    """Close cached notification transports. Tests use this between cases."""
    if conf is None:
        states = list(_STATES.values())
        _STATES.clear()
    else:
        states = [_STATES.pop(id(conf), None)]
    for state in states:
        transport = state.get('transport') if state else None
        if transport is None:
            continue
        try:
            transport.cleanup()
        except Exception:
            LOG.warning('Notification transport cleanup failed.')


def notifier_for(conf, service):
    """Return a cached Notifier. An empty driver config selects messagingv2."""
    state = _state(conf)
    publisher_id = '%s.%s' % (service, socket.gethostname())
    notifier = state['notifiers'].get(publisher_id)
    if notifier is None:
        if state['transport'] is None:
            state['transport'] = oslo_messaging.get_notification_transport(
                conf)
        # None lets Notifier read [oslo_messaging_notifications] driver.
        driver = None
        if not conf.oslo_messaging_notifications.driver:
            driver = 'messagingv2'
        notifier = oslo_messaging.Notifier(
            state['transport'], publisher_id=publisher_id, driver=driver,
            retry=conf.oslo_messaging_notifications.retry,
            serializer=Serializer())
        state['notifiers'][publisher_id] = notifier
    return notifier


def _user_id(context):
    if context is None:
        return None
    if isinstance(context, dict):
        return context.get('user_id') or context.get('user')
    return getattr(context, 'user_id', None)


def _attr(resource, name):
    fields = getattr(resource, 'fields', None)
    if fields is not None and name not in fields:
        return None
    checker = getattr(resource, 'obj_attr_is_set', None)
    if checker is not None and not checker(name):
        return None
    return getattr(resource, name, None)


def payload_for(resource, context=None):
    """Build the allow-listed payload for one resource."""
    data = {}
    if resource is not None:
        resource_id = _attr(resource, 'id')
        if resource_id:
            data['resource_id'] = resource_id
        for name in ('project_id', 'status', 'stack_id', 'service_id',
                     'server_id'):
            value = _attr(resource, name)
            if value:
                data[name] = value
    user_id = _user_id(context)
    if user_id:
        data['user_id'] = user_id
    return sanitize(data)


def sanitize(payload, allowed=ALLOWED):
    """Return only allow-listed scalar fields. Drop everything else."""
    if not isinstance(payload, dict):
        return {}
    clean = {}
    for key in allowed:
        value = payload.get(key)
        if isinstance(value, (dict, list, tuple, set)) or value in (None, ''):
            continue
        if isinstance(value, (str, int, float)):
            clean[key] = value
    return clean


def emit(context, service, resource, action, phase, payload, conf=None,
         notifier=None):
    """Emit one info notification. Delivery failure does not change the API."""
    event_type = 'coast.%s.%s.%s' % (
        resource, public_action(action), phase)
    return _deliver(context, service, event_type, sanitize(payload), conf,
                    notifier)


def exists(context, resource, payload, conf=None, notifier=None):
    """Emit a current-resource usage snapshot with a separate allow-list."""
    _deliver(context, CONDUCTOR, 'coast.%s.exists' % resource,
             sanitize(payload, USAGE_ALLOWED), conf, notifier)


def _deliver(context, service, event_type, body, conf, notifier):
    try:
        target = notifier or notifier_for(conf or cfg.CONF, service)
        target.info(context, event_type, body)
        return True
    except Exception:
        # Do not log the exception: transport errors can echo credentials.
        LOG.warning('Notification delivery failed for %s.', event_type)
        return False


def accepted(context, resource, action, name=None):
    """Record that the API accepted a create, update, or delete."""
    emit(context, API, name or resource.repository.resource, action, 'start',
         payload_for(resource, context))


def completed(context, resource, action, name=None):
    """Record a change that finished in the API request."""
    body = payload_for(resource, context)
    resource_name = name or resource.repository.resource
    emit(context, API, resource_name, action, 'start', body)
    emit(context, API, resource_name, action, 'end', body)
