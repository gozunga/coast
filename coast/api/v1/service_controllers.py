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

"""Project-scoped service endpoints using the M1 no-op handoff seam."""

import contextlib
import copy
import datetime
import ipaddress
import math
import secrets
from urllib import parse

from oslo_config import cfg
from oslo_utils import timeutils
import pecan

from coast.api import handlers
from coast.api import microversions
from coast.api import secret_store
from coast.api.v1 import console_controllers
from coast.api.v1 import dns_controllers
from coast.api.v1 import schemas
from coast.api.v1 import stack_controllers as common
from coast.api.v1 import template_controllers
from coast.api.v1 import validation
from coast.api import webhooks
from coast.common import compose as compose_rules
from coast.common import context as context_module
from coast.common import exception
from coast.common import file_mounts
from coast.common import naming
from coast.common import notifications
from coast.common import policy
from coast.common import secret_limits
from coast.common import secrets as key_secrets
from coast.common import states
from coast.common import storage as storage_rules
from coast.conductor import deploy_keys
from coast.db import api as db_api
from coast.db import metrics
from coast.network import customer_dns
from coast.network import domain_check
from coast import objects

SERVER_REQUIRED = ('server_id is required: a service must be placed on a '
                   'worker server in the app group\'s stack.')


TIMESTAMPS = ('created_at', 'updated_at')
APP_FIELDS = ('id', 'project_id', 'stack_id', 'environment_id',
              'template_id', 'name', 'description',
              'server_id', 'ports', 'replicas', 'cpu_limit', 'memory_limit_mb',
              'healthcheck_path', 'health_check', 'health_check_port',
              'auto_deploy', 'status',
              'current_deployment_id') + TIMESTAMPS
DEP_FIELDS = ('id', 'project_id', 'service_id', 'number', 'source_ref',
              'server_id',
              'trigger', 'rollback_of', 'status', 'fault', 'started_at',
              'finished_at') + TIMESTAMPS
DOMAIN_FIELDS = ('id', 'project_id', 'service_id', 'fqdn',
                 'verified_at', 'tls', 'auto_dns', 'dns_mode',
                 'status') + TIMESTAMPS
EVENT_FIELDS = ('id', 'project_id', 'stack_id', 'resource_type',
                'resource_id', 'action', 'outcome', 'message',
                'request_id') + TIMESTAMPS
SOURCE_FIELDS = ('type', 'repo_url', 'branch', 'build_pack', 'dockerfile_path',
                 'compose_path', 'base_directory')


def workload_kind(source_type, requested):
    """Classify new workloads using the negotiated create semantics."""
    if source_type == 'dockerfile':
        if requested not in (None, 'app'):
            raise exception.Invalid('Dockerfile requires app kind.')
        return 'app'
    if source_type == 'compose':
        return requested or 'service'
    if microversions.at_least(
            _request_version(), microversions.APP_KIND_DEFAULT_VERSION):
        return requested or ('service' if source_type == 'template' else 'app')
    new = microversions.at_least(_request_version(), '1.11')
    if requested is not None and not new:
        raise exception.Invalid('kind requires API version 1.11.')
    if source_type == 'template':
        kind = 'service'
    elif source_type == 'image':
        kind = requested or ('service' if new else 'app')
    else:
        kind = 'app'
    if requested is not None and requested != kind:
        raise exception.Invalid('A %s source cannot create a %s.'
                                % (source_type, requested))
    return kind


def source_spec(source):
    """Return deployment settings independent of API response metadata."""
    if source.type in ('dockerfile', 'compose'):
        return {'type': source.type}
    if source.type == 'image':
        image, tag = source.image.rsplit(':', 1)
        return {'type': 'image', 'image': image, 'tag': tag}
    return values(source, SOURCE_FIELDS)


def public_source(source):
    result = source_spec(source)
    public = naming.optional_attr(source, 'deploy_public_key')
    if public and microversions.at_least(
            _request_version(), microversions.DEPLOY_KEY_VERSION):
        result['deploy_key'] = {'public_key': public,
                                'fingerprint': key_secrets.key_fingerprint(
                                    public)}
    return result


def url(path):
    return (cfg.CONF.api.public_endpoint or
            pecan.request.host_url).rstrip('/') + '/v1' + path


def values(row, names):
    result = {}
    for name in names:
        value = getattr(row, name)
        if isinstance(value, datetime.datetime):
            value = value.isoformat().replace('+00:00', 'Z')
        result[name] = value
    if isinstance(row, objects.Event) and row.action == 'delete.cleanup':
        result['message'] = None  # Internal receipt; no Cinder ID disclosure.
    return result


def stack_open(ctx, stack_id):
    """Q145: service work needs no stack lock, only a non-exclusive stack.

    A short row lock orders this check against exclusive stack tasks
    (delete, upgrade, failover), which check for busy services in turn.
    """
    stack = objects.Stack.obj_from_db(
        ctx, db_api.stacks._get(ctx, stack_id, lock=True))
    if stack.task_state not in (states.NONE, states.RECONCILING):
        raise exception.Conflict()


def claim_service(ctx, service, op, owner=None):
    """Lock the stack before claiming its service, as repository writes do."""
    stack_open(ctx, service.stack_id)
    service.acquire_task(op, owner=owner)


def settings_lock(ctx, service):
    """Row-lock a service for a DB-only settings change.

    The task state is kept, so a running deploy is not blocked; only a
    delete refuses the change.
    """
    db_api.stacks._get(ctx, service.stack_id, lock=True)
    db_api.cas_task_state(ctx, objects.Service.repository.model, service.id,
                          new=service.task_state,
                          expected=service.task_state)
    if states.DELETING in (service.status, service.task_state):
        raise exception.Conflict('This app is being deleted.')


AFFECTING = ('ports', 'replicas', 'cpu_limit', 'memory_limit_mb',
             'healthcheck_path', 'health_check', 'health_check_port')
# A full snapshot() always stores both. A partial spec keeps the timestamp.
_SNAPSHOT_KEYS = ('ports', 'source')


def _env_view(ctx, service_id):
    return {
        row.key: {'value_ref': row.value_ref, 'version': row.version,
                  'secret': row.is_secret, 'build_time': row.build_time}
        for row in all_rows(objects.SecretList, ctx,
                            {'service_id': service_id})}


def _live_spec(ctx, service):
    view = {key: getattr(service, key) for key in AFFECTING}
    view['source'] = source_spec(
        objects.Source.get_by_id(ctx, service.source_id))
    view['env'] = _env_view(ctx, service.id)
    view['compose_ref'] = service.compose_ref
    view['dockerfile_ref'] = service.dockerfile_ref
    view['file_mounts'] = file_mounts.snapshot(ctx, service.id)
    return view


def _frozen_spec(spec):
    view = {key: spec.get(key) for key in AFFECTING}
    view['health_check'] = spec.get('health_check', 'http')
    source = spec.get('source')
    # Ignore response metadata included by older snapshot writers.
    view['source'] = ({key: value for key, value in source.items()
                       if key != 'deploy_key'}
                      if isinstance(source, dict) else source)
    view['env'] = spec.get('env') or {}
    view['compose_ref'] = spec.get('compose_ref')
    view['dockerfile_ref'] = spec.get('dockerfile_ref')
    view['file_mounts'] = spec.get('file_mounts') or []
    return view


def pending_changes(ctx, service):
    """True when deployment-affecting settings differ from the live release.

    Name and description are metadata. A complete frozen spec wins over
    ``settings_changed_at``, so a stale stamp clears when the fields match.
    An incomplete spec keeps the timestamp check.
    """
    current = None
    if service.current_deployment_id:
        current = objects.Deployment.get_by_id(
            ctx, service.current_deployment_id)
        spec = current.spec if isinstance(current.spec, dict) else None
        if spec and all(key in spec for key in _SNAPSHOT_KEYS):
            return _live_spec(ctx, service) != _frozen_spec(spec)
    changed = service.settings_changed_at
    if changed is None or current is None:
        return changed is not None
    return changed.replace(tzinfo=None) > current.created_at.replace(
        tzinfo=None)


