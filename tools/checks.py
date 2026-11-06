#!/usr/bin/env python3
"""Small repo checks used by ci.sh and the pre-commit hook (stdlib only; any Python 3.10+).

usage: python3 tools/checks.py json      # live/plans*.json + live/focus.json parse; plan steps are well-formed
       python3 tools/checks.py page OUT  # write the last <script> of live/overlay.html to OUT (for node --check)
       python3 tools/checks.py ast       # ast.parse every first-party .py file
       python3 tools/checks.py tests     # run first-party test_*.py files
"""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "live"
SKIP = {".pkgs", ".python", ".venv", ".uv-cache", ".models", ".rust", ".tools", ".git", "target", "out", "__pycache__",
        "node_modules", ".angular"}


def secs(t):
    m, s = str(t).split(":")
    return int(m) * 60 + int(s)


def check_json():
    errs = []
    files = sorted(LIVE.glob("plans*.json")) + [LIVE / "focus.json"]
    for f in files:
        try:
            data = json.loads(f.read_bytes())
        except (OSError, ValueError) as e:
            errs.append(f"{f.name}: does not parse: {e}")
            continue
        if f.name == "focus.json":
            if not isinstance(data.get("focus"), list) or not all(isinstance(x, str) for x in data["focus"]):
                errs.append("focus.json: 'focus' must be a list of strings")
            continue
        plans = data.get("plans")
        if not isinstance(plans, dict) or not plans:
            errs.append(f"{f.name}: no 'plans' object")
            continue
        n = 0
        for pid, plan in plans.items():
            for lane in ("steps", "ally"):
                steps = plan.get(lane)
                if steps is None:
                    if lane == "steps":
                        errs.append(f"{f.name} {pid}: no steps")
                    continue
                prev = -1
                for i, st in enumerate(steps):
                    n += 1
                    where = f"{f.name} {pid}.{lane}[{i}]"
                    miss = [k for k in ("sec", "t", "action") if k not in st]
                    if miss:
                        errs.append(f"{where}: missing {', '.join(miss)}")
                        continue
                    if not isinstance(st["sec"], int) or not re.fullmatch(r"\d+:[0-5]\d", str(st["t"])):
                        errs.append(f"{where}: bad sec/t {st['sec']!r}/{st['t']!r}")
                    elif secs(st["t"]) != st["sec"]:
                        errs.append(f"{where}: t {st['t']} != sec {st['sec']}")
                    elif st["sec"] < prev:
                        errs.append(f"{where}: out of order ({st['t']} after {prev}s)")
                    else:
                        prev = st["sec"]
                    if not str(st["action"]).strip():
                        errs.append(f"{where}: empty action")
        print(f"  {f.name}: {len(plans)} plans, {n} steps")
    return errs


def extract_page(out):
    html = (LIVE / "overlay.html").read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
    if not blocks:
        return ["overlay.html: no inline <script>"]
    Path(out).write_bytes(blocks[-1].encode("utf-8"))
    return []


def py_files():
    for p in ROOT.rglob("*.py"):
        if not SKIP.intersection(p.relative_to(ROOT).parts):
            yield p


def check_ast():
    errs, n = [], 0
    for p in sorted(py_files()):
        n += 1
        try:
            ast.parse(p.read_bytes(), filename=str(p))
        except SyntaxError as e:
            errs.append(f"{p.relative_to(ROOT)}:{e.lineno}: {e.msg}")
    print(f"  parsed {n} files")
    return errs


def run_tests():
    """Run first-party test_*.py / *_test.py files (each must exit 0); none exist yet."""
    import subprocess
    tests = [p for p in sorted(py_files()) if p.name.startswith("test_") or p.stem.endswith("_test")]
    if not tests:
        print("  no Python test files (test_*.py) yet")
    errs = []
    for p in tests:
        rc = subprocess.call([sys.executable, str(p)], cwd=p.parent)
        print(f"  {p.relative_to(ROOT)}: {'ok' if rc == 0 else f'exit {rc}'}")
        if rc:
            errs.append(f"{p.relative_to(ROOT)} failed")
    return errs


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "json":
        errs = check_json()
    elif cmd == "page" and len(sys.argv) > 2:
        errs = extract_page(sys.argv[2])
    elif cmd == "ast":
        errs = check_ast()
    elif cmd == "tests":
        errs = run_tests()
    else:
        sys.exit(__doc__)
    for e in errs:
        print("  " + e)
    sys.exit(1 if errs else 0)
