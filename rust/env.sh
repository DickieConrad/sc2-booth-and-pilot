# source rust/env.sh -- project-local Rust toolchain (nothing system-wide)
_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RUSTUP_HOME="$_root/.rust/rustup" CARGO_HOME="$_root/.rust/cargo"
export PATH="$CARGO_HOME/bin:$PATH"