def _worker_address(service, ctx=None):
    if not service.server_id:
        return None
    try:
        server = objects.Server.get_by_id(
            ctx or common.context(), service.server_id)
    except exception.NotFound:
        return None
    return server.floating_ip if server is not None else None


def default_host(service, address=None, ctx=None):
    """Return the stored default host, or compute one for an old row."""
    if not service.ports and not service.is_engine_service:
        return None
    host = naming.optional_attr(service, 'default_domain')
    if host:
        return host
    if address is None:
        address = _worker_address(service, ctx)
    return naming.default_hostname(
        service.name, service.id, cfg.CONF.domains.default_suffix, address)


def default_url(service, address=None, ctx=None):
    if naming.optional_attr(service, 'default_domain_enabled') is False:
        return None
    host = default_host(service, address, ctx)
    return 'https://%s' % host if host else None


def primary_domains(ctx, service, lock=False):
    if lock:
        # Use current reads after the service lock, including on MySQL
        # configurations using REPEATABLE READ. Call inside a writer.
        query = db_api.domains._query(ctx).filter_by(
            service_id=service.id, primary=True)
        return [objects.Domain.obj_from_db(ctx, row) for row in
                query.populate_existing().with_for_update().all()]
    return list(all_rows(objects.DomainList, ctx,
                         {'service_id': service.id, 'primary': True}))


def primary_enabled():
    return microversions.at_least(_request_version(),
                                  microversions.PRIMARY_DOMAIN_VERSION)


def primary_schema(schema):
    result = copy.deepcopy(schema)
    result['properties']['primary'] = {'type': 'boolean'}
    result.pop('required', None)
    result['minProperties'] = 1
    return result


def require_primary_version(options):
    if ({'primary', 'default_domain_enabled'} & options.keys() and
            not primary_enabled()):
        pecan.abort(406)


def check_primary_removal(service, row):
    if row.primary and not service.default_domain_enabled:
        raise exception.Conflict(
            'Turn the default address back on or pick another main domain '
            'first.')


def change_primary(ctx, service, row, value):
    if not value:
        check_primary_removal(service, row)
    if value:
        for other in primary_domains(ctx, service, lock=True):
            if other.id != row.id:
                other.primary = False
                other.save()
    row.primary = value


def dns_records(service, domain=None, ctx=None):
    records = []
    address = _worker_address(service, ctx)
    service_url = default_url(service, address, ctx)
    host = parse.urlsplit(service_url).hostname if service_url else None
    name = domain.fqdn if domain else host
    if domain:
        records.append({'type': 'TXT',
                        'name': '_coast-challenge.' + domain.fqdn,
                        'value': domain.verification_token,
                        'purpose': 'verification'})
    automatic = domain and naming.optional_attr(domain, 'zone_id')
    cname = automatic and domain.record_type == 'CNAME' and host
    if name and address and not cname:
        kind = 'AAAA' if ipaddress.ip_address(address).version == 6 else 'A'
        records.append({'type': kind, 'name': name,
                        'value': address, 'purpose': 'routing'})
    if domain and host and (not automatic or cname):
        records.append({'type': 'CNAME', 'name': name, 'value': host,
                        'purpose': ('routing' if cname
                                    else 'routing-alternative')})
    return records


def all_rows(list_class, ctx, filters):
    # Env vars and frozen snapshots must not silently lose rows at max_limit.
    marker = None
    while True:
        rows = list_class.get_all(ctx, filters=filters, marker=marker,
                                  sort_key='id')
        yield from rows
        if not rows:
            return
        marker = rows[-1].id


@contextlib.contextmanager
def secret_writes(ctx):
    references = []
    try:
        yield references
    except Exception:
        for ref in references:
            try:
                secret_store.call('delete', ctx, ref)
            except Exception:
                # Do not replace the original error or log secret-store errors.
                handlers.LOG.warning('Secret reference cleanup failed.')
        raise


ENV_MASK = '********'


def write_env(ctx, service, incoming, replace, references):
    """Store env changes and return (action, row) pairs. Values stay out."""
    if service.is_engine_service and any(
            item and item.get('build_time') is True
            for item in incoming.values()):
        raise exception.Invalid(
            'Template service variables are runtime-only; '
            'build_time is not supported for template services')
    for item in incoming.values():
        if item is not None and item['value'] != ENV_MASK:
            secret_limits.validate(item['value'], 'Environment variable value')
    default_build_time = (not service.is_engine_service and
                          microversions.at_least(_request_version(), '1.6'))
    changes = []
    current = {row.key: row for row in all_rows(
        objects.SecretList, ctx, {'service_id': service.id})}
    for key, row in current.items():
        if (replace and key not in incoming) or incoming.get(key, {}) is None:
            # Keep immutable secret versions for rollback snapshots.
            changes.append(('delete', row))
            row.destroy()
    for key, item in incoming.items():
        if item is None:
            continue
        row = current.get(key)
        if item.get('value') == ENV_MASK:
            # The mask clients display means "keep the saved value".
            if row is None or (row.is_secret and
                               item.get('secret') is False):
                raise exception.Invalid()
            continue
        preserve = row is not None and not replace
        flags = {
            'is_secret': item.get(
                'secret', row.is_secret if preserve else False),
            'build_time': item.get(
                'build_time',
                row.build_time if preserve else default_build_time)}
        if (row is not None and not row.is_secret and
                not flags['is_secret'] and
                flags['build_time'] == row.build_time and
                secret_store.call('get', ctx, row.value_ref) ==
                item['value']):
            # Same plain value and flags: nothing to store.
            continue
        ref = secret_store.call(
            'put', ctx, item['value'],
            secret_store.SecretIdentity.for_resource(service, 'env'))
        references.append(ref)
        if row:
            row.value_ref = ref
            row.version += 1
            for name, value in flags.items():
                setattr(row, name, value)
            row.save()
            changes.append(('update', row))
        else:
            created = objects.Secret(
                context=ctx, service_id=service.id, key=key, value_ref=ref,
                version=1, **flags)
            created.create()
            changes.append(('create', created))
    return changes


def _notify_env(ctx, changes):
    for action, row in changes:
        notifications.completed(ctx, row, action, 'env_var')


def snapshot(ctx, service):
    spec = values(service, ('name', 'ports', 'replicas', 'cpu_limit',
                            'memory_limit_mb', 'healthcheck_path',
                            'health_check', 'health_check_port'))
    spec['compose_ref'] = service.compose_ref
    spec['dockerfile_ref'] = service.dockerfile_ref
    spec['source'] = source_spec(
        objects.Source.get_by_id(ctx, service.source_id))
    spec['env'] = {
        row.key: {'value_ref': row.value_ref, 'version': row.version,
                  'secret': row.is_secret, 'build_time': row.build_time}
        for row in all_rows(objects.SecretList, ctx,
                            {'service_id': service.id})}
    spec['file_mounts'] = file_mounts.snapshot(ctx, service.id)
    spec['mounts'] = [
        values(row, ('volume_id', 'sub_path', 'container_path', 'read_only'))
        for row in all_rows(
            objects.AppVolumeMountList, ctx, {'service_id': service.id})]
    return spec


def create_deployment(ctx, service, options, target=None, trigger='api'):
    spec = copy.deepcopy(target.spec) if target else snapshot(ctx, service)
    if target:
        spec['file_mounts'] = file_mounts.snapshot(ctx, service.id)
    if not target and spec['source']['type'] == 'image':
        options = dict(options)
        options.setdefault('source_ref', spec['source']['tag'])
        try:
            schemas.jsonschema.validate(
                options['source_ref'], schemas.IMAGE_TAG)
        except schemas.jsonschema.ValidationError:
            raise exception.Invalid() from None
    row = objects.Deployment(
        context=ctx, service_id=service.id, status=states.PENDING,
        source_ref=((target.resolved_ref or target.source_ref)
                    if target else options.get(
            'source_ref', spec['source'].get('branch'))),
        server_id=target.server_id if target else service.server_id,
        trigger='rollback' if target else trigger,
        rollback_of=target.id if target else None,
        image_digest=target.image_digest if target else None, spec=spec)
    row.create()
    return row


