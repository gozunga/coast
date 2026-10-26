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
# Create the Coast CA material. The Coast CA is separate from the Kolla CA.
# A future kolla-ansible coast-certificates command can call this script
# unchanged, including --check-expiry.
#
# Exit status:
#   0  success, or every certificate is valid beyond --check-expiry DAYS
#   1  --check-expiry found a certificate that expires within DAYS
#   2  usage error, missing tool, or generation error

set -euo pipefail
umask 077
export LC_ALL=C

OUT=""
DAYS=730
KEY_TYPE="rsa"
KEY_SIZE=""
SUBJECT_PREFIX="/O=Coast"
REGISTRY_HOST=""
FORCE=0
CHECK_EXPIRY="-1"
WORK=""

cleanup() {
    if [[ -n "${WORK}" && -d "${WORK}" ]]; then
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
Usage: gen-ca.sh --output-dir DIR [options]

Create a Coast server CA, a Coast client CA, and the controller client
certificate. The script is non-interactive. A second run keeps existing
files. Private keys are mode 0600. The script never prints private keys.

Options:
  --output-dir DIR       Directory for certificates and keys (required)
  --days DAYS            Validity of new certificates (default: 730)
  --key-type TYPE        rsa or ec (default: rsa)
  --key-size BITS        rsa: 2048, 3072, or 4096. ec: 256 or 384
  --subject-prefix DN    Subject prefix (default: /O=Coast)
  --registry-host HOST   Also issue a registry server certificate
  --force                Replace existing Coast certificate files
  --check-expiry DAYS    Read-only expiry check. Prints one line per
                         certificate and does not write files
  --help                 Show this help

--check-expiry prints one line per certificate:
  OK <file> <notAfter>
  EXPIRING <file> <notAfter>
notAfter is UTC ISO-8601, for example 2028-10-10T00:00:00Z.
Exit 1 when any certificate expires within DAYS.
EOF
}

die() {
    echo "gen-ca.sh: $*" >&2
    exit 2
}

note() {
    echo "$1 $2"
}

require_int() {
    local label="$1"
    local value="$2"
    local max="$3"
    if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
        die "${label} requires a whole number"
    fi
    if (( value > max )); then
        die "${label} is too large"
    fi
}

validate_prefix() {
    if [[ ! "${SUBJECT_PREFIX}" =~ ^(/[A-Za-z][A-Za-z0-9]*=[A-Za-z0-9._+=-]+)+$ ]]; then
        die "invalid --subject-prefix"
    fi
    if [[ "${SUBJECT_PREFIX}" == *CN=* || "${SUBJECT_PREFIX}" == *cn=* ]]; then
        die "--subject-prefix must not set CN"
    fi
}

validate_registry_host() {
    if [[ -z "${REGISTRY_HOST}" ]]; then
        return 0
    fi
    if (( ${#REGISTRY_HOST} > 64 )); then
        die "--registry-host is too long"
    fi
    if [[ ! "${REGISTRY_HOST}" =~ ^[A-Za-z0-9.-]+$ ]]; then
        die "invalid --registry-host"
    fi
    if [[ "${REGISTRY_HOST}" == .* || "${REGISTRY_HOST}" == *..* ]]; then
        die "invalid --registry-host"
    fi
}

apply_key_defaults() {
    case "${KEY_TYPE}" in
        rsa|ec) ;;
        *) die "--key-type must be rsa or ec" ;;
    esac
    if [[ -z "${KEY_SIZE}" ]]; then
        if [[ "${KEY_TYPE}" == "rsa" ]]; then
            KEY_SIZE=2048
        else
            KEY_SIZE=256
        fi
    fi
    if [[ "${KEY_TYPE}" == "rsa" ]]; then
        case "${KEY_SIZE}" in
            2048|3072|4096) ;;
            *) die "--key-size for rsa must be 2048, 3072, or 4096" ;;
        esac
    else
        case "${KEY_SIZE}" in
            256|384) ;;
            *) die "--key-size for ec must be 256 or 384" ;;
        esac
    fi
}

gen_key() {
    local dest="$1"
    if [[ "${KEY_TYPE}" == "rsa" ]]; then
        if ! openssl genpkey -quiet -algorithm RSA \
            -pkeyopt "rsa_keygen_bits:${KEY_SIZE}" -out "${dest}" \
            >/dev/null 2>&1; then
            die "could not create a private key"
        fi
    else
        local curve="prime256v1"
        if [[ "${KEY_SIZE}" == "384" ]]; then
            curve="secp384r1"
        fi
        if ! openssl genpkey -quiet -algorithm EC \
            -pkeyopt "ec_paramgen_curve:${curve}" -out "${dest}" \
            >/dev/null 2>&1; then
            die "could not create a private key"
        fi
    fi
    chmod 0600 "${dest}"
}

