#!/usr/bin/env bash
# Publish one release snapshot of this repository to its public GitHub repo.
#
# Usage: tools/publish-snapshot.sh TAG
#
# Steps:
#   1. Extract the tree at TAG with git archive.
#   2. Remove every path listed in .publish-exclude (read from TAG).
#   3. Fail on internal host names or addresses, then run gitleaks on the
#      tree and fail on any finding outside .gitleaks.toml.
#   4. Commit the tree as one commit "Coast TAG" on top of the public
#      repo's main branch, tag that commit TAG and push both atomically.
#      The push is never forced. The script stops if the public repo
#      already has TAG.
#
# Environment:
#   PUBLISH_REMOTE        public repo URL or local path. Default:
#                         https://github.com/gozunga/${CI_PROJECT_NAME}.git
#   GITHUB_PUBLISH_TOKEN  token for an https remote. Never printed.
#   PUBLISH_AUTHOR_NAME   commit and tag identity. Default: Gozunga
#   PUBLISH_AUTHOR_EMAIL  Default: support@gozunga.com
#   PUBLISH_TITLE         commit and tag title prefix. Default: Coast
#   GITLEAKS              gitleaks binary. Default: gitleaks on PATH, else
#                         the pinned release below, checked by SHA-256.
#   PUBLISH_NO_PUSH=1     stop after the checks and the local commit.
set -euo pipefail

GITLEAKS_VERSION=8.30.0
GITLEAKS_SHA256=79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e

# Internal names and address ranges that must never reach the public tree.
DENY_RE='gitlab\.gozunga\.com|gozunga\.local|(^|[^0-9.])(10\.1|172\.31|103\.229\.15[0-9])\.[0-9]{1,3}'

die() { echo "publish-snapshot: $*" >&2; exit 1; }

tag=${1:-}
[ -n "$tag" ] || die "usage: tools/publish-snapshot.sh TAG"
remote=${PUBLISH_REMOTE:-}
if [ -z "$remote" ]; then
    [ -n "${CI_PROJECT_NAME:-}" ] || die "set PUBLISH_REMOTE"
    remote="https://github.com/gozunga/${CI_PROJECT_NAME}.git"
fi
case "$remote" in
    *@*) die "PUBLISH_REMOTE must not contain credentials" ;;
esac
author_name=${PUBLISH_AUTHOR_NAME:-Gozunga}
author_email=${PUBLISH_AUTHOR_EMAIL:-support@gozunga.com}
title="${PUBLISH_TITLE:-Coast} ${tag}"

root=$(git rev-parse --show-toplevel)
cd "$root"
source_commit=$(git rev-parse --verify --quiet "refs/tags/${tag}^{commit}") \
    || die "tag ${tag} not found in this checkout"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tree=$work/tree
mkdir "$tree"
git archive --format=tar "$source_commit" | tar -x -C "$tree"