def validate_cpu(options):
    limit = options.get('cpu_limit')
    try:
        if limit is not None and not math.isfinite(limit):
            raise ValueError()
    except (ValueError, OverflowError):
        raise exception.Invalid() from None


def convert_filters(params):
    for key in params:
        if key.endswith('_at'):
            try:
                params[key] = datetime.datetime.fromisoformat(
                    params[key].replace('Z', '+00:00'))
            except ValueError:
                raise validation.filter_value(
                    key, params[key], 'an ISO 8601 timestamp') from None
        elif key in ('number', 'replicas', 'memory_limit_mb', 'cpu_limit'):
            try:
                params[key] = (float(params[key]) if key == 'cpu_limit'
                               else int(params[key]))
                if key == 'cpu_limit' and not math.isfinite(params[key]):
                    raise ValueError()
            except ValueError:
                raise validation.filter_value(
                    key, params[key],
                    'a finite number' if key == 'cpu_limit' else
                    'an integer') from None
        elif key in ('auto_dns', 'auto_deploy'):
            validation.choice(key, params[key], ('true', 'false'))
            params[key] = params[key] == 'true'


DEPLOY_TASKS = (states.DEPLOYING, states.ROLLING_BACK, states.PROVISIONING)
IN_FLIGHT = (states.PENDING, states.BUILDING, states.DEPLOYING)


def cancel_inflight(ctx, service):
    """Mark the latest in-flight deployment CANCELLED (delete-wins path).

    The conductor delete cancels the engine deployment before removing the
    app, so the engine stops the build or rollout.
    """
    latest = objects.DeploymentList.get_all(
        ctx, filters={'service_id': service.id}, sort_key='number',
        sort_dir='desc', limit=1)
    if latest and latest[0].status in IN_FLIGHT:
        latest[0].status = states.CANCELLED
        latest[0].save()


def _request_version():
    return getattr(pecan.request, 'version', microversions.MIN_VERSION)


def _require_tls_version():
    if not microversions.at_least(_request_version(), '1.2'):
        pecan.abort(406)


HEALTH_FIELDS = {'health_check', 'health_check_port', 'health_check_path'}


def first_port(ports):
    try:
        return int(ports.split(',')[0].strip())
    except (ValueError, AttributeError):
        return None


def validate_health(options, row=None):
    if HEALTH_FIELDS & options.keys():
        if not microversions.at_least(_request_version(), '1.4'):
            pecan.abort(406)
    if 'health_check_path' in options:
        if 'healthcheck_path' in options:
            raise exception.Invalid('Specify only one health check path.')
        options['healthcheck_path'] = options.pop('health_check_path')
    if isinstance(options.get('ports'), str):
        options['ports'] = options['ports'].strip()
    mode = options.get('health_check', row.health_check if row else 'none')
    ports = options.get('ports', row.ports if row else '')
    if mode == 'http' and not (first_port(ports) and
                               1 <= first_port(ports) <= 65535):
        raise exception.Invalid('HTTP health checks require an app port.')
    if 'ports' in options and not ports and row:
        domains = objects.DomainList.get_all(
            row._context, filters={'service_id': row.id}, limit=1)
        if domains:
            raise exception.Invalid('Remove custom domains before app ports.')


def _new_domain(ctx, service, options):
    verified = cfg.CONF.domains.skip_verification
    return objects.Domain(
        context=ctx, service_id=service.id, fqdn=options['fqdn'],
        auto_dns=options.get('auto_dns', False),
        dns_mode='designate' if options.get('auto_dns') else 'manual',
        verification_token=secrets.token_urlsafe(32),
        tls=options.get('tls', 'letsencrypt'),
        zone_id=options.get('zone_id'),
        dns_trust_id=options.get('dns_trust_id'),
        record_type=options.get('record_type', 'A'),
        managed_records=[], status_reason=None,
        verification_started_at=timeutils.utcnow(),
        verified_at=timeutils.utcnow() if verified else None,
        status=(states.ACTIVE if verified else states.PENDING_VERIFICATION))


def validate_git_key(source, existing=None):
    key = source.get('deploy_key')
    repo = source.get('repo_url') or (existing.repo_url if existing else '')
    ssh = not repo.startswith('https://')
    if (key is not None or ssh) and not microversions.at_least(
            _request_version(), microversions.DEPLOY_KEY_VERSION):
        raise exception.Invalid('source requires a newer API version.')
    if key is not None and not ssh:
        raise exception.Invalid('source.deploy_key requires an SSH repo_url.')
    if ssh and key is None and not (
            existing and existing.credentials_ref):
        raise exception.Invalid('source.deploy_key is required.')
    if key and 'private_key' in key:
        key_secrets.parse_deploy_key(key['private_key'])


def _validate_source_patch(ctx, row, options):
    if 'source' not in options:
        return
    source = objects.Source.get_by_id(ctx, row.source_id)
    if source.type in ('dockerfile', 'compose'):
        raise exception.Invalid('Use the source content editor.')
    schema = (schemas.IMAGE_SOURCE_UPDATE if source.type == 'image'
              else schemas.GIT_SOURCE_UPDATE)
    if {'image', 'tag'} & options['source'].keys():
        if source.type != 'image':
            raise exception.Invalid(
                'Image and tag updates require an image source.')
    if 'image' in options['source'] and not microversions.at_least(
            _request_version(), microversions.IMAGE_UPDATE_VERSION):
        raise exception.Invalid(
            'Image updates require API version %s.' %
            microversions.IMAGE_UPDATE_VERSION)
    if 'deploy_key' in options['source']:
        if source.type != 'git':
            raise exception.Invalid('source.deploy_key requires Git.')
        validate_git_key(options['source'], source)
    if source.type == 'template':
        raise exception.Invalid('Template sources cannot be patched.')
    try:
        schemas.jsonschema.validate(options['source'], schema)
    except schemas.jsonschema.ValidationError:
        raise exception.Invalid() from None


def _save_service_fields(ctx, row, options):
    source_options = options.pop('source', None)
    if source_options is not None:
        source = objects.Source.get_by_id(ctx, row.source_id)
        if source.type == 'image':
            image, tag = source.image.rsplit(':', 1)
            source_options = {
                'image': source_options.get('image', image) + ':' +
                source_options.get('tag', tag)}
        for key, value in source_options.items():
            setattr(source, key, value)
        source.save()
    for key, value in options.items():
        setattr(row, key, value)
    row.save()


def _push_metadata(ctx, row, handler):
    """Send name and description when the service is idle and known."""
    if not row.engine_ref or row.task_state != states.NONE:
        return
    with db_api.writer.using(ctx):
        claim_service(ctx, row, states.RECONCILING)
    handler.rename_service(ctx, row)


def _apply_txt_check(row):
    """Record the TXT result. A lookup failure leaves the row unverified."""
    if row.status == states.ERROR:
        row.status = states.PENDING_VERIFICATION
    if row.status != states.PENDING_VERIFICATION:
        return
    if cfg.CONF.domains.skip_verification:
        # Development only: treat "Verify now" as a match.
        row.status = states.VERIFIED
        return
    try:
        resolver = domain_check.make_resolver(cfg.CONF.domains.nameservers)
        resolver.timeout = 1
        resolver.lifetime = 2
        matched = domain_check.txt_matches(
            row.fqdn, row.verification_token, resolver)
    except Exception:
        matched = False
    if matched:
        row.status = states.VERIFIED
        row.verified_at = timeutils.utcnow()


def pasted_content(source):
    """Validate and detach protected content before RPC serialization."""
    if source['type'] not in ('dockerfile', 'compose'):
        return None, None
    if not microversions.at_least(
            _request_version(), microversions.PASTED_SOURCE_VERSION):
        raise exception.Invalid('source requires a newer API version.')
    content = source.pop(source['type'])
    secret_limits.validate(content, source['type'],
                           cfg.CONF.api.max_compose_bytes)
    document = None
    if source['type'] == 'compose':
        try:
            document = compose_rules.parse(content)
        except exception.Invalid:
            raise exception.Invalid('Invalid compose.') from None
    return content, document


