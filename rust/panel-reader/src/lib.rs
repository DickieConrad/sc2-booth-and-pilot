//! Faithful port of live/panel_ocr.py (read path only; `--learn` stays in Python).
//! Same constants, same templates (glyphs2.json), same decisions. Comments name the Python function each
//! piece mirrors; see panel_ocr.py for the reasoning behind every rule.

use std::path::Path;

// ---------- constants (panel_ocr.py) ----------
const PANEL: (f64, f64, f64, f64) = (0.755, 0.008, 0.975, 0.042);
pub const SIZE_H: usize = 49;
const SIZE_W: f64 = 525.0;
const COLUMNS: [(f64, f64); 3] = [(0.105, 0.39), (0.41, 0.68), (0.722, 0.99)];
const START: [i64; 3] = [4, 8, 9];
const BAND: (usize, usize) = (21, 34);
const WHITE: i16 = 165;
const GREY: i16 = 30;
const HI: i16 = 200;
const LO: i16 = 100;
const MAX_GAP: i64 = 12;
const MAX_DIST: u32 = 6;
const MARGIN: u32 = 3;
const SPLIT_MAX: u32 = 10;
const CW: usize = 26;
const CH: usize = 15; // canvas height: 13-row band + 1 px top/bottom
const BIG: u32 = 1_000_000;
const SHIFT_RANGE: (i64, i64) = (-60, 100);
const ICON_ERR: f64 = 60.0;
const MASK26: u32 = (1 << CW) - 1;

/// An RGB8 image, row-major, 3 bytes per pixel.
#[derive(Clone)]
pub struct Rgb {
    pub w: usize,
    pub h: usize,
    pub px: Vec<u8>,
}

impl Rgb {
    #[inline]
    fn at(&self, x: usize, y: usize) -> [i16; 3] {
        let i = (y * self.w + x) * 3;
        [self.px[i] as i16, self.px[i + 1] as i16, self.px[i + 2] as i16]
    }

    /// PIL Image.crop semantics: out-of-bounds area is filled with black.
    fn crop(&self, x0: i64, y0: i64, x1: i64, y1: i64) -> Rgb {
        let w = (x1 - x0).max(0) as usize;
        let h = (y1 - y0).max(0) as usize;
        let mut px = vec![0u8; w * h * 3];
        for y in 0..h {
            let sy = y0 + y as i64;
            if sy < 0 || sy >= self.h as i64 {
                continue;
            }
            for x in 0..w {
                let sx = x0 + x as i64;
                if sx < 0 || sx >= self.w as i64 {
                    continue;
                }
                let s = (sy as usize * self.w + sx as usize) * 3;
                let d = (y * w + x) * 3;
                px[d..d + 3].copy_from_slice(&self.px[s..s + 3]);
            }
        }
        Rgb { w, h, px }
    }
}

/// Decode a PNG to RGB (alpha dropped, like PIL convert("RGB")).
pub fn decode_png(bytes: &[u8]) -> Result<Rgb, String> {
    let mut dec = png::Decoder::new(bytes);
    dec.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut r = dec.read_info().map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; r.output_buffer_size()];
    let info = r.next_frame(&mut buf).map_err(|e| e.to_string())?;
    let (w, h) = (info.width as usize, info.height as usize);
    let n = w * h;
    let px = match info.color_type {
        png::ColorType::Rgb => {
            buf.truncate(n * 3);
            buf
        }
        png::ColorType::Rgba => buf.as_chunks::<4>().0.iter().take(n).flat_map(|p| [p[0], p[1], p[2]]).collect(),
        png::ColorType::Grayscale => buf.iter().take(n).flat_map(|&g| [g, g, g]).collect(),
        png::ColorType::GrayscaleAlpha => {
            buf.as_chunks::<2>().0.iter().take(n).flat_map(|p| [p[0], p[0], p[0]]).collect()
        }
        c => return Err(format!("unsupported PNG color type {c:?}")),
    };
    Ok(Rgb { w, h, px })
}

pub fn panel_box(w: usize, h: usize) -> (i64, i64, i64, i64) {
    let (x0, y0, x1, y1) = PANEL;
    let (w, h) = (w as f64, h as f64);
    ((w * x0) as i64, (h * y0) as i64, (w * x1) as i64, (h * y1) as i64)
}

