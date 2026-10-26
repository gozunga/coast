#!/bin/bash
#
# Copyright 2026 Gozunga and Coast contributors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# DevStack plugin for the Coast API and the limited agent user.
# The API process is uWSGI plus the coast-wsgi-api script.
# It does not start the coast-api development server.
# Create the limited coast-agent Keystone user in the extra phase, after
# Keystone accounts exist. clean and unstack do not delete that user.
# Do not grant admin or the Keystone service role.
# https://docs.openstack.org/devstack/latest/plugins.html

COAST_PLUGIN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$COAST_PLUGIN_DIR/lib/coast"

coast_create_agent_credential() {
    local xtrace project domain
    xtrace=$(set +o | grep xtrace || true)
    set +o xtrace

    project="${COAST_AGENT_PROJECT:-${SERVICE_PROJECT_NAME:-service}}"
    domain="${SERVICE_DOMAIN_NAME:-Default}"

    if [[ -z "${COAST_AGENT_PASSWORD}" ]]; then
        die $LINENO "COAST_AGENT_PASSWORD is required"
    fi
    if [[ -z "${COAST_AGENT_USER}" || "${COAST_AGENT_USER}" == "coast" ]]; then
        die $LINENO "COAST_AGENT_USER must be a dedicated user"
    fi
    if [[ -z "${COAST_AGENT_ROLE}" || "${COAST_AGENT_ROLE}" == "admin" || \
            "${COAST_AGENT_ROLE}" == "service" ]]; then
        die $LINENO "COAST_AGENT_ROLE must not be admin or service"
    fi

    get_or_create_project "$project" "$domain"
    get_or_create_role "$COAST_AGENT_ROLE"
    get_or_create_user "$COAST_AGENT_USER" "$COAST_AGENT_PASSWORD" "$domain"
    openstack --os-cloud devstack-system-admin user set \
        --domain "$domain" --password "$COAST_AGENT_PASSWORD" \
        "$COAST_AGENT_USER"
    get_or_add_user_project_role "$COAST_AGENT_ROLE" "$COAST_AGENT_USER" \
        "$project" "$domain" "$domain"

    if [[ -f "${COAST_CONF}" ]]; then
        iniset "$COAST_CONF" agent_auth auth_type password
        iniset "$COAST_CONF" agent_auth auth_url "$KEYSTONE_SERVICE_URI"
        iniset "$COAST_CONF" agent_auth username "$COAST_AGENT_USER"
        iniset "$COAST_CONF" agent_auth password "$COAST_AGENT_PASSWORD"
        iniset "$COAST_CONF" agent_auth project_name "$project"
        iniset "$COAST_CONF" agent_auth project_domain_name "$domain"
        iniset "$COAST_CONF" agent_auth user_domain_name "$domain"
        iniset "$COAST_CONF" agent_auth insecure false
    fi

    eval "$xtrace"
}

# Each DevStack phase is a new shell. Compute the prefix again.
function coast_set_bin_dir {
    COAST_BIN_DIR=$(get_python_exec_prefix)
}

function install_coast {
    setup_develop "$COAST_DIR"
    # uWSGI is a DevStack process dependency. It is not a Coast library.
    pip_install uwsgi
    coast_set_bin_dir
}

function configure_coast {
    local script url listen
    listen=$(coast_api_listen_address "$COAST_SERVICE_LISTEN_ADDRESS") \
        || die $LINENO "Coast API listen address must be one host address."
    coast_api_listen_address "$COAST_SERVICE_HOST" >/dev/null \
        || die $LINENO "Coast API catalog host must be one host address."
    COAST_SERVICE_LISTEN_ADDRESS="$listen"

    if [[ ! -d "$COAST_CONF_DIR" ]]; then
        sudo mkdir -p "$COAST_CONF_DIR"
        sudo chown "$STACK_USER" "$COAST_CONF_DIR"
    fi
    sudo cp "$COAST_API_PASTE" "$COAST_CONF_DIR/api-paste.ini"
    sudo chown "$STACK_USER" "$COAST_CONF_DIR/api-paste.ini"

    rm -f "$COAST_CONF"
    iniset "$COAST_CONF" DEFAULT debug "$ENABLE_DEBUG_LOG_LEVEL"
    iniset_rpc_backend coast "$COAST_CONF"
    iniset "$COAST_CONF" database connection \
        "$(database_connection_url coast)"
    iniset "$COAST_CONF" api bind_host "$COAST_SERVICE_LISTEN_ADDRESS"
    iniset "$COAST_CONF" api bind_port "$COAST_SERVICE_PORT"
    iniset "$COAST_CONF" api auth_strategy keystone
    iniset "$COAST_CONF" api api_paste_config \
        "$COAST_CONF_DIR/api-paste.ini"
    url=$(coast_api_endpoint_url "$COAST_SERVICE_HOST" "$COAST_SERVICE_PORT")
    iniset "$COAST_CONF" api public_endpoint "$url"
    configure_keystone_authtoken_middleware "$COAST_CONF" coast

    coast_set_bin_dir
    script="$COAST_BIN_DIR/coast-wsgi-api"
    if [[ ! -x "$script" ]]; then
        die $LINENO "coast-wsgi-api is not installed."
    fi
    coast_write_uwsgi_config "$COAST_UWSGI_CONF" "$script" \
        "$COAST_SERVICE_LISTEN_ADDRESS" "$COAST_SERVICE_PORT" \
        || die $LINENO "Coast uWSGI configuration failed."

    recreate_database coast utf8
    "$COAST_BIN_DIR/coast-db-manage" --config-file "$COAST_CONF" upgrade
}

function create_coast_accounts {
    local service url
    create_service_user coast
    service=$(get_or_create_service coast app-hosting "Coast App Hosting")
    url=$(coast_api_endpoint_url "$COAST_SERVICE_HOST" "$COAST_SERVICE_PORT")
    # Public, admin and internal use the same host. No separate internet URL.
    get_or_create_endpoint "$service" "$REGION_NAME" "$url" "$url" "$url"
}

function start_coast {
    coast_set_bin_dir
    run_process coast-api \
        "$COAST_BIN_DIR/uwsgi --procname-prefix coast-api --ini $COAST_UWSGI_CONF"
    if ! wait_for_service "$SERVICE_TIMEOUT" \
            "$(coast_api_endpoint_url "$COAST_SERVICE_HOST" \
            "$COAST_SERVICE_PORT")"; then
        die $LINENO "Coast API did not start."
    fi
}

function stop_coast {
    stop_process coast-api
}

function clean_coast {
    stop_coast
    sudo rm -f "$COAST_CONF" "$COAST_UWSGI_CONF" \
        "$COAST_CONF_DIR/api-paste.ini"
}

# DevStack sources this file as: source plugin.sh <mode> [phase]
# The agent user is separate from the coast-api service.
if [[ "${1:-}" == "stack" && "${2:-}" == "extra" ]]; then
    coast_create_agent_credential
fi

if declare -F is_service_enabled >/dev/null && is_service_enabled coast-api; then
    if [[ "$1" == "stack" && "$2" == "install" ]]; then
        echo_summary "Installing Coast"
        install_coast
    elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
        echo_summary "Configuring Coast"
        configure_coast
        create_coast_accounts
    elif [[ "$1" == "stack" && "$2" == "extra" ]]; then
        echo_summary "Starting Coast"
        start_coast
    fi
    if [[ "$1" == "unstack" ]]; then
        stop_coast
    fi
    if [[ "$1" == "clean" ]]; then
        clean_coast
    fi
fi
