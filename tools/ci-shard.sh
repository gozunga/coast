#!/usr/bin/env bash
# Run one deterministic shard of the Coast unit suite.
#   tools/ci-shard.sh <index 1..N> <total N>
# Test classes are spread over N shards by greedy bin packing on the
# per-class CPU seconds in tools/ci-shard-times.json (classes missing from
# it are weighted by test count), so the union of all shards is exactly the
# full suite and the shards finish at about the same time. Within a shard
# stestr still runs tests on all CPUs. Fails if a shard selects nothing.
#
# Refresh the timings after big test changes (stale timings only unbalance
# shards; they can never drop tests):
#   tox -e py3 && tools/ci-shard.sh --update-times
set -euo pipefail
STESTR=${STESTR:-.tox/py3/bin/stestr}
BIN=$(dirname "$STESTR")
TIMES=tools/ci-shard-times.json

if [ "${1:-}" = --update-times ]; then
  "$STESTR" last --subunit | "$BIN/subunit2csv" --no-passthrough |
    "$BIN/python" "$(dirname "$0")/ci_shard.py" update "$TIMES"
  exit 0
fi
[ -d .stestr ] || "$STESTR" init >/dev/null  # fresh CI checkout
idx=${1:?shard index (1-based)}
total=${2:?shard total}
"$STESTR" --test-path=./coast/tests/unit list > .stestr-all.txt
python3 "$(dirname "$0")/ci_shard.py" select "$TIMES" "$idx" "$total" \
  < .stestr-all.txt > .stestr-shard.txt
n=$(grep -c . .stestr-shard.txt || true)
echo "shard ${idx}/${total}: ${n} of $(grep -c . .stestr-all.txt) tests"
[ "$n" -gt 0 ] || { echo "empty shard" >&2; exit 1; }
"$STESTR" --test-path=./coast/tests/unit run --load-list .stestr-shard.txt
"$STESTR" slowest
