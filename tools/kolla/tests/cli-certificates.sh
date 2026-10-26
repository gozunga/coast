#!/bin/bash
# Exercise `kolla-ansible coast-certificates` end to end in a temp dir:
# generate, check expiry (pass), then require failure for a long horizon.
set -euo pipefail
T=${1:?tmpdir}; ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
python -m pip install -q "$ROOT/tools/kolla/cli"
kolla-ansible coast-certificates --help | grep -q -- --check-expiry
printf 'localhost ansible_connection=local\n' > "$T/inventory"
mkdir -p "$T/kolla"; echo "{}" > "$T/kolla/globals.yml"; echo "{}" > "$T/kolla/passwords.yml"
D="$T/coast-certs"; rm -rf "$D"
A=(-i "$T/inventory" --configdir "$T/kolla" --coast-tools-dir "$ROOT/tools" --coast-certificates-dir "$D")
kolla-ansible coast-certificates "${A[@]}"
test -s "$D/coast_server_ca.cert.pem"; test -s "$D/coast_client.cert-and-key.pem"
kolla-ansible coast-certificates "${A[@]}" --check-expiry 30
if kolla-ansible coast-certificates "${A[@]}" --check-expiry 100000; then
  echo "expected expiry failure"; exit 1
fi
echo "coast-certificates CLI OK"