/// A small 2-D array of levels (0 background, 1 maybe ink, 2 sure ink).
#[derive(Clone)]
struct Lev {
    w: usize,
    h: usize,
    v: Vec<u8>,
}

impl Lev {
    #[inline]
    fn get(&self, y: usize, x: usize) -> u8 {
        self.v[y * self.w + x]
    }
    fn cols(&self, x0: usize, x1: usize) -> Lev {
        let w = x1 - x0;
        let mut v = Vec::with_capacity(w * self.h);
        for y in 0..self.h {
            v.extend_from_slice(&self.v[y * self.w + x0..y * self.w + x1]);
        }
        Lev { w, h: self.h, v }
    }
}

/// _ink: (segmentation mask, levels) for the whole image.
fn ink(img: &Rgb) -> (Vec<bool>, Vec<u8>) {
    let n = img.w * img.h;
    let mut mask = vec![false; n];
    let mut lev = vec![0u8; n];
    for i in 0..n {
        let (r, g, b) = (img.px[i * 3] as i16, img.px[i * 3 + 1] as i16, img.px[i * 3 + 2] as i16);
        let mn = r.min(g).min(b);
        let mx = r.max(g).max(b);
        let grey = mx - mn <= GREY;
        let red = r > 170 && g < 90 && b < 90;
        mask[i] = (mn >= WHITE && grey) || red;
        let mut l = (mn >= LO && grey) as u8 + (mn >= HI && grey) as u8;
        if red {
            l = 2;
        }
        if r > 120 && g < 110 && b < 110 && l == 0 {
            l = 1;
        }
        lev[i] = l;
    }
    (mask, lev)
}

/// Level band (rows BAND) of an image.
fn band_levels(img: &Rgb) -> Lev {
    let (_, lev) = ink(img);
    let (b0, b1) = (BAND.0.min(img.h), BAND.1.min(img.h));
    Lev { w: img.w, h: b1 - b0, v: lev[b0 * img.w..b1 * img.w].to_vec() }
}

struct Comp {
    x0: usize,
    x1: usize,
    y0: usize,
    y1: usize,
    pts: Vec<(usize, usize)>,
}

/// _components: 8-neighbour connected components, discovered in row-major order.
fn components(ink: &[bool], w: usize, h: usize) -> Vec<Comp> {
    let mut seen = vec![false; w * h];
    let mut out = Vec::new();
    let mut stack = Vec::new();
    for sy in 0..h {
        for sx in 0..w {
            if !ink[sy * w + sx] || seen[sy * w + sx] {
                continue;
            }
            seen[sy * w + sx] = true;
            stack.clear();
            stack.push((sx, sy));
            let mut pts = Vec::new();
            while let Some((x, y)) = stack.pop() {
                pts.push((x, y));
                for nx in x as i64 - 1..=x as i64 + 1 {
                    for ny in y as i64 - 1..=y as i64 + 1 {
                        if nx < 0 || ny < 0 || nx >= w as i64 || ny >= h as i64 {
                            continue;
                        }
                        let k = ny as usize * w + nx as usize;
                        if ink[k] && !seen[k] {
                            seen[k] = true;
                            stack.push((nx as usize, ny as usize));
                        }
                    }
                }
            }
            let x0 = pts.iter().map(|p| p.0).min().unwrap();
            let x1 = pts.iter().map(|p| p.0).max().unwrap() + 1;
            let y0 = pts.iter().map(|p| p.1).min().unwrap();
            let y1 = pts.iter().map(|p| p.1).max().unwrap() + 1;
            out.push(Comp { x0, x1, y0, y1, pts });
        }
    }
    out
}

struct Seg {
    x0: i64,
    x1: i64,
    bits: Lev,
}

