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
#
# Build a Coast golden image with diskimage-builder.
# Octavia builds amphora images the same way.
# The controller image stages the pinned Coolify installer.
# It does not run the installer. Q78 awaits James's final review.
#
# Exit status:
#   0  success
#   2  usage error or missing build tool

set -euo pipefail
export LC_ALL=C

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ELEMENTS_DIR="${SCRIPT_DIR}/elements"

ROLE=""
DISTRO="ubuntu"
RELEASE="noble"
FORMAT="qcow2"
export PYTHONPATH="${SCRIPT_DIR}/../..${PYTHONPATH:+:$PYTHONPATH}"
COOLIFY_VERSION=$(python3 -c 'from coast.common.engine_versions import DEFAULT; print(DEFAULT)')
OFFLINE=0
OUTPUT=""
PRINT_COMMAND=0
AGENT_SOURCE=""
AGENT_SHA256="${COAST_AGENT_SHA256:-}"
COOLIFY_INSTALLER=""
COOLIFY_SHA256="${COAST_COOLIFY_INSTALLER_SHA256:-}"
WORK_DIR=""

usage() {
    cat <<EOF
Usage: coast-image-create.sh --role controller|worker [options]

Build one Coast golden image. The base distro is ubuntu-noble.
The output format is qcow2. The default Coolify pin is ${COOLIFY_VERSION}.

Options:
  --role ROLE              controller or worker (required)
  --distro NAME            ubuntu or ubuntu-noble (default: ubuntu)
  --release NAME           noble (default: noble)
  --format FORMAT          qcow2 (default: qcow2)
  --coolify-version VER    Coolify pin (default: ${COOLIFY_VERSION})
  --offline                Do not download the installer or the agent
  --output PATH            Image path (default: role and version)
  --agent-source PATH|URL  Local coast-agent file, or an http(s) URL
  --agent-sha256 HEX       Required sha256 of --agent-source.
                           COAST_AGENT_SHA256 is the same input.
  --coolify-installer PATH Local install.sh for a controller build
  --coolify-installer-sha256 HEX
                           Override the reviewed installer sha256.
                           COAST_COOLIFY_INSTALLER_SHA256 is the same input.
  --work-dir DIR           Temporary build directory
  --print-command          Print the build plan and do not run it
  --help                   Show this help

Controller installer checksums default to the reviewed release registry.
An agent source requires --agent-sha256. A flag replaces the environment
value. A missing or mismatched checksum fails the build.

--offline requires cached support files and preloaded digest images.
First boot uses the staged files and archive without downloading them.
EOF
}

die() {
    echo "coast-image-create.sh: $*" >&2
    exit 2
}

require_value() {
    if [ $# -lt 2 ] || [ -z "${2:-}" ] || [ "${2#-}" != "$2" ]; then
        die "$1 needs a value"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --print-command)
            PRINT_COMMAND=1
            shift
            ;;
        --offline)
            OFFLINE=1
            shift
            ;;
        --role|--distro|--release|--format|--coolify-version|--output|--agent-source|--agent-sha256|--coolify-installer|--coolify-installer-sha256|--work-dir)
            require_value "$@"
            key=$1
            value=$2
            shift 2
            case "$key" in
                --role) ROLE=$value ;;
                --distro) DISTRO=$value ;;
                --release) RELEASE=$value ;;
                --format) FORMAT=$value ;;
                --coolify-version) COOLIFY_VERSION=$value ;;
                --output) OUTPUT=$value ;;
                --agent-source) AGENT_SOURCE=$value ;;
                --agent-sha256) AGENT_SHA256=$value ;;
                --coolify-installer) COOLIFY_INSTALLER=$value ;;
                --coolify-installer-sha256) COOLIFY_SHA256=$value ;;
                --work-dir) WORK_DIR=$value ;;
            esac
            ;;
        --role=*|--distro=*|--release=*|--format=*|--coolify-version=*|--output=*|--agent-source=*|--agent-sha256=*|--coolify-installer=*|--coolify-installer-sha256=*|--work-dir=*)
            key=${1%%=*}
            value=${1#*=}
            if [ -z "$value" ]; then
                die "${key} needs a value"
            fi
            case "$key" in
                --role) ROLE=$value ;;
                --distro) DISTRO=$value ;;
                --release) RELEASE=$value ;;
                --format) FORMAT=$value ;;
                --coolify-version) COOLIFY_VERSION=$value ;;
                --output) OUTPUT=$value ;;
                --agent-source) AGENT_SOURCE=$value ;;
                --agent-sha256) AGENT_SHA256=$value ;;
                --coolify-installer) COOLIFY_INSTALLER=$value ;;
                --coolify-installer-sha256) COOLIFY_SHA256=$value ;;
                --work-dir) WORK_DIR=$value ;;
                *) die "unknown option: ${key}" ;;
            esac
            shift
            ;;
        --)
            shift
            if [ $# -gt 0 ]; then
                die "unexpected argument: $1"
            fi
            ;;
        -*)
            die "unknown option: $1"
            ;;
        *)
            die "unexpected argument: $1"
            ;;
    esac
