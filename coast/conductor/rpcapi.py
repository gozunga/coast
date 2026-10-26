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

"""Version 1.0 control-plane handoff; no guest uses this transport."""

import datetime
import logging as std_logging
import re
import uuid

from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_serialization import jsonutils
from oslo_utils import strutils

from coast.api import errors
from coast.common import context as context_module
from coast.common import exception
from coast.common import safe_log
from coast.common import states
from coast.drivers import base as driver_base
from coast.objects import base


RPC_API_VERSION = '1.0'
TOPIC = 'coast-conductor'
LOG = logging.getLogger(__name__)


class ExportURLLogFilter(std_logging.Filter):
    """Mask download capabilities in late AMQP reply logs."""

    def filter(self, record):
        # SigV4 query auth puts the access key id in X-Amz-Credential.
        record.msg = re.sub(
            r"(?i)((?:temp_url_sig|x-amz-signature|x-amz-credential|"
            r"x-amz-security-token)=)[^&\s'\"]+", r"\1***",
            strutils.mask_password(record.getMessage()))
        record.args = ()
        return True


_EXPORT_URL_LOG_FILTER = ExportURLLogFilter()


def revision(resource):
    # The API casts the in-memory row after save(). oslo.db onupdate is
    # naive UTC. A later read can be timezone-aware UTC. Keep
    # microseconds so two writes in one second stay distinct.
    # receipt_id uses this value too.
    stamp = resource.updated_at or resource.created_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    else:
        stamp = stamp.astimezone(datetime.timezone.utc)
    return stamp.replace(tzinfo=None).isoformat()


def receipt_id(operation, resource, accepted_revision=None, task_claim=None):
    identity = '%s:%s:%s' % (
        operation, resource.id, accepted_revision or revision(resource))
    if task_claim is not None:
        # A new claim can target an unchanged revision after a failed attempt.
        # Canonical encoding keeps redelivery of that claim deduplicated.
        identity += ':' + jsonutils.dumps(task_claim, sort_keys=True)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


class Serializer(base.CoastObjectSerializer):
    def serialize_context(self, context):
        # Do not forward the Keystone token or transient plaintext values.
        data = context.to_dict()
        data.pop('auth_token', None)
        return data

    def deserialize_context(self, context):
        return context_module.RequestContext.from_dict(context)


class ConductorAPI:
    RPC_API_VERSION = RPC_API_VERSION

    def __init__(self, transport=None):
        std_logging.getLogger(
            'oslo_messaging._drivers.amqpdriver').addFilter(
                _EXPORT_URL_LOG_FILTER)
        self.transport = transport
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self.transport = self.transport or messaging.get_rpc_transport(
                cfg.CONF)
            self._client = messaging.get_rpc_client(
                self.transport, messaging.Target(topic=TOPIC,
                                                 version=RPC_API_VERSION),
                version_cap=RPC_API_VERSION, serializer=Serializer())
        return self._client

    def cast(self, context, operation, resource, options=None):
        # Service options can contain plaintext env values. Desired state is
        # already persisted; provisioning forwards only needed options.
        safe_options = dict(options or {}) if operation in (
            'create_stack', 'delete_stack') else {}
        if '_task_claim' in (options or {}):
            safe_options['_task_claim'] = dict(options['_task_claim'])
        if operation in ('resize_server', 'resize_workers_stack'):
            safe_options = dict(options or {})
        if operation == 'create_server':
            safe_options = {'plan': (options or {}).get('plan')}
        if operation == 'failover_server':
            status = (options or {}).get('admission_status')
            if status in (states.ACTIVE, states.UNHEALTHY, states.ERROR):
                safe_options['admission_status'] = status
        try:
            self.client.prepare(version=RPC_API_VERSION, retry=0).cast(
                context, 'execute', operation=operation, resource=resource,
                options=safe_options,
                revision=revision(resource),
                reservation=getattr(context, 'quota_reservation', {}))
        except Exception as error:
            safe_log.log_failure(
                LOG, 'Conductor handoff failed: %(cause)s at %(where)s '
                'operation=%(operation)s resource_id=%(resource_id)s',
                error, operation=operation, resource_id=resource.id)
            # Callers can cancel unclaimed work after uncertain delivery.
            # Never include broker credentials or payloads in an exception.
            raise exception.CoastException(
                'Conductor handoff failed.') from None

    def delete_appgroup(self, context, appgroup):
        self.cast(context, 'delete_appgroup', appgroup)

    def delete_app_environment(self, context, environment):
        self.cast(context, 'delete_app_environment', environment)

    def _sync_call(self, context, method, message, failure_fields=None,
                   **kwargs):
        try:
            return self.client.prepare(
                version=RPC_API_VERSION, retry=0,
                timeout=cfg.CONF.rpc_response_timeout).call(
                    context, method, **kwargs)
        except Exception as error:
            if failure_fields is not None:
                safe_log.log_failure(
                    LOG, 'Conductor read failed: %(cause)s at %(where)s '
                    'operation=%(operation)s stack_id=%(stack_id)s '
                    'service_id=%(service_id)s request_id=%(request_id)s '
                    'remote_class=%(remote_class)s',
                    error, operation=method,
                    request_id=getattr(context, 'request_id', None),
                    remote_class=getattr(error, 'exc_type', None),
                    **failure_fields)
            # Keep an engine fault typed so the API returns 503. Drop the
            # remote text. It can contain a URL, a credential, or a traceback.
            fault = errors.engine_fault(error)
            if fault is not None:
                if error.__traceback__ is not None:
                    fault = fault.with_traceback(error.__traceback__)
                raise fault from None
            raise exception.CoastException(message) from None

    def list_volume_mounts(self, context, service_id, stack_id=None):
        try:
            return self._sync_call(
                context, 'list_volume_mounts',
                'Conductor storage read failed.', service_id=service_id,
                failure_fields={'stack_id': stack_id,
                                'service_id': service_id})
        except (exception.CoastException, driver_base.EngineUnreachable,
                driver_base.EngineAuthError):
            # A complete list requires the conductor; never expose remote
            # exception text or return partial data.
            raise errors.StorageUnavailable() from None

    def apply_mounts(self, context, service_id):
        return self._sync_call(
            context, 'apply_mounts', 'Conductor storage request failed.',
            service_id=service_id)

    def get_containers(self, context, service_id, container=None, logs=False,
                       options=None):
        return self._sync_call(
            context, 'get_containers', 'Conductor container request failed.',
            service_id=service_id, container=container, logs=logs,
            options=options or {})

    def get_logs(self, context, resource, resource_id, options=None):
        return self._sync_call(
            context, 'get_logs', 'Conductor log request failed.',
            resource=resource, resource_id=resource_id,
            options=options or {})

    def get_export_urls(self, context, export_id):
        return self._sync_call(
            context, 'get_export_urls',
            'Conductor export URL request failed.', export_id=export_id)

    def import_compose(self, context, service_id):
        return self._sync_call(
            context, 'import_compose', 'Conductor compose request failed.',
            service_id=service_id)

    def rotate_deploy_key(self, context, service_id, reference, public_key):
        return self._sync_call(
            context, 'rotate_deploy_key', 'Deploy key update failed.',
            service_id=service_id, reference=reference, public_key=public_key)
