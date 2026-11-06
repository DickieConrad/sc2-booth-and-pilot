"""Resource panel reader (minerals | gas | supply) for the timing helper: plain template matching, no OCR engine.

Drop-in for the OCR part of timing.py (same function names). What it does differently:
- Works at native pixel size (the crop is resized to the 2560x1440 panel size first), so a glyph is never stretched:
  the old 14x14 normalisation shifted a digit by a column and turned 5 into 3.
- Ink is bright, neutral (white/grey) pixels only, inside the fixed text band: coloured terrain/units behind the
  panel and anything above/below the digits are ignored.
- Glyphs must span the band and have real stroke thickness (thin diagonal lines and specks are dropped), each
  number must start at its column's fixed left edge and run without big gaps, and a blob that is two glyphs
  stuck together (or a glyph with junk on it) is split by trying cuts against the templates.
- Matching uses 3 levels (sure ink / maybe / background) so antialiased edges and 1-2 px shifts cost nothing
  while a missing stroke does (5 vs 3 vs 6 vs 8), and needs a margin over the next-best character (only digits
  in minerals/gas). Anything unsure is '?', so parse_panel returns None: unreadable beats a wrong number.

Build templates: python panel_ocr.py --learn IMAGE "min|gas|used/cap" [--learn ...]
Read a crop:     python panel_ocr.py IMAGE [IMAGE ...]
"""
import json
import re
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
GLYPHS = HERE / "glyphs2.json"

# Resource panel as fractions of the screen (measured at 2560x1440, standard UI). x1 0.975 (not timing.py's
# 0.96) leaves room for 3-digit supply in the 1v1 layout; any crop width works as long as x0 stays 0.755.
PANEL = (0.755, 0.008, 0.975, 0.042)
SIZE = (525, 49)                 # the 2v2 panel crop at 2560x1440 (x1 0.96); other heights are scaled to 49
# Text spans inside the panel crop (fraction of the 525 px crop), icons excluded: minerals, gas, supply.
COLUMNS = ((0.105, 0.39), (0.41, 0.68), (0.722, 0.99))
COLUMN_PX = [(int(SIZE[0] * a), int(SIZE[0] * b)) for a, b in COLUMNS]   # the same in pixels (2v2 layout)
START = (4, 8, 9)                # x where each column's number starts (px into the column; '1' starts 1 px later)
BAND = (21, 34)                  # text rows in the crop (digits are 13 px tall)
WHITE, GREY = 165, 30            # ink: min(r,g,b) >= WHITE and max-min <= GREY (HUD text is white)
HI, LO = 200, 100                # matching: >= HI is surely ink, LO..HI may be ink (antialiased edges)
MAX_GAP = 12                     # widest gap between two glyphs of one number ('1' to '1' is ~10)
MAX_DIST = 6                     # mismatched pixels (beyond 1 px tolerance) allowed for a match
MARGIN = 3                       # the next-best character must be at least this much worse
SPLIT_MAX = 10                   # most mismatch + dropped junk pixels allowed when a blob is split
CW = 26                         # matching canvas width (a glyph is at most ~18 px wide)


def panel_box(w, h):
    x0, y0, x1, y1 = PANEL
    return int(w * x0), int(h * y0), int(w * x1), int(h * y1)


def _ink(img):
    """(segmentation mask, levels): white HUD text (or red = supply blocked). Levels: 2 = surely ink,
    1 = maybe (antialiased edge or the half-covered top/bottom row), 0 = background."""
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    mn, mx = a.min(axis=2), a.max(axis=2)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    grey = mx - mn <= GREY
    red = (r > 170) & (g < 90) & (b < 90)
    mask = ((mn >= WHITE) & grey) | red
    lev = ((mn >= LO) & grey).astype(np.int8) + ((mn >= HI) & grey)
    lev[red] = 2
    lev[((r > 120) & (g < 110) & (b < 110)) & (lev == 0)] = 1
    return mask, lev


