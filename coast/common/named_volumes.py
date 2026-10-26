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

"""Named storage accounting from unrestricted worker Compose."""

import base64
import binascii
from importlib import resources
import json
import re

from coast.common import compose
from coast.common import engine_versions
from coast.common import exception


_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$')
_PART = re.compile(r'[A-Za-z0-9._-]+')


def volume_name_ok(name):
    return isinstance(name, str) and bool(_NAME.fullmatch(name))


def container_ok(path):
    """Accept normalized absolute container paths."""
    if not isinstance(path, str) or not path.startswith('/') or path == '/':
        return False
    parts = path.split('/')[1:]
    return (bool(parts) and not any(part in ('', '.', '..') for part in parts)
            and all(_PART.fullmatch(part) for part in parts))


def _mount(item):
    if isinstance(item, str):
        parts = item.split(':')
        if len(parts) not in (2, 3):
            return None
        if len(parts) == 3 and 'ro' in parts[2].split(','):
            return None
        source, target = parts[:2]
    else:
        if item.get('type') != 'volume' or item.get('read_only'):
            return None
        source, target = item.get('source'), item.get('target')
    if not volume_name_ok(source) or not container_ok(target):
        return None
    return {'name': source, 'container_path': target}


def parse_compose(text):
    """Extract named storage metadata; binds do not consume Coast accounts."""
    try:
        document = compose.parse(text, max_bytes=16 * 1024 * 1024)
    except exception.Invalid:
        return None
    found = {}
    docker_socket = False
    for service in document['services'].values():
        for item in service.get('volumes', []):
            if isinstance(item, str):
                parts = item.split(':')
                source = parts[0] if len(parts) > 1 else None
                target = parts[1] if len(parts) > 1 else parts[0]
            else:
                source, target = item.get('source'), item.get('target')
            if source == target == compose.DOCKER_SOCKET:
                docker_socket = True
            mount = _mount(item)
            if mount is not None:
                found.setdefault(mount['name'], mount['container_path'])
    if not found and not docker_socket:
        return None
    result = {'volumes': [
        {'name': name, 'container_path': found[name]}
        for name in sorted(found)]}
    if docker_socket:
        result['docker_socket'] = True
    return result


def _compose_text(template):
    raw = template.get('compose') or ''
    if not raw:
        return ''
    try:
        return base64.b64decode(raw, validate=True).decode('utf-8')
    except (binascii.Error, UnicodeError, ValueError):
        return None


def volume_index(source):
    """Return ``{template_id: declaration}`` for templates that mount data."""
    if not isinstance(source, dict):
        raise ValueError('Invalid template source')
    result = {}
    for template_id, template in sorted(source.items()):
        if not isinstance(template, dict):
            continue
        text = _compose_text(template)
        if text == '':
            continue
        if text is None:
            continue
        declared = parse_compose(text)
        if declared is not None:
            result[template_id] = declared
    return result


def _load(version):
    target = resources.files('coast.drivers.coolify').joinpath(
        'templates', 'volumes-%s.json' % engine_versions.TEMPLATE_VERSION)
    try:
        return json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('Volume index cannot be read') from None


def template_index(version):
    """Return the pinned volume index, or None for an unknown version."""
    from coast.drivers.coolify import catalog
    return _load(version) if version in catalog.VERSIONS else None


def for_template(version, template_id):
    """Return the declaration for one template.

    ``None`` means this engine version has no index. Callers skip
    storage rows in that case. A missing id means the template declares
    no volumes.
    """
    index = template_index(version)
    if index is None:
        return None
    item = index.get(template_id)
    if item is None:
        return {'volumes': []}
    return item
