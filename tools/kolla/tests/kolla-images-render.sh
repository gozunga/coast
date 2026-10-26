#!/bin/bash
# Copyright 2026 Gozunga and Coast contributors
# SPDX-License-Identifier: Apache-2.0
# Render the Coast kolla-build overlay (Q141) with --template-only and check
# the image graph. No Docker daemon is needed.
set -euo pipefail
work=${1:?usage: $0 WORKDIR}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../images" && pwd)
rm -rf "$work/kib"; mkdir -p "$work/kib"
cat > "$work/kib/conf" <<CONF
[DEFAULT]
base = ubuntu
base_tag = noble
registry = registry.example.com:5000
namespace = kolla
tag = dev-test
[coast-base]
type = local
location = /dev/null
CONF
cat > "$work/kib/extra.j2" <<'J2'
{% extends parent_template %}
{% block coast_api_footer %}RUN echo overlay-extra{% endblock %}
J2
COAST_KOLLA_PARENT=registry.example.com:5000/kolla/openstack-base:2026.1-ubuntu-noble \
    kolla-build --config-file "$work/kib/conf" \
    --docker-dir "$here/docker" \
    --template-override "$here/template-overrides.j2" \
    --template-override "$work/kib/extra.j2" \
    --work-dir "$work/kib/build" --template-only '^coast-'
out=$work/kib/build/docker/coast
fail() { echo "FAIL: $*" >&2; exit 1; }
grep -qx 'FROM registry.example.com:5000/kolla/openstack-base:2026.1-ubuntu-noble' \
    "$out/coast-base/Dockerfile" || fail "coast-base parent"
grep -q 'ADD coast-base-archive /coast-base-source' \
    "$out/coast-base/Dockerfile" || fail "coast-base source"
grep -q 'coast-db-manage version' "$out/coast-base/Dockerfile" \
    || fail "coast-base checks"
for s in api conductor health-manager console-proxy; do
    f=$out/coast-$s/Dockerfile
    grep -qx 'FROM registry.example.com:5000/kolla/coast-base:dev-test' "$f" \
        || fail "coast-$s parent"
    [[ $(grep '^USER' "$f" | tail -1) == 'USER coast' ]] || fail "coast-$s user"
done
grep -q 'RUN echo overlay-extra' "$out/coast-api/Dockerfile" \
    || fail "operator overlay not applied"
echo "kolla-images render: OK"