server_key_usage() {
    if [[ "${KEY_TYPE}" == "ec" ]]; then
        echo "critical,digitalSignature,keyAgreement"
    else
        echo "critical,digitalSignature,keyEncipherment"
    fi
}

require_complete() {
    local first="$1"
    local second="$2"
    if [[ -e "${first}" && ! -e "${second}" ]]; then
        die "incomplete pair $(basename "${first}"); use --force"
    fi
    if [[ ! -e "${first}" && -e "${second}" ]]; then
        die "incomplete pair $(basename "${second}"); use --force"
    fi
}

create_ca() {
    local key="$1"
    local cert="$2"
    local common_name="$3"
    if [[ -f "${key}" && -f "${cert}" ]]; then
        note unchanged "$(basename "${cert}")"
        note unchanged "$(basename "${key}")"
        return 0
    fi
    gen_key "${key}"
    if ! openssl req -batch -new -x509 \
        -key "${key}" -out "${cert}" -days "${DAYS}" \
        -subj "${SUBJECT_PREFIX}/CN=${common_name}" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1; then
        die "could not create a CA certificate"
    fi
    chmod 0600 "${key}"
    chmod 0644 "${cert}"
    note wrote "$(basename "${cert}")"
    note wrote "$(basename "${key}")"
}

sign_leaf() {
    local ca_cert="$1"
    local ca_key="$2"
    local key="$3"
    local cert="$4"
    local common_name="$5"
    local key_usage="$6"
    local eku="$7"
    local san="$8"
    local csr="${WORK}/leaf.csr"
    local ext="${WORK}/leaf.ext"
    local serial

    if ! openssl req -batch -new -key "${key}" -out "${csr}" \
        -subj "${SUBJECT_PREFIX}/CN=${common_name}" >/dev/null 2>&1; then
        die "could not create a certificate request"
    fi
    cat > "${ext}" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=${key_usage}
extendedKeyUsage=${eku}
subjectAltName=${san}
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
    serial="0x$(openssl rand -hex 16)"
    if ! openssl x509 -req -in "${csr}" \
        -CA "${ca_cert}" -CAkey "${ca_key}" \
        -out "${cert}" -days "${DAYS}" -set_serial "${serial}" \
        -extfile "${ext}" >/dev/null 2>&1; then
        die "could not sign a leaf certificate"
    fi
    rm -f "${csr}" "${ext}"
}

create_client() {
    local bundle="${OUT}/coast_client.cert-and-key.pem"
    local key="${WORK}/client.key"
    local cert="${WORK}/client.cert"
    if [[ -f "${bundle}" ]]; then
        note unchanged "coast_client.cert-and-key.pem"
        return 0
    fi
    gen_key "${key}"
    sign_leaf \
        "${OUT}/coast_client_ca.cert.pem" \
        "${OUT}/coast_client_ca.key.pem" \
        "${key}" "${cert}" "coast-controller" \
        "critical,digitalSignature" "clientAuth" "DNS:coast-controller"
    cat "${cert}" "${key}" > "${bundle}"
    chmod 0600 "${bundle}"
    rm -f "${key}" "${cert}"
    note wrote "coast_client.cert-and-key.pem"
}

