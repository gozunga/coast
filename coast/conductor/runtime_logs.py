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

"""Runtime output of crash-looping containers.

The engine API only returns container logs while the container is running.
A crash-looping container is running for a moment on each restart, so the
conductor retries briefly and keeps the last captured output in memory
(with a durable deployment tail) for service and deployment log views.
"""

import json
import time

from oslo_utils import strutils
from oslo_utils import timeutils

from coast.db import api as db_api
from coast.drivers import base
from coast import objects

# service_id -> (captured_at ISO string, [LogLine])
LAST = {}
MAX_SERVICES = 256


def remember(service_id, lines):
    lines = list(lines)
    if not lines:
        return
    if len(LAST) >= MAX_SERVICES and service_id not in LAST:
        LAST.pop(next(iter(LAST)))
    LAST[service_id] = (timeutils.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                        list(lines))


def burst(read, attempts=10, delay=0.5, sleep=None, nonempty=bool):
    """Retry empty output and driver errors within one five-second window."""
    sleep = sleep or time.sleep
    deadline = time.monotonic() + 5
    result, error = None, None
    for attempt in range(attempts):
        try:
            result = read()
            error = None
            if nonempty(result):
                return result
        except base.DriverError as caught:
            error = caught
        remaining = deadline - time.monotonic()
        if attempt + 1 >= attempts or remaining <= delay:
            break
        sleep(delay)
    if error:
        raise error
    return result


def capture(driver, endpoint, env, service, tail=200, attempts=10,
            delay=0.5, sleep=None, masks=None):
    """Catch a running interval; empty reads must not end the burst."""
    lines = burst(lambda: list(driver.get_app_logs(
        endpoint, env, service, tail=tail, masks=masks)),
        attempts, delay, sleep)
    if lines:
        remember(service.id, lines)
    return lines


def latest(ctx, service_id, with_output=False):
    """Select the newest scoped deployment, optionally with stored output."""
    with db_api.reader.using(ctx):
        repo = db_api.deployments
        query = repo._query(ctx).filter_by(service_id=service_id)
        if with_output:
            query = query.filter(repo.model.runtime_log.isnot(None),
                                 repo.model.runtime_log != '')
        row = query.order_by(repo.model.number.desc()).first()
        return objects.Deployment.obj_from_db(ctx, row) if row else None


def last(service_id):
    return LAST.get(service_id)


def tail_text(service_id, count=50, limit=6000):
    """Last lines as plain text for a deployment fault, or ''."""
    held = LAST.get(service_id)
    if not held:
        return ''
    text = '\n'.join(line.line for line in held[1][-count:])
    return text[-limit:]


def _records(text):
    """Read stored JSON lines."""
    return [json.loads(line) for line in (text or '').splitlines()]


def _identity(record):
    return record.get('ts', ''), record['stream'], record['line']


def persist(deployment, lines):
    """Append deduplicated tails under a lock; retain at most 65535 bytes."""
    captured = timeutils.utcnow().isoformat() + 'Z'
    new = []
    for line in lines:
        for text in strutils.mask_password(line.line).splitlines():
            if not text:
                continue
            record = dict(ts=line.ts or '', stream=line.stream, line=text,
                          captured_at=captured)
            if new and _identity(new[-1]) == _identity(record):
                if not line.ts:
                    new[-1]['repeated'] = new[-1].get('repeated', 1) + 1
                continue
            new.append(record)
    if not new:
        return
    ctx = deployment._context
    with db_api._write(ctx):
        row = db_api.deployments._get(ctx, deployment.id, lock=True)
        old = _records(row.runtime_log)
        old_keys = [_identity(record) for record in old]
        new_keys = [_identity(record) for record in new]
        match = next((i for i in range(len(old))
                      if old_keys[i:i + len(new)] == new_keys), None)
        if match is not None:
            shared = old[match:match + len(new)]
            records = old
        else:
            overlap = min(len(old), len(new))
            while overlap and old_keys[-overlap:] != new_keys[:overlap]:
                overlap -= 1
            shared = old[-overlap:] if overlap else []
            records = old + new[overlap:]
        for previous, incoming in zip(shared, new):
            count = max(previous.get('repeated', 1),
                        incoming.get('repeated', 1))
            if count > 1:
                previous['repeated'] = count
        encoded = [json.dumps(record, ensure_ascii=False)
                   for record in records]
        # Keep complete records. Trim an oversized newest message, not JSON.
        if len(encoded[-1].encode('utf-8')) > 65535:
            record = records[-1]
            record['line'] = record['line'].encode('utf-8')[-60000:].decode(
                'utf-8', errors='ignore')
            encoded[-1] = json.dumps(record, ensure_ascii=False)
            while len(encoded[-1].encode('utf-8')) > 65535:
                record['line'] = record['line'][len(record['line']) // 4:]
                encoded[-1] = json.dumps(record, ensure_ascii=False)
        size = 0
        kept = []
        for record in reversed(encoded):
            length = len(record.encode('utf-8')) + bool(kept)
            if size + length > 65535:
                break
            kept.append(record)
            size += length
        row.runtime_log = '\n'.join(reversed(kept))
        ctx.session.flush()


def stored(deployment, tail=200):
    if not deployment.runtime_log:
        return None
    records = _records(deployment.runtime_log)
    lines = []
    for record in records[-tail:] if tail else []:
        text = record['line']
        if record.get('repeated', 1) > 1:
            text += ' (repeated %s times)' % record['repeated']
        lines.append(dict(ts=record['ts'] or record['captured_at'],
                          stream=record['stream'], line=text))
    return {'lines': lines,
            'captured_at_failure': True,
            'captured_at': records[-1]['captured_at'],
            'note': 'Stored runtime output captured at failure time.',
            'deployment_id': deployment.id}
