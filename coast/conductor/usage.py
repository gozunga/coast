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

"""Current-resource snapshots for the previous complete UTC audit period."""

import datetime

from oslo_log import log as logging
from oslo_service import periodic_task
from oslo_utils import timeutils

from coast.common import context as context_module
from coast.common import notifications
from coast.db import api as db_api
from coast.db import models


LOG = logging.getLogger(__name__)
BATCH_SIZE = 1000
RESOURCES = {
    'environment': (models.Stack, ('plan',)),
    'server': (models.Server, ('stack_id', 'flavor_id')),
    'volume': (models.Volume, ('stack_id', 'server_id', 'size_gb')),
    'app': (models.Service, ('stack_id', 'server_id', 'replicas',
                             'cpu_limit', 'memory_limit_mb')),
}


def audit_bounds(now, period):
    """Return the previous complete calendar hour, day, or month in UTC."""
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    end = now.replace(minute=0, second=0, microsecond=0)
    if period == 'hour':
        if end >= now:
            end -= datetime.timedelta(hours=1)
        begin = end - datetime.timedelta(hours=1)
    elif period == 'day':
        end = end.replace(hour=0)
        if end >= now:
            end -= datetime.timedelta(days=1)
        begin = end - datetime.timedelta(days=1)
    elif period == 'month':
        end = end.replace(day=1, hour=0)
        if end >= now:
            end = (end - datetime.timedelta(days=1)).replace(day=1)
        begin = (end - datetime.timedelta(days=1)).replace(day=1)
    else:
        raise ValueError('Unsupported usage audit period.')
    return begin, end


def timestamp(value):
    return value.isoformat(timespec='seconds') + 'Z'


class UsageTasks(periodic_task.PeriodicTasks):
    def __init__(self, conf, notifier=None):
        super().__init__(conf)
        self.notifier = notifier
        self.last_period = None

    @periodic_task.periodic_task(spacing=1, run_immediately=True)
    def audit(self, context):
        try:
            self._audit()
        except Exception:
            # DB/transport exceptions can contain credentials. Retain the
            # incomplete period for another sweep without logging details.
            LOG.warning('Usage audit sweep failed.')

    def _audit(self):
        begin, end = audit_bounds(timeutils.utcnow(),
                                  self.conf.notifications.usage_audit_period)
        if self.last_period == (begin, end):
            return
        # This control-plane task is not a tenant-facing path. Select only
        # public usage columns; never load secret references into payloads.
        ctx = context_module.RequestContext(is_admin=True)
        for resource, (model, extra) in RESOURCES.items():
            names = ('id', 'project_id', 'status', 'created_at') + extra
            marker = None
            while True:
                with db_api.reader.using(ctx):
                    query = ctx.session.query(
                        *(getattr(model, name) for name in names))
                    if hasattr(model, 'deleted_at'):
                        query = query.filter(model.deleted_at.is_(None))
                    if marker is not None:
                        query = query.filter(model.id > marker)
                    rows = query.order_by(model.id).limit(BATCH_SIZE).all()
                if not rows:
                    break
                for row in rows:
                    payload = dict(zip(names, row))
                    payload['resource_id'] = payload.pop('id')
                    payload['created_at'] = timestamp(payload['created_at'])
                    payload.update(
                        audit_period_beginning=timestamp(begin),
                        audit_period_ending=timestamp(end),
                        service_id=self.conf.notifications.service_id)
                    notifications.exists(ctx, resource, payload, self.conf,
                                         self.notifier)
                marker = rows[-1].id
        # Process-local suppression only: restart or another replica may
        # repeat a period. Consumers deduplicate by resource and audit bounds.
        self.last_period = (begin, end)
