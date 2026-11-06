# Contributing

Work from WSL (Windows git refuses this repo: "dubious ownership").

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/). The subject becomes a line in the release notes the owner
reads on the page, so write it in plain words about what changed for him, not how.

```
<type>[(scope)][!]: <what changed>

[optional body]

[Impact: <measured effect>]
```

- **type**: `feat` (new behaviour), `fix`, `perf`, `refactor`, `docs`, `chore`, `test`.
- **scope** (optional): `page`, `helper`, `mic`, `voice`, `core`, `reader`, `plans`, `db`, ...
- **`!`** or a `BREAKING CHANGE:` trailer marks a breaking change (major bump).
- **`Impact:`** trailer (optional, repeatable): a measured effect, shown next to the entry.

```
perf(mic): transcribe push-to-talk on the GPU

Impact: mic transcription 1.3 s -> 0.3 s
```

## Releases

`live/CHANGELOG.md` is the version source: its top `## vX.Y.Z` heading is the running version, and each release is
tagged `vX.Y.Z`.

```
python3 tools/release.py notes            # preview the next section (writes nothing)
python3 tools/release.py bump [--title T] # prepend it to live/CHANGELOG.md, print the commit + tag commands
```

The default bump is `minor` if any `feat`, else `patch` (`major` on a breaking change); pass `major|minor|patch` to
override. Edit the generated lines by hand if one reads badly, then run the printed commands.

## Checks

```
bash tools/dev-setup.sh   # once: ruff -> .tools/py, mypy + pydantic -> .tools/typing (venv), rustfmt + clippy -> .rust/
bash ci.sh                # full local CI: lint + mypy -> Rust tests -> Python checks (a few seconds warm)
bash ci.sh --fast         # the pre-commit subset (ruff, mypy, cargo fmt, page script, JSON)
ln -sf ../../tools/hooks/pre-commit .git/hooks/pre-commit   # install the hook
```

Without `.tools/typing` the mypy step and the Python tests FAIL with "missing ... -- run: bash tools/dev-setup.sh"
(the type check is enforced, not skipped). In a git worktree whose `.tools` points at the main checkout, make `.tools`
a real directory instead (`mkdir .tools && ln -s <main>/.tools/py .tools/py`) and run only the two typing lines of
`tools/dev-setup.sh`, so nothing is written into the main checkout.

## Typing

- **mypy** (`mypy.ini`) checks every `live/*.py`, `live/voice/*.py` and `tools/*.py`:
  `.tools/typing/bin/mypy` from the repo root (~0.1 s warm, cache in `.tools/mypy_cache`).
- **Strict** (fully annotated, no implicit `Any` generics, no untyped calls): `models`, `rules_client`, `brain`,
  `brain_run`, `watch`, `history`, `looptime`, `db`. A new module joins the strict list once it passes.
- **Basic** for the rest (`timing`, `mic`, `panel_ocr`, voice scripts, tools, tests): annotated functions are checked,
  unannotated bodies are not. The Windows-only packages (dxcam, mss, PIL, numpy, faster-whisper, ...) have no stubs
  in WSL and are `ignore_missing_imports`.
- Python 3.12 is the target (the Windows runtime); the tests run on the WSL python3 (3.11), so use `X | None` and
  `typing_extensions.TypedDict` for anything pydantic validates (pydantic refuses `typing.TypedDict` before 3.12).
- **JSON crossing a process or HTTP boundary has a model in `live/models.py`** (see `live/ARCHITECTURE.md`,
  "Boundary models"). Inside a process it stays a plain dict typed as a TypedDict; at the boundary it goes through
  the model's `TypeAdapter.validate_json` (parse + check in one pass) or a `BaseModel` for request bodies. Catch
  `pydantic.ValidationError` where you read: log and fall back (or answer 400); a bad payload must never end a tick.
- `brain.py`, `db.py` and `voice/coach_say.py` import the model types for the checker only (`TYPE_CHECKING`), so
  they stay stdlib-only at run time (coach_say.py's start-up time matters; importing pydantic costs ~1.6 s cold on
  the share).
- Hot-path cost: `python tools/bench_models.py` (validate_json vs the json.loads it replaced).
