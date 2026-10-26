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

"""API history: 1.0 initial; 1.1 stack plans; 1.2 domain TLS;
1.3 app webhooks; 1.4 optional HTTP checks; 1.5 stored runtime output;
1.6 application env build-time defaults; 1.7 template HTTP defaults;
1.8 stack event timeline; 1.9 container shell sessions;
1.10 persistent named storage; 1.11 explicit workload kind;
1.12 compose editor; 1.13 service container views;
1.14 image source updates; 1.15 container config files;
1.16 observed storage and stock template Docker socket;
1.17 unrestricted dedicated worker workloads;
1.18 admin stack inventory; 1.19 typed service storage;
1.20 database backups; 1.21 service CPU and memory metrics;
1.22 customer Designate DNS; 1.23 primary application address;
1.24 task holder diagnostics; 1.25 worker ingress rules;
1.26 server resize and rolling worker resize;
1.27 customer server flavors and allowed flavor catalog;
1.28 app source default workload kind; 1.29 databases;
1.30 private Git deploy keys;
1.31 pasted Dockerfile and Compose sources."""

import re

from webob import exc
from webob import Request

from coast.api import errors


HEADER = 'OpenStack-API-Version'
MIN_VERSION = '1.0'
MAX_VERSION = '1.31'
DATABASE_VERSION = '1.29'
DEPLOY_KEY_VERSION = '1.30'
PASTED_SOURCE_VERSION = '1.31'
APP_KIND_DEFAULT_VERSION = '1.28'
FLAVOR_VERSION = '1.27'
RESIZE_VERSION = '1.26'
PRIMARY_DOMAIN_VERSION = '1.23'
DNS_VERSION = '1.22'
METRICS_VERSION = '1.21'
STORAGE_TYPES_VERSION = '1.19'
BACKUP_VERSION = '1.20'
ADMIN_STACK_VERSION = '1.18'
IMAGE_UPDATE_VERSION = '1.14'
VERSION_HISTORY = {
    '1.29': 'Databases',
    '1.30': 'Private Git apps with generated or uploaded deploy keys',
    '1.31': 'Pasted Dockerfile and Compose sources',
    '1.28': 'App sources default to app kind on create',
    '1.27': 'Customer server flavors and allowed flavor catalog',
    '1.26': 'Customer server resize and rolling worker resize',
    '1.25': 'Customer worker ingress rules',
    '1.24': 'Read-only task holder and start time',
    '1.23': 'Primary custom domains and optional default address',
    '1.22': 'Customer Designate zones and automatic DNS',
    '1.0': 'Initial API',
    '1.1': 'Stack role plans and images (images accepted in later versions)',
    '1.2': 'Per-domain TLS mode letsencrypt or none',
    '1.3': 'Per-app deploy and Git host webhooks',
    '1.4': 'Optional per-app HTTP health checks',
    '1.5': 'Persistent failure runtime logs and service reconcile fault',
    '1.6': 'Application env variables default to build-time exposure',
    '1.7': 'Template HTTP port metadata and HTTP health defaults',
    '1.8': 'Stack event timeline',
    '1.9': 'Container shell sessions',
    '1.10': 'Persistent named storage for apps and services',
    '1.11': 'Explicit app or service kind on create',
    '1.12': 'Protected compose editor for services',
    '1.13': 'Read-only compose service container views and logs',
    '1.14': 'In-place image source updates',
    '1.15': 'Container config file mounts',
    '1.16': 'Observed storage and stock template Docker socket',
    '1.17': 'Unrestricted workloads on dedicated stack workers',
    '1.21': 'Per-container CPU and memory metrics',
    '1.19': 'Bind mounts and typed service storage',
    '1.18': 'Admin stack inventory and cross-project infrastructure reads',
    '1.20': 'Database backup policies, backups, downloads and restore',
}


def at_least(current, minimum):
    """Return whether a negotiated version is at or above minimum."""
    def parts(value):
        return tuple(int(part) for part in value.split('.'))

    return parts(current) >= parts(minimum)


def negotiate(value):
    """Return a supported version; absent headers select the minimum."""
    if value is None:
        return MIN_VERSION
    match = re.fullmatch(r'app-hosting ([0-9]+\.[0-9]+|latest)', value)
    if not match:
        raise exc.HTTPBadRequest()
    version = match.group(1)
    if version == 'latest':
        return MAX_VERSION
    # Compare digit strings without converting untrusted, unbounded integers.
    if version not in VERSION_HISTORY:
        raise exc.HTTPNotAcceptable()
    return version


class MicroversionMiddleware:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        request = Request(environ)
        if request.path_info == '/healthcheck':
            return self.application(environ, start_response)
        version = MIN_VERSION
        try:
            version = negotiate(request.headers.get(HEADER))
            environ['coast.api_version'] = version
            response = request.get_response(self.application)
        except exc.HTTPException as error:
            response = errors.error_response(error.status_int)
        response.headers[HEADER] = 'app-hosting ' + version
        vary = list(response.vary or [])
        if HEADER.lower() not in [item.lower() for item in vary]:
            vary.append(HEADER)
        response.vary = vary
        return response(environ, start_response)


def factory(global_conf, **local_conf):
    return MicroversionMiddleware
