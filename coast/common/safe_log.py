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

"""Sanitized failure descriptions for operator logs.

Never interpolate ``str(exc)``, ``exc.details``, response bodies, header
sets, or traceback locals. Those values can contain credentials.
"""

import json
import os
import re
import traceback
import urllib.parse

from keystoneauth1.exceptions import http as ks_http
from openstack import exceptions as sdk_exceptions
from oslo_utils import strutils


# coast/common/safe_log.py -> package directory, then its parent.
_COAST_DIR = os.path.realpath(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TREE = os.path.dirname(_COAST_DIR)

_CLASS = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*\Z')
_FIELD = re.compile(r'^[A-Za-z0-9_.:-]{1,128}\Z')
_METHOD = re.compile(r'^[A-Za-z]{1,16}\Z')
_NAME = re.compile(r'^[A-Za-z0-9_<>]+\Z')
_PATH = re.compile(r'^[A-Za-z0-9_./-]+\Z')
_REQUEST_ID = re.compile(r'^[A-Za-z0-9._-]{1,200}\Z')
_HTTP = (sdk_exceptions.HttpException, ks_http.HttpError)


def describe(exc):
    """Return ``(cause, location)`` for an operator log line.

    ``cause`` is one line. HTTP errors from openstacksdk or keystoneauth
    add ``status_code``, method, URL, and request id when those values
    are safe. The URL has no userinfo, query string, or fragment. Every
    other exception contributes only its class path. ``location`` is the
    innermost Coast frame as ``file:line function``, or ``-``.
    """
    try:
        cause = _cause(exc)
        where = _where(exc)
    except Exception:
        return ('unavailable', '-')
    if not isinstance(cause, str) or any(char in cause for char in '\r\n'):
        cause = 'unavailable'
    if not isinstance(where, str) or any(char in where for char in '\r\n'):
        where = '-'
    return cause, where


def log_failure(logger, message, exc, **fields):
    """Write one ERROR line. ``message`` must use ``cause`` and ``where``."""
    cause, where = describe(exc)
    values = {key: _field(value) for key, value in fields.items()}
    values['cause'] = cause
    values['where'] = where
    logger.error(message, values)


# Static segments of the Coolify routes Coast calls. Any other segment
# is an id, a name, or a secret.
_API_WORDS = frozenset({
    'appgroups', 'bulk', 'cancel', 'deploy', 'deployments',
    'dockerimage', 'environments', 'envs', 'health', 'keys', 'logs',
    'private-deploy-key', 'projects', 'proxy', 'public', 'registries',
    'restart', 'rollback', 'security', 'servers', 'services', 'start',
    'stop', 'storages', 'team', 'validate', 'version',
})
_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,:'()/_-]{0,159}\Z")
_WORD = re.compile(r'^[A-Za-z][A-Za-z.]{0,40}\Z')
_ERROR_KEY = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,63}\Z')
# A hyphenated word is an id or a credential unless it is this label.
_HYPHEN_WORD = frozenset({'Content-Type'})
_SECRET_TEXT = re.compile(
    r'-----BEGIN|OPENSSH|ssh-rsa|ssh-ed25519|Bearer ',
    re.IGNORECASE)
_FAULT_EMPTY = {
    'method': '-',
    'path': '-',
    'status': '-',
    'message': '-',
    'error_keys': '-',
    'detail': '-',
}


def http_fault(method, path, status, payload):
    """Return safe fields for one HTTP client 4xx log line.

    The path keeps only static API words. Every other segment becomes
    ``{id}``. ``payload`` contributes the JSON ``message``, validation
    error key names, and short safe error text. A token, a private key,
    or the raw body is not returned.
    """
    try:
        return _http_fault(method, path, status, payload)
    except Exception:
        return dict(_FAULT_EMPTY)


def _cause(exc):
    path = _class_path(exc)
    if not isinstance(exc, _HTTP):
        return path
    status, method, url, request_id = _http_fields(exc)
    parts = [path]
    if status is not None:
        parts.append('status_code=%d' % status)
    if method:
        parts.append('method=%s' % method)
    if url:
        parts.append('url=%s' % url)
    if request_id:
        parts.append('request_id=%s' % request_id)
    return ' '.join(parts)


def _class_path(exc):
    cls = type(exc)
    module = getattr(cls, '__module__', None)
    name = getattr(cls, '__qualname__', None)
    if not isinstance(module, str) or not isinstance(name, str):
        return 'Exception'
    text = '%s.%s' % (module, name)
    if len(text) > 200 or _CLASS.fullmatch(text) is None:
        return 'Exception'
    return text


def _http_fields(exc):
    status = _status(getattr(exc, 'status_code', None))
    if status is None:
        status = _status(getattr(exc, 'http_status', None))
    method = _method(getattr(exc, 'method', None))
    url = _safe_url(getattr(exc, 'url', None))
    request_id = _request_id(getattr(exc, 'request_id', None))
    response = getattr(exc, 'response', None)
    if response is None:
        return status, method, url, request_id
    if status is None:
        status = _status(getattr(response, 'status_code', None))
    if request_id is None:
        headers = getattr(response, 'headers', None)
        getter = getattr(headers, 'get', None)
        if getter is not None:
            request_id = _request_id(getter('x-openstack-request-id'))
    request = getattr(response, 'request', None)
    if request is not None:
        if method is None:
            method = _method(getattr(request, 'method', None))
        if url is None:
            url = _safe_url(getattr(request, 'url', None))
    return status, method, url, request_id


