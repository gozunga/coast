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

"""References owned by an app, including immutable deployment snapshots."""

from coast.common import exception


def spec_refs(spec):
    spec = spec or {}
    refs = {item['value_ref'] for item in spec.get('env', {}).values()
            if item.get('value_ref')}
    refs.update(item['content_ref'] for item in spec.get('file_mounts', [])
                if item.get('content_ref'))
    if spec.get('dockerfile_ref'):
        refs.add(spec['dockerfile_ref'])
    if spec.get('compose_ref'):
        refs.add(spec['compose_ref'])
    return refs


def rows(cls, ctx, service_id):
    marker = None
    while True:
        page = cls.get_all(ctx, filters={'service_id': service_id},
                           marker=marker, sort_key='id')
        if not page:
            return
        yield from page
        marker = page[-1].id


def references(ctx, service):
    from coast import objects

    refs = set(service.file_mount_secret_refs or [])
    for name in ('password_ref', 'root_password_ref'):
        reference = getattr(service, name, None)
        if reference:
            refs.add(reference)
    try:
        source = objects.Source.get_by_id(ctx, service.source_id)
    except exception.NotFound:
        source = None
    if source and source.credentials_ref:
        refs.add(source.credentials_ref)
    if source and source.deploy_key_rotation:
        pending = source.deploy_key_rotation
        refs.update(pending[key] for key in ('reference', 'previous')
                    if pending.get(key))
    if service.compose_ref:
        refs.add(service.compose_ref)
    if service.dockerfile_ref:
        refs.add(service.dockerfile_ref)
    refs.update(row.value_ref for row in
                rows(objects.SecretList, ctx, service.id))
    refs.update(row.content_ref for row in
                rows(objects.FileMountList, ctx, service.id))
    for deployment in rows(objects.DeploymentList, ctx, service.id):
        refs.update(spec_refs(deployment.spec))
    return sorted(refs)
