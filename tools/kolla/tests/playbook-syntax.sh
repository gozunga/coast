#!/bin/bash
# Copyright 2026 Gozunga and Coast contributors
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
kolla_ansible_dir=${1:?usage: playbook-syntax.sh KOLLA_ANSIBLE_DIR WORKDIR}
work=${2:?usage: playbook-syntax.sh KOLLA_ANSIBLE_DIR WORKDIR}
mkdir -p "$work/coast-play"
cp tools/kolla/ansible/coast.yml "$work/coast-play/coast.yml"
cp "$kolla_ansible_dir/gather-facts.yml" "$work/coast-play/gather-facts.yml"
ansible-playbook --syntax-check -i localhost, "$work/coast-play/coast.yml"
