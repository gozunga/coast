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

"""Bounded Compose parsing for secret storage and storage accounting."""

import re

from oslo_config import cfg
import yaml

from coast.common import exception
from coast.common import secret_limits


NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z')
DOCKER_SOCKET = '/var/run/docker.sock'


def invalid(service, key, reason):
    # Values and parser exception strings can contain credentials.
    raise exception.Invalid('Compose service %s, key %s: %s.' % (
        service, key, reason)) from None


class Loader(yaml.SafeLoader):
    """Reject aliases and duplicate keys instead of ambiguous expansion."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError()
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = super().construct_mapping(node, deep=deep)
        if len(result) != len(node.value):
            raise ValueError()
        return result


def mapping(value, service, key):
    if not isinstance(value, dict):
        invalid(service, key, 'must be a mapping')
    return value


def parse(raw, *, max_bytes=None):
    """Check document structure without restricting worker workloads."""
    if not isinstance(raw, str):
        invalid('<document>', 'yaml', 'must be text')
    if max_bytes is None:
        secret_limits.validate(raw, 'Compose content',
                               cfg.CONF.api.max_compose_bytes)
    else:
        # Explicit caps are for parsing observed engine documents, which
        # need not be stored as secrets (for example mount inspection).
        try:
            size = len(raw.encode('utf-8'))
        except UnicodeError:
            invalid('<document>', 'yaml', 'invalid UTF-8')
        if size > max_bytes:
            invalid('<document>', 'yaml', 'exceeds %d bytes' % max_bytes)
    try:
        document = yaml.load(raw, Loader=Loader)  # nosec: SafeLoader subclass
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        invalid('<document>', 'yaml', 'invalid YAML, duplicate key or alias')
    mapping(document, '<document>', 'yaml')
    services = mapping(document.get('services'), '<document>', 'services')
    if not services:
        invalid('<document>', 'services', 'must not be empty')
    volumes = mapping(document.get('volumes', {}), '<document>', 'volumes')
    for name in volumes:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            invalid('<document>', 'volumes', 'invalid volume name')
    for name, service in services.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            invalid('<document>', 'services', 'invalid service name')
        mapping(service, name, 'services')
        mounts = service.get('volumes', [])
        if not isinstance(mounts, list):
            invalid(name, 'volumes', 'must be a list')
        for mount in mounts:
            if isinstance(mount, str):
                continue
            if not isinstance(mount, dict) or not isinstance(
                    mount.get('target'), str):
                invalid(name, 'volumes', 'must be mount text or a mapping')
    return document


def mount_targets(document):
    """Yield container targets from a parsed document.

    A short-syntax string yields a target only when it contains
    ``source:target``. A single token has no clear container path.
    """
    for service in document['services'].values():
        for mount in service.get('volumes', []):
            if isinstance(mount, str):
                parts = mount.split(':')
                # Bare names such as "data" are not container paths. Overlap
                # checks normalize them to "/data" and collide with real
                # targets.
                if len(parts) > 1:
                    yield parts[1]
            else:
                yield mount['target']