def store_pasted_content(ctx, row, source_type, content, document, refs):
    if content is None:
        return
    reference = secret_store.call(
        'put', ctx, content,
        secret_store.SecretIdentity.for_resource(row, source_type))
    refs.append(reference)
    setattr(row, source_type + '_ref', reference)
    if document is not None:
        storage_rules.account_compose_volumes(ctx, row, document)


class Collection:
    resource = 'service'
    object_class = objects.Service
    list_class = objects.ServiceList
    fields = APP_FIELDS
    computed = ('appgroup_id', 'kind', 'source', 'default_url',
                'dns_records', 'pending_changes')

    def __init__(self, service_id=None, handler=None):
        self.service_id = service_id
        self.handler = handler or handlers.Handler()

    def parent(self):
        if self.service_id:
            return objects.Service.get_by_id(common.context(), self.service_id)

    def path(self, id=None):
        # Public routes are /v1/services. Nested collections stay under one
        # service. Other resources, such as events, keep their own name.
        if self.service_id:
            base = '/services/' + self.service_id + '/' + self.resource + 's'
        elif self.resource == 'service':
            base = '/services'
        else:
            base = '/' + self.resource + 's'
        return base + ('/' + id if id else '')

    def render(self, row):
        result = values(row, self.fields)
        if self.resource == 'service':
            if microversions.at_least(_request_version(), '1.24'):
                result.update(values(row, ('task_owner', 'task_started_at')))
            if microversions.at_least(_request_version(), '1.5'):
                result['reconcile_fault'] = row.reconcile_fault
            if microversions.at_least(_request_version(), '1.4'):
                result['health_check_path'] = row.healthcheck_path or '/'
                result['health_check_port'] = (
                    row.health_check_port or first_port(row.ports))
            else:
                result.pop('health_check', None)
                result.pop('health_check_port', None)
        ctx = context_module.RequestContext(project_id=row.project_id)
        if self.resource == 'service':
            parent = (objects.Environment.get_by_id(
                ctx, row.environment_id, show_deleted=True)
                if row.environment_id else None)
            result['appgroup_id'] = (
                parent.appgroup_id if parent else None)
            result['kind'] = row.kind
            result['pending_changes'] = pending_changes(ctx, row)
            result.update(source=public_source(objects.Source.get_by_id(
                ctx, row.source_id)),
                default_url=default_url(row, ctx=ctx),
                dns_records=dns_records(row, ctx=ctx))
            if primary_enabled():
                main = primary_domains(ctx, row)
                result['default_domain_enabled'] = row.default_domain_enabled
                result['primary_url'] = (naming.domain_url(
                    main[0].fqdn, main[0].tls) if main else
                    result['default_url'])
            for domain in all_rows(
                    objects.DomainList, ctx, {'service_id': row.id}):
                result['dns_records'].extend(dns_records(row, domain, ctx))
        elif self.resource == 'deployment':
            result['resolved_ref'] = row.resolved_ref
            if microversions.at_least(_request_version(), '1.5'):
                result['runtime_log'] = row.runtime_log
        elif self.resource == 'domain':
            if primary_enabled():
                result['primary'] = row.primary
            result['dns_records'] = dns_records(self.parent(), row)
            if dns_controllers.enabled():
                result.update(zone_id=row.zone_id,
                              managed_records=row.managed_records or [],
                              record_type=row.record_type or 'A',
                              status_reason=row.status_reason)
        path = (self.path() + '?id=' + row.id if self.resource == 'event'
                else self.path(row.id))
        result['links'] = [{'rel': 'self', 'href': url(path)}]
        fields = (pecan.request.params.get('fields')
                  if pecan.request.method == 'GET' else None)
        if fields:
            self.validate_fields(fields)
            result = {key: value for key, value in result.items()
                      if key in fields.split(',') or key == 'links'}
        return result

    def validate_fields(self, fields):
        names = self.fields + self.computed + ('links',)
        if self.resource == 'service' and microversions.at_least(
                _request_version(), '1.24'):
            names += ('task_owner', 'task_started_at')
        if microversions.at_least(_request_version(), '1.5'):
            names += {'service': ('reconcile_fault',),
                      'deployment': ('runtime_log',)}.get(self.resource, ())
        if primary_enabled():
            names += {'service': ('primary_url', 'default_domain_enabled'),
                      'domain': ('primary',)}.get(self.resource, ())
        if self.resource == 'domain' and dns_controllers.enabled():
            names += ('zone_id', 'managed_records', 'record_type',
                      'status_reason')
        if self.resource == 'service':
            if microversions.at_least(_request_version(), '1.4'):
                names += ('health_check_path',)
            elif set(fields.split(',')) & HEALTH_FIELDS:
                pecan.abort(406)
        if set(fields.split(',')) - set(names):
            raise exception.Invalid(
                "Invalid fields '%s'. Use one of: %s." %
                (fields, ', '.join(names)))

    @pecan.expose()
    def _lookup(self, id, *remainder):
        if self.resource == 'event':
            pecan.abort(404)
        if self.resource == 'service':
            ctx = common.context()
            try:
                policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
                ctx.is_admin = True
            except exception.NotAuthorized:
                pass
            row = objects.Service.get_by_id(ctx, id, all_projects=ctx.is_admin)
            if row.kind == 'database':
                pecan.abort(404)
        return Item(self, id), remainder

    @pecan.expose('json')
    def index(self, **kwargs):
        service = self.parent()
        if pecan.request.method == 'GET':
            return self.list()
        if pecan.request.method != 'POST' or self.resource == 'event':
            pecan.abort(405)
        ctx = common.authorize(self.resource, 'create')
        schema = {'service': schemas.SERVICE, 'deployment': schemas.DEPLOYMENT,
                  'domain': schemas.DOMAIN}[self.resource]
        if self.resource == 'domain':
            schema = dns_controllers.schema(schema, 'domain')
        options = schemas.body(schema, self.resource)
        require_primary_version(options)
        if self.resource == 'domain' and dns_controllers.enabled():
            dns_controllers.domain_link(ctx, options)
            if options.get('fqdn') == default_host(service, ctx=ctx):
                raise exception.Invalid(
                    'The default hostname is already managed.')
        if self.resource == 'service':
            return self.create_service(ctx, options)
        if self.resource == 'domain':
            if not service.ports and not service.is_engine_service:
                raise exception.Invalid('App has no ports.')
            if 'tls' in options:
                _require_tls_version()
        with db_api.writer.using(ctx):
            common.quota_hook(ctx, self.resource, options)
            claim_service(
                ctx, service,
                states.DEPLOYING if self.resource == 'deployment'
                else states.RECONCILING, owner=(
                    'domain_change' if self.resource == 'domain' else
                    'deployment'))
            states.check_transition('service', service.status, service.status)
            if service.status == states.DELETING:
                raise exception.Conflict()
            if self.resource == 'deployment':
                row = create_deployment(ctx, service, options)
            else:
                row = _new_domain(ctx, service, options)
                row.create()
                if options.get('primary'):
                    change_primary(ctx, service, row, True)
                    row.save()
                service.release_task(states.RECONCILING)
        getattr(self.handler, 'create_' + self.resource)(ctx, row, options)
        pecan.response.status = 201 if self.resource == 'domain' else 202
        return {self.resource: self.render(row)}

    def create_service(self, ctx, options):
        server_id = options['server_id']
        if not server_id:
            # Q144: Coast has no automatic placement; the driver needs a
            # worker, so reject before any lookup, quota or DB write.
            raise exception.Invalid(SERVER_REQUIRED)
        validate_cpu(options)
        appgroup = objects.Appgroup.get_by_id(
            ctx, options['appgroup_id'])
        environment = objects.Environment.get_by_id(
            ctx, options['environment_id'])
        server = objects.Server.get_by_id(ctx, server_id)
        stack_id = appgroup.stack_id
        stack = objects.Stack.get_by_id(ctx, stack_id)
        if (environment.appgroup_id != appgroup.id or
                server.role != 'worker' or server.stack_id != stack_id):
            raise exception.Invalid()
        if any(parent.status != states.ACTIVE or not parent.engine_ref
               for parent in (appgroup, environment)):
            raise exception.Conflict()
        source = options['source']
        if isinstance(source, str):
            source = {'type': 'template', 'template_id': source[9:]}
            options['source'] = source
        if source['type'] == 'git':
            validate_git_key(source)
            if source.get('deploy_key', {}).get('generate'):
                options.setdefault('deploy', False)
        content, document = pasted_content(source)
        template_id = None
        if source['type'] == 'template':
            template = template_controllers.get_template(
                ctx, stack, source['template_id'])
            http_port = (template.get('http_port') if microversions.at_least(
                _request_version(), '1.7') else None)
            if not (options.get('ports') or '').strip():
                ports = template.get('ports') or []
                if http_port or ports:
                    options['ports'] = str(http_port or ports[0])
            if 'health_check' not in options and http_port:
                options['health_check'] = 'http'
                options.setdefault('health_check_port', http_port)
                if 'health_check_path' not in options:
                    options.setdefault('healthcheck_path', '/')
            template_id = source['template_id']
        kind = workload_kind(source['type'], options.pop('kind', None))
        validate_health(options)
        data = {key: options[key] for key in APP_FIELDS if key in options}
        data.update(stack_id=stack_id,
                    ports=options.get('ports', ''),
                    health_check=options.get('health_check', 'none'),
                    auto_deploy=False, status=states.PENDING,
                    task_state=states.NONE)
        changes = []
        with secret_writes(ctx) as refs, db_api.writer.using(ctx):
            common.quota_hook(ctx, self.resource, options)
            stack_open(ctx, stack_id)
            # template_id is a service column. The source row stores type.
            source_values = {
                key: value for key, value in options['source'].items()
                if key not in ('template_id', 'deploy_key')}
            if source_values['type'] == 'git':
                source_values.setdefault('build_pack', 'nixpacks')
            if source_values['type'] == 'image':
                source_values['image'] += ':' + source_values.pop('tag')
            source = objects.Source(context=ctx, **source_values)
            source.create()
            row = objects.Service(context=ctx, source_id=source.id,
                                  template_id=template_id, kind=kind,
                                  **data)
            row.create()
            if 'deploy_key' in options['source']:
                reference, public = key_secrets.store_deploy_key(
                    ctx, row, options['source'].pop('deploy_key'))
                refs.append(reference)
                source.credentials_ref = reference
                source.deploy_public_key = public
                source.save()
            store_pasted_content(
                ctx, row, source.type, content, document, refs)
            linked = objects.Stack.get_by_id(ctx, row.stack_id)
            row.default_dns_zone_id = linked.dns_zone_id
            row.default_dns_trust_id = linked.dns_trust_id
            suffix = linked.default_domain or cfg.CONF.domains.default_suffix
            if linked.default_domain:
                host = customer_dns.default_hostname(row.name, row.id, suffix)
            else:
                host = naming.default_hostname(
                    row.name, row.id, suffix, server.floating_ip)
            row.default_domain = (
                host if row.ports or row.is_engine_service else None)
            row.save()
            row.acquire_task(states.PROVISIONING)
            changes = write_env(ctx, row, options.get('env', {}), True, refs)
            if template_id:
                storage_rules.create_template_storages(
                    ctx, row, server, template_id, stack.engine_version)
            deployment = (create_deployment(ctx, row, {})
                          if options.get('deploy', True) else None)
        self.handler.create_service(ctx, row, options)
        _notify_env(ctx, changes)
        if deployment:
            notifications.accepted(ctx, deployment, 'create')
        pecan.response.status = 202
        result = self.render(row)
        if deployment:
            result['deployment_id'] = deployment.id
        elif naming.optional_attr(source, 'deploy_public_key'):
            result['deploy_required'] = True
        return {'service': result}

    def list(self):
        ctx = common.authorize(self.resource, 'get_all')
        params = dict(pecan.request.params)
        if self.resource == 'service' and not microversions.at_least(
                _request_version(), '1.4'):
            requested = set(params) | {params.get('sort_key')}
            if requested & HEALTH_FIELDS:
                pecan.abort(406)
        permitted = set(self.fields) | {'limit', 'marker', 'sort_key',
                                        'sort_dir', 'fields', 'all_projects'}
        if self.resource in ('service', 'database'):
            permitted.update(('appgroup_id', 'kind'))
        validation.parameters(params, permitted)
        all_projects = params.pop('all_projects', 'false')
        validation.choice('all_projects', all_projects, ('true', 'false'))
        all_projects = all_projects == 'true'
        if all_projects or 'project_id' in params:
            policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
            all_projects = True
            ctx.is_admin = True
        limit = min(validation.limit(params.pop(
            'limit', cfg.CONF.api.max_limit)), cfg.CONF.api.max_limit)
        marker = params.pop('marker', None)
        if marker and self.service_id:
            row = self.object_class.get_by_id(ctx, marker,
                                              all_projects=all_projects)
            if row.service_id != self.service_id:
                raise exception.NotFound()
        sort_key = params.pop('sort_key', 'created_at')
        sort_dir = params.pop('sort_dir', 'asc')
        fields = params.pop('fields', None)
        if fields:
            self.validate_fields(fields)
        validation.choice('sort key', sort_key, self.fields)
        validation.choice('sort direction', sort_dir, ('asc', 'desc'))
        if self.resource == 'event' and 'outcome' in params:
            validation.choice('outcome', params['outcome'],
                              validation.EVENT_OUTCOMES)
        if self.resource == 'service' and 'kind' in params:
            validation.choice('kind', params['kind'], ('app', 'service'))
        if self.resource == 'service':
            params['_exclude_databases'] = True
            if marker:
                marked = self.object_class.get_by_id(
                    ctx, marker, all_projects=all_projects)
                if marked.kind == 'database':
                    raise exception.NotFound()
        convert_filters(params)
        if self.service_id:
            params['service_id'] = self.service_id
        if self.resource == 'database':
            params['kind'] = 'database'
            if marker:
                marked = self.object_class.get_by_id(
                    ctx, marker, all_projects=all_projects)
                if marked.kind != 'database':
                    raise exception.NotFound()
        rows = self.list_class.get_all(
            ctx, filters=params, limit=limit, marker=marker,
            sort_key=sort_key, sort_dir=sort_dir, all_projects=all_projects)
        links = []
        if limit and len(rows) == limit and self.list_class.get_all(
                ctx, filters=params, limit=1, marker=rows[-1].id,
                sort_key=sort_key, sort_dir=sort_dir,
                all_projects=all_projects):
            query = dict(pecan.request.params, marker=rows[-1].id, limit=limit)
            links = [{'rel': 'next', 'href': url(self.path()) + '?' +
                      parse.urlencode(query)}]
        return {self.resource + 's': [self.render(row) for row in rows],
                'links': links}


