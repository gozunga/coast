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

"""Read-only orphan candidates for operators; never a deletion inventory."""

import re
from urllib import parse

from coast.common import clients
from coast.common import context
from coast.common import exception
from coast.common import naming
from coast.db import api as db_api
from coast.db import models


_UUID = re.compile(
    r'(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])', re.IGNORECASE)
_LISTS = (
    ('server', 'nova', 'compute', 'servers'),
    ('volume', 'cinder', 'block_storage', 'volumes'),
    ('snapshot', 'cinder', 'block_storage', 'snapshots'),
    ('port', 'neutron', 'network', 'ports'),
    ('network', 'neutron', 'network', 'networks'),
    ('subnet', 'neutron', 'network', 'subnets'),
    ('router', 'neutron', 'network', 'routers'),
    ('security_group', 'neutron', 'network', 'security_groups'),
    ('floating_ip', 'neutron', 'network', 'ips'),
    ('secret', 'barbican', 'key_manager', 'secrets'),
)


def _strings(value):
    """Read saved references, not contents or arbitrary cloud payloads."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _secret_ids(values):
    """Only secret reference UUIDs belong in the public operator report."""
    identifiers = set()
    for value in values:
        if '/secrets/' not in value:
            continue
        url = parse.urlsplit(value)
        identifier = url.path.rsplit('/secrets/', 1)[-1]
        if (url.scheme in ('http', 'https') and url.netloc and
                not url.query and not url.fragment and
                _UUID.fullmatch(identifier)):
            identifiers.add(identifier)
    return identifiers


def references(ctx):
    """Protect live ownership and durable references across tenant projects."""
    if not ctx.is_admin:
        raise exception.NotAuthorized()
    protected = set()
    with db_api.reader.using(ctx) as session:
        for model in (models.Stack, models.Server, models.Volume):
            query = session.query(model)
            if hasattr(model, 'deleted_at'):
                query = query.filter(model.deleted_at.is_(None))
            for row in query:
                protected.add(row.id)
                for column in model.__table__.columns:
                    if column.name.endswith(('_id', '_ref')):
                        value = getattr(row, column.name)
                        if value:
                            protected.add(value)
        # Retention receipts remain authoritative after stack soft deletion.
        for row in session.query(models.StackDeleteSnapshot):
            protected.add(row.cinder_snapshot_id)
        # Include every durable secret location and historical deployment spec.
        # Keep references on soft-deleted rows for recovery and audit as well.
        for model in (models.Stack, models.Service, models.Source,
                      models.Secret, models.FileMount, models.BackupPolicy,
                      models.Backup, models.BackupAccount, models.Deployment):
            for row in session.query(model):
                if not getattr(row, 'deleted_at', None):
                    protected.add(row.id)
                    if isinstance(row, models.BackupAccount):
                        protected.add(row.project_id)
                for column in model.__table__.columns:
                    if (column.name.endswith('_ref') or column.name in
                            ('spec', 'file_mount_secret_refs',
                             'deploy_key_rotation')):
                        protected.update(_strings(getattr(row, column.name)))
    # Barbican list identities are UUIDs, but Coast stores full secret refs.
    protected.update(_secret_ids(protected))
    return protected


def required_references(ctx):
    """Collect secret references still needed by live owners only."""
    if not ctx.is_admin:
        raise exception.NotAuthorized()
    required = set()
    with db_api.reader.using(ctx) as session:
        live_stacks = session.query(models.Stack.id).filter(
            models.Stack.deleted_at.is_(None))
        live_services = session.query(models.Service).filter(
            models.Service.deleted_at.is_(None),
            models.Service.stack_id.in_(live_stacks))
        service_ids = live_services.with_entities(models.Service.id)
        source_ids = live_services.with_entities(models.Service.source_id)
        queries = [
            session.query(models.Stack).filter(
                models.Stack.deleted_at.is_(None)),
            live_services,
            session.query(models.Source).filter(
                models.Source.id.in_(source_ids)),
            # Project accounts survive deletion of their last stack.
            session.query(models.BackupAccount),
        ]
        for model in (models.Secret, models.FileMount, models.BackupPolicy,
                      models.Backup, models.Deployment):
            query = session.query(model).filter(
                model.service_id.in_(service_ids))
            if hasattr(model, 'deleted_at'):
                query = query.filter(model.deleted_at.is_(None))
            queries.append(query)
        for query in queries:
            for row in query:
                for column in row.__table__.columns:
                    if (column.name.endswith('_ref') or column.name in
                            ('spec', 'file_mount_secret_refs',
                             'deploy_key_rotation')):
                        required.update(_strings(getattr(row, column.name)))
    return required


def secret_owners(ctx):
    """Existence is independent of indirect references from other DB rows."""
    if not ctx.is_admin:
        raise exception.NotAuthorized()
    owners = set()
    with db_api.reader.using(ctx) as session:
        for model in (models.Stack, models.Service, models.BackupPolicy,
                      models.BackupAccount):
            query = session.query(model)
            if hasattr(model, 'deleted_at'):
                query = query.filter(model.deleted_at.is_(None))
            for row in query:
                owners.add(row.id)
                if isinstance(row, models.BackupAccount):
                    owners.add(row.project_id)
    return owners


def _identity(kind, resource):
    if kind == 'secret':
        return (getattr(resource, 'secret_id', None) or
                parse.urlsplit(resource.secret_ref).path.rsplit('/', 1)[-1])
    return resource.id


def is_orphan(kind, resource, protected, prefix, owners=None):
    """Use exact markers; suppress candidates with any known live owner."""
    tags = set(getattr(resource, 'tags', None) or [])
    metadata = getattr(resource, 'metadata', None) or {}
    name = getattr(resource, 'name', None) or ''
    if kind == 'secret' and name.startswith('coast:'):
        parts = name.split(':')
        if len(parts) != 4:
            return True
        _, stack_id, secret_kind, owner_id = parts
        owners = protected if owners is None else owners
        return (_identity(kind, resource) not in protected or
                stack_id not in owners or owner_id not in owners)
    if not ('coast' in tags or metadata.get('coast') == 'true' or
            name.startswith(prefix + '-')):
        return False
    links = {_identity(kind, resource)}
    links.update(tag.split('=', 1)[1] for tag in tags
                 if tag.startswith(('environment_id=', 'stack_id=',
                                    'server_id=', 'volume_id=')))
    links.update(metadata.get(key) for key in (
        'environment_id', 'stack_id', 'server_id', 'volume_id', 'export_id'))
    links.update(_UUID.findall(name))
    # Neutron-created router/DHCP ports need not carry Coast tags. Marked
    # resources attached to live guests/networks/volumes are not orphans.
    links.update(getattr(resource, key, None) for key in (
        'device_id', 'network_id', 'router_id', 'port_id', 'volume_id',
        'snapshot_id'))
    for attachment in getattr(resource, 'attachments', None) or []:
        links.add(attachment.get('server_id'))
    return not links.intersection(protected)


def report(conf):
    """Complete all project-scoped lists before returning any candidates."""
    ctx = context.RequestContext(is_admin=True)
    project_id = clients.get_session(conf).get_project_id()
    if not project_id:
        raise exception.NotAuthorized()
    prefix = naming.resource_name('', conf=conf)
    if not prefix:
        raise ValueError('A resource name prefix is required')
    protected = references(ctx)
    initial_refs = _secret_ids(required_references(ctx))
    owners = secret_owners(ctx)
    inventory = []
    scanned = {}
    for kind, service, attribute, method in _LISTS:
        if service == 'barbican':
            proxy = clients.get_sensitive_proxy(service, conf)
        else:
            proxy = getattr(clients.get_connection(service, conf), attribute)
        if service == 'neutron':
            query = {'project_id': project_id}
        elif service in ('nova', 'cinder'):
            query = {'details': True, 'all_projects': False}
        else:
            query = {}
        resources = list(getattr(proxy, method)(**query))
        scanned[kind] = len(resources)
        inventory.extend((kind, resource) for resource in resources)
    # A create may have checkpointed new references while the cloud was read.
    # Keep both observations; a concurrent delete is not an orphan this run.
    final_refs = references(ctx)
    protected.update(final_refs)
    owners.update(secret_owners(ctx))
    orphans = [{'resource_type': kind, 'id': _identity(kind, resource)}
               for kind, resource in inventory
               if is_orphan(kind, resource, protected, prefix, owners)]
    present = {_identity(kind, resource) for kind, resource in inventory
               if kind == 'secret'}
    final_required = _secret_ids(required_references(ctx))
    missing = initial_refs.intersection(final_required) - present
    return {'complete': True, 'scanned': scanned,
            'missing_secrets': sorted(missing),
            'orphans': sorted(orphans, key=lambda row: (
                row['resource_type'], row['id']))}
