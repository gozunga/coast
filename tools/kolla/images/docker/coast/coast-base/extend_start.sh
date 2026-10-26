#!/bin/bash
# Copyright 2026 Gozunga and Coast contributors
# SPDX-License-Identifier: Apache-2.0
set -o errexit

# The shared log volume hides image-build directories. Create this at startup.
COAST_LOG_DIR=/var/log/kolla/coast
mkdir -p "$COAST_LOG_DIR"
chown coast:coast "$COAST_LOG_DIR"
chmod 0755 "$COAST_LOG_DIR"

# Synchronous RPC reply queues use oslo.concurrency external locks.
COAST_LOCK_DIR=/var/lib/coast/locks
mkdir -p "$COAST_LOCK_DIR"
chown coast:coast "$COAST_LOCK_DIR"
chmod 0700 "$COAST_LOCK_DIR"

# Kolla sources this after set_configs, with xtrace enabled. Do not trace an
# operator command: it can contain credentials.
set +o xtrace
if [[ "${!KOLLA_BOOTSTRAP[@]}" ]]; then
    if [[ -n "${COAST_BOOTSTRAP_COMMAND:-}" ]]; then
        bash -c "$COAST_BOOTSTRAP_COMMAND"
    else
        coast-db-manage --config-file /etc/coast/coast.conf upgrade
    fi
    # errexit propagates migration failures; never start the conductor here.
    exit 0
fi