# 1. Exclusions. One path or glob per line, relative to the repo root.
[ -f "$tree/.publish-exclude" ] || die ".publish-exclude missing at ${tag}"
cp "$tree/.publish-exclude" "$work/exclude"
shopt -s nullglob dotglob globstar
while IFS= read -r pattern || [ -n "$pattern" ]; do
    pattern=${pattern%%#*}
    pattern=$(printf '%s' "$pattern" | sed 's/[[:space:]]*$//')
    [ -n "$pattern" ] || continue
    case "$pattern" in
        /*|*..*|*' '*) die "bad .publish-exclude entry: ${pattern}" ;;
    esac
    # shellcheck disable=SC2206  # the glob must expand; spaces are rejected
    candidates=("$tree"/$pattern)
    # A literal path that does not exist stays unexpanded, so check each.
    matches=()
    for path in "${candidates[@]}"; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            matches+=("$path")
        fi
    done
    [ "${#matches[@]}" -gt 0 ] \
        || die ".publish-exclude entry matches nothing: ${pattern}"
    rm -rf -- "${matches[@]}"
    echo "excluded: ${pattern}"
done < "$work/exclude"
shopt -u nullglob dotglob globstar

# 2. Internal hosts and addresses. Print file names only.
rc=0
leaks=$(grep -rIlE "$DENY_RE" "$tree") || rc=$?
[ "$rc" -le 1 ] || die "grep failed"
if [ "$rc" -eq 0 ]; then
    printf '%s\n' "$leaks" | sed "s#^${tree}/#internal reference: #" >&2
    die "internal host names or addresses found"
fi

# 3. Secret scan of the exact tree that will be published.
gitleaks_bin=${GITLEAKS:-$(command -v gitleaks || true)}
if [ -z "$gitleaks_bin" ]; then
    archive=gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz
    curl -fsSL -o "$work/$archive" \
        "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${archive}"
    echo "${GITLEAKS_SHA256}  $work/$archive" | sha256sum -c --quiet -
    tar -xzf "$work/$archive" -C "$work" gitleaks
    gitleaks_bin=$work/gitleaks
fi
gitleaks_args=(dir --no-banner --redact --exit-code 1)
if [ -f "$tree/.gitleaks.toml" ]; then
    gitleaks_args+=(--config "$tree/.gitleaks.toml")
fi
"$gitleaks_bin" "${gitleaks_args[@]}" "$tree" \
    || die "gitleaks found secrets in the snapshot"

# 4. One commit on the public main branch, plus a tag.
export GIT_TERMINAL_PROMPT=0
if [ -n "${GITHUB_PUBLISH_TOKEN:-}" ]; then
    # The helper reads the token from the environment, so it is never
    # written to a URL, a config file or the job log.
    # shellcheck disable=SC2016
    export GIT_CONFIG_COUNT=2 \
        GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0='' \
        GIT_CONFIG_KEY_1=credential.helper \
        GIT_CONFIG_VALUE_1='!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=${GITHUB_PUBLISH_TOKEN}"; }; f'
fi
mirror=$work/mirror.git
git init -q --bare "$mirror"
mgit() { git --git-dir="$mirror" "$@"; }
mgit config user.name "$author_name"
mgit config user.email "$author_email"

if [ -n "$(mgit ls-remote --tags "$remote" "refs/tags/${tag}")" ]; then
    die "${remote} already has tag ${tag}"
fi
parent=
if [ -n "$(mgit ls-remote --heads "$remote" refs/heads/main)" ]; then
    mgit fetch -q "$remote" refs/heads/main:refs/remotes/public/main
    parent=$(mgit rev-parse refs/remotes/public/main)
fi

export GIT_INDEX_FILE=$work/index
git -C "$tree" --git-dir="$mirror" --work-tree=. add -A -f .
new_tree=$(mgit write-tree)
unset GIT_INDEX_FILE
if [ -n "$parent" ] && [ "$(mgit rev-parse "${parent}^{tree}")" = "$new_tree" ]; then
    die "public main already matches ${tag}; nothing to publish"
fi

new_commit=$(
    GIT_AUTHOR_NAME=$author_name GIT_AUTHOR_EMAIL=$author_email \
    GIT_COMMITTER_NAME=$author_name GIT_COMMITTER_EMAIL=$author_email \
    GIT_AUTHOR_DATE=$(git log -1 --format=%cI "$source_commit") \
    mgit commit-tree "$new_tree" ${parent:+-p "$parent"} -m "$title"
)
mgit tag -a "$tag" -m "$title" "$new_commit"
echo "snapshot commit ${new_commit} (tree ${new_tree}) for ${tag}"

if [ "${PUBLISH_NO_PUSH:-}" = 1 ]; then
    echo "PUBLISH_NO_PUSH=1: not pushing"
    exit 0
fi
mgit push --atomic "$remote" \
    "${new_commit}:refs/heads/main" "refs/tags/${tag}:refs/tags/${tag}"
echo "published ${tag} to ${remote}"