class Deployments(Collection):
    resource = 'deployment'
    object_class = objects.Deployment
    list_class = objects.DeploymentList
    fields = DEP_FIELDS
    computed = ('resolved_ref',)


class Domains(Collection):
    resource = 'domain'
    object_class = objects.Domain
    list_class = objects.DomainList
    fields = DOMAIN_FIELDS
    computed = ('dns_records',)


class Events(Collection):
    resource = 'event'
    object_class = objects.Event
    list_class = objects.EventList
    fields = EVENT_FIELDS
    computed = ()


@db_api.retry_transaction
def _claim_domain_delete(ctx, c, domain_id):
    with db_api.writer.using(ctx):
        service = c.parent()
        claim_service(
            ctx, service,
            states.RECONCILING, owner='domain_change')
        service = objects.Service.obj_from_db(
            ctx, db_api.services._get(ctx, service.id, lock=True))
        row = objects.Domain.obj_from_db(
            ctx, db_api.domains._get(ctx, domain_id, lock=True))
        check_primary_removal(service, row)
        states.check_transition('domain', row.status, row.status)
        if service.status == states.DELETING:
            raise exception.Conflict()
    return row


class Item:
    def __init__(self, collection, id):
        self.collection = collection
        self.id = id
        if collection.resource == 'service':
            from coast.api.v1 import backup_controllers
            self.backup_policy = backup_controllers.BackupPolicy(id)
            self.backups = backup_controllers.ServiceBackups(id)
            self.deployments = Deployments(id, collection.handler)
            self.domains = Domains(id, collection.handler)
            from coast.api.v1 import file_mount_controllers
            self.file_mounts = file_mount_controllers.FileMounts(id)
            self.env = Env(id, collection.handler)
            self.compose = Compose(id, collection.handler)
            self.dockerfile = Dockerfile(id, collection.handler)
            self.containers = Containers(id, collection.handler)
            self.metrics = Metrics(id)
            self.webhooks = Webhooks(id)
            self.console = console_controllers.Session(id, 'service')

    def get(self):
        self.collection.parent()
        row = self.collection.object_class.get_by_id(common.context(), self.id)
        if (self.collection.service_id and
                row.service_id != self.collection.service_id):
            raise exception.NotFound()
        return row

    def response(self, row):
        return {self.collection.resource: self.collection.render(row)}

    @pecan.expose('json')
    def index(self, **kwargs):
        row = self.get()
        c = self.collection
        method = pecan.request.method
        if method == 'GET':
            common.authorize(c.resource, 'get_one', row)
            return self.response(row)
        if method == 'PATCH' and c.resource == 'service':
            return self.update(row)
        if method == 'PATCH' and c.resource == 'domain':
            return self.update_domain(row)
        if method != 'DELETE' or c.resource == 'deployment':
            pecan.abort(405)
        ctx = common.authorize(c.resource, 'delete', row)
        if pecan.request.body:
            schemas.body(schemas.document({}))
        if c.resource == 'service':
            with db_api.writer.using(ctx):
                stack_open(ctx, row.stack_id)
                states.check_transition('service', row.status, states.DELETING)
                if row.task_state in DEPLOY_TASKS:
                    # Delete always wins: cancel the in-flight deployment and
                    # take over its service lock.
                    db_api.cas_task_state(
                        ctx, objects.Service.repository.model, row.id,
                        new=states.DELETING, expected=row.task_state)
                    cancel_inflight(ctx, row)
                    row = objects.Service.get_by_id(ctx, row.id)
                else:
                    row.acquire_task(states.DELETING)
                row.status = states.DELETING
                row.save()
        else:
            row = _claim_domain_delete(ctx, c, row.id)
        # Cleanup can finish before cast returns. Freeze the response while
        # deployment history and the other rendering inputs still exist.
        response = self.response(row)
        getattr(c.handler, 'delete_' + c.resource)(ctx, row)
        pecan.response.status = 202
        return response

    def update(self, row):
        ctx = common.authorize('service', 'update', row)
        options = schemas.body(schemas.SERVICE_UPDATE, 'service')
        require_primary_version(options)
        validate_cpu(options)
        _validate_source_patch(ctx, row, options)
        key_options = options.get('source', {}).pop('deploy_key', None)
        if key_options is not None and not options['source']:
            options.pop('source')
        metadata_only = bool(options) and set(options) <= {
            'name', 'description'}
        with secret_writes(ctx) as refs, db_api.writer.using(ctx):
            # Settings do not take the deploy lock (James 2026-10-15).
            settings_lock(ctx, row)
            if (options.get('default_domain_enabled') is False and
                    row.task_state == states.RECONCILING):
                raise exception.Conflict(
                    'Wait for the current domain operation to finish before '
                    'turning off the default address.')
            if (options.get('default_domain_enabled') is False and
                    not primary_domains(ctx, row, lock=True)):
                raise exception.Conflict(
                    'Pick a main domain before turning off the default '
                    'address.')
            validate_health(options, row)
            if key_options is not None:
                stack_open(ctx, row.stack_id)
                source = objects.Source.get_by_id(ctx, row.source_id)
                if row.task_state != states.NONE or source.deploy_key_rotation:
                    raise exception.Conflict(
                        'Wait for the current operation on this app to '
                        'finish, then try again.')
                if not row.engine_ref:
                    raise exception.Conflict(
                        'Deploy the app once before changing its deploy key.')
                # Validate all fields before creating a secret or checkpoint.
                reference, public = key_secrets.store_deploy_key(
                    ctx, row, key_options)
                refs.append(reference)
                row.acquire_task(states.RECONCILING, owner=deploy_keys.OWNER)
                source.deploy_key_rotation = {
                    'phase': 'queued', 'reference': reference,
                    'public_key': public, 'previous': source.credentials_ref,
                    'updated_at': timeutils.utcnow().isoformat()}
                source.save()
            if set(options) & {
                    'ports', 'cpu_limit', 'memory_limit_mb',
                    'healthcheck_path', 'health_check',
                    'health_check_port', 'source'}:
                row.settings_changed_at = timeutils.utcnow()
            _save_service_fields(ctx, row, options)
        if key_options is not None:
            # The checkpoint owns the secret if RPC delivery is uncertain.
            try:
                self.collection.handler.rpc.rotate_deploy_key(
                    ctx, row.id, reference, public)
            except exception.CoastException:
                with db_api.writer.using(ctx):
                    row = objects.Service.get_by_id(ctx, row.id)
                    settings_lock(ctx, row)
                    source = objects.Source.get_by_id(ctx, row.source_id)
                    pending = source.deploy_key_rotation
                    if (pending and pending['reference'] == reference and
                            pending['public_key'] == public):
                        deploy_keys.mark_failure(row, source)
                raise exception.Conflict('Deploy key update failed.') from None
            row = objects.Service.get_by_id(ctx, row.id)
        if metadata_only:
            _push_metadata(ctx, row, self.collection.handler)
        notifications.completed(ctx, row, 'update')
        return self.response(row)

    def update_domain(self, row):
        ctx = common.authorize('domain', 'update', row)
        _require_tls_version()
        options = schemas.body(dns_controllers.schema(
            primary_schema(schemas.DOMAIN_UPDATE), 'domain'), 'domain')
        require_primary_version(options)
        if dns_controllers.enabled() and set(options).intersection(
                {'fqdn', 'host', 'auto_dns', 'zone_id', 'dns_trust_id',
                 'record_type'}):
            dns_controllers.domain_link(ctx, options, row)
            if options.get('fqdn') == default_host(
                    self.collection.parent(), ctx=ctx):
                raise exception.Invalid(
                    'The default hostname is already managed.')
        with db_api.writer.using(ctx):
            service = self.collection.parent()
            claim_service(
                ctx, service,
                states.RECONCILING, owner='domain_change')
            if service.status == states.DELETING:
                raise exception.Conflict()
            service = objects.Service.obj_from_db(
                ctx, db_api.services._get(ctx, service.id, lock=True))
            row = objects.Domain.obj_from_db(
                ctx, db_api.domains._get(ctx, row.id, lock=True))
            if 'primary' in options:
                change_primary(ctx, service, row, options['primary'])
            states.check_transition('domain', row.status, row.status)
            if dns_controllers.enabled() and any(
                    key in options for key in ('fqdn', 'auto_dns', 'zone_id',
                                               'dns_trust_id', 'record_type')):
                if row.dns_previous:
                    raise exception.Conflict()
                row.dns_previous = dict(
                    fqdn=row.fqdn, zone_id=row.zone_id,
                    dns_trust_id=row.dns_trust_id, auto_dns=row.auto_dns)
                row.status = states.PENDING_VERIFICATION
                row.verified_at = None
                row.verification_started_at = timeutils.utcnow()
                row.verification_token = secrets.token_urlsafe(32)
            for key, value in options.items():
                setattr(row, key, value)
            row.dns_mode = 'designate' if row.auto_dns else 'manual'
            row.save()
        self.collection.handler.update_domain(ctx, row)
        pecan.response.status = 202
        return self.response(row)

    @pecan.expose('json')
    def action(self, **kwargs):
        if pecan.request.method != 'POST':
            pecan.abort(405)
        row = self.get()
        c = self.collection
        actions = {'service': ('start', 'stop', 'restart'),
                   'deployment': ('rollback', 'cancel'),
                   'domain': ('verify',)}[c.resource]
        body = schemas.body(schemas.document(
            {key: schemas.document({}) for key in actions}))
        if len(body) != 1:
            raise exception.Invalid()
        action = next(iter(body))
        ctx = common.authorize(c.resource, action, row)
        with db_api.writer.using(ctx):
            if c.resource == 'deployment':
                row = self.deployment_action(ctx, row, action)
            elif c.resource == 'service':
                claim_service(ctx, row, states.RECONCILING, owner=action)
                required = (states.STOPPED if action == 'start'
                            else states.ACTIVE)
                if row.status != required:
                    raise exception.Conflict()
                desired = (states.STOPPED if action == 'stop'
                           else states.STARTING)
                states.check_transition('service', row.status, desired)
            else:
                service = c.parent()
                claim_service(
                    ctx, service,
                    states.RECONCILING, owner='domain_change')
                if service.status == states.DELETING:
                    raise exception.Conflict()
                desired = (states.PENDING_VERIFICATION if row.status ==
                           states.ERROR else states.VERIFIED)
                states.check_transition('domain', row.status, desired)
                if row.auto_dns and row.zone_id:
                    row.verification_started_at = timeutils.utcnow()
                _apply_txt_check(row)
                row.save()
        getattr(c.handler, action + '_' + c.resource)(ctx, row)
        pecan.response.status = 202
        return self.response(row)

    def deployment_action(self, ctx, row, action):
        service = self.collection.parent()
        if action == 'rollback':
            claim_service(ctx, service, states.ROLLING_BACK)
            if service.status == states.DELETING:
                raise exception.Conflict()
            current = (objects.Deployment.get_by_id(
                ctx,
                service.current_deployment_id) if service.current_deployment_id
                else None)
            if (row.status not in (states.ACTIVE, states.SUPERSEDED,
                                   states.ROLLED_BACK) or not current or
                    row.number >= current.number):
                raise exception.Conflict()
            return create_deployment(ctx, service, {}, target=row)
        if service.task_state not in (states.DEPLOYING, states.ROLLING_BACK,
                                      states.PROVISIONING):
            raise exception.Conflict()
        stack_open(ctx, service.stack_id)
        # Re-check the existing lock atomically, not NONE -> a second task.
        db_api.cas_task_state(ctx, objects.Service.repository.model,
                              service.id,
                              new=service.task_state,
                              expected=service.task_state)
        latest = objects.DeploymentList.get_all(
            ctx, filters={'service_id': service.id}, sort_key='number',
            sort_dir='desc', limit=1)
        if not latest or latest[0].id != row.id:
            raise exception.Conflict()
        row = self.get()
        states.check_transition('deployment', row.status, states.CANCELLED)
        if row.status == states.CANCELLED:
            raise exception.Conflict()
        row.status = states.CANCELLED
        row.save()
        return row

    @pecan.expose('json')
    def log(self, **kwargs):
        if (pecan.request.method != 'GET' or
                self.collection.resource != 'deployment'):
            pecan.abort(405)
        row = self.get()
        ctx = common.authorize('deployment', 'get_log', row)
        if pecan.request.params:
            raise exception.Invalid()
        return {'log': self.collection.handler.deployment_log(ctx, row)}

    @pecan.expose('json')
    def logs(self, **kwargs):
        if (pecan.request.method != 'GET' or
                self.collection.resource != 'service'):
            pecan.abort(405)
        row = self.get()
        ctx = common.authorize('service', 'get_logs', row)
        params = dict(pecan.request.params)
        if set(params) - {'since', 'tail'}:
            raise exception.Invalid()
        try:
            if 'tail' in params:
                params['tail'] = int(params['tail'])
                if params['tail'] < 0:
                    raise ValueError()
            if 'since' in params:
                datetime.datetime.fromisoformat(params['since'].replace(
                    'Z', '+00:00'))
        except ValueError:
            raise exception.Invalid() from None
        return {'logs': self.collection.handler.service_logs(ctx, row, params)}


