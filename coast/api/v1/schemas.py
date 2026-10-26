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

"""Strict request schemas from the v1 examples."""

import jsonschema
import pecan

from coast.common import exception


def text(length):
    return {'type': 'string', 'minLength': 1, 'maxLength': length}


def document(properties, required=()):
    return {'type': 'object', 'properties': properties,
            'required': list(required), 'additionalProperties': False}


WORKER = document({'kind': {'enum': ['vm', 'baremetal']},
                   'plan': text(64)}, ('kind', 'plan'))
STACK = document({
    'name': text(255),
    'description': {'type': ['string', 'null'], 'maxLength': 255},
    'plan': text(64), 'workers': {'type': 'array', 'items': WORKER},
    'build_server': {'type': 'boolean'},
    'data_volume_gb': {'type': 'integer', 'minimum': 1}}, ('name', 'plan'))
STACK_1_1 = document({
    **STACK['properties'],
    'image': text(255),
    'build_server_plan': text(64),
    'build_server_image': text(255),
    'workers': {'type': 'array', 'minItems': 1,
                'items': document({'kind': {'enum': ['vm']},
                                   'plan': text(64), 'image': text(255)},
                                  ('kind', 'plan'))}},
    ('name', 'plan', 'workers'))


STACK_IMAGES = document({
    **STACK['properties'],
    **{key: STACK_1_1['properties'][key] for key in (
        'image', 'build_server_plan', 'build_server_image')},
    'workers': {'type': 'array', 'items': document({
        **WORKER['properties'], 'image': text(255)}, WORKER['required'])}},
    STACK['required'])


STACK_UPDATE = document({
    key: STACK['properties'][key] for key in ('name', 'description')})
APPGROUP = document({
    'name': text(255),
    'description': {'type': ['string', 'null'], 'maxLength': 255}},
    ('name',))
APPGROUP_UPDATE = document({
    key: APPGROUP['properties'][key] for key in ('name', 'description')})
SERVER = document({'name': text(255), 'kind': {'enum': ['vm', 'baremetal']},
                   'role': {'enum': ['worker', 'build']}, 'plan': text(64)},
                  ('kind', 'role', 'plan'))
STACK_DELETE = document({
    'snapshot_volumes': {'type': 'boolean'},
    'snapshot_retention_days': {'type': 'integer', 'minimum': 0},
    'export_before_delete': {'type': 'boolean'}})


def _nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


VOLUME = document({
    'name': text(255),
    'size_gb': {'type': 'integer', 'minimum': 1},
    'server_id': _nullable(text(36)),
    'volume_type': _nullable(text(64)),
    'mount_path': text(255),
    'purpose': {'enum': ['data', 'docker', 'scratch', 'controller']},
}, ('name', 'size_gb'))
VOLUME_EXTEND = document({
    'size_gb': {'type': 'integer', 'minimum': 1}}, ('size_gb',))