def _status(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 100 <= value <= 599:
        return value
    return None


def _method(value):
    if isinstance(value, str) and _METHOD.fullmatch(value):
        return value.upper()
    return None


def _request_id(value):
    if isinstance(value, str) and _REQUEST_ID.fullmatch(value):
        return value
    return None


def _safe_url(url):
    """Return scheme, host, port, and path. Drop userinfo and the query."""
    if not isinstance(url, str) or not url:
        return None
    if any(char in url for char in '\r\n\x00'):
        return None
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not parts.scheme or not host:
        return None
    if ':' in host:
        host = '[%s]' % host
    netloc = host
    if parts.port:
        netloc = '%s:%d' % (netloc, parts.port)
    safe = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, '', ''))
    if (not safe or len(safe) > 300 or
            any(char in safe for char in '\r\n\x00 ?@#')):
        return None
    return safe


def _where(exc):
    tb = getattr(exc, '__traceback__', None)
    if tb is None:
        return '-'
    chosen = None
    # extract_tb does not capture locals or the exception text.
    for frame in traceback.extract_tb(tb):
        if _is_coast(frame.filename):
            chosen = frame
    if chosen is None or not isinstance(chosen.lineno, int):
        return '-'
    if chosen.lineno < 1 or not isinstance(chosen.name, str):
        return '-'
    if _NAME.fullmatch(chosen.name) is None:
        return '-'
    path = _relative(chosen.filename)
    if path is None:
        return '-'
    return '%s:%d %s' % (path, chosen.lineno, chosen.name)


def _is_coast(filename):
    if not isinstance(filename, str) or not filename:
        return False
    try:
        path = os.path.realpath(filename)
    except (OSError, ValueError):
        return False
    return path == _COAST_DIR or path.startswith(_COAST_DIR + os.sep)


def _relative(filename):
    try:
        rel = os.path.relpath(
            os.path.realpath(filename), _TREE).replace(os.sep, '/')
    except (OSError, ValueError):
        return None
    if (rel.startswith('../') or '/../' in rel or
            _PATH.fullmatch(rel) is None):
        return None
    return rel


def _field(value):
    if isinstance(value, str) and _FIELD.fullmatch(value):
        return value
    return '-'


def _http_fault(method, path, status, payload):
    code = _status(status)
    message, keys, detail = _payload_fields(payload)
    return {
        'method': _method(method) or '-',
        'path': _api_path(path),
        'status': str(code) if code is not None else '-',
        'message': message,
        'error_keys': keys,
        'detail': detail,
    }


def _api_path(path):
    if not isinstance(path, str) or not path.startswith('/'):
        return '-'
    if any(char in path for char in ' ?#@\\\r\n\x00'):
        return '-'
    parts = [part for part in path.split('/') if part]
    if not parts or any(part in ('.', '..') for part in parts):
        return '-'
    rendered = '/' + '/'.join(
        part if part in _API_WORDS else '{id}' for part in parts)
    if len(rendered) > 200:
        return '-'
    return rendered


def _public_text(value):
    """Return short error text, or None when the text can carry a secret."""
    if not isinstance(value, str) or len(value) > 160:
        return None
    if any(char in value for char in '\r\n\x00'):
        return None
    if _SECRET_TEXT.search(value) or 'secret' in value.lower():
        return None
    masked = strutils.mask_password(value)
    if masked != value or _TEXT.fullmatch(masked) is None:
        return None
    if ' ' not in masked:
        if _WORD.fullmatch(masked) is None:
            return None
    elif not _hyphen_words_ok(masked):
        return None
    return masked


def _hyphen_words_ok(text):
    for word in text.split():
        bare = word.strip('.,:;')
        if '-' in bare and bare not in _HYPHEN_WORD:
            return False
    return True


def _payload_fields(payload):
    message = '-'
    keys = '-'
    detail = '-'
    if not isinstance(payload, dict):
        return message, keys, detail
    text = _public_text(payload.get('message'))
    if text:
        message = text
    errors = payload.get('errors')
    if isinstance(errors, dict):
        names, parts = _error_items(errors)
        if names:
            keys = ','.join(names)
        if parts:
            detail = _bounded_detail(parts)
    else:
        extra = _public_text(payload.get('error'))
        if extra:
            detail = extra
    return message, keys, detail


def _error_items(errors):
    names = []
    parts = []
    for key in sorted(errors):
        if not isinstance(key, str) or _ERROR_KEY.fullmatch(key) is None:
            continue
        names.append(key)
        value = errors[key]
        texts = value if isinstance(value, list) else (value,)
        for item in texts:
            safe = _public_text(item)
            if safe:
                parts.append(safe)
    if len(','.join(names)) > 200:
        return [], parts
    return names, parts


def _bounded_detail(parts):
    joined = '; '.join(parts)
    if len(joined) <= 160:
        return joined
    return parts[0]


def log_failover(logger, level, server, event, reason, **extra):
    """Log masked, bounded fields with names escaped onto one line."""
    fields = dict(stack_id=server.stack_id, server_id=server.id,
                  event=event, reason=reason, server_name=server.name,
                  role=server.role, compute_id=server.compute_id,
                  last_heartbeat_at=server.last_heartbeat_at)
    fields.update(extra)
    rendered = []
    for key, value in fields.items():
        value = strutils.mask_password(str(value))[:256]
        # Simple identifiers stay grep-friendly; quote names and whitespace.
        if (key == 'server_name' or
                not re.fullmatch(r'[a-zA-Z0-9_.:-]+', value)):
            value = json.dumps(value, ensure_ascii=True)
        rendered.append('%s=%s' % (key, value))
    getattr(logger, level)('Failover: %s', ' '.join(rendered))
