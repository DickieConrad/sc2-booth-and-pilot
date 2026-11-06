#!/usr/bin/env python3
"""Offline experiment: can Jev (TypeSafe System One `Choice`) sort the owner's mic callouts into intents fast and
accurately enough for an instant "intent lane"? Compares against a keyword baseline on tools/fixtures/intents.jsonl
(synthetic lines in the style of push-to-talk speech, hand-labeled, source=synthetic; real HEARD lines from your own
live/out/mic.log can be added with source=mic.log).

    live\\.python\\python.exe tools\\eval_intents.py              # keyword baseline + Jev single + Jev batched
    ... --no-jev                                                   # keyword baseline only (no API calls)
    ... --batch 8 --threshold 0.7 --cap 190

The intents, instructions, the Noul "talking to the coach" question and the pinned model come from live/intents.py,
so the eval scores exactly what the mic's intent lane asks (one request: Choice + Noul).
The key is read from the MAIN checkout's live/.env.typesafe (via brain.load_key) and never printed or logged.
Writes tools/out/intents_eval.md (tools/out/ is gitignored).
"""
import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tools" / "fixtures" / "intents.jsonl"
OUT = ROOT / "tools" / "out" / "intents_eval.md"
# The SDK wheels and the key live in the main checkout (worktrees don't carry live/.pkgs or live/.env.typesafe).
MAIN_LIVE = ROOT.parent.parent / "sc2-coach" / "live"
sys.path.insert(0, str(ROOT / "live"))
import intents  # noqa: E402  (this checkout's lane: INTENTS, INSTRUCTIONS, TALK, MODEL, decide)

INTENTS = intents.INTENTS
INSTRUCTIONS = intents.INSTRUCTIONS

KEYWORDS = [   # trivial baseline: first match wins, else chatter
    ("pause", r"\b(pause|hold on|stop the clock)\b(?!.*\bun)"),
    ("resume", r"\b(resume|unpause|go again|start the clock|we're back)\b"),
    ("counter_unit", r"\b(counter|beat|deal with)\b"),
    ("behind", r"\b(behind|late on|forgot)\b"),
    ("enemy_shows", r"\b(rush|proxy|double gas|dts?|dark shrine|oracle|roach(es)?|gateways|warren|double toss|"
                    r"they('re| are) going)\b"),
    ("ask_build", r"\b(should i|what should|what do i|how should i build|expo|take my|what upgrades|push with)\b"),
]


def keyword(text):
    low = text.lower()
    if low.strip(" .!?") == "pause" or re.search(r"\bpause\b", low) and "unpause" not in low:
        return "pause"
    for k, rx in KEYWORDS:
        if re.search(rx, low):
            return k
    return "chatter"


def load():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")


class Jev:
    def __init__(self, cap):
        sys.path.insert(0, str(MAIN_LIVE / ".pkgs"))
        import brain
        from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient
        key = brain.load_key(MAIN_LIVE / ".env.typesafe")
        if not key:
            raise RuntimeError("no TYPESAFE_API_KEY in the main checkout's live/.env.typesafe")
        self.client = TypeSafeClient(api_key=key, model=intents.MODEL, retry=RetryPolicy(max_retries=0, timeout=20.0))
        del key
        self.Choice, self.calls, self.cap, self.errors = Choice, 0, cap, []
        self.lane_qs = intents.questions(Choice, Noul)

    def _q(self, instructions=INSTRUCTIONS):
        return self.Choice(instructions=instructions, criteria=INTENTS)

    def _call(self, state, questions):
        if self.calls >= self.cap:
            raise RuntimeError("call cap reached")
        self.calls += 1
        t0 = time.perf_counter()
        r = self.client.system_one(state=state, questions=questions)
        return r, (time.perf_counter() - t0) * 1000

    def single(self, text):
        """The lane's exact request (Choice + Noul): (intent, confidence, talk, ms)."""
        r, ms = self._call(intents.state(text), self.lane_qs)
        v = intents.parse(r.answers)
        return v.intent, v.conf, v.talk, ms

    def batched(self, texts):
        state = {f"l{i}": t for i, t in enumerate(texts)}
        qs = {f"q{i}": self._q(f"{INSTRUCTIONS} Classify only state field l{i}.") for i in range(len(texts))}
        r, ms = self._call(state, qs)
        return [(r.answers[f"q{i}"].choice, float(r.answers[f"q{i}"].confidence)) for i in range(len(texts))], ms


