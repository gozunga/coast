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

"""Container-only config file validation and lifecycle helpers."""

import posixpath

from coast.api import secret_store
from coast.common import compose
from coast.common import exception
from coast.common import named_volumes
from coast.common import states
from coast import objects


MAX_BYTES = 64 * 1024


def path_ok(path):
    return named_volumes.container_ok(path) and len(path) <= 255


def validate(values):
    if 'container_path' in values and not path_ok(values['container_path']):
        raise exception.Invalid()
    if 'content' in values:
        try:
            content = values['content']
            if '\x00' in content or len(content.encode('utf-8')) > MAX_BYTES:
                raise ValueError()
        except (UnicodeError, ValueError):
            raise exception.Invalid() from None


def rows(ctx, service_id):
    marker = None
    while True:
        page = objects.FileMountList.get_all(
            ctx, filters={'service_id': service_id}, marker=marker,
            sort_key='id')
        yield from page
        if not page:
            return
        marker = page[-1].id


def overlaps(left, right):
    # Compose accepts root, repeated and trailing separators in targets.
    left = posixpath.normpath('/' + left.lstrip('/'))
    right = posixpath.normpath('/' + right.lstrip('/'))
    return (left == right or left.startswith(right.rstrip('/') + '/') or
            right.startswith(left.rstrip('/') + '/'))


def check_free(ctx, service_id, path, ignore_id=None):
    service = objects.Service.get_by_id(ctx, service_id)
    if service.compose_ref:
        document = compose.parse(secret_store.call(
            'get', ctx, service.compose_ref))
        if any(overlaps(path, target) for target in
               compose.mount_targets(document)):
            raise exception.Conflict()
    if any(row.id != ignore_id and overlaps(path, row.container_path)
           for row in rows(ctx, service_id)):
        raise exception.Conflict()
    marker = None
    while True:
        page = objects.StorageList.get_all(
            ctx, filters={'service_id': service_id, 'status': states.IN_USE},
            marker=marker, sort_key='id')
        if any(row.container_path and overlaps(path, row.container_path)
               for row in page):
            raise exception.Conflict()
        if not page:
            return
        marker = page[-1].id


def drop(ctx, service, delete_secrets=True):
    if delete_secrets:
        for ref in service.file_mount_secret_refs or []:
            secret_store.call('delete', ctx, ref)
    for row in list(rows(ctx, service.id)):
        row.destroy()


def snapshot(ctx, service_id):
    return [{key: getattr(row, key) for key in
             ('id', 'container_path', 'content_ref', 'resource_name')}
            for row in rows(ctx, service_id)]


def check_compose(ctx, service_id, document):
    targets = list(compose.mount_targets(document))
    if any(overlaps(row.container_path, path)
           for row in rows(ctx, service_id) for path in targets):
        raise exception.Conflict()
