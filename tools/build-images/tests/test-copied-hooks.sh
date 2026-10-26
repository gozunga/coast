#!/bin/bash
# Copyright 2026 Gozunga and Coast contributors
# Licensed under the Apache License, Version 2.0.
set -euo pipefail
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/../../.." && pwd)
root=$(mktemp -d "${TMPDIR:-/var/tmp}/coast-hook-test.XXXXXX")
trap 'rm -rf "$root"' EXIT
export TARGET_ROOT=$root/target
export COAST_TEST_ELEMENTS=$repo/tools/build-images/elements
export PYTHONPATH=$repo
export COAST_COOLIFY_VERSION=$(python3 -c 'from coast.common.engine_versions import DEFAULT; print(DEFAULT)')
# DIB copies hooks out of their element directories and exports this map.
get_image_element_array() {
    printf '[coast-agent]=%s/coast-agent [coast-controller]=%s/controller\n' \
        "$COAST_TEST_ELEMENTS" "$COAST_TEST_ROOT"
}
export -f get_image_element_array
export COAST_TEST_ROOT=$root
mkdir -p "$root/hooks/root.d" "$root/controller/files" "$root/cache" "$root/bin"
cat > "$root/bin/sudo" <<'EOF'
#!/bin/bash
exec "$@"
EOF
chmod +x "$root/bin/sudo"
export PATH=$root/bin:$PATH
cp "$COAST_TEST_ELEMENTS/coast-agent/root.d/21-stage-firstboot" "$root/hooks/root.d/"
bash "$root/hooks/root.d/21-stage-firstboot"
cmp "$repo/coast/agent/firstboot.py" "$TARGET_ROOT/usr/local/sbin/coast-firstboot"
cp "$COAST_TEST_ELEMENTS/coast-controller/root.d/21-stage-offline" "$root/hooks/root.d/"
python3 -c 'import os; from coast.common.engine_versions import images; print("\n".join(tag + " sha256:" + "a" * 64 for tag in sorted(images(os.environ["COAST_COOLIFY_VERSION"]))))' > "$root/images.lock"
# A copied hook resolves release data from the coast-agent element mapping.
# Supply a complete synthetic registry/cache to test checksum enforcement.
python3 - "$repo" "$root" <<'PYTEST'
import hashlib
from pathlib import Path
import sys
from coast.common import engine_versions

repo, root = map(Path, sys.argv[1:])
package = root / 'coast/common'
package.mkdir(parents=True)
(package.parent / '__init__.py').touch()
(package / '__init__.py').touch()
source = (repo / 'coast/common/engine_versions.py').read_text()
digest = hashlib.sha256(b'support').hexdigest()
checksums = set(engine_versions.ARTIFACTS.values())
checksums.update(row[4] for row in engine_versions.RELEASES.values())
for checksum in checksums:
    source = source.replace(checksum, digest)
(package / 'engine_versions.py').write_text(source)
for name in (*engine_versions.ARTIFACTS, 'versions.json'):
    (root / 'cache' / name).write_bytes(b'support')
PYTEST
export COAST_TEST_ELEMENTS=$root/tools/build-images/elements
mkdir -p "$COAST_TEST_ELEMENTS/coast-agent"
# Python's -c import path starts at cwd, so use the synthetic repo.
cd "$root"
cat > "$root/bin/docker" <<'EOF'
#!/bin/bash
set -eu
[ "$1" != pull ] || exit 99
if [ "$1" = save ]; then printf archive; fi
EOF
chmod +x "$root/bin/docker"
export PATH=$root/bin:$PATH COAST_OFFLINE=1
export COAST_COOLIFY_CACHE=$root/cache
export COAST_COOLIFY_IMAGES_LOCK=$root/images.lock
bash "$root/hooks/root.d/21-stage-offline"
(cd "$TARGET_ROOT/usr/lib/coast/coolify"; sha256sum -c SHA256SUMS)
# Corrupt support files must fail even when DIB has copied the hook.
printf corrupt > "$root/cache/docker-compose.yml"
if bash "$root/hooks/root.d/21-stage-offline" > "$root/failure.log" 2>&1; then
    echo 'Corrupt support file was accepted' >&2
    exit 1
fi
echo 'Copied-hook regression checks passed'
python3 "$repo/tools/build-images/tests/test-unit-ordering.py"