/// _segments: glyph candidates in one column crop, left to right.
fn segments(img: &Rgb, x_min: i64) -> Vec<Seg> {
    let (mask, lev) = ink(img);
    let (b0, b1) = (BAND.0.min(img.h), BAND.1.min(img.h));
    let bh = b1 - b0;
    let w = img.w;
    let xm = x_min.max(0) as usize;
    let mut band = mask[b0 * w..b1 * w].to_vec();
    for y in 0..bh {
        for x in 0..xm.min(w) {
            band[y * w + x] = false;
        }
    }
    let levb = Lev { w, h: bh, v: lev[b0 * w..b1 * w].to_vec() };
    let mut comps: Vec<Comp> = components(&band, w, bh).into_iter().filter(|c| c.pts.len() >= 4).collect();
    comps.sort_by_key(|c| c.x0); // stable, like Python's sorted
    let mut groups: Vec<Comp> = Vec::new();
    for c in comps {
        if let Some(g) = groups.last_mut() {
            if c.x0 < g.x1 {
                g.x1 = g.x1.max(c.x1);
                g.y0 = g.y0.min(c.y0);
                g.y1 = g.y1.max(c.y1);
                g.pts.extend(c.pts);
                continue;
            }
        }
        groups.push(c);
    }
    let mut out = Vec::new();
    for g in groups {
        let (x0, x1, y0, y1) = (g.x0, g.x1, g.y0, g.y1);
        if y0 > 1 || y1 + 1 < bh || x1 - x0 < 2 || (g.pts.len() as f64) < 1.8 * (y1 - y0) as f64 {
            continue;
        }
        // HUD text has a pure-white core (>= HI); pale terrain behind the panel passes WHITE but stays below HI.
        if g.pts.iter().filter(|&&(x, y)| levb.get(y, x) == 2).count() < bh {
            continue;
        }
        // own pixels plus a 1 px rim (3x3 dilation), on a grid with 1 px margin left/right.
        let ow = x1 - x0 + 2;
        let mut own = vec![false; bh * ow];
        for &(x, y) in &g.pts {
            own[y * ow + x - x0 + 1] = true;
        }
        let mut dil = vec![false; bh * ow];
        for y in 0..bh {
            for x in 0..ow {
                if !own[y * ow + x] {
                    continue;
                }
                for yy in y.saturating_sub(1)..=(y + 1).min(bh - 1) {
                    for xx in x.saturating_sub(1)..=(x + 1).min(ow - 1) {
                        dil[yy * ow + xx] = true;
                    }
                }
            }
        }
        let gw = x1 - x0;
        let mut v = vec![0u8; bh * gw];
        for y in 0..bh {
            for x in 0..gw {
                if dil[y * ow + x + 1] {
                    v[y * gw + x] = levb.get(y, x0 + x);
                }
            }
        }
        out.push(Seg { x0: x0 as i64, x1: x1 as i64, bits: Lev { w: gw, h: bh, v } });
    }
    out
}

// ---------- matching ----------

/// A glyph on the matching canvas as row bitmasks (bit j = canvas column j).
#[derive(Clone, Copy)]
struct Canvas {
    sure: [u32; CH],  // level == 2
    maybe: [u32; CH], // level >= 1
}

/// _canvas: 2 px margin left, 1 px top/bottom, width capped at CW - 4.
fn canvas(arr: &Lev) -> Canvas {
    let mut c = Canvas { sure: [0; CH], maybe: [0; CH] };
    let w = arr.w.min(CW - 4);
    for y in 0..arr.h.min(CH - 2) {
        for x in 0..w {
            let v = arr.get(y, x);
            let bit = 1u32 << (2 + x);
            if v == 2 {
                c.sure[y + 1] |= bit;
            }
            if v >= 1 {
                c.maybe[y + 1] |= bit;
            }
        }
    }
    c
}

#[inline]
fn rot(row: u32, dx: i64) -> u32 {
    // np.roll along a CW-wide row: new bit j = old bit (j - dx) mod CW
    let k = dx.rem_euclid(CW as i64) as u32;
    if k == 0 {
        row
    } else {
        ((row << k) | (row >> (CW as u32 - k))) & MASK26
    }
}

struct Icon {
    pts: Vec<(usize, usize, [i32; 3])>,
    xmin: i64,
    xmax: i64,
}

/// Templates (glyphs2.json) loaded once.
pub struct Reader {
    chars: Vec<u8>,     // one per template sample, sorted by char
    uniq: Vec<u8>,      // distinct chars, sorted (== Python dict insertion order)
    idx: Vec<usize>,    // sample -> index into uniq
    t2: Vec<[u32; CH]>, // sure ink
    t1: Vec<[u32; CH]>, // maybe or sure ink
    icons: Vec<Icon>,
}

