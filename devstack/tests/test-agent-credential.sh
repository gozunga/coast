#!/bin/bash
#
# Copyright 2026 Gozunga and Coast contributors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# Exercise the DevStack credential function with stubbed Keystone helpers.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRET='agent-secret-value'
FAILURES=0

fail() {
    echo "FAIL: $*" >&2
    FAILURES=$((FAILURES + 1))
}

new_log() {
    LOG="$(mktemp)"
}

stub_helpers() {
    die() {
        echo "DIE:$2" >>"$LOG"
        exit 7
    }
    get_or_create_project() {
        echo "create-project:$1 domain:$2" >>"$LOG"
    }
    get_or_create_role() {
        echo "create-role:$1" >>"$LOG"
    }
    get_or_create_user() {
        if [[ "$2" == "$SECRET" ]]; then
            echo "user-password-ok" >>"$LOG"
        else
            echo "user-password-bad" >>"$LOG"
        fi
        echo "create-user:$1 domain:$3" >>"$LOG"
    }
    get_or_add_user_project_role() {
        echo "grant:$1 user:$2 project:$3 udom:$4 pdom:$5" >>"$LOG"
    }
    openstack() {
        local cloud="" domain="" password="" user=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --os-cloud) cloud="$2"; shift 2 ;;
                --domain) domain="$2"; shift 2 ;;
                --password) password="$2"; shift 2 ;;
                user|set) shift ;;
                *) user="$1"; shift ;;
            esac
        done
        if [[ "$cloud" != "devstack-system-admin" ]]; then
            echo "bad-cloud" >>"$LOG"
        fi
        if [[ "$password" == "$SECRET" ]]; then
            echo "set-password-ok" >>"$LOG"
        else
            echo "set-password-bad" >>"$LOG"
        fi
        echo "openstack-user:$user domain:$domain" >>"$LOG"
    }
    iniset() {
        if [[ "$3" == "password" ]]; then
            if [[ "$4" == "$SECRET" ]]; then
                echo "iniset-password-ok" >>"$LOG"
            else
                echo "iniset-password-bad" >>"$LOG"
            fi
            echo "iniset:$2:$3" >>"$LOG"
        else
            echo "iniset:$2:$3=$4" >>"$LOG"
        fi
    }
}

source_plugin() {
    # shellcheck disable=SC1091
    source "$ROOT/plugin.sh"
}

expect_die() {
    local label="$1"
    local user="$2"
    local role="$3"
    local password="$4"
    new_log
    local output status
    set +e
    output="$(
        set -euo pipefail
        export COAST_AGENT_USER="$user"
        export COAST_AGENT_ROLE="$role"
        export COAST_AGENT_PASSWORD="$password"
        export COAST_CONF="/tmp/coast-agent-missing.conf"
        stub_helpers
        source_plugin
        set -x
        coast_create_agent_credential 2>&1
    )"
    status=$?
    set -e
    if [[ "$status" -eq 0 ]]; then
        fail "$label did not fail"
        rm -f "$LOG"
        return
    fi
    if [[ -n "$SECRET" && "$output" == *"$SECRET"* ]]; then
        fail "$label printed the password"
    fi
    if ! grep -q '^DIE:' "$LOG"; then
        fail "$label did not record die"
    fi
    if grep -q '^grant:' "$LOG"; then
        fail "$label granted a role"
    fi
    rm -f "$LOG"
}

new_log
CONF="$(mktemp)"
output="$(
    set -euo pipefail
    export COAST_AGENT_PASSWORD="$SECRET"
    export COAST_AGENT_USER=coast-agent
    export COAST_AGENT_ROLE=coast_agent
    export SERVICE_PROJECT_NAME=service
    export SERVICE_DOMAIN_NAME=Default
    export KEYSTONE_SERVICE_URI=https://example.com/identity
    export COAST_CONF="$CONF"
    stub_helpers
    source_plugin
    set -x
    coast_create_agent_credential 2>&1
)"
if [[ "$output" == *"$SECRET"* ]]; then
    fail "happy path printed the password"
fi
grep -q 'create-project:service domain:Default' "$LOG" || fail "project"
grep -q 'create-role:coast_agent' "$LOG" || fail "role"
grep -q 'create-user:coast-agent domain:Default' "$LOG" || fail "user"
grep -q 'user-password-ok' "$LOG" || fail "user password"
grep -q 'set-password-ok' "$LOG" || fail "password update"
grep -q 'openstack-user:coast-agent domain:Default' "$LOG" || fail "user set"
grep -q 'grant:coast_agent user:coast-agent project:service udom:Default pdom:Default' \
    "$LOG" || fail "grant"
if grep -q 'grant:admin' "$LOG" || grep -q 'grant:service ' "$LOG"; then
    fail "extra grant"
fi
grep -q 'iniset:agent_auth:username=coast-agent' "$LOG" || fail "username"
grep -q 'iniset-password-ok' "$LOG" || fail "config password"
if grep -q 'iniset:agent_auth:password=' "$LOG"; then
    fail "config log stored the password"
fi
rm -f "$LOG" "$CONF"

new_log
MISSING="$(mktemp -u)"
output="$(
    set -euo pipefail
    export COAST_AGENT_PASSWORD="$SECRET"
    export COAST_AGENT_USER=coast-agent
    export COAST_AGENT_ROLE=coast_agent
    export COAST_CONF="$MISSING"
    stub_helpers
    source_plugin
    coast_create_agent_credential 2>&1
)"
if [[ "$output" == *"$SECRET"* ]]; then
    fail "missing file printed the password"
fi
if grep -q '^iniset:' "$LOG"; then
    fail "missing file wrote config"
fi
grep -q '^grant:coast_agent ' "$LOG" || fail "grant without config file"
rm -f "$LOG"

expect_die "empty password" coast-agent coast_agent ""
expect_die "service user name" coast coast_agent "$SECRET"
expect_die "admin role" coast-agent admin "$SECRET"
expect_die "service role" coast-agent service "$SECRET"

if [[ "$FAILURES" -ne 0 ]]; then
    echo "$FAILURES shell test failure(s)" >&2
    exit 1
fi
echo "devstack agent credential checks passed"