class Webhooks:
    """GET the app's deploy and Git host webhook URLs (1.3+)."""

    def __init__(self, service_id):
        self.service_id = service_id

    @pecan.expose('json')
    def index(self, **kwargs):
        if pecan.request.method != 'GET':
            pecan.abort(405)
        if not microversions.at_least(_request_version(), '1.3'):
            pecan.abort(404)
        ctx = common.context()
        service = objects.Service.get_by_id(ctx, self.service_id)
        common.authorize('service', 'get_webhooks', service)
        if pecan.request.params:
            raise exception.Invalid()
        return {'webhooks': webhook_view(service)}

    @pecan.expose('json')
    def regenerate(self, **kwargs):
        if pecan.request.method != 'POST':
            pecan.abort(405)
        if not microversions.at_least(_request_version(), '1.3'):
            pecan.abort(404)
        ctx = common.context()
        service = objects.Service.get_by_id(ctx, self.service_id)
        common.authorize('service', 'regenerate_webhooks', service)
        if not webhooks.enabled():
            raise exception.Conflict()
        with db_api.writer.using(ctx):
            service.webhook_nonce = webhooks.new_nonce()
            service.save()
        return {'webhooks': webhook_view(service)}


def webhook_view(service):
    urls = webhooks.urls_for(service, pecan.request.host_url)
    if urls is None:
        return {'enabled': False}
    return dict(urls, enabled=True)


