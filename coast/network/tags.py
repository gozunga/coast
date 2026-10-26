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

"""Create a Neutron resource, then set its tags.

Neutron rejects ``tags`` in a create body. The tag API is a separate
request. ``set_tags`` sends ``PUT /v2.0/{collection}/{id}/tags``.

https://docs.openstack.org/neutron/latest/contributor/internals/tag.html
https://docs.openstack.org/openstacksdk/latest/user/proxies/network.html
"""

from oslo_log import log as logging


LOG = logging.getLogger(__name__)

# SDK proxy method suffixes. 'ip' is the floating IP proxy.
_KINDS = ('network', 'subnet', 'router', 'security_group', 'port', 'ip')


def create_tagged(proxy, kind, tags, **attrs):
    """Create ``kind`` without tags, then replace its tags.

    Return the tagged resource. If tagging fails, delete the new
    resource and re-raise. A delete failure does not replace the tag
    error. Callers still find an existing resource by name and tags.
    """
    if kind not in _KINDS:
        raise ValueError('Unsupported Neutron resource')
    resource = getattr(proxy, 'create_' + kind)(**attrs)
    try:
        return proxy.set_tags(resource, list(tags))
    except Exception:
        try:
            getattr(proxy, 'delete_' + kind)(resource, ignore_missing=True)
        except Exception:
            LOG.debug('Delete of untagged %s failed', kind)
        raise
