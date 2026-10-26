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

"""Injectable secret storage with an explicitly configured Barbican backend.

The default fails closed. Never substitute a process-local store in production:
all values, including non-secret env vars, require durable opaque references.
"""

from dataclasses import dataclass
import re

from oslo_config import cfg

from coast.common import exception


@dataclass(frozen=True)
class SecretIdentity:
    """Attribution only: identifiers must never contain secret material.

    Project-wide signing keys use the project ID as their stack scope, since
    they outlive individual stacks. All other owners are stacks, services or
    backup policies. Env variable names and values are deliberately omitted.
    """

    stack_id: str
    kind: str
    owner_id: str
    project_id: str | None = None

    def __post_init__(self):
        for value in (self.stack_id, self.kind, self.owner_id):
            if not isinstance(value, str) or not re.fullmatch(
                    r'[a-zA-Z0-9_-]+', value):
                raise ValueError('Invalid secret identity')

    @property
    def name(self):
        return 'coast:%s:%s:%s' % (self.stack_id, self.kind, self.owner_id)

    @property
    def metadata(self):
        result = {'coast-stack': self.stack_id, 'coast-kind': self.kind,
                  'coast-owner': self.owner_id}
        if self.project_id:
            result['coast-project'] = self.project_id
        return result

    @classmethod
    def for_resource(cls, resource, kind):
        return cls(getattr(resource, 'stack_id', None) or resource.id,
                   kind, resource.id, resource.project_id)


class SecretStore:
    def put(self, context, value, identity: SecretIdentity):
        raise exception.CoastException('Secret storage is not configured.')

    def get(self, context, reference):
        raise exception.CoastException('Secret storage is not configured.')

    def delete(self, context, reference):
        raise exception.CoastException('Secret storage is not configured.')


STORE = None


def get_store():
    if STORE is not None:
        return STORE
    if cfg.CONF.secrets.backend == 'barbican':
        from coast.common import secrets
        return secrets.BarbicanSecretStore()
    return SecretStore()


def call(operation, context, *args):
    try:
        if operation == 'put' and (len(args) != 2 or not isinstance(
                args[1], SecretIdentity)):
            raise ValueError('Secret identity is required')
        return getattr(get_store(), operation)(context, *args)
    except Exception:
        # Backend errors may contain supplied values or authentication secrets.
        raise exception.SecretStoreUnavailable() from None