done

if [ "$DISTRO" = "ubuntu-noble" ]; then
    if [ "$RELEASE" != "noble" ]; then
        die "ubuntu-noble uses release noble"
    fi
    DISTRO="ubuntu"
    RELEASE="noble"
fi

case "$ROLE" in
    controller|worker) ;;
    "") die "--role is required (controller or worker)" ;;
    *) die "--role must be controller or worker" ;;
esac

if [ "$DISTRO" != "ubuntu" ] || [ "$RELEASE" != "noble" ]; then
    die "only ubuntu-noble is supported"
fi

if [ "$FORMAT" != "qcow2" ]; then
    die "--format must be qcow2"
fi

if ! python3 -c 'import sys; from coast.common.engine_versions import RELEASES; sys.exit(sys.argv[1] not in RELEASES)' "$COOLIFY_VERSION"; then
    die "unsupported --coolify-version"
fi

valid_sha256() {
    printf '%s\n' "$1" | grep -Eq '^[0-9a-fA-F]{64}$'
}

match_sha256() {
    file=$1
    expected=$2
    label=$3
    if [ -z "$expected" ]; then
        echo "coast-image-create.sh: ${label} is missing" >&2
        return 1
    fi
    if ! valid_sha256 "$expected"; then
        echo "coast-image-create.sh: ${label} must be 64 hex characters" >&2
        return 1
    fi
    if ! command -v sha256sum >/dev/null 2>&1; then
        echo "coast-image-create.sh: sha256sum is not installed" >&2
        return 1
    fi
    actual=$(sha256sum "$file" | awk 'NR==1 { print $1 }')
    actual=$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')
    expected=$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')
    if [ "$actual" != "$expected" ]; then
        echo "coast-image-create.sh: ${label} does not match ${file}" >&2
        return 1
    fi
}

if [ "$ROLE" = "worker" ]; then
    if [ -n "$COOLIFY_INSTALLER" ]; then
        die "Coolify installer is only valid for --role controller"
    fi
    if [ -n "$COOLIFY_SHA256" ]; then
        die "--coolify-installer-sha256 is only valid for --role controller"
    fi
fi

if [ "$OFFLINE" -eq 1 ] && [ "$ROLE" = "controller" ] && \
        [ -z "$COOLIFY_INSTALLER" ]; then
    die "--offline controller builds need --coolify-installer"
fi

if [ -n "$COOLIFY_INSTALLER" ] && [ ! -f "$COOLIFY_INSTALLER" ]; then
    die "--coolify-installer is not a file"
fi

if [ "$ROLE" = "controller" ]; then
    reviewed_sha=$(python3 -c 'import sys; from coast.common.engine_versions import RELEASES; print(RELEASES[sys.argv[1]][0])' "$COOLIFY_VERSION")
    COOLIFY_SHA256=${COOLIFY_SHA256:-$reviewed_sha}
    if [ -z "$COOLIFY_SHA256" ]; then
        die "--coolify-installer-sha256 is required"
    fi
    if ! valid_sha256 "$COOLIFY_SHA256"; then
        die "--coolify-installer-sha256 must be 64 hex characters"
    fi
fi

if [ -n "$COOLIFY_INSTALLER" ]; then
    if ! match_sha256 "$COOLIFY_INSTALLER" "$COOLIFY_SHA256" \
            "--coolify-installer-sha256"; then
        exit 2
    fi
fi

if [ -n "$AGENT_SOURCE" ]; then
    if [ -z "$AGENT_SHA256" ]; then
        die "--agent-sha256 is required"
    fi
    if ! valid_sha256 "$AGENT_SHA256"; then
        die "--agent-sha256 must be 64 hex characters"
    fi
    case "$AGENT_SOURCE" in
        http://*|https://*)
            if [ "$OFFLINE" -eq 1 ]; then
                die "--offline does not fetch --agent-source"
            fi
            ;;
        *)
            if [ ! -f "$AGENT_SOURCE" ]; then
                die "--agent-source is not a file"
            fi
            if ! match_sha256 "$AGENT_SOURCE" "$AGENT_SHA256" \
                    "--agent-sha256"; then
                exit 2
            fi
            ;;
    esac
