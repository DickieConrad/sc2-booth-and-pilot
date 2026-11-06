#!/usr/bin/env bash
# Local CI for the SC2 coach. Run from WSL at the repo root:
#   bash ci.sh           full run: lint + types (mypy) -> Rust tests -> Python checks -> web (Angular) tests + build
#   bash ci.sh --fast    pre-commit subset: ruff, mypy (~0.1 s warm), cargo fmt, page script, JSON (no compiling)
# One-time tool setup (project-local, nothing system-wide):  bash tools/dev-setup.sh
# Never touches the running helper (:8765); read-only except Rust target/ dirs, .tools/mypy_cache and a temp file.
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
FAST=0; [ "${1:-}" = "--fast" ] && FAST=1

[ -f rust/env.sh ] && source rust/env.sh
RUFF="$ROOT/.tools/py/bin/ruff"
PY="$(command -v python3 || true)"
TYPING="$ROOT/.tools/typing"      # venv with mypy + pydantic (tools/dev-setup.sh): the type check and the Python tests
MYPY="$TYPING/bin/mypy"
PYT="$TYPING/bin/python"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

names=(); results=(); times=(); fails=0
if [ -t 1 ]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'; else G=; R=; Y=; B=; N=; fi
now() { date +%s.%N; }

# step NAME [--advisory] CMD...   (advisory steps report WARN instead of failing the run)
step() {
    local name="$1"; shift
    local advisory=0; [ "$1" = "--advisory" ] && { advisory=1; shift; }
    local t0 t1 dt rc log="$TMP/step.log"
    printf '%s-- %s%s\n' "$B" "$name" "$N"
    t0=$(now)
    "$@" >"$log" 2>&1; rc=$?
    t1=$(now); dt=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b-a}')
    if [ $rc -eq 0 ]; then
        results+=("${G}PASS${N}"); [ -s "$log" ] && sed 's/^/   /' "$log" | tail -n 5
    elif [ $advisory -eq 1 ]; then
        results+=("${Y}WARN${N}"); sed 's/^/   /' "$log" | tail -n 25
    else
        results+=("${R}FAIL${N}"); fails=$((fails + 1)); sed 's/^/   /' "$log" | tail -n 60
    fi
    names+=("$name"); times+=("$dt")
}

need() { command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || { echo "missing $1 -- run: bash tools/dev-setup.sh"; return 127; }; }

ruff_check()  { need "$RUFF" && "$RUFF" check . --output-format concise; }
ruff_format() { need "$RUFF" && "$RUFF" format --check . | tail -n 3; }
json_check()  { need "$PY" && "$PY" tools/checks.py json; }
page_check()  { need node && need "$PY" && "$PY" tools/checks.py page "$TMP/page.js" && node --check "$TMP/page.js"; }
ast_check()   { need "$PY" && "$PY" tools/checks.py ast; }
crate()       { local c="$1"; shift; need cargo && (cd "rust/$c" && "$@"); }
cargo_fmt()   { crate "$1" cargo fmt --check; }
clippy()      { crate "$1" cargo clippy --all-targets -q -- -D warnings; }
cargo_tests() {
    local rc
    crate "$1" cargo test -q >"$TMP/cargo.log" 2>&1; rc=$?
    grep -E '^(error|failures:|---- )|panicked|FAILED' "$TMP/cargo.log" | head -n 40
    awk '/^test result:/{p+=$4; f+=$6} END{printf "  %d passed, %d failed\n", p, f}' "$TMP/cargo.log"
    return $rc
}
# mypy (config: mypy.ini) and the tests need the typing venv: missing = FAIL with the install command, never a skip
mypy_check()  { need "$MYPY" && "$MYPY" | tail -n 20; }
py_tests()    { need "$PYT" && "$PYT" tools/checks.py tests; }
# Angular page (live/web): full run only; skipped (PASS) until `cd live/web && npm ci` has been run once.
# The CI build goes to a temp dir, so live/web/dist (what the helper serves at /next/) is never touched.
web() {
    [ -d live/web/node_modules ] || { echo "  skipped: no live/web/node_modules (cd live/web && npm ci)"; return 0; }
    need node && (cd live/web && NG_CLI_ANALYTICS=false "$@")
}
web_test()    { web npx ng test --watch=false; }
web_build()   { web npx ng build --output-path "$TMP/web"; }

T0=$(now)
echo "${B}== lint${N}"
step "ruff check"                 ruff_check
step "ruff format (advisory)"     --advisory ruff_format
step "mypy (types)"               mypy_check
step "cargo fmt coach-core"       cargo_fmt coach-core
step "cargo fmt panel-reader"     cargo_fmt panel-reader
step "page script (node --check)" page_check
step "plans/focus JSON"           json_check
if [ $FAST -eq 0 ]; then
    step "clippy coach-core"      clippy coach-core
    step "clippy panel-reader"    clippy panel-reader
    echo "${B}== Rust tests${N}"
    step "cargo test coach-core"  cargo_tests coach-core
    step "cargo test panel-reader" cargo_tests panel-reader
    echo "${B}== Python${N}"
    step "ast.parse all .py"      ast_check
    step "python tests"           py_tests
    echo "${B}== Web (Angular)${N}"
    step "web unit tests (vitest)" web_test
    step "web build"              web_build
fi
TOTAL=$(awk -v a="$T0" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}')

echo; echo "${B}== summary${N}"
for i in "${!names[@]}"; do printf '  %-28s %s  %6ss\n' "${names[$i]}" "${results[$i]}" "${times[$i]}"; done
if [ $fails -eq 0 ]; then echo "${G}${B}PASS${N} in ${TOTAL}s"; exit 0; fi
echo "${R}${B}FAIL${N} ($fails step(s)) in ${TOTAL}s"; exit 1
