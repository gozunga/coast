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

"""Customer storage: bind paths, volume capacity, and template mounts."""

import posixpath
from types import SimpleNamespace
import uuid

from oslo_config import cfg

from coast.common import exception
from coast.common import named_volumes
from coast.common import quota
from coast.common import states
from coast import objects


def _docker_disk(ctx, server_id):
    rows = objects.VolumeList.get_all(
        ctx, filters={'server_id': server_id, 'purpose': 'docker'})
    live = [row for row in rows if row.status != states.DELETING]
    if len(live) != 1:
        raise exception.Conflict()
    return live[0]


def reserved_on_server(ctx, server_id, ignore_id=None):
    """Return the accounted GiB already stored for one worker."""
    total = 0
    for row in objects.StorageList.get_all(
            ctx, filters={'server_id': server_id}):
        if ignore_id and row.id == ignore_id:
            continue
        total += row.size_gb or 0
    return total


def ensure_capacity(ctx, server_id, extra_gb, ignore_id=None):
    """Reject an attach that does not fit on the worker Docker disk."""
    disk = _docker_disk(ctx, server_id)
    used = reserved_on_server(ctx, server_id, ignore_id=ignore_id)
    if used + extra_gb > disk.size_gb:
        raise exception.Conflict()


def storage_name(service, volume_name):
    """Return a stack-unique name for a template volume."""
    candidate = '%s-%s' % (service.name, volume_name)
    if volume_name_ok(candidate):
        return candidate
    prefix = service.id.replace('-', '')[:8]
    candidate = '%s-%s' % (prefix, volume_name)
    if volume_name_ok(candidate):
        return candidate
    raise exception.Invalid()


def volume_name_ok(name):
    return named_volumes.volume_name_ok(name) and len(name) <= 63


def _release(ctx, rows):
    total = sum(row.size_gb or 0 for row in rows)
    if total:
        quota.release(ctx, storage_gb=total)
    for row in rows:
        row.destroy()


def drop_service_storages(ctx, service):
    """Release template rows and attached rows. Keep detached api rows."""
    rows = list(objects.StorageList.get_all(
        ctx, filters={'service_id': service.id}))
    doomed = [row for row in rows if row.origin == 'template' or
              row.status == states.IN_USE]
    _release(ctx, doomed)


def drop_stack_storages(ctx, stack):
    """Release and remove every storage on a stack that is going away."""
    rows = list(objects.StorageList.get_all(
        ctx, filters={'stack_id': stack.id}))
    _release(ctx, rows)


def create_template_storages(ctx, service, server, template_id, version):
    """Create IN_USE accounting rows for template named volumes."""
    declared = named_volumes.for_template(version, template_id)
    if declared is None:
        return
    volumes = declared.get('volumes') or []
    if not volumes:
        return
    size = cfg.CONF.controller_worker.template_storage_gb
    ensure_capacity(ctx, server.id, size * len(volumes))
    held = quota.reserve(ctx, storage_gb=size * len(volumes))
    for item in volumes:
        objects.Storage(
            context=ctx, stack_id=service.stack_id,
            name=storage_name(service, item['name']), size_gb=size,
            status=states.IN_USE, origin='template',
            service_id=service.id, server_id=server.id,
            container_path=item['container_path']).create()
    quota.commit(ctx, held)


def account_compose_volumes(ctx, service, document):
    """Charge new compose volumes; retained Docker data keeps its charge."""
    existing = {row.name for row in objects.StorageList.get_all(
        ctx, filters={'service_id': service.id})}
    managed = {mount.get('source') for item in
               document.get('services', {}).values() for mount in
               item.get('volumes', []) if isinstance(mount, dict) and
               mount.get('x-coast-storage')}
    names = [storage_name(service, name)
             for name in document.get('volumes', {}) if name not in managed]
    added = [name for name in names if name not in existing]
    if not added:
        return
    size = cfg.CONF.controller_worker.template_storage_gb
    ensure_capacity(ctx, service.server_id, size * len(added))
    held = quota.reserve(ctx, storage_gb=size * len(added))
    for name in added:
        objects.Storage(
            context=ctx, stack_id=service.stack_id, name=name, size_gb=size,
            status=states.IN_USE, origin='template', service_id=service.id,
            server_id=service.server_id).create()
    quota.commit(ctx, held)


def _all_rows(list_class, ctx, filters):
    rows = []
    marker = None
    while True:
        page = list(list_class.get_all(
            ctx, filters=filters, marker=marker, sort_key='id'))
        rows.extend(page)
        if not page or len(page) < cfg.CONF.api.max_limit:
            return rows
        marker = page[-1].id


