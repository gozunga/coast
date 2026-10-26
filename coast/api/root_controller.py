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

"""Public API version discovery and v1 resource routes."""

from oslo_config import cfg
import pecan
from wsme import types as wtypes
from wsmeext import pecan as wsme_pecan

from coast.api import microversions
from coast.api.v1 import appgroup_controllers
from coast.api.v1 import backup_controllers
from coast.api.v1 import console_controllers
from coast.api.v1 import database_controllers
from coast.api.v1 import dns_controllers
from coast.api.v1 import flavor_controllers
from coast.api.v1 import plan_controllers
from coast.api.v1 import quota_controllers
from coast.api.v1 import service_controllers
from coast.api.v1 import stack_controllers
from coast.api.v1 import template_controllers


class Link(wtypes.Base):
    rel = wtypes.text
    href = wtypes.text


class Version(wtypes.Base):
    id = wtypes.text
    status = wtypes.text
    min_version = wtypes.text
    max_version = wtypes.text
    links = [Link]


class VersionResponse(wtypes.Base):
    versions = [Version]


class VersionDetailResponse(wtypes.Base):
    version = Version


def get_version():
    if pecan.request.method not in ('GET', 'HEAD'):
        pecan.abort(405)
    endpoint = (cfg.CONF.api.public_endpoint or
                pecan.request.application_url.removesuffix('/v1')).rstrip('/')
    return Version(id='v1', status='CURRENT',
                   min_version=microversions.MIN_VERSION,
                   max_version=microversions.MAX_VERSION,
                   links=[Link(rel='self', href=endpoint + '/v1/')])


class RootController:
    @wsme_pecan.wsexpose(VersionResponse)
    def index(self):
        return VersionResponse(versions=[get_version()])


class V1Controller:
    dns = dns_controllers.DNS()
    """Public v1 discovery and authenticated resource routes."""

    stacks = stack_controllers.Collection()
    apps = console_controllers.Apps()
    appgroups = appgroup_controllers.Appgroups()
    environments = appgroup_controllers.Environments()
    databases = database_controllers.Collection()
    database_engines = database_controllers.Engines()
    services = service_controllers.Collection()
    events = service_controllers.Events()
    backups = backup_controllers.Backups()
    plans = plan_controllers.Plans()
    flavors = flavor_controllers.Flavors()
    templates = template_controllers.Templates()
    quotas = quota_controllers.Quotas()

    @wsme_pecan.wsexpose(VersionDetailResponse)
    def index(self):
        return VersionDetailResponse(version=get_version())
