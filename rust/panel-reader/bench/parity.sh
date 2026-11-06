#!/bin/bash
# Parity: raw ocr_text from Python panel_ocr vs Rust for every labeled crop. Run from rust/panel-reader.
set -e
export PYTHONDONTWRITEBYTECODE=1
S=$(mktemp -d)
python3 bench/py_bench.py dump "$S/py.json"
./target/release/panel-reader dump > "$S/rs.json"
python3 - "$S/py.json" "$S/rs.json" <<'PY'
import json, sys
a, b = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
m = [k for k in a if a[k] != b.get(k)]
print(f"parity: {len(a) - len(m)}/{len(a)} identical raw text; same files: {set(a) == set(b)}")
for k in m:
    print("  MISMATCH", k, repr(a[k]), repr(b.get(k)))
PY
rm -rf "$S"

# Same check on stress.py's junk-perturbed held-out crops (exercises split / junk / '?' paths).
S=$(mktemp -d)
python3 bench/py_bench.py stress "$S"
./target/release/panel-reader read "$S"/*.png > "$S/rs.txt"
python3 - "$S" <<'PY'
import json, sys, os
d = sys.argv[1]
py = json.load(open(os.path.join(d, "texts.json")))
rs = {}
for line in open(os.path.join(d, "rs.txt")):
    path, rest = line.split(" ", 1)
    rs[os.path.basename(path)] = json.loads(rest[:rest.index('" ') + 1])
m = [k for k in py if py[k] != rs.get(k)]
print(f"stress parity: {len(py) - len(m)}/{len(py)} identical raw text")
for k in m:
    print("  MISMATCH", k, repr(py[k]), repr(rs.get(k)))
PY
rm -rf "$S"
