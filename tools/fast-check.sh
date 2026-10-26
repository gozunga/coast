#!/usr/bin/env bash
# Fast local pre-merge check for hot-patch lanes. NOT a substitute for CI:
# the MR pipeline (and the full main/nightly pipeline) still runs.
#
#   tools/fast-check.sh [base-ref]      # default: origin/main
#
# 1. flake8 on changed Python files.
# 2. Unit tests for changed modules: test modules whose name matches a
#    changed module, plus test modules that import it. Changes to build
#    files (setup.cfg, requirements, tox.ini, coast/tests/base.py ...) fall
#    back to the whole unit suite.
# Reuses .tox/py3 (created with `tox -e py3 --notest` on first run).
set -euo pipefail
# Private temp dir: pip/tox leave build dirs behind in /tmp otherwise.
export TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/fast-check.XXXXXX")
trap "rm -rf \"$TMPDIR\"" EXIT
cd "$(git rev-parse --show-toplevel)"
base=${1:-origin/main}
mapfile -t changed < <( { git diff --name-only --diff-filter=d "$(git merge-base "$base" HEAD)"; \
                          git diff --name-only --diff-filter=d HEAD; \
                          git ls-files --others --exclude-standard; } | sort -u)
[ ${#changed[@]} -gt 0 ] || { echo "fast-check: nothing changed vs $base"; exit 0; }
[ -x .tox/py3/bin/stestr ] || tox -e py3 --notest
PY=.tox/py3/bin/python

py=(); full=0; regex=()
for f in "${changed[@]}"; do
  case "$f" in
    setup.cfg|setup.py|requirements.txt|test-requirements.txt|tox.ini|.stestr.conf|coast/tests/base.py|coast/tests/unit/base.py|coast/tests/unit/__init__.py) full=1 ;;
  esac
  [[ "$f" == *.py && -f "$f" ]] && py+=("$f")
  if [[ "$f" == coast/tests/unit/*.py ]]; then
    m=${f%.py}; regex+=("${m//\//.}")
  elif [[ "$f" == coast/*.py && "$f" != coast/tests/* ]]; then
    m=${f%.py}; m=${m%/__init__}; dotted=${m//\//.}; leaf=${dotted##*.}
    # tests named after the module, and tests that import it
    while IFS= read -r t; do t=${t%.py}; regex+=("${t//\//.}"); done < <(
      { ls coast/tests/unit/test_"${leaf}"*.py 2>/dev/null
        grep -rlE --include='*.py' -e "${dotted//./\\.}([^a-z_]|$)" -e "from ${dotted%.*} import .*\b${leaf}\b" coast/tests/unit 2>/dev/null || true
      } | sort -u)
  fi
done

if [ ${#py[@]} -gt 0 ]; then
  echo "== flake8 (${#py[@]} files)"; "$PY" -m flake8 "${py[@]}"
fi
if [ "$full" = 1 ]; then
  echo "== build files changed: full unit suite"
  .tox/py3/bin/stestr --test-path=./coast/tests/unit run
elif [ ${#regex[@]} -gt 0 ]; then
  sel=$(printf '%s\n' "${regex[@]}" | sort -u | paste -sd'|')
  echo "== unit tests: ${sel}"
  .tox/py3/bin/stestr --test-path=./coast/tests/unit run "^(${sel})\\."
else
  echo "== no Python modules changed; unit tests skipped"
fi
echo "fast-check: OK (CI still runs the full MR pipeline)"
