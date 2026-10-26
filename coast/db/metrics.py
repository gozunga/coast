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

"""Scoped ingestion and bounded, transactional time-series retention."""

import datetime
import math
import re

from oslo_config import cfg
from oslo_utils import timeutils

from coast.common import context as context_module
from coast.common import exception
from coast.db import api
from coast.db import models

RANGES = {'15m': (900, 30), '1h': (3600, 30), '6h': (21600, 30),
          '24h': (86400, 30), '7d': (604800, 300),
          '30d': (2592000, 3600)}
EPOCH = datetime.datetime(1970, 1, 1)
FIELDS = {'engine_ref', 'container_name', 'cpu_percent', 'memory_bytes',
          'memory_limit_bytes'}


def bucket(timestamp, step):
    seconds = (timestamp - EPOCH).total_seconds()
    return EPOCH + datetime.timedelta(seconds=int(seconds // step) * step)


def validate(sample):
    if not isinstance(sample, dict) or set(sample) != FIELDS:
        raise ValueError('Invalid metric')
    patterns = {'engine_ref': r'[A-Za-z0-9_-]{1,128}',
                'container_name': r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}'}
    for key, pattern in patterns.items():
        if not isinstance(sample[key], str) or not re.fullmatch(
                pattern, sample[key]):
            raise ValueError('Invalid metric')
    cpu = sample['cpu_percent']
    if type(cpu) not in (int, float) or not math.isfinite(cpu) or not (
            0 <= cpu <= 1000000):
        raise ValueError('Invalid metric')
    for key in ('memory_bytes', 'memory_limit_bytes'):
        value = sample[key]
        if value is None and key == 'memory_limit_bytes':
            continue
        if type(value) is not int or not 0 <= value < 2 ** 63:
            raise ValueError('Invalid metric')


def ingest(owner, server, sample, timestamp):
    validate(sample)
    if server.role != 'worker':
        raise ValueError('Invalid metrics owner')
    with api.writer.using(owner) as session:
        query = session.query(models.Service).filter_by(
            project_id=server.project_id, stack_id=server.stack_id,
            server_id=server.id, engine_ref=sample['engine_ref'],
            deleted_at=None)
        service = api._lock_query(session, query, models.Service).one_or_none()
        if service is None:
            raise exception.NotFound()
        key = dict(service_id=service.id,
                   container_name=sample['container_name'], resolution=30,
                   timestamp=bucket(timestamp, 30))
        if session.query(models.ServiceMetric).filter_by(**key).first():
            return
        session.add(models.ServiceMetric(
            **key, cpu_percent=sample['cpu_percent'],
            memory_bytes=sample['memory_bytes'],
            memory_limit_bytes=sample['memory_limit_bytes'], sample_count=1))


def average(rows):
    count = sum(row.sample_count for row in rows)
    limit = None if any(row.memory_limit_bytes is None for row in rows) else (
        sum(row.memory_limit_bytes * row.sample_count for row in rows) //
        count)
    return dict(cpu_percent=sum(row.cpu_percent * row.sample_count
                                for row in rows) / count,
                memory_bytes=sum(row.memory_bytes * row.sample_count
                                 for row in rows) // count,
                memory_limit_bytes=limit, sample_count=count)


def read(ctx, service_id, range_name, now=None):
    seconds, step = RANGES[range_name]
    now = now or timeutils.utcnow()
    start = now - datetime.timedelta(seconds=seconds)
    groups = {}
    with api.reader.using(ctx) as session:
        # Scope the samples query itself, even after the controller's lookup.
        query = session.query(models.ServiceMetric).join(
            models.Service).filter(
            models.Service.id == service_id,
            models.Service.deleted_at.is_(None),
            models.ServiceMetric.timestamp >= bucket(start, step),
            models.ServiceMetric.timestamp <= now)
        if not ctx.is_admin:
            query = query.filter(models.Service.project_id == ctx.project_id)
        for row in query.yield_per(1000):
            key = (row.container_name, bucket(row.timestamp, step))
            totals = groups.setdefault(key, dict(count=0, cpu=0.0, memory=0,
                                                 limit=0))
            count = row.sample_count
            totals['count'] += count
            totals['cpu'] += row.cpu_percent * count
            totals['memory'] += row.memory_bytes * count
            if row.memory_limit_bytes is None:
                totals['limit'] = None
            elif totals['limit'] is not None:
                totals['limit'] += row.memory_limit_bytes * count
        containers = {}
        for (name, timestamp), totals in sorted(groups.items()):
            count = totals['count']
            containers.setdefault(name, []).append(dict(
                timestamp=timestamp.isoformat() + 'Z',
                cpu_percent=totals['cpu'] / count,
                memory_bytes=totals['memory'] // count,
                memory_limit_bytes=(None if totals['limit'] is None else
                                    totals['limit'] // count)))
    return dict(service_id=service_id, range=range_name, step_seconds=step,
                containers=[dict(name=name, points=points)
                            for name, points in containers.items()])


def maintain(conf=cfg.CONF, now=None):
    now = now or timeutils.utcnow()
    options = conf.metrics
    ctx = context_module.RequestContext(is_admin=True)
    for source, target, retention in (
            (30, 300, options.detail_retention),
            (300, 3600, options.five_minute_retention)):
        cutoff = bucket(now - datetime.timedelta(seconds=retention), target)
        for unused in range(options.batch_size):
            if not _roll_one(ctx, source, target, cutoff):
                break
    with api.writer.using(ctx) as session:
        rows = session.query(models.ServiceMetric).filter(
            models.ServiceMetric.resolution == 3600,
            models.ServiceMetric.timestamp < now - datetime.timedelta(
                seconds=options.hourly_retention)).order_by(
                    models.ServiceMetric.timestamp).limit(
                        options.batch_size).with_for_update().all()
        for row in rows:
            session.delete(row)


def _roll_one(ctx, source, target, cutoff):
    with api.writer.using(ctx) as session:
        first = session.query(models.ServiceMetric).filter(
            models.ServiceMetric.resolution == source,
            models.ServiceMetric.timestamp < cutoff).order_by(
                models.ServiceMetric.timestamp).first()
        if first is None:
            return False
        # Serialize with ingest, other sweepers and service deletion.
        service = session.query(models.Service).filter_by(id=first.service_id)
        api._lock_query(session, service, models.Service).one()
        start = bucket(first.timestamp, target)
        key = dict(service_id=first.service_id,
                   container_name=first.container_name)
        rows = session.query(models.ServiceMetric).filter_by(
            **key, resolution=source).filter(
                models.ServiceMetric.timestamp >= start,
                models.ServiceMetric.timestamp < start + datetime.timedelta(
                    seconds=target)).with_for_update().all()
        if not rows:
            return True
        existing = session.query(models.ServiceMetric).filter_by(
            **key, resolution=target,
            timestamp=start).with_for_update().first()
        result = average(rows + ([existing] if existing else []))
        if existing:
            for key_, value in result.items():
                setattr(existing, key_, value)
        else:
            session.add(models.ServiceMetric(
                **key, resolution=target, timestamp=start, **result))
        for row in rows:
            session.delete(row)
    return True
