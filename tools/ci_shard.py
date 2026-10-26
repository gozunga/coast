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

"""Helpers for tools/ci-shard.sh (stdlib only)."""
import collections
import csv
import datetime
import json
import os
import sys


def select(times_file, idx, total):
    times = {}
    if os.path.exists(times_file):
        with open(times_file) as f:
            times = json.load(f)
    classes = collections.OrderedDict()
    for line in sys.stdin:
        test = line.strip()
        if test.startswith('coast.tests.'):
            classes.setdefault(test.rsplit('.', 1)[0], []).append(test)

    def weight(item):
        return times.get(item[0], 0.5 * len(item[1]))

    bins = [[0.0, []] for _ in range(total)]
    for item in sorted(classes.items(), key=lambda kv: (-weight(kv), kv[0])):
        b = min(range(total), key=lambda i: (bins[i][0], i))
        bins[b][0] += weight(item)
        bins[b][1].extend(item[1])
    sys.stderr.write('shard weights (cpu s): %s\n'
                     % [round(b[0]) for b in bins])
    print('\n'.join(bins[idx - 1][1]))


def update(times_file):
    fmt = '%Y-%m-%d %H:%M:%S.%f%z'
    totals = collections.Counter()
    for row in csv.DictReader(sys.stdin):
        if row.get('status') != 'success' or not row.get('start_time'):
            continue
        start = datetime.datetime.strptime(row['start_time'], fmt)
        stop = datetime.datetime.strptime(row['stop_time'], fmt)
        totals[row['test_id'].rsplit('.', 1)[0]] += (
            stop - start).total_seconds()
    with open(times_file, 'w') as f:
        json.dump({k: round(v, 1) for k, v in sorted(totals.items())},
                  f, indent=0)
        f.write('\n')
    print('%d classes written to %s' % (len(totals), times_file))


if __name__ == '__main__':
    if sys.argv[1] == 'select':
        select(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
    else:
        update(sys.argv[2])