def score(rows, preds, confs=None, thr=None):
    keep = [i for i in range(len(rows)) if confs is None or thr is None or confs[i] >= thr]
    ok = sum(preds[i] == rows[i]["intent"] for i in keep)
    return ok, len(keep)


def per_intent(rows, preds):
    lines = ["| intent | n | precision | recall |", "|---|---|---|---|"]
    for k in INTENTS:
        tp = sum(p == k and r["intent"] == k for p, r in zip(preds, rows))
        np_ = sum(p == k for p in preds)
        nr = sum(r["intent"] == k for r in rows)
        lines.append(f"| {k} | {nr} | {tp}/{np_} | {tp}/{nr} |")
    return lines


def confusion(rows, preds):
    ks = list(INTENTS)
    c = Counter((r["intent"], p) for r, p in zip(rows, preds))
    lines = ["| true \\ pred | " + " | ".join(ks) + " |", "|---" * (len(ks) + 1) + "|"]
    for t in ks:
        lines.append(f"| {t} | " + " | ".join(str(c[(t, p)] or ".") for p in ks) + " |")
    return lines


def game_only(rows, preds):
    """Accuracy on the non-chatter lines (the ones an intent lane would act on)."""
    idx = [i for i, r in enumerate(rows) if r["intent"] != "chatter"]
    return sum(preds[i] == rows[i]["intent"] for i in idx), len(idx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-jev", action="store_true")
    ap.add_argument("--batch", type=int, default=8, help="lines per batched request; 0 skips the batched run")
    ap.add_argument("--threshold", type=float, default=0.7)
    ap.add_argument("--cap", type=int, default=190)
    a = ap.parse_args()
    rows = load()
    md = [f"# Intent lane eval ({time.strftime('%Y-%m-%d %H:%M')})", "",
          f"{len(rows)} lines: {sum(r['source'] == 'mic.log' for r in rows)} real (mic.log HEARD, hand-labeled), "
          f"{sum(r['source'] == 'synthetic' for r in rows)} synthetic. Labels: "
          + ", ".join(f"{k} {v}" for k, v in Counter(r['intent'] for r in rows).most_common()), ""]
    summary = []

    kw = [keyword(r["text"]) for r in rows]
    ok, n = score(rows, kw)
    g = game_only(rows, kw)
    summary.append(f"keyword: {ok}/{n} = {ok / n:.0%} overall, {g[0]}/{g[1]} on game callouts")
    md += ["## Keyword baseline", "", summary[-1], "", *per_intent(rows, kw), "", *confusion(rows, kw), ""]

    if not a.no_jev:
        jev = Jev(a.cap)
        preds, confs, talks, lat = [], [], [], []
        for r in rows:
            try:
                c, p, tk, ms = jev.single(r["text"])
            except Exception as e:   # message never contains the key
                jev.errors.append(type(e).__name__)
                c, p, tk, ms = "ERROR", 0.0, 0.0, float("nan")
            preds.append(c), confs.append(p), talks.append(tk), lat.append(ms)
        lat_ok = [x for x in lat if x == x]
        ok, n = score(rows, preds)
        g = game_only(rows, preds)
        summary.append(f"jev single: {ok}/{n} = {ok / n:.0%} overall, {g[0]}/{g[1]} on game callouts; "
                       f"latency p50 {pct(lat_ok, 50):.0f} ms p95 {pct(lat_ok, 95):.0f} ms; errors {len(jev.errors)}")
        md += ["## Jev single (one request per line)", "", summary[-1], ""]
        md += ["| threshold | acted on (coverage) | accuracy when acting | game callouts acted on & right | "
               "chatter wrongly acted on |", "|---|---|---|---|---|"]
        for thr in sorted({0.5, 0.6, a.threshold, 0.8, 0.9}):
            ok_t, n_t = score(rows, preds, confs, thr)
            # an intent lane only acts on non-chatter predictions; chatter predictions fall through to the slow path
            act = [i for i in range(len(rows)) if confs[i] >= thr and preds[i] not in ("chatter", "ERROR")]
            right = sum(rows[i]["intent"] == preds[i] for i in act)
            false_fire = sum(rows[i]["intent"] == "chatter" for i in act)
            md.append(f"| {thr:.2f} | {n_t}/{len(rows)} ({n_t / len(rows):.0%}) | {ok_t}/{n_t} "
                      f"({ok_t / max(n_t, 1):.0%}) | {right}/{game_only(rows, preds)[1]} | {false_fire}/"
                      f"{sum(r['intent'] == 'chatter' for r in rows)} |")
            if thr == a.threshold:
                summary.append(f"jev @conf>={thr}: covers {n_t}/{len(rows)}, accuracy {ok_t}/{n_t}; lane fires "
                               f"{len(act)} times, {right} right, {false_fire} on chatter")
        # the lane as shipped: intents.decide() with the Noul gate (in a game), what it would do per line
        acts = [intents.decide(None if p == "ERROR" else p, c, True, r["text"], tk)
                for r, p, c, tk in zip(rows, preds, confs, talks)]
        fired = [i for i, a in enumerate(acts) if a.act != "slow"]
        right = sum(rows[i]["intent"] == preds[i] for i in fired)
        on_chatter = sum(rows[i]["intent"] == "chatter" for i in fired)
        said = sum(a.act == "say" for a in acts)
        gate = Counter(a.tag for a in acts)
        talk_ch = [tk for r, tk in zip(rows, talks) if r["intent"] == "chatter"]
        talk_game = [tk for r, tk in zip(rows, talks) if r["intent"] != "chatter"]
        summary.append(f"lane (decide + Noul gate, model {intents.MODEL}): acts on {len(fired)} lines "
                       f"({said} answered, {len(fired) - said} passed), {right} right, {on_chatter} on chatter; "
                       f"talk p50 {pct(talk_game, 50):.2f} on game lines vs {pct(talk_ch, 50):.2f} on chatter")
        md += ["", "## Lane as shipped (intents.decide, talk + confidence gates)", "", summary[-1], "",
               "| decide tag | lines |", "|---|---|", *[f"| {k} | {v} |" for k, v in gate.most_common()]]
        md += ["", *per_intent(rows, preds), "", *confusion(rows, preds), "", "### Misses", "",
               "| text | label | jev | conf | talk | keyword |", "|---|---|---|---|---|---|"]
        for r, p, c, tk, k in zip(rows, preds, confs, talks, kw):
            if p != r["intent"]:
                md.append(f"| {r['text'][:90].replace('|', '/')} | {r['intent']} | {p} | {c:.2f} | {tk:.2f} | {k} |")

        if a.batch <= 0:
            md += ["", f"API calls this run: {jev.calls}; errors: {dict(Counter(jev.errors)) or 'none'}", ""]
        # batched: several lines as separate questions in one request (Choice only, as before)
        bpreds, blat = [], []
        for s in range(0, len(rows) if a.batch > 0 else 0, max(a.batch, 1)):
            chunk = [r["text"] for r in rows[s:s + a.batch]]
            try:
                res, ms = jev.batched(chunk)
                bpreds += [c for c, _ in res]
                blat.append(ms)
            except Exception as e:
                jev.errors.append(type(e).__name__)
                bpreds += ["ERROR"] * len(chunk)
        if a.batch > 0:
            ok, n = score(rows, bpreds)
            per_line = [x / a.batch for x in blat]
            summary.append(f"jev batched x{a.batch}: {ok}/{n} = {ok / n:.0%}; request p50 {pct(blat, 50):.0f} ms "
                           f"p95 {pct(blat, 95):.0f} ms "
                           f"(~{statistics.mean(per_line) if per_line else 0:.0f} ms/line amortized); "
                           f"agrees with single on {sum(x == y for x, y in zip(preds, bpreds))}/{n}")
            md += ["", f"## Jev batched ({a.batch} lines per request)", "", summary[-1], "",
                   f"API calls this run: {jev.calls}; errors: {dict(Counter(jev.errors)) or 'none'}", ""]

    md += ["## Summary", "", *[f"- {s}" for s in summary], ""]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(md), encoding="utf-8")
    print("\n".join(summary))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