elif [ -n "$AGENT_SHA256" ]; then
    die "--agent-sha256 requires --agent-source"
fi

if [ -n "$WORK_DIR" ] && [ ! -d "$WORK_DIR" ]; then
    die "--work-dir is not a directory"
fi

if [ -z "$OUTPUT" ]; then
    if [ "$ROLE" = "controller" ]; then
        OUTPUT="coast-coolify-controller-${COOLIFY_VERSION}.qcow2"
    else
        OUTPUT="coast-worker-${COOLIFY_VERSION}.qcow2"
    fi
fi

output_base=$OUTPUT
case "$output_base" in
    *.qcow2) output_base=${output_base%.qcow2} ;;
esac

if [ "$ROLE" = "controller" ]; then
    element_array=(base vm ubuntu-minimal coast-base coast-agent \
        coast-controller)
else
    element_array=(base vm ubuntu-minimal coast-base coast-agent \
        coast-worker)
fi
elements="${element_array[*]}"
dib_cmd=(disk-image-create -a amd64 -t qcow2 -o "$output_base" \
    "${element_array[@]}")

emit_plan() {
    printf 'role=%s\n' "$ROLE"
    printf 'distro=%s\n' "$DISTRO"
    printf 'release=%s\n' "$RELEASE"
    printf 'format=%s\n' "$FORMAT"
    printf 'coolify_version=%s\n' "$COOLIFY_VERSION"
    printf 'offline=%s\n' "$OFFLINE"
    printf 'output=%s\n' "${output_base}.qcow2"
    printf 'elements=%s\n' "$elements"
    printf 'agent_source=%s\n' "$AGENT_SOURCE"
    printf 'agent_sha256=%s\n' "$AGENT_SHA256"
    printf 'coolify_installer=%s\n' "$COOLIFY_INSTALLER"
    printf 'coolify_installer_sha256=%s\n' "$COOLIFY_SHA256"
    printf 'elements_path=%s\n' "$ELEMENTS_DIR"
    printf 'command='
    printf '%q ' "${dib_cmd[@]}"
    printf '\n'
}

if [ "$PRINT_COMMAND" -eq 1 ]; then
    emit_plan
    exit 0
fi

if ! command -v disk-image-create >/dev/null 2>&1; then
    die "disk-image-create is not installed"
fi


export ELEMENTS_PATH="${ELEMENTS_DIR}${ELEMENTS_PATH:+:${ELEMENTS_PATH}}"
export DIB_RELEASE="$RELEASE"
export DIB_DEBIAN_COMPONENTS="${DIB_DEBIAN_COMPONENTS:-main,universe}"
export COAST_ROLE="$ROLE"
export COAST_DISTRO="$DISTRO"
export COAST_RELEASE="$RELEASE"
export COAST_COOLIFY_VERSION="$COOLIFY_VERSION"
export COAST_OFFLINE="$OFFLINE"
export COAST_AGENT_SOURCE="$AGENT_SOURCE"
export COAST_AGENT_SHA256="$AGENT_SHA256"
export COAST_COOLIFY_INSTALLER="$COOLIFY_INSTALLER"
export COAST_COOLIFY_INSTALLER_SHA256="$COOLIFY_SHA256"
export DIB_COAST_ROLE="$ROLE"
export DIB_COAST_COOLIFY_VERSION="$COOLIFY_VERSION"
export DIB_COAST_OFFLINE="$OFFLINE"
export DIB_COAST_AGENT_SOURCE="$AGENT_SOURCE"
export DIB_COAST_AGENT_SHA256="$AGENT_SHA256"
export DIB_COAST_COOLIFY_INSTALLER="$COOLIFY_INSTALLER"
export DIB_COAST_COOLIFY_INSTALLER_SHA256="$COOLIFY_SHA256"

if [ -n "$WORK_DIR" ]; then
    export TMPDIR="$WORK_DIR"
    export DIB_IMAGE_CACHE="${WORK_DIR}/image-cache"
    mkdir -p "$DIB_IMAGE_CACHE"
fi

emit_plan
exec "${dib_cmd[@]}"
