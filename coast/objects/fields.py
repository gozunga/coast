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

"""Coast field types. Keep enum values in common.states."""

import copy

from oslo_serialization import jsonutils
from oslo_versionedobjects import fields

from coast.common import states


class ReferenceField(fields.StringField):
    """Serialize references normally, but never display them in repr."""

    def stringify(self, value):
        return '***'


class JSON(fields.FieldType):
    """A JSON value, including nested containers and scalar values."""

    def coerce(self, obj, attr, value):
        try:
            jsonutils.dumps(value, default=None, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError('Invalid JSON value') from None
        if not hasattr(obj, '_json_baselines'):
            obj._json_baselines = {}
        obj._json_baselines[attr] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def stringify(self, value):
        # Frozen deployment specs can contain nested secret references.
        return '***'


class JSONField(fields.AutoTypedField):
    AUTO_TYPE = JSON()


class EnvironmentStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.STACK_STATES))


class AppgroupStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.APPGROUP_STATES))


class AppEnvironmentStateField(fields.BaseEnumField):
    """Status of an environment row.

    EnvironmentStateField is the stack status set. The repository name
    app_environment keeps the Q47 stack alias `environment`.
    """

    AUTO_TYPE = fields.Enum(valid_values=sorted(
        states.APP_ENVIRONMENT_STATES))


class ServerStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.SERVER_STATES))


class VolumeStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.VOLUME_STATES))


class StorageStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.STORAGE_STATES))


class AppStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.SERVICE_STATES))


class DeploymentStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.DEPLOYMENT_STATES))


class DomainStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.DOMAIN_STATES))


class ExportStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.EXPORT_STATES))


class TaskStateField(fields.BaseEnumField):
    AUTO_TYPE = fields.Enum(valid_values=sorted(states.TASK_STATES))
