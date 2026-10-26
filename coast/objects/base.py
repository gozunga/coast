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

"""Versioned objects and project-scoped persistence helpers."""

import copy

from oslo_utils import timeutils
from oslo_versionedobjects import base

from coast.common import exception
from coast.common import states
from coast.db import api


class CoastObjectRegistry(base.VersionedObjectRegistry):
    pass


class CoastObject(base.VersionedObject, base.VersionedObjectDictCompat):
    OBJ_SERIAL_NAMESPACE = 'coast_object'
    OBJ_PROJECT_NAMESPACE = 'coast'

    def obj_what_changed(self):
        changes = super().obj_what_changed()
        for name, original in getattr(self, '_json_baselines', {}).items():
            if self.obj_attr_is_set(name) and getattr(self, name) != original:
                changes.add(name)
        return changes

    def obj_reset_changes(self, fields=None, recursive=False):
        super().obj_reset_changes(fields=fields, recursive=recursive)
        for name in getattr(self, '_json_baselines', {}):
            if (not fields or name in fields) and self.obj_attr_is_set(name):
                self._json_baselines[name] = copy.deepcopy(getattr(self, name))

    @classmethod
    def obj_from_db(cls, context, row):
        obj = cls(context=context)
        obj._from_db(row)
        return obj

    def _from_db(self, row):
        for name in self.fields:
            setattr(self, name, getattr(row, name))
        self.obj_reset_changes()
        return self


class CoastObjectSerializer(base.VersionedObjectSerializer):
    OBJ_BASE_CLASS = CoastObject


class RepositoryObject(CoastObject):
    """Delegate all authorization scope and write rules to the DB API."""

    @base.remotable_classmethod
    def get_by_id(cls, context, id, all_projects=False, show_deleted=False):
        return cls.obj_from_db(context, cls.repository.get(
            context, id, all_projects=all_projects, show_deleted=show_deleted))

    @base.remotable
    def create(self, all_projects=False):
        return self._from_db(self.repository.create(
            self._context, self.obj_get_changes(), all_projects=all_projects))

    @base.remotable
    def save(self, all_projects=False):
        # Do not discard forbidden changes: the repository must reject them.
        return self._from_db(self.repository.update(
            self._context, self.id, self.obj_get_changes(),
            all_projects=all_projects))

    @base.remotable
    def destroy(self, all_projects=False):
        self.repository.delete(self._context, self.id,
                               all_projects=all_projects)
        if 'deleted_at' in self.fields:
            self._from_db(self.repository.get(
                self._context, self.id, all_projects=all_projects,
                show_deleted=True))
        else:
            self.obj_reset_changes()


class TaskObject(RepositoryObject):

    def _task(self, new, expected, all_projects, owner=None):
        # Refreshing the whole row must not silently discard pending edits.
        if self.obj_what_changed():
            raise exception.Invalid()
        row = api.cas_task_state(self._context, self.repository.model,
                                 self.id, new=new, expected=expected,
                                 all_projects=all_projects, owner=owner)
        return self._from_db(row)

    @base.remotable
    def acquire_task(self, op, all_projects=False):
        return self._task(op, states.NONE, all_projects)

    @base.remotable
    def release_task(self, op, all_projects=False):
        return self._task(states.NONE, op, all_projects)


class OwnedTaskObject(TaskObject):

    @base.remotable
    def acquire_task(self, op, all_projects=False, owner=None):
        result = self._task(op, states.NONE, all_projects, owner=owner)
        # Keep the accepted claim, not a later database observation. This
        # request-local snapshot is forwarded only by the API handoff.
        claims = getattr(self._context, '_task_claims', {})
        claims[(states.kind(self.repository.resource), self.id)] = {
            'resource': states.kind(self.repository.resource), 'id': self.id,
            'state': self.task_state, 'owner': self.task_owner,
            'started_at': timeutils.normalize_time(
                self.task_started_at).isoformat()}
        self._context._task_claims = claims
        return result


class CoastObjectList(base.ObjectListBase, CoastObject):
    pass


class RepositoryObjectList(CoastObjectList):

    @base.remotable_classmethod
    def get_all(cls, context, filters=None, marker=None, limit=None,
                sort_key='created_at', sort_dir='asc', all_projects=False,
                show_deleted=False):
        rows = cls.object_class.repository.list(
            context, filters=filters, marker=marker, limit=limit,
            sort_key=sort_key, sort_dir=sort_dir, all_projects=all_projects,
            show_deleted=show_deleted)
        obj = cls(context=context, objects=[
            cls.object_class.obj_from_db(context, row) for row in rows])
        obj.obj_reset_changes()
        return obj
