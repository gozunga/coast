#!/bin/bash
# Copyright 2026 Gozunga and Coast contributors
# SPDX-License-Identifier: Apache-2.0
#
# Build coast-base, coast-api, coast-conductor, coast-health-manager and
# coast-console-proxy with
# kolla-build and the overlay in this directory (Q141). Requires kolla-build
# (the kolla package matching the Kolla release of BASE) and Docker.
set -euo pipefail

usage() {
    cat >&2 <<USAGE
Usage: $0 [-o OVERRIDE.j2]... [-p] BASE TAG

  BASE  existing Kolla openstack-base image, e.g.
        registry.example.com:5000/kolla/openstack-base:2026.1-ubuntu-noble
  TAG   tag for the Coast images, e.g. dev-\$(git rev-parse --short HEAD)
  -o    extra kolla-build template override (repeatable)
  -p    push the five images after a successful build
USAGE
    exit 2
}

overrides=()
push=0
while getopts "o:ph" opt; do
    case $opt in
        o) overrides+=("$(realpath -- "$OPTARG")") ;;
        p) push=1 ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))
[[ $# -eq 2 && -n "$1" && -n "$2" ]] || usage

base=$1
tag=$2
# BASE = [REGISTRY/]NAMESPACE/openstack-base:BASETAG
base_ref=${base%:*}
[[ "$base_ref" != "$base" && "${base_ref##*/}" == openstack-base ]] || {
    echo "BASE must be <...>/openstack-base:<tag>" >&2; exit 2; }
prefix=${base_ref%/openstack-base}
namespace=${prefix##*/}
registry=
if [[ "$prefix" == */* ]]; then
    registry=${prefix%/*}
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$script_dir/../../.." && pwd)
command -v kolla-build >/dev/null || {
    echo "kolla-build not found; pip install kolla" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# Committed HEAD only: no local changes, untracked files or Git history.
git -C "$repo_dir" archive --format=tar --prefix=coast/ \
    --output="$work/coast.tar" HEAD
cat > "$work/kolla-build.conf" <<CONF
[DEFAULT]
base = ubuntu
base_tag = noble
namespace = $namespace
tag = $tag
pull = false
push = false
skip_parents = false
network_mode = host
${registry:+registry = $registry}

[coast-base]
type = local
location = $work/coast.tar
CONF

args=(--config-file "$work/kolla-build.conf"
      --docker-dir "$script_dir/docker"
      --template-override "$script_dir/template-overrides.j2"
      --work-dir "$work/build"
      --summary-json-file "$work/summary.json")
for o in "${overrides[@]}"; do
    args+=(--template-override "$o")
done

# coast-base starts FROM the given BASE, not Kolla's own parent chain.
COAST_KOLLA_PARENT=$base kolla-build "${args[@]}" \
    '^coast-(base|api|conductor|health-manager|console-proxy)$'

python3 - "$work/summary.json" <<'PY'
import json, sys
summary = json.load(open(sys.argv[1]))
built = sorted(i['name'] for i in summary.get('built', []))
want = ['coast-api', 'coast-base', 'coast-conductor', 'coast-console-proxy',
        'coast-health-manager']
if summary.get('failed') or built != want:
    sys.exit('kolla-build: built %s, failed %s' % (built, summary.get('failed')))
PY

for image in coast-base coast-api coast-conductor coast-health-manager \
        coast-console-proxy; do
    ref="${registry:+$registry/}$namespace/$image:$tag"
    echo "$ref"
    if [[ $push -eq 1 ]]; then
        docker push "$ref"
    fi
done