def observed_rows(ctx, stack_id, service_id=None):
    """Merge accounting with safe engine observations; never write on GET."""
    from coast.conductor import rpcapi

    rows = _all_rows(objects.StorageList, ctx, {'stack_id': stack_id})
    service_filters = {'stack_id': stack_id}
    if service_id:
        service_filters['id'] = service_id
        rows = [row for row in rows if row.service_id == service_id]
    for service in _all_rows(objects.ServiceList, ctx, service_filters):
        if not service.engine_ref:
            continue
        observed = rpcapi.ConductorAPI().list_volume_mounts(
            ctx, service.id, stack_id=stack_id)
        owned = [row for row in rows if row.service_id == service.id]
        # Template accounts may outlive removed compose declarations.
        rows = [row for row in rows if row.service_id != service.id or
                row.origin != 'template']
        for mount in observed:
            if mount.get('type', 'volume') != 'volume':
                continue
            name, path = mount['name'], mount['container_path']
            if not isinstance(name, str):
                continue
            matched = next((row for row in owned if row.name and
                            (row.container_path == path or
                             row.container_path is None) and
                            (name == row.name or
                             name.endswith('-' + row.name) or
                             row.origin == 'template' and
                             (row.container_path == path or
                              row.name in (
                                  service.name + '-' + name,
                                  service.id.replace('-', '')[:8] + '-' +
                                  name)))), None)
            if matched is not None and not any(
                    row.id == matched.id for row in rows
                    if row.origin == 'template'):
                if matched.origin == 'api':
                    continue
                row = matched.obj_clone()
                row.container_path = path
            else:
                identity = str(uuid.uuid5(
                    uuid.UUID(service.id), name + ':' + path))
                values = dict(size_gb=None, id=identity,
                              project_id=service.project_id,
                              stack_id=stack_id, service_id=service.id,
                              server_id=service.server_id, name=name,
                              container_path=path, origin='template',
                              status=states.IN_USE,
                              created_at=service.created_at,
                              updated_at=service.updated_at)
                row = SimpleNamespace(**values)
            if not any(item.id == row.id and
                       item.container_path == path for item in rows):
                rows.append(row)
    return rows


def host_path_ok(path):
    """Accept normalized POSIX paths on a dedicated worker."""
    try:
        if not isinstance(path, str):
            return False
        path.encode('utf-8')
    except UnicodeError:
        return False
    return (path.startswith('/') and not path.startswith('//') and
            len(path) <= 4096 and
            posixpath.normpath(path) == path and
            not any(ord(char) < 32 or ord(char) == 127 or
                    char in ':\\' for char in path) and
            all(part not in ('.', '..') for part in path.split('/')))


def typed_rows(ctx, stack_id, service_id=None):
    """Return user desired mounts and every observed template mount."""
    filters = {'stack_id': stack_id}
    if service_id:
        filters['service_id'] = service_id
    rows = [row for row in _all_rows(objects.StorageList, ctx, filters)
            if row.origin == 'api']
    from coast.conductor import rpcapi
    services = {'stack_id': stack_id}
    if service_id:
        objects.Service.get_by_id(ctx, service_id)
        services['id'] = service_id
    for service in _all_rows(objects.ServiceList, ctx, services):
        if not service.engine_ref:
            rows.extend(row for row in _all_rows(
                objects.StorageList, ctx, {'service_id': service.id})
                        if row.origin == 'template')
            continue
        mounts = rpcapi.ConductorAPI().list_volume_mounts(
            ctx, service.id, stack_id=stack_id)
        for index, mount in enumerate(mounts):
            path = mount['container_path']
            if any(row.service_id == service.id and
                   row.container_path == path for row in rows):
                continue
            source = mount.get('source', mount.get('name'))
            kind = mount.get('type', 'volume')
            identity = str(uuid.uuid5(
                uuid.UUID(service.id), '%s:%s:%s' % (source, path, index)))
            rows.append(SimpleNamespace(
                id=identity, project_id=service.project_id, stack_id=stack_id,
                service_id=service.id, server_id=service.server_id,
                name=source if kind == 'volume' else None,
                host_path=source if kind == 'bind' else None,
                type=kind, read_only=mount.get('read_only', False),
                size_gb=None, container_path=path, origin='template',
                status=states.IN_USE, created_at=service.created_at,
                updated_at=service.updated_at))
    return rows
