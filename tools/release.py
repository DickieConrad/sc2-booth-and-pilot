#!/usr/bin/env python3
"""Versioning and release notes from Conventional Commits. Run from WSL at the repo root (stdlib only).

    python3 tools/release.py notes [major|minor|patch] [--title T]   preview the next section (writes nothing)
    python3 tools/release.py bump  [major|minor|patch] [--title T]   prepend it to live/CHANGELOG.md, print git commands
    python3 tools/release.py version                                 print the running version (top of the changelog)

Commit format (see CONTRIBUTING.md):  <type>[(scope)][!]: <what changed, in plain words>
  types: feat, fix, perf, refactor, docs, chore, test      optional trailer:  Impact: <measured effect>
Commits since the last v* tag are grouped Features, Fixes, Performance, Other. Default bump: major if any
breaking change (`!` or a BREAKING CHANGE: trailer), else minor if any feat, else patch. Nothing is committed or tagged
by this script; it prints the commands.
"""
import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "live" / "CHANGELOG.md"
SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
GROUPS = [  # (heading in the preview, types, prefix on the page line)
    ("Features", {"feat"}, ""),
    ("Fixes", {"fix"}, "Fix: "),
    ("Performance", {"perf"}, "Faster: "),
    ("Other", None, "Under the hood: "),
]
SKIP_TYPES = {"release"}


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def current_version():
    m = re.search(r"^## v(\d+)\.(\d+)\.(\d+)", CHANGELOG.read_text(encoding="utf-8"), re.M)
    if not m:
        sys.exit(f"no '## vX.Y.Z' heading in {CHANGELOG}")
    return tuple(int(x) for x in m.groups())


def vstr(v):
    return "v%d.%d.%d" % v


def last_tag():
    out = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", check=False).strip()
    return out or None


def commits(since):
    if not git("rev-parse", "--verify", "-q", "HEAD", check=False).strip():
        return []
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    raw = git("log", "--no-merges", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", *rng)
    out = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, subject, body = (rec.split("\x1f") + ["", ""])[:3]
        m = SUBJECT.match(subject.strip())
        typ, scope, desc = (m["type"], m["scope"], m["desc"]) if m else ("other", None, subject.strip())
        if typ == "chore" and (scope or "") in SKIP_TYPES:
            continue
        impacts = [x.strip() for x in re.findall(r"^Impact:\s*(.+)$", body, re.M)]
        breaking = bool(m and m["bang"]) or bool(re.search(r"^BREAKING[ -]CHANGE:", body, re.M))
        out.append({"sha": sha[:7], "type": typ, "desc": desc, "impacts": impacts, "breaking": breaking})
    return out


def line(c, prefix):
    d = c["desc"].strip().rstrip(".")
    if not prefix:  # "Fix: the first sample ..." keeps the existing changelog's casing after a prefix
        d = d[:1].upper() + d[1:]
    text = f"{prefix}{d}."
    if c["impacts"]:
        text += " Impact: " + "; ".join(i.rstrip(".") for i in c["impacts"]) + "."
    if c["breaking"]:
        text = "Breaking: " + text
    return text


def grouped(cs):
    taken, groups = set(), []
    for name, types, prefix in GROUPS:
        rows = [c for c in cs if (c["type"] in types if types else c["sha"] not in taken)]
        taken.update(c["sha"] for c in rows)
        if rows:
            groups.append((name, [line(c, prefix) for c in rows]))
    return groups


def next_version(cur, part):
    major, minor, patch = cur
    if part == "major":
        return (major + 1, 0, 0)
    if part == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def plan(args):
    cur, tag = current_version(), last_tag()
    if tag != vstr(cur):
        if tag is None:
            hint = f"git tag -a {vstr(cur)} -m {vstr(cur)}"
            sys.exit(f"{vstr(cur)} (top of CHANGELOG.md) is not tagged yet; tag the commit it describes first:\n"
                     f"  {hint}")
        sys.exit(f"CHANGELOG.md says {vstr(cur)} but the last tag is {tag}; tag or fix the changelog first.")
    cs = commits(tag)
    if not cs:
        sys.exit(f"no commits since {tag}; nothing to release.")
    part = args.part or ("major" if any(c["breaking"] for c in cs) else
                         "minor" if any(c["type"] == "feat" for c in cs) else "patch")
    new = next_version(cur, part)
    head = f"## {vstr(new)} · {datetime.date.today().isoformat()}" + (f" · {args.title}" if args.title else "")
    groups = grouped(cs)
    section = head + "\n" + "".join(f"- {t}\n" for _, rows in groups for t in rows)
    return tag, cs, part, new, groups, section


def cmd_notes(args):
    tag, cs, part, new, groups, section = plan(args)
    print(f"{len(cs)} commits since {tag} -> {part} bump -> {vstr(new)}\n")
    for name, rows in groups:
        print(f"{name}: {len(rows)}")
    print("\n" + section)


def cmd_bump(args):
    tag, cs, part, new, groups, section = plan(args)
    text = CHANGELOG.read_bytes().decode("utf-8").replace("\r\n", "\n")
    m = re.search(r"^## v", text, re.M)
    text = text[:m.start()] + section + "\n" + text[m.start():]
    CHANGELOG.write_bytes(text.encode("utf-8"))  # bytes: keeps LF on Windows too
    v = vstr(new)
    print(section)
    print(f"Wrote {v} ({part}, {len(cs)} commits since {tag}) to {CHANGELOG.relative_to(ROOT)}. Review it, then run:\n")
    print(f'  git add live/CHANGELOG.md && git commit -m "chore(release): {v}" && git tag -a {v} -m "{v}"')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("notes", "bump"):
        p = sub.add_parser(name)
        p.add_argument("part", nargs="?", choices=["major", "minor", "patch"])
        p.add_argument("--title", help='optional release name, e.g. "Booth & Pilot"')
    sub.add_parser("version")
    args = ap.parse_args()
    if args.cmd == "version":
        print(vstr(current_version()))
    else:
        {"notes": cmd_notes, "bump": cmd_bump}[args.cmd](args)


if __name__ == "__main__":
    main()