registry_san() {
    if [[ "${REGISTRY_HOST}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "IP:${REGISTRY_HOST}"
    else
        echo "DNS:${REGISTRY_HOST}"
    fi
}

create_registry() {
    local key="${OUT}/coast_registry.key.pem"
    local cert="${OUT}/coast_registry.cert.pem"
    if [[ -z "${REGISTRY_HOST}" ]]; then
        return 0
    fi
    if [[ -f "${key}" && -f "${cert}" ]]; then
        note unchanged "coast_registry.cert.pem"
        note unchanged "coast_registry.key.pem"
        return 0
    fi
    gen_key "${key}"
    sign_leaf \
        "${OUT}/coast_server_ca.cert.pem" \
        "${OUT}/coast_server_ca.key.pem" \
        "${key}" "${cert}" "${REGISTRY_HOST}" \
        "$(server_key_usage)" "serverAuth" "$(registry_san)"
    chmod 0600 "${key}"
    chmod 0644 "${cert}"
    note wrote "coast_registry.cert.pem"
    note wrote "coast_registry.key.pem"
}

enforce_modes() {
    chmod 0700 "${OUT}"
    chmod 0600 \
        "${OUT}/coast_server_ca.key.pem" \
        "${OUT}/coast_client_ca.key.pem" \
        "${OUT}/coast_client.cert-and-key.pem"
    chmod 0644 \
        "${OUT}/coast_server_ca.cert.pem" \
        "${OUT}/coast_client_ca.cert.pem"
    if [[ -f "${OUT}/coast_registry.key.pem" ]]; then
        chmod 0600 "${OUT}/coast_registry.key.pem"
    fi
    if [[ -f "${OUT}/coast_registry.cert.pem" ]]; then
        chmod 0644 "${OUT}/coast_registry.cert.pem"
    fi
}

format_line() {
    local file="$1"
    local raw iso epoch now limit
    if ! raw="$(openssl x509 -in "${OUT}/${file}" -noout -enddate)"; then
        echo "gen-ca.sh: cannot read certificate ${file}" >&2
        return 2
    fi
    raw="${raw#notAfter=}"
    iso="$(date -u -d "${raw}" +%Y-%m-%dT%H:%M:%SZ)"
    epoch="$(date -u -d "${raw}" +%s)"
    now="$(date -u +%s)"
    limit="$((now + CHECK_EXPIRY * 86400))"
    if (( epoch <= limit )); then
        echo "EXPIRING ${file} ${iso}"
        return 1
    fi
    echo "OK ${file} ${iso}"
}

check_expiry() {
    local files=(
        coast_server_ca.cert.pem
        coast_client_ca.cert.pem
        coast_client.cert-and-key.pem
    )
    local file line code=0 status=0
    if [[ -e "${OUT}/coast_registry.cert.pem" || -e "${OUT}/coast_registry.key.pem" ]]; then
        files+=(coast_registry.cert.pem)
    fi
    for file in "${files[@]}"; do
        if [[ ! -f "${OUT}/${file}" ]]; then
            die "missing certificate ${file}"
        fi
    done
    for file in "${files[@]}"; do
        if line="$(format_line "${file}")"; then
            printf '%s\n' "${line}"
        else
            status=$?
            if [[ -n "${line}" ]]; then
                printf '%s\n' "${line}"
            fi
            if (( status > code )); then
                code="${status}"
            fi
        fi
    done
    exit "${code}"
}

remove_existing() {
    rm -f \
        "${OUT}/coast_server_ca.cert.pem" \
        "${OUT}/coast_server_ca.key.pem" \
        "${OUT}/coast_client_ca.cert.pem" \
        "${OUT}/coast_client_ca.key.pem" \
        "${OUT}/coast_client.cert-and-key.pem" \
        "${OUT}/coast_registry.cert.pem" \
        "${OUT}/coast_registry.key.pem"
}

args="$(getopt -o h \
    -l help,output-dir:,days:,key-type:,key-size:,subject-prefix:,registry-host:,check-expiry:,force \
    -n gen-ca.sh -- "$@")" || exit 2
eval set -- "${args}"
while true; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --output-dir)
            OUT="$2"
            shift 2
            ;;
        --days)
            DAYS="$2"
            shift 2
            ;;
        --key-type)
            KEY_TYPE="$2"
            shift 2
            ;;
        --key-size)
            KEY_SIZE="$2"
            shift 2
            ;;
        --subject-prefix)
            SUBJECT_PREFIX="$2"
            shift 2
            ;;
        --registry-host)
            REGISTRY_HOST="$2"
            shift 2
            ;;
        --check-expiry)
            CHECK_EXPIRY="$2"
            shift 2
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --)
            shift
            break
            ;;
        *)
            die "unknown argument"
            ;;
    esac
done
if [[ $# -gt 0 ]]; then
    die "unexpected argument"
fi
if [[ -z "${OUT}" || "${OUT}" == "/" ]]; then
    die "--output-dir is required"
fi

if [[ "${CHECK_EXPIRY}" != "-1" ]]; then
    require_int "--check-expiry" "${CHECK_EXPIRY}" 1000000
    if [[ ! -d "${OUT}" ]]; then
        die "output directory does not exist"
    fi
    check_expiry
fi

command -v openssl >/dev/null 2>&1 || die "openssl is not installed"
require_int "--days" "${DAYS}" 36500
if (( DAYS < 1 )); then
    die "--days must be at least 1"
fi
apply_key_defaults
validate_prefix
validate_registry_host

mkdir -p "${OUT}"
if [[ ! -d "${OUT}" ]]; then
    die "output directory cannot be created"
fi
chmod 0700 "${OUT}"

if (( FORCE == 1 )); then
    remove_existing
fi

require_complete "${OUT}/coast_server_ca.key.pem" "${OUT}/coast_server_ca.cert.pem"
require_complete "${OUT}/coast_client_ca.key.pem" "${OUT}/coast_client_ca.cert.pem"
require_complete "${OUT}/coast_registry.key.pem" "${OUT}/coast_registry.cert.pem"

WORK="$(mktemp -d)"
chmod 0700 "${WORK}"

create_ca "${OUT}/coast_server_ca.key.pem" \
    "${OUT}/coast_server_ca.cert.pem" "Coast Server CA"
create_ca "${OUT}/coast_client_ca.key.pem" \
    "${OUT}/coast_client_ca.cert.pem" "Coast Client CA"
create_client
create_registry
enforce_modes
