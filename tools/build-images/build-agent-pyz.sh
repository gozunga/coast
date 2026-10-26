#!/bin/bash
# Copyright 2026 Gozunga and Coast contributors
# Licensed under the Apache License, Version 2.0.
set -euo pipefail

# Build tools and temporary files stay off /tmp. Noble runs Python 3.12.
export TMPDIR=${TMPDIR:-/var/tmp/coast-dib}
build_dir=${COAST_AGENT_BUILD_DIR:-/var/tmp/coast-agent-build}
output=${1:-/var/tmp/coast-images/coast-agent.pyz}
python=${COAST_AGENT_BUILD_PYTHON:-python3.12}
constraints=${TOX_CONSTRAINTS_FILE:-https://releases.openstack.org/constraints/upper/2026.1}
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
mkdir -p "$TMPDIR" "$build_dir" "$(dirname -- "$output")"
"$python" -m venv "$build_dir/venv"
python=$build_dir/venv/bin/python
"$python" -m pip install 'shiv==1.0.8' wheel pbr
stage=$(mktemp -d "$build_dir/stage.XXXXXX")
trap 'rm -rf "$stage"' EXIT
# The Coast wheel is pure Python. Resolve dependencies for the guest ABI.
"$python" -m pip wheel --no-deps --wheel-dir "$stage/wheels" "$repo"
target_flags=()
if [ "$("$python" -c 'import sys; print("%s.%s" % sys.version_info[:2])')" != 3.12 ]; then
    target_flags=(--platform manylinux_2_17_x86_64
        --platform manylinux_2_28_x86_64 --platform manylinux_2_34_x86_64
        --python-version 3.12 --implementation cp --abi cp312
        --only-binary=:all:)
fi
"$python" -m pip install --target "$stage/site-packages" \
    -c "$constraints" "${target_flags[@]}" "$stage"/wheels/gozunga_coast-*.whl
"$build_dir/venv/bin/shiv" --site-packages "$stage/site-packages" \
    --entry-point coast.cmd.agent:main --python '/usr/bin/python3' \
    --output-file "$output"
sha256sum "$output"