class Env:
    def __init__(self, service_id, handler=None):
        self.service_id = service_id
        self.handler = handler or handlers.Handler()

    @pecan.expose('json')
    def index(self, **kwargs):
        ctx = common.context()
        service = objects.Service.get_by_id(ctx, self.service_id)
        method = pecan.request.method
        if method not in ('GET', 'PUT', 'PATCH'):
            pecan.abort(405)
        common.authorize('secret', 'get_all' if method == 'GET' else 'update',
                         service)
        if pecan.request.params:
            raise exception.Invalid()
        changes = []
        if method != 'GET':
            incoming = schemas.body(schemas.ENV, 'env')
            pushes = bool(service.is_engine_service and service.engine_ref)
            with secret_writes(ctx) as refs, db_api.writer.using(ctx):
                if pushes:
                    # One-click services push env to the engine now.
                    claim_service(
                        ctx, service,
                        states.RECONCILING, owner='settings_sync')
                    if service.status == states.DELETING:
                        raise exception.Conflict()
                else:
                    settings_lock(ctx, service)
                changes = write_env(ctx, service, incoming,
                                    method == 'PUT', refs)
                if changes and not pushes:
                    service.settings_changed_at = timeutils.utcnow()
                if changes:
                    service.save()  # New revision for the conductor receipt.
                elif pushes:
                    # A no-op save leaves the service untouched.
                    service.release_task(states.RECONCILING)
            if changes:
                if pushes:
                    self.handler.update_service_env(ctx, service)
                _notify_env(ctx, changes)
                pecan.response.headers['X-Coast-Redeploy-Required'] = 'true'
        return {'env': {row.key: {
            'value': None if row.is_secret else secret_store.call(
                'get', ctx, row.value_ref), 'secret': row.is_secret,
            'version': row.version, 'build_time': row.build_time}
            for row in all_rows(
                objects.SecretList, ctx, {'service_id': service.id})}}