impl Reader {
    pub fn load(path: &Path) -> Result<Reader, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
        Self::from_json(&text)
    }

    pub fn from_json(text: &str) -> Result<Reader, String> {
        let raw: serde_json::Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
        let obj = raw.as_object().ok_or("glyphs: not an object")?;
        let mut keys: Vec<&String> = obj.keys().collect();
        keys.sort(); // Python sorted() on str keys == byte order for these ASCII keys
        let mut r = Reader { chars: vec![], uniq: vec![], idx: vec![], t2: vec![], t1: vec![], icons: vec![] };
        for k in keys {
            if k.starts_with('_') {
                continue;
            }
            let ch = k.as_bytes()[0];
            for s in obj[k].as_array().ok_or("glyph samples: not a list")? {
                let s = s.as_str().ok_or("glyph sample: not a string")?;
                let rows: Vec<&str> = s.split(',').collect();
                let w = rows[0].len();
                let mut v = Vec::with_capacity(rows.len() * w);
                for row in &rows {
                    v.extend(row.bytes().map(|b| b - b'0'));
                }
                let arr = Lev { w, h: rows.len(), v };
                if arr.h + 2 != CH {
                    return Err(format!("template height {} (expected {})", arr.h, CH - 2));
                }
                let c = canvas(&arr);
                if r.uniq.last() != Some(&ch) {
                    r.uniq.push(ch);
                }
                r.chars.push(ch);
                r.idx.push(r.uniq.len() - 1);
                r.t2.push(c.sure);
                r.t1.push(c.maybe);
            }
        }
        if let Some(icons) = obj.get("_icons").and_then(|v| v.as_object()) {
            for (_, list) in icons {
                let mut pts = Vec::new();
                for p in list.as_array().ok_or("icon: not a list")? {
                    let p: Vec<i64> = p.as_array().ok_or("icon px")?.iter().map(|v| v.as_i64().unwrap_or(0)).collect();
                    pts.push((p[0] as usize, p[1] as usize, [p[2] as i32, p[3] as i32, p[4] as i32]));
                }
                if pts.is_empty() {
                    continue;
                }
                let xmin = pts.iter().map(|p| p.0).min().unwrap() as i64;
                let xmax = pts.iter().map(|p| p.0).max().unwrap() as i64;
                r.icons.push(Icon { pts, xmin, xmax });
            }
        }
        Ok(r)
    }

    /// _distances: smallest mismatch count per distinct char (indexed like `uniq`); None if no templates fit.
    fn distances(&self, arr: &Lev) -> Option<Vec<u32>> {
        if self.t2.is_empty() || arr.h + 2 != CH {
            return None;
        }
        let c = canvas(arr);
        // canvas levels as masks: s2 = sure, s0 = background
        let mut best = vec![BIG; self.uniq.len()];
        for dy in -1i64..=1 {
            for dx in -2i64..=2 {
                let mut s2 = [0u32; CH];
                let mut s0 = [0u32; CH];
                for i in 0..CH {
                    let src = (i as i64 - dy).rem_euclid(CH as i64) as usize;
                    s2[i] = rot(c.sure[src], dx);
                    s0[i] = !rot(c.maybe[src], dx) & MASK26;
                }
                for (t, (t2, t1)) in self.t2.iter().zip(&self.t1).enumerate() {
                    let mut d = 0u32;
                    for i in 0..CH {
                        d += (s2[i] & !t1[i]).count_ones() + (t2[i] & s0[i]).count_ones();
                    }
                    let u = self.idx[t];
                    if d < best[u] {
                        best[u] = d;
                    }
                }
            }
        }
        Some(best)
    }

    /// _match: (char or b'?', distance, margin to the next-best allowed char).
    fn matches(&self, arr: &Lev, allowed: &[u8]) -> (u8, u32, u32) {
        let Some(best) = self.distances(arr) else { return (b'?', BIG, 0) };
        let mut ds: Vec<(u8, u32)> =
            self.uniq.iter().zip(best).filter(|(c, _)| allowed.contains(c)).map(|(&c, d)| (c, d)).collect();
        if ds.is_empty() {
            return (b'?', BIG, 0);
        }
        ds.sort_by_key(|kv| kv.1); // stable
        let (ch, d) = ds[0];
        let margin = if ds.len() > 1 { ds[1].1 - d } else { BIG };
        (if d <= MAX_DIST && margin >= MARGIN { ch } else { b'?' }, d, margin)
    }

    /// _split: cheapest explanation of a blob as glyphs plus dropped columns.
    fn split(&self, bits: &Lev, allowed: &[u8]) -> Option<(u32, String)> {
        let w = bits.w;
        let sure: Vec<u32> = (0..w).map(|x| (0..bits.h).filter(|&y| bits.get(y, x) == 2).count() as u32).collect();
        let mut best: Vec<Option<(u32, String)>> = vec![None; w + 1];
        best[0] = Some((0, String::new()));
        for x in 0..w {
            let Some((cost, text)) = best[x].clone() else { continue };
            let nc = cost + sure[x];
            if best[x + 1].as_ref().is_none_or(|b| nc < b.0) {
                best[x + 1] = Some((nc, text.clone()));
            }
            if sure[x] == 0 {
                continue;
            }
            for y in x + 5..=w.min(x + 18) {
                if sure[y - 1] == 0 {
                    continue;
                }
                let (ch, d, _) = self.matches(&bits.cols(x, y), allowed);
                if ch != b'?' && best[y].as_ref().is_none_or(|b| cost + d < b.0) {
                    let mut t = text.clone();
                    t.push(ch as char);
                    best[y] = Some((cost + d, t));
                }
            }
        }
        match best.pop().flatten() {
            Some(b) if !b.1.is_empty() => Some(b),
            _ => None,
        }
    }

    /// read_column: one column crop -> text ('?' where unsure).
    fn read_column(&self, img: &Rgb, i: usize) -> String {
        let start = START[i];
        let allowed: &[u8] = if i == 2 { b"0123456789/" } else { b"0123456789" };
        let segs = segments(img, start - 2);
        if segs.is_empty() {
            return String::new();
        }
        if !(start - 3 <= segs[0].x0 && segs[0].x0 <= start + 4) {
            return "?".into();
        }
        let mut n = 1;
        for k in 1..segs.len() {
            if segs[k].x0 - segs[n - 1].x1 > MAX_GAP {
                if segs[k..].iter().any(|s| self.matches(&s.bits, allowed).0 != b'?') {
                    return "?".into();
                }
                break;
            }
            n += 1;
        }
        let run = &segs[..n];
        if run[n - 1].x1 >= img.w as i64 - 1 {
            return "?".into();
        }
        if i < 2 {
            let lev = band_levels(img);
            let end = run[n - 1].x1 as usize;
            let (a, b) = ((end + 2).min(lev.w), (end + 16).min(lev.w));
            let rows: Vec<usize> = (0..lev.h).filter(|&y| (a..b).any(|x| lev.get(y, x) == 2)).collect();
            if !rows.is_empty() && rows[rows.len() - 1] - rows[0] >= 6 {
                return "?".into();
            }
        }
        let mut text = String::new();
        for (k, s) in run.iter().enumerate() {
            let (c, d, _) = self.matches(&s.bits, allowed);
            let mut ch = (c as char).to_string();
            if c == b'?' {
                if let Some((cost, t)) = self.split(&s.bits, allowed) {
                    if cost <= SPLIT_MAX {
                        ch = t;
                    }
                }
            }
            if ch == "?" && i == 2 && k == n - 1 && is_full_supply(&text) && d > 3 * MAX_DIST {
                break;
            }
            text.push_str(&ch);
        }
        text
    }

    /// panel_shift: x offset of the panel inside the crop, from the icon positions; 0 if not found.
    fn panel_shift(&self, img: &Rgb) -> i64 {
        let w = img.w as i64;
        let mut found: Vec<(f64, i64)> = Vec::new();
        for icon in &self.icons {
            let mut best: Option<(f64, i64)> = None;
            for s in SHIFT_RANGE.0..=SHIFT_RANGE.1 {
                if icon.xmin + s < 0 || icon.xmax + s >= w {
                    continue;
                }
                let mut tot: i64 = 0;
                for &(x, y, rgb) in &icon.pts {
                    let p = img.at((x as i64 + s) as usize, y);
                    for c in 0..3 {
                        tot += (p[c] as i32 - rgb[c]).abs() as i64;
                    }
                }
                let err = tot as f64 / icon.pts.len() as f64;
                if best.is_none_or(|b| err < b.0) {
                    best = Some((err, s));
                }
            }
            if let Some(b) = best {
                if b.0 <= ICON_ERR {
                    found.push(b);
                }
            }
        }
        // min over (err, shift) tuples
        found.into_iter().min_by(|a, b| a.0.partial_cmp(&b.0).unwrap().then(a.1.cmp(&b.1))).map_or(0, |b| b.1)
    }

    /// ocr_text: 'minerals | gas | supply'. Err only for crops this port cannot prepare (non-1440p height).
    pub fn ocr_text(&self, img: &Rgb) -> Result<String, String> {
        let img = prepare(img)?;
        let shift = self.panel_shift(&img);
        let w = img.w as i64;
        let h = img.h as i64;
        let mut parts = Vec::with_capacity(3);
        for (i, (a, b)) in COLUMNS.iter().enumerate() {
            let (pa, pb) = ((SIZE_W * a) as i64 + shift, (SIZE_W * b) as i64 + shift);
            let pb = if i == 2 { w } else { pb };
            let col = img.crop(w.min(pa), 0, w.min(pb), h);
            parts.push(self.read_column(&col, i));
        }
        Ok(parts.join(" | "))
    }

    pub fn read(&self, img: &Rgb) -> Result<Option<Panel>, String> {
        Ok(parse_panel(&self.ocr_text(img)?))
    }
}

