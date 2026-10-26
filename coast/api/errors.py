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

"""One safe HTTP boundary for application and middleware errors."""

from oslo_log import log as logging
import oslo_messaging
from webob import exc
from webob import Request
from webob import Response

from coast.common import exception
from coast.common import public_text
from coast.common import safe_log
from coast.drivers import base as driver_base


LOG = logging.getLogger(__name__)

STORAGE_UNAVAILABLE = (
    'Storage details are unavailable right now. Try again in a moment.')


class StorageUnavailable(exception.CoastException):
    code = 503
    msg_fmt = STORAGE_UNAVAILABLE


# Oslo Messaging rebuilds an allowed remote class with a ``_Remote`` suffix.
# This service does not allow driver modules, so the client receives
# RemoteError and exc_type is the remote class name.
# https://docs.openstack.org/oslo.messaging/latest/reference/rpcclient.html
_REMOTE_SUFFIX = '_Remote'
_ENGINE_FAULTS = {
    'EngineUnreachable': driver_base.EngineUnreachable,
    'EngineAuthError': driver_base.EngineAuthError,
}


def _engine_name(value):
    if not isinstance(value, str) or not value:
        return None
    name = value.rsplit('.', 1)[-1]
    if name.endswith(_REMOTE_SUFFIX):
        name = name[:-len(_REMOTE_SUFFIX)]
    if name in _ENGINE_FAULTS:
        return name
    return None


def engine_fault(error):
    """Return a secret-free engine fault, or None.

    Do not copy ``str(error)`` or ``RemoteError.value``. Those strings
    can contain an engine URL, a credential, or traceback text.
    """
    for cls in _ENGINE_FAULTS.values():
        if isinstance(error, cls):
            return cls()
    name = None
    if type(error).__name__.endswith(_REMOTE_SUFFIX):
        name = _engine_name(type(error).__name__)
    elif isinstance(error, oslo_messaging.RemoteError):
        name = _engine_name(getattr(error, 'exc_type', None))
    if name is None:
        return None
    return _ENGINE_FAULTS[name]()


def error_response(code, message=None):
    error = exc.status_map.get(code, exc.HTTPInternalServerError)()
    if code >= 500 or message is None:
        message = error.title
    return Response(status=code, json_body={
        'faultstring': public_text.scrub(message),
        'faultcode': 'Server' if code >= 500 else 'Client',
        'debuginfo': None})


class ErrorMiddleware:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        try:
            response = Request(environ).get_response(self.application)
        except exception.CoastException as error:
            response = error_response(error.code, str(error))
            if isinstance(error, StorageUnavailable):
                body = response.json
                body['faultstring'] = STORAGE_UNAVAILABLE
                response.json = body
        except exc.HTTPException as error:
            response = error_response(error.status_int)
        except Exception as error:
            # Exception text and tracebacks can contain secrets.
            fault = engine_fault(error)
            if fault is not None and error.__traceback__ is not None:
                fault = fault.with_traceback(error.__traceback__)
            safe_log.log_failure(
                LOG,
                'API request failed: %(cause)s at %(where)s '
                'request_id=%(request_id)s',
                fault or error,
                request_id=environ.get('openstack.request_id'))
            response = error_response(503 if fault is not None else 500)
        else:
            if response.status_int >= 400:
                safe = error_response(response.status_int)
                # Preserve auth challenges, Allow, CORS, and version headers.
                for name, value in response.headerlist:
                    if name.lower() not in ('content-type', 'content-length'):
                        safe.headers.add(name, value)
                response = safe
        return response(environ, start_response)


def factory(global_conf, **local_conf):
    return ErrorMiddleware
