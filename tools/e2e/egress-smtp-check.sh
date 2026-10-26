#!/bin/bash
# M3 acceptance: tcp/25 egress is blocked from a container on a worker,
# while tcp/443 egress works (control).
# Usage: egress-smtp-check.sh SSH_TARGET   (e.g. ubuntu@<worker mgmt IP>)
# Needs the optional admin SSH key and a temporary tcp/22 rule on the
# management security group (see "Optional admin SSH key" in
# doc/source/operations.md).
set -u
[ $# -eq 1 ] || { echo "usage: $0 SSH_TARGET" >&2; exit 2; }
SSH_OPTS=${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=8}
# shellcheck disable=SC2086,SC2016
out=$(ssh $SSH_OPTS "$1" 'sudo docker run --rm alpine sh -c "
  nc -z -w5 ${SMTP_HOST:-1.1.1.1} 25 && echo smtp=open || echo smtp=blocked
  nc -z -w5 1.1.1.1 443 && echo https=open || echo https=blocked"' 2>&1)
echo "$out"
echo "$out" | grep -q smtp=blocked && echo "$out" | grep -q https=open \
    && { echo "SMTP PASS"; exit 0; }
echo "SMTP FAIL"; exit 1
