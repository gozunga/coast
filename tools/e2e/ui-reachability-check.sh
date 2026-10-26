#!/bin/bash
# M3 acceptance: Coolify UI/realtime ports on the controller are
# unreachable from a worker and from the public network.
# Usage: ui-reachability-check.sh SSH_TARGET CONTROLLER_IP [CONTROLLER_IP...]
# Run it from a host on the public network; SSH_TARGET is a worker.
set -u
[ $# -ge 2 ] || { echo "usage: $0 SSH_TARGET CONTROLLER_IP..." >&2; exit 2; }
W=$1; shift
PORTS=${PORTS:-8000 6001 6002 80 443}
SSH_OPTS=${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=8}
probe='for h in "$@"; do for p in '"$PORTS"'; do
  timeout 4 bash -c "</dev/tcp/$h/$p" 2>/dev/null && echo "$h:$p OPEN" \
    || echo "$h:$p closed"; done; done'
fail=0
echo "== from worker $W"
# shellcheck disable=SC2086
r=$(ssh $SSH_OPTS "$W" bash -s -- "$@" <<<"$probe"); echo "$r"
echo "$r" | grep -q OPEN && fail=1
echo "== from this host (public)"
r=$(bash -c "$probe" _ "$@"); echo "$r"
echo "$r" | grep -q OPEN && fail=1
[ $fail -eq 0 ] && echo "UI PASS" || echo "UI FAIL"
exit $fail
