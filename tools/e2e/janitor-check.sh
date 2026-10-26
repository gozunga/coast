#!/bin/bash
# M3 acceptance: after stack delete, no OpenStack resource may still
# reference the stack. Usage: janitor-check.sh STACK_ID [EXTRA_ID ...]
# Pass server ids and the worker FIP as extra ids. Uses $OS_CLOUD or the
# usual OS_* environment for the Coast service project.
set -u
[ $# -ge 1 ] || { echo "usage: $0 STACK_ID [EXTRA_ID ...]" >&2; exit 2; }
OS=${OPENSTACK:-openstack}
if [ -z "${OS_CLOUD:-}" ] && [ -z "${OS_AUTH_URL:-}" ]; then
    echo "JANITOR FAIL: no OpenStack credentials (set OS_CLOUD, with" \
         "OS_CLIENT_CONFIG_FILE if needed, or the OS_* variables)" >&2
    exit 2
fi
if ! $OS token issue -f value -c id >/dev/null 2>&1; then
    echo "JANITOR FAIL: OpenStack authentication failed" >&2; exit 2
fi
pat=$(printf '%s|' "$@"); pat=${pat%|}; pat=${pat//./\\.}
fail=0
for r in "server list" "volume list" "volume snapshot list" "port list" \
         "network list" "subnet list" "router list" \
         "security group list" "floating ip list"; do
    if ! out=$($OS $r --long -f json 2>/dev/null) &&
       ! out=$($OS $r -f json 2>&1); then
        echo "ERROR $r: query failed"; fail=1; continue
    fi
    n=$(printf '%s' "$out" | grep -c -i -E "$pat")
    echo "$r: $n"; [ "$n" -eq 0 ] || fail=1
done
n=$($OS secret list -f value 2>/dev/null | grep -c -i -E "$pat")
echo "secret list: $n"; [ "$n" -eq 0 ] || fail=1
[ $fail -eq 0 ] && echo "JANITOR PASS" || echo "JANITOR FAIL"
exit $fail