def _components(ink):
    """8-neighbour connected components of a 2-D bool array -> [(x0, x1, y0, y1, [(x, y), ...])]."""
    h, w = ink.shape
    seen = np.zeros_like(ink)
    out = []
    for sy, sx in zip(*np.nonzero(ink)):
        if seen[sy, sx]:
            continue
        stack, pts = [(sx, sy)], []
        seen[sy, sx] = True
        while stack:
            x, y = stack.pop()
            pts.append((x, y))
            for nx in (x - 1, x, x + 1):
                for ny in (y - 1, y, y + 1):
                    if 0 <= nx < w and 0 <= ny < h and ink[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((nx, ny))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        out.append((min(xs), max(xs) + 1, min(ys), max(ys) + 1, pts))
    return out


def _segments(img, x_min=0):
    """Glyph candidates in one column crop: [(x0, x1, level array band_h x width)], left to right.
    Pixels left of x_min are ignored (the number never starts before its column's START)."""
    mask, lev = _ink(img)
    band, lev = mask[BAND[0]:BAND[1]].copy(), lev[BAND[0]:BAND[1]]
    band[:, :max(0, x_min)] = False
    bh = band.shape[0]
    comps = [c for c in _components(band) if len(c[4]) >= 4]          # specks
    # Pieces of one glyph overlap in x (a thin corner can break off); merge those.
    groups = []
    for c in sorted(comps, key=lambda c: c[0]):
        if groups and c[0] < groups[-1][1]:
            g = groups[-1]
            groups[-1] = (g[0], max(g[1], c[1]), min(g[2], c[2]), max(g[3], c[3]), g[4] + c[4])
        else:
            groups.append(c)
    out = []
    for x0, x1, y0, y1, pts in groups:
        # Digits and '/' fill the band top to bottom with strokes ~3 px thick; junk rarely does both.
        if y0 > 1 or y1 < bh - 1 or x1 - x0 < 2 or len(pts) < 1.8 * (y1 - y0):
            continue
        # HUD text has a pure-white core (>= HI); a pale wall or road edge behind the panel passes WHITE but
        # stays below HI. Fewer sure-ink pixels than one band-high stroke is terrain, not a glyph ('/' has ~24).
        if sum(lev[y, x] == 2 for x, y in pts) < bh:
            continue
        # Levels of the glyph's own pixels plus a 1 px rim (its antialiased edge), nothing else in the box.
        own = np.zeros((bh, x1 - x0 + 2), dtype=bool)
        for x, y in pts:
            own[y, x - x0 + 1] = True
        own = _dilate(own)[:, 1:-1]
        out.append((x0, x1, np.where(own, lev[:, x0:x1], 0).astype(np.int8)))
    return out


def _to_bits(arr):
    return ",".join("".join(str(int(v)) for v in row) for row in arr)


def _from_bits(bits):
    return np.array([[int(ch) for ch in row] for row in bits.split(",")], dtype=np.int8)


def segment(img):
    """Split one column crop into glyph bitmaps (left to right): rows of 0/1/2 (no/maybe/sure ink) joined by ','."""
    return [_to_bits(b) for _, _, b in _segments(img)]


# ---------- matching ----------

def _canvas(arr):
    """Place a glyph on the fixed matching canvas (2 px margin left, 1 px top/bottom)."""
    h, w = arr.shape
    c = np.zeros((h + 2, CW), dtype=np.int8)
    w = min(w, CW - 4)
    c[1:h + 1, 2:2 + w] = arr[:, :w]
    return c


def _dilate(c):
    d = c.copy()
    d[1:] |= c[:-1]
    d[:-1] |= c[1:]
    d[:, 1:] |= d[:, :-1].copy()
    d[:, :-1] |= d[:, 1:].copy()
    return d


_db = None


def _templates():
    """(chars, stacked 'sure ink' canvases, stacked 'maybe ink' canvases)."""
    global _db
    if _db is None:
        _db = _build(json.loads(GLYPHS.read_text(encoding="utf-8")) if GLYPHS.exists() else {})
    return _db


def _build(raw):
    chars, cans = [], []
    for ch, samples in sorted(raw.items()):
        if ch.startswith("_"):          # "_icons": not a glyph
            continue
        for s in samples:
            chars.append(ch)
            cans.append(_canvas(_from_bits(s)))
    if not cans:
        return [], None, None
    t = np.stack(cans)
    return chars, t == 2, t >= 1


SHIFTS = [(dy, dx) for dy in (-1, 0, 1) for dx in (-2, -1, 0, 1, 2)]


def _distances(arr):
    """{char: smallest mismatch count} over all template samples and small shifts. A pixel mismatches when
    one side is surely ink and the other surely not (edge pixels that are 'maybe' on either side are free),
    so antialiasing and 1 px offsets cost nothing but a missing or extra stroke does (5 vs 6 vs 8)."""
    chars, t2, t1 = _templates()
    if t2 is None or arr.shape[0] + 2 != t2.shape[1]:
        return {}
    c = _canvas(arr)
    best = np.full(len(chars), 10 ** 6)
    for dy, dx in SHIFTS:
        s = np.roll(np.roll(c, dy, axis=0), dx, axis=1)
        d = ((s == 2) & ~t1).sum(axis=(1, 2)) + (t2 & (s == 0)).sum(axis=(1, 2))
        best = np.minimum(best, d)
    out = {}
    for ch, d in zip(chars, best):
        out[ch] = min(out.get(ch, 10 ** 6), int(d))
    return out


def _match(arr, allowed=None):
    """(char or '?', distance, margin to the next-best allowed char)."""
    ds = sorted(((c, d) for c, d in _distances(arr).items() if allowed is None or c in allowed),
                key=lambda kv: kv[1])
    if not ds:
        return "?", 10 ** 6, 0
    ch, d = ds[0]
    margin = ds[1][1] - d if len(ds) > 1 else 10 ** 6
    return (ch if d <= MAX_DIST and margin >= MARGIN else "?"), d, margin


def classify(bits):
    return _match(_from_bits(bits))[0]


def _split(bits, allowed):
    """A blob that is not one glyph: glyphs touching each other, or a glyph with junk stuck to it.
    Explain it left to right as glyphs plus dropped columns (each dropped sure-ink pixel costs 1).
    -> (cost, text) of the cheapest explanation, or None."""
    w = bits.shape[1]
    sure = (bits == 2).sum(axis=0)
    best = [None] * (w + 1)
    best[0] = (0, "")
    for x in range(w):
        if best[x] is None:
            continue
        cost, text = best[x]
        nxt = (cost + int(sure[x]), text)                       # drop column x
        if best[x + 1] is None or nxt[0] < best[x + 1][0]:
            best[x + 1] = nxt
        if not sure[x]:
            continue
        for y in range(x + 5, min(w, x + 18) + 1):
            if not sure[y - 1]:
                continue
            ch, d, _ = _match(bits[:, x:y], allowed)
            if ch != "?" and (best[y] is None or cost + d < best[y][0]):
                best[y] = (cost + d, text + ch)
    return best[w] if best[w] and best[w][1] else None


def read_column(img, i):
    """One column crop -> text ('?' where unsure)."""
    start = START[i]
    allowed = "0123456789/" if i == 2 else "0123456789"
    # The number is left-aligned at a fixed x: ignore anything left of that.
    segs = _segments(img, start - 2)
    if not segs or not start - 3 <= segs[0][0] <= start + 4:
        return "?" if segs else ""
    run = [segs[0]]
    for k, s in enumerate(segs[1:], 1):
        if s[0] - run[-1][1] > MAX_GAP:
            # Past the number: junk, unless something there is clearly a digit (one was hidden, e.g. by the
            # mouse cursor, and the number would read short).
            if any(_match(b, allowed)[0] != "?" for _, _, b in segs[k:]):
                return "?"
            break
        run.append(s)
    if run[-1][1] >= img.width - 1:
        return "?"                      # the number runs off the crop (shifted panel): its end is not visible
    if i < 2:
        # Bright pixels where a next digit would sit, but no glyph: a digit may be covered (cursor, tooltip)
        # and minerals/gas would read short. Unsure.
        lev = _ink(img)[1][BAND[0]:BAND[1]]
        end = run[-1][1]
        rows = np.nonzero((lev[:, end + 2:end + 16] == 2).any(axis=1))[0]
        if len(rows) and rows[-1] - rows[0] >= 6:          # taller than a speck or a terrain edge
            return "?"
    text = ""
    for k, (_x0, _x1, bits) in enumerate(run):
        ch, d, _ = _match(bits, allowed)
        if ch == "?":
            sp = _split(bits, allowed)
            if sp and sp[0] <= SPLIT_MAX:
                ch = sp[1]
        if ch == "?" and i == 2 and k == len(run) - 1 and re.fullmatch(r"\d+/\d\d", text) and d > 3 * MAX_DIST:
            # Last blob after a full 'used/cap' looks nothing like a glyph: junk touching the number. Only here:
            # dropping a blob from minerals/gas could turn 2513 into 251; a cut-short cap fails parse_panel.
            break
        text += ch
    return text


def _prepare(img):
    img = img.convert("RGB")
    if img.width > 1000:                                 # a full screenshot: cut the panel out first
        img = img.crop(panel_box(img.width, img.height))
    if img.height != SIZE[1]:                            # other resolutions: scale to 1440p pixel size
        from PIL import Image
        img = img.resize((round(img.width * SIZE[1] / img.height), SIZE[1]), Image.LANCZOS)
    return img


# ---------- panel position ----------
# The panel is not always at the same x inside panel_box: in 1v1 (no resource-trading button) everything sits
# ~43 px further right than in 2v2. The mineral and gas icons are found by colour template and the columns
# move with them. Icon templates: "_icons" in glyphs2.json, cut from a 2v2 crop (offset 0).
ICON_BOXES = {"min": (10, 55), "gas": (180, 215)}     # x spans holding each icon in the reference crop
SHIFT_RANGE = (-60, 100)
ICON_ERR = 60                    # mean |RGB diff| per icon pixel accepted as a match


def _icon_masks(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return {"min": (b > 180) & (g > 120) & (r < 140) & (b - r > 80),
            "gas": (g > 140) & (r < 150) & (b < 120) & (g - r > 50) & (g - b > 60)}


def make_icons(img):
    """Icon templates from a clean reference crop: {name: [[x, y, r, g, b], ...]}."""
    a = np.asarray(_prepare(img), dtype=np.int16)
    out = {}
    for name, m in _icon_masks(a).items():
        x0, x1 = ICON_BOXES[name]
        ys, xs = np.nonzero(m[:, x0:x1])
        out[name] = [[int(x + x0), int(y)] + [int(v) for v in a[y, x + x0]] for x, y in zip(xs, ys)]
    return out


_icons = None


def panel_shift(img):
    """x offset of the panel inside the crop (0 = 2v2 layout), from the icon positions; 0 if not found."""
    global _icons
    if _icons is None:
        raw = json.loads(GLYPHS.read_text(encoding="utf-8")) if GLYPHS.exists() else {}
        _icons = {k: np.array(v, dtype=np.int32) for k, v in raw.get("_icons", {}).items()}
    a = np.asarray(img, dtype=np.int32)
    w = a.shape[1]
    found = {}
    for name, t in _icons.items():
        xs, ys, rgb = t[:, 0], t[:, 1], t[:, 2:]
        best = None
        for s in range(SHIFT_RANGE[0], SHIFT_RANGE[1] + 1):
            if xs.min() + s < 0 or xs.max() + s >= w:
                continue
            err = np.abs(a[ys, xs + s] - rgb).sum(axis=1).mean()
            if best is None or err < best[0]:
                best = (err, s)
        if best and best[0] <= ICON_ERR:
            found[name] = best
    if not found:
        return 0
    return min(found.values())[1]                 # the better match (they agree on clean frames)


def _columns(img, shift=0):
    """Column crops in pixels from the crop's left edge (which panel_box fixes at 75.5% of the screen), moved by
    the detected panel shift; supply runs to the crop's right edge, so a wider crop only adds room."""
    spans = [(a + shift, b + shift) for a, b in COLUMN_PX]
    spans[-1] = (spans[-1][0], img.width)
    return [img.crop((min(img.width, a), 0, min(img.width, b), img.height)) for a, b in spans]


def ocr_text(img):
    """Read the panel crop as 'minerals | gas | supply'."""
    img = _prepare(img)
    return " | ".join(read_column(c, i) for i, c in enumerate(_columns(img, panel_shift(img))))


def learn(pairs):
    """pairs: [(panel image path, '1015|253|35/46'), ...] -> add templates to glyphs2.json."""
    global _db, _icons
    from PIL import Image
    db = json.loads(GLYPHS.read_text(encoding="utf-8")) if GLYPHS.exists() else {}
    if "_icons" not in db:          # icon positions define offset 0: the first image must be a 2v2 panel
        db["_icons"] = make_icons(Image.open(pairs[0][0]))
        GLYPHS.write_text(json.dumps(db), encoding="utf-8")
        _icons = None
    for path, label in pairs:
        img = _prepare(Image.open(path))
        for i, (col, text) in enumerate(zip(_columns(img, panel_shift(img)), label.split("|"))):
            segs = _segments(col, START[i] - 2)
            gl = segs[:len(text)]
            if len(gl) != len(text) or any(b.shape[1] > 19 for _, _, b in gl):
                print(f"skip {path} '{text}': found {len(segs)} glyphs")
                continue
            for ch, (_, _, bits) in zip(text, gl):
                _db = _build(db)
                ds = _distances(bits)
                own = ds.get(ch, 10 ** 6)
                other = min([d for c, d in ds.items() if c != ch] or [10 ** 6])
                # Skip samples already covered, and noisy ones (junk stuck on, or close to another digit):
                # those would blur the 5/6/8/9 distinctions for every later read.
                if ch in db and (own <= 1 or own > 2 * MAX_DIST or other < 6):
                    continue
                db.setdefault(ch, []).append(_to_bits(bits))
    GLYPHS.write_text(json.dumps(db), encoding="utf-8")
    _db = None
    print("templates:", {k: len(v) for k, v in sorted(db.items()) if not k.startswith("_")})


def parse_panel(text):
    """'455 | 40 | 19/23' -> dict(minerals, gas, used, cap); None if it doesn't look like the panel."""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3 or "?" in text:
        return None
    num = r"0|[1-9]\d{0,4}"                              # no leading zeros
    if not (re.fullmatch(num, parts[0]) and re.fullmatch(num, parts[1])):
        return None
    sup = re.fullmatch(r"([1-9]\d{0,2})/([1-9]\d{1,2})", parts[2])
    if not sup:
        return None
    used, cap = int(sup.group(1)), int(sup.group(2))
    if not 15 <= cap <= 200 or used > min(200, cap + 16):
        return None
    return {"minerals": int(parts[0]), "gas": int(parts[1]), "used": used, "cap": cap, "raw": text}


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--learn"]:
        pairs = []
        while args[:1] == ["--learn"]:
            pairs.append((args[1], args[2]))
            args = args[3:]
        learn(pairs)
    else:
        from PIL import Image
        for p in args:
            t = ocr_text(Image.open(p))
            print(p, repr(t), parse_panel(t))
