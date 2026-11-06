#!/usr/bin/env bash
# One-time dev tools for ci.sh, all inside this repo (nothing system-wide). Run from WSL:  bash tools/dev-setup.sh
#   ruff            -> .tools/py/      (Linux build, via uv; .tools/ is git-ignored)
#   mypy + pydantic -> .tools/typing/  (a venv on the WSL python3: the type check and the Python tests run in it)
#   rustfmt, clippy -> the project-local toolchain in .rust/ (see rust/env.sh)
# Safe to re-run; it only adds to .tools/ (pydantic is pinned to the version in the Windows live/.pkgs).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export UV_CACHE_DIR="$ROOT/.uv-cache"
uv pip install --target "$ROOT/.tools/py" "ruff==0.17.3"
[ -x "$ROOT/.tools/typing/bin/python" ] || uv venv --python "$(command -v python3)" "$ROOT/.tools/typing"
uv pip install --python "$ROOT/.tools/typing/bin/python" "mypy==2.4.1" "pydantic==2.14.2"
source "$ROOT/rust/env.sh"
(cd "$ROOT/rust/coach-core" && rustup component add rustfmt clippy)
"$ROOT/.tools/py/bin/ruff" --version
"$ROOT/.tools/typing/bin/mypy" --version
"$ROOT/.tools/typing/bin/python" -c "import pydantic; print('pydantic', pydantic.VERSION)"
(cd "$ROOT/rust/coach-core" && cargo fmt --version && cargo clippy --version)
command -v node >/dev/null || echo "note: node not found; the page-script check needs node (any 18+)"