/// _prepare: cut the panel out of a full screenshot. The Python version LANCZOS-resizes other heights to 49;
/// this port refuses them instead of producing pixels that differ from PIL's resampler.
fn prepare(img: &Rgb) -> Result<std::borrow::Cow<'_, Rgb>, String> {
    let img = if img.w > 1000 {
        let (x0, y0, x1, y1) = panel_box(img.w, img.h);
        std::borrow::Cow::Owned(img.crop(x0, y0, x1, y1))
    } else {
        std::borrow::Cow::Borrowed(img)
    };
    if img.h != SIZE_H {
        return Err(format!("crop height {} != {} (only 1440p supported in this port)", img.h, SIZE_H));
    }
    Ok(img)
}

/// re.fullmatch(r"\d+/\d\d", text)
fn is_full_supply(t: &str) -> bool {
    let Some((a, b)) = t.split_once('/') else { return false };
    !a.is_empty() && a.bytes().all(|c| c.is_ascii_digit()) && b.len() == 2 && b.bytes().all(|c| c.is_ascii_digit())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Panel {
    pub minerals: u32,
    pub gas: u32,
    pub used: u32,
    pub cap: u32,
    pub raw: String,
}

/// "0|[1-9]\d{0,4}"
fn is_num(s: &str) -> bool {
    let b = s.as_bytes();
    s == "0" || (!b.is_empty() && b.len() <= 5 && (b'1'..=b'9').contains(&b[0]) && b.iter().all(u8::is_ascii_digit))
}

/// [1-9]\d{lo-1..hi-1}: positive, no leading zero, lo..=hi digits
fn is_pos(s: &str, lo: usize, hi: usize) -> bool {
    let b = s.as_bytes();
    (lo..=hi).contains(&b.len()) && (b'1'..=b'9').contains(&b[0]) && b.iter().all(u8::is_ascii_digit)
}

/// parse_panel
pub fn parse_panel(text: &str) -> Option<Panel> {
    let parts: Vec<&str> = text.split('|').map(str::trim).collect();
    if parts.len() != 3 || text.contains('?') {
        return None;
    }
    if !(is_num(parts[0]) && is_num(parts[1])) {
        return None;
    }
    let (u, c) = parts[2].split_once('/')?;
    if !(is_pos(u, 1, 3) && is_pos(c, 2, 3)) {
        return None;
    }
    let (used, cap): (u32, u32) = (u.parse().ok()?, c.parse().ok()?);
    if !(15..=200).contains(&cap) || used > 200.min(cap + 16) {
        return None;
    }
    Some(Panel { minerals: parts[0].parse().ok()?, gas: parts[1].parse().ok()?, used, cap, raw: text.to_string() })
}