class Dockerfile:
    """Protected desired Dockerfile; saves never deploy."""

    def __init__(self, service_id, handler):
        self.service_id = service_id
        self.handler = handler

    @pecan.expose('json')
    def index(self, **kwargs):
        if not microversions.at_least(
                _request_version(), microversions.PASTED_SOURCE_VERSION):
            pecan.abort(404)
        method = pecan.request.method
        if method not in ('GET', 'PUT'):
            pecan.abort(405)
        ctx = common.context()
        service = objects.Service.get_by_id(ctx, self.service_id)
        common.authorize('service', 'get_dockerfile' if method == 'GET' else
                         'update_dockerfile', service)
        if pecan.request.params:
            raise exception.Invalid()
        source = objects.Source.get_by_id(ctx, service.source_id)
        if source.type != 'dockerfile' or not service.dockerfile_ref:
            raise exception.Conflict('Dockerfile content is unavailable.')
        pecan.response.headers['Cache-Control'] = 'no-store'
        if method == 'GET':
            return {'dockerfile': {'text': secret_store.call(
                'get', ctx, service.dockerfile_ref)}}
        incoming = schemas.body(schemas.document(
            {'text': {'type': 'string', 'minLength': 1}}, ['text']),
            'dockerfile')
        secret_limits.validate(incoming['text'], 'dockerfile',
                               cfg.CONF.api.max_compose_bytes)
        with secret_writes(ctx) as refs, db_api.writer.using(ctx):
            db_api._lock_project(ctx.session, ctx.project_id)
            stack_open(ctx, service.stack_id)
            service = objects.Service.obj_from_db(
                ctx, db_api.services._get(ctx, service.id, lock=True))
            service.acquire_task(states.RECONCILING, owner='settings_sync')
            if service.status == states.DELETING:
                raise exception.Conflict()
            reference = secret_store.call(
                'put', ctx, incoming['text'],
                secret_store.SecretIdentity.for_resource(
                    service, 'dockerfile'))
            refs.append(reference)
            service.dockerfile_ref = reference
            service.settings_changed_at = timeutils.utcnow()
            service.save()
            service.release_task(states.RECONCILING)
        notifications.completed(ctx, service, 'update')
        pecan.response.headers['X-Coast-Redeploy-Required'] = 'true'
        return {'dockerfile': incoming}


class Compose:
    """Read and replace protected desired compose, then deploy separately."""

    def __init__(self, service_id, handler):
        self.service_id = service_id
        self.handler = handler

    @pecan.expose('json')
    def index(self, **kwargs):
        if not microversions.at_least(_request_version(), '1.12'):
            pecan.abort(404)
        method = pecan.request.method
        if method not in ('GET', 'PUT'):
            pecan.abort(405)
        ctx = common.context()
        service = objects.Service.get_by_id(ctx, self.service_id)
        common.authorize('service', 'get_compose' if method == 'GET' else
                         'update_compose', service)
        if pecan.request.params:
            raise exception.Invalid()
        source = objects.Source.get_by_id(ctx, service.source_id)
        if not (service.is_engine_service or
                (source.build_pack == 'compose' and service.compose_ref)):
            raise exception.Conflict('No Coast-managed compose content.')
        stack = objects.Stack.get_by_id(ctx, service.stack_id)
        if stack.driver == 'coolify_v4' and not service.is_engine_service:
            raise exception.Conflict(
                'The pinned engine cannot edit application compose through '
                'its public API.')
        pecan.response.headers['Cache-Control'] = 'no-store'
        if method == 'GET':
            reference = service.compose_ref
            if not reference:
                reference = self.handler.rpc.import_compose(ctx, service.id)
            if not reference:
                raise exception.Conflict(
                    'Compose content is unavailable or the service is busy.')
            return {'compose': {'yaml': secret_store.call(
                'get', ctx, reference)}}
        incoming = schemas.body(schemas.document(
            {'yaml': {'type': 'string', 'minLength': 1}}, ['yaml']), 'compose')
        try:
            document = compose_rules.parse(incoming['yaml'])
        except exception.Invalid:
            if source.type == 'compose':
                raise exception.Invalid('Invalid compose.') from None
            raise
        with secret_writes(ctx) as refs, db_api.writer.using(ctx):
            # Serialize Compose/file checks before locking the service.
            db_api._lock_project(ctx.session, ctx.project_id)
            stack_open(ctx, service.stack_id)
            # Compose cannot change under an engine deploy or stack teardown.
            service = objects.Service.obj_from_db(
                ctx, db_api.services._get(ctx, service.id, lock=True))
            service.acquire_task(
                states.RECONCILING, owner='settings_sync')
            if service.status == states.DELETING:
                raise exception.Conflict()
            file_mounts.check_compose(ctx, service.id, document)
            storage_rules.account_compose_volumes(ctx, service, document)
            reference = secret_store.call(
                'put', ctx, incoming['yaml'],
                secret_store.SecretIdentity.for_resource(service, 'compose'))
            refs.append(reference)
            service.compose_ref = reference
            service.settings_changed_at = timeutils.utcnow()
            service.save()
            service.release_task(states.RECONCILING)
        notifications.completed(ctx, service, 'update')
        pecan.response.headers['X-Coast-Redeploy-Required'] = 'true'
        return {'compose': {'yaml': incoming['yaml']}}


class Containers:
    """Read-only container routes (API 1.13)."""

    def __init__(self, service_id, handler, container=None):
        self.service_id = service_id
        self.handler = handler
        self.container = container

    @pecan.expose()
    def _lookup(self, container, *remainder):
        if self.container is not None:
            pecan.abort(404)
        return Containers(self.service_id, self.handler, container), remainder

    def _read(self, logs=False):
        if not microversions.at_least(_request_version(), '1.13'):
            pecan.abort(404)
        if pecan.request.method != 'GET':
            pecan.abort(405)
        ctx = common.context()
        service = objects.Service.get_by_id(ctx, self.service_id)
        action = ('get_container_logs' if logs else 'get_container' if
                  self.container is not None else 'get_containers')
        common.authorize('service', action, service)
        source = objects.Source.get_by_id(ctx, service.source_id)
        if not service.is_engine_service and source.build_pack != 'compose':
            raise exception.Conflict(
                'Container view requires a compose service.')
        params = dict(pecan.request.params)
        if set(params) - ({'since', 'tail'} if logs else set()):
            raise exception.Invalid()
        try:
            if 'tail' in params:
                params['tail'] = int(params['tail'])
                if not 0 <= params['tail'] <= 10000:
                    raise ValueError()
            if 'since' in params:
                datetime.datetime.fromisoformat(params['since'].replace(
                    'Z', '+00:00'))
        except ValueError:
            raise exception.Invalid() from None
        if self.container is not None and (
                not compose_rules.NAME.fullmatch(self.container) or
                len(self.container) > 130):
            pecan.abort(404)
        result = self.handler.rpc.get_containers(
            ctx, service.id, container=self.container, logs=logs,
            options=params)
        if result is None:
            pecan.abort(404)
        pecan.response.headers['Cache-Control'] = 'no-store'
        return {'logs': result} if logs else result

    @pecan.expose('json')
    def index(self, **kwargs):
        return self._read()

    @pecan.expose('json')
    def logs(self, **kwargs):
        if self.container is None:
            pecan.abort(404)
        return self._read(logs=True)


class Metrics:
    def __init__(self, service_id):
        self.service_id = service_id

    @pecan.expose('json')
    def index(self, **kwargs):
        if not microversions.at_least(
                _request_version(), microversions.METRICS_VERSION):
            pecan.abort(404)
        if pecan.request.method != 'GET':
            pecan.abort(405)
        ctx = common.context()
        try:
            policy.authorize(ctx, 'ADMIN', {'project_id': ctx.project_id})
            ctx.is_admin = True
        except exception.NotAuthorized:
            ctx.is_admin = False
        service = db_api.services.get(ctx, self.service_id,
                                      all_projects=ctx.is_admin)
        common.authorize('service', 'get_one', service)
        params = pecan.request.params
        range_name = params.get('range', '1h')
        if (set(params) - {'range'} or len(params.getall('range')) > 1 or
                range_name not in metrics.RANGES):
            raise exception.Invalid('Invalid metrics range.')
        return {'metrics': metrics.read(ctx, service.id, range_name)}