_STORAGE_NAME = (r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$')
_CONTAINER_PATH = (r'^/(?:[A-Za-z0-9._-]+)(?:/[A-Za-z0-9._-]+)*$')
STORAGE = document({
    'name': {'type': 'string', 'pattern': _STORAGE_NAME},
    'size_gb': {'type': 'integer', 'minimum': 1},
}, ('name', 'size_gb'))
ATTACH = document({
    'service_id': text(36),
    'container_path': {'type': 'string', 'pattern': _CONTAINER_PATH,
                       'maxLength': 255},
}, ('service_id', 'container_path'))


CONSOLE = document({
    'command': {'anyOf': [
        {'type': 'null'},
        {'type': 'array', 'minItems': 1, 'maxItems': 32,
         'items': {'type': 'string', 'minLength': 1, 'maxLength': 256}},
    ]},
})


def body(schema, envelope=None, optional=False):
    if optional and not pecan.request.body:
        return {}
    if pecan.request.content_type != 'application/json':
        raise exception.Invalid()
    try:
        value = pecan.request.json
        jsonschema.validate(value, document({envelope: schema}, (envelope,))
                            if envelope else schema)
    except (ValueError, jsonschema.ValidationError):
        # Validation errors include input values; never return or log them.
        raise exception.Invalid() from None
    return value[envelope] if envelope else value


DEPLOY_KEY = {'oneOf': [
    document({'generate': {'const': True}}, ('generate',)),
    document({'private_key': {'type': 'string'}},
             ('private_key',))]}
SSH_REPO = (r'^(?:git@[A-Za-z0-9.-]+:[^/?#\s][^?#\s]*|'
            r'ssh://git@[A-Za-z0-9.-]+(?::[0-9]+)?/[^?#\s]+)$(?![\s\S])')

# HTTPS rules remain unchanged; SSH requires an explicit deploy key.
SOURCE = document({
    'type': {'enum': ['git']},
    'repo_url': {'type': 'string', 'maxLength': 1024,
                 'anyOf': [
                     {'pattern': r'^https://[A-Za-z0-9.-]+'
                                 r'(?::[0-9]+)?/[^?#\s]+$'},
                     {'pattern': SSH_REPO}]},
    'deploy_key': DEPLOY_KEY,
    'branch': text(255),
    'build_pack': {'enum': ['nixpacks', 'dockerfile', 'static', 'compose']},
    'dockerfile_path': text(255), 'compose_path': text(255),
    'base_directory': text(255)}, ('type', 'repo_url'))
SOURCE['allOf'] = [{
    'if': {'required': ['deploy_key']},
    'then': {'properties': {'repo_url': {'pattern': SSH_REPO}}},
    'else': {'properties': {'repo_url': {'pattern': '^https://'}}},
}]
# Docker reference name grammar, without tag/digest.
IMAGE_COMPONENT = r'[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*'
IMAGE_DOMAIN = (r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?'
                r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*'
                r'|\[[A-Fa-f0-9:]+\])(?::[0-9]+)?')
IMAGE_NAME = {'type': 'string', 'maxLength': 255,
              'pattern': (r'^(?:' + IMAGE_DOMAIN + r'/)?' +
                          IMAGE_COMPONENT + r'(?:/' + IMAGE_COMPONENT +
                          r')*$(?![\s\S])')}
IMAGE_TAG = {'type': 'string',
             'pattern': r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$(?![\s\S])'}
IMAGE_SOURCE = document({'type': {'enum': ['image']},
                         'image': IMAGE_NAME, 'tag': IMAGE_TAG},
                        ('type', 'image', 'tag'))
GIT_SOURCE_UPDATE = document({key: SOURCE['properties'][key] for key in (
    'branch', 'build_pack', 'dockerfile_path', 'compose_path',
    'base_directory', 'deploy_key')})
IMAGE_SOURCE_UPDATE = document({'image': IMAGE_NAME, 'tag': IMAGE_TAG})


ENV_VALUE = document({'value': {'type': 'string'},
                      'secret': {'type': 'boolean'},
                      'build_time': {'type': 'boolean'}}, ('value',))
ENV = {'type': 'object',
       'propertyNames': {'type': 'string', 'minLength': 1, 'maxLength': 255,
                         'pattern': r'^[A-Za-z_][A-Za-z0-9_]*$'},
       'additionalProperties': {'anyOf': [ENV_VALUE, {'type': 'null'}]}}
SERVICE = document({
    'name': {'type': 'string', 'maxLength': 63,
             'pattern': r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$'},
    'appgroup_id': text(36),
    'environment_id': text(36),
    'server_id': {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 36},
    'source': {'oneOf': [
        SOURCE, IMAGE_SOURCE,
        document({'type': {'enum': ['dockerfile']},
                  'dockerfile': {'type': 'string', 'minLength': 1}},
                 ('type', 'dockerfile')),
        document({'type': {'enum': ['compose']},
                  'compose': {'type': 'string', 'minLength': 1}},
                 ('type', 'compose')),
        document({'type': {'enum': ['template']},
                  'template_id': text(255)}, ('type', 'template_id')),
        {'type': 'string', 'maxLength': 264,
         'pattern': r'^template:[A-Za-z0-9_-]+$'}]},
    'description': {'type': ['string', 'null'], 'maxLength': 255},
    'ports': {'type': ['string', 'null'], 'maxLength': 255},
    'replicas': {'type': 'integer', 'enum': [1]},
    'auto_deploy': {'type': 'boolean', 'enum': [False]},
    'cpu_limit': {'type': ['number', 'null'], 'exclusiveMinimum': 0},
    'memory_limit_mb': {'type': ['integer', 'null'], 'minimum': 1},
    'health_check': {'type': 'string', 'enum': ['none', 'http']},
    'health_check_port': {'type': ['integer', 'null'],
                          'minimum': 1, 'maximum': 65535},
    'health_check_path': {'type': 'string', 'maxLength': 255,
                          'pattern': r'^/[^\r\n ]*$(?![\s\S])'},
    'healthcheck_path': {'type': ['string', 'null'], 'maxLength': 255},
    'env': ENV, 'deploy': {'type': 'boolean'},
    'kind': {'type': 'string', 'enum': ['app', 'service']}},
    ('name', 'appgroup_id', 'environment_id', 'server_id', 'source'))
SERVICE_UPDATE = document({
    'default_domain_enabled': {'type': 'boolean'},
    **{key: SERVICE['properties'][key] for key in (
        'name', 'description', 'ports', 'cpu_limit', 'memory_limit_mb',
        'healthcheck_path', 'health_check', 'health_check_port',
        'health_check_path')},
    'source': {'anyOf': [GIT_SOURCE_UPDATE, IMAGE_SOURCE_UPDATE]}})
DEPLOYMENT = document({'source_ref': text(255)})
TLS_MODE = {'type': 'string', 'enum': ['letsencrypt', 'none']}
DOMAIN = document({
    'primary': {'type': 'boolean'},
    'fqdn': {'type': 'string', 'maxLength': 253,
             'pattern': r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+'
                        r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$'},
    'auto_dns': {'type': 'boolean'},
    'tls': TLS_MODE}, ('fqdn',))
DOMAIN_UPDATE = document({'tls': TLS_MODE}, ('tls',))


EXPORT = document({
    'volume_ids': _nullable({
        'type': 'array', 'minItems': 1, 'uniqueItems': True,
        'items': text(36)})})


STORAGE_MOUNT_FIELDS = {
    'type': {'enum': ['bind', 'volume']},
    'name': {'type': 'string', 'pattern': _STORAGE_NAME},
    'size_gb': {'type': 'integer', 'minimum': 1},
    'host_path': text(4096),
    'service_id': text(36),
    'container_path': {'type': 'string', 'pattern': _CONTAINER_PATH,
                       'maxLength': 255},
    'read_only': {'type': 'boolean'},
}
STORAGE_MOUNT = document(STORAGE_MOUNT_FIELDS,
                         ('type', 'service_id', 'container_path'))
STORAGE_EDIT = document({key: value for key, value in
                         STORAGE_MOUNT_FIELDS.items()
                         if key not in ('type', 'service_id', 'size_gb')})
