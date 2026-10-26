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

"""Swappable customer-project Designate credentials.

Trust IDs are opaque Keystone references, not bearer credentials. The
operator must grant the trustee service user the DNS roles in each trust.
"""

import abc
import copy
import logging

from keystoneauth1 import loading
from openstack import connection
from oslo_config import cfg

from coast.common import clients
from coast.common import exception


class CredentialProvider(abc.ABC):
    @abc.abstractmethod
    def proxy(self, project_id, zone_id=None, trust_id=None):
        """Return a DNS proxy restricted to the specified project."""


class ServiceProjectProvider(CredentialProvider):
    """Keep the optional operator-zone path in the service project."""

    def __init__(self, conf=cfg.CONF):
        self.conf = conf

    def proxy(self, project_id, zone_id=None, trust_id=None):
        proxy = copy.copy(clients.get_connection(
            'designate', conf=self.conf).dns)
        proxy.additional_headers = dict(proxy.additional_headers or {})
        proxy.logger = logging.Logger('coast.sdk.dns')
        proxy.logger.disabled = True
        return proxy


class ServiceAccountProvider(ServiceProjectProvider):
    def proxy(self, project_id, zone_id=None, trust_id=None):
        if not project_id:
            raise exception.Invalid()
        # Adapter headers belong to this copy, never the cached connection.
        proxy = super().proxy(project_id, zone_id, trust_id)
        proxy.additional_headers['X-Auth-Sudo-Project-ID'] = project_id
        return proxy


class TrustProvider(CredentialProvider):
    def __init__(self, conf=cfg.CONF):
        self.conf = conf

    def proxy(self, project_id, zone_id=None, trust_id=None):
        if not project_id or not trust_id:
            raise exception.Invalid('A linked-zone trust is required.')
        # A trust supplies the project scope; do not also request one.
        auth = loading.load_auth_from_conf_options(
            self.conf, 'service_auth', trust_id=trust_id,
            project_id=None, project_name=None, project_domain_id=None,
            project_domain_name=None)
        session = loading.load_session_from_conf_options(
            self.conf, 'service_auth', auth=auth,
            **clients._verify_override(self.conf, 'designate'))
        if session.get_project_id() != project_id:
            raise exception.Invalid('The trust belongs to another project.')
        clients._prepare_service(self.conf, 'designate')
        proxy = connection.Connection(
            session=session, oslo_conf=self.conf,
            service_types=[self.conf.designate.service_type]).dns
        proxy.logger = logging.Logger('coast.sdk.dns')
        proxy.logger.disabled = True
        return proxy


def get_provider(conf=cfg.CONF, trust_id=None):
    if trust_id or conf.dns.credential_provider == 'trust':
        return TrustProvider(conf)
    return ServiceAccountProvider(conf)
