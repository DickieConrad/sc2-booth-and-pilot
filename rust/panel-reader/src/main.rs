//! panel-reader eval | bench | dump | read FILE... | serve --port 8766
//! Options: --live DIR (the project's live/ folder; default: found by walking up from the cwd)

use panel_reader::{decode_png, parse_panel, Panel, Reader, Rgb};
use std::path::{Path, PathBuf};
use std::time::Instant;

fn find_live(args: &[String]) -> PathBuf {
    if let Some(i) = args.iter().position(|a| a == "--live") {
        return PathBuf::from(&args[i + 1]);
    }
    let mut d = std::env::current_dir().unwrap();
    loop {
        if d.join("live/glyphs2.json").exists() {
            return d.join("live");
        }
        if !d.pop() {
            panic!("live/glyphs2.json not found above the cwd; pass --live DIR");
        }
    }
}

fn opt<T: std::str::FromStr>(args: &[String], name: &str, default: T) -> T {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn load_png(p: &Path) -> Rgb {
    decode_png(&std::fs::read(p).unwrap_or_else(|e| panic!("{}: {e}", p.display()))).unwrap()
}

/// labels.json as (file, label) in file order.
fn labels(live: &Path) -> Vec<(String, Option<String>)> {
    let text = std::fs::read_to_string(live.join("out/dev/labels.json")).unwrap();
    // keep file order: parse keys by hand-scanning would be fragile; serde_json's Map is sorted, which is fine
    // because every use below either sorts or only counts.
    let v: serde_json::Value = serde_json::from_str(&text).unwrap();
    v.as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().map(String::from))).collect()
}

/// eval.py split(): held-out = miss crops sorted by name, every third block of 6.
fn split(l: &[(String, Option<String>)]) -> (Vec<String>, Vec<String>, Vec<String>) {
    let mut miss: Vec<String> = l.iter().map(|x| x.0.clone()).filter(|k| k.contains("/miss/")).collect();
    miss.sort();
    let test: Vec<String> = miss.iter().enumerate().filter(|(i, _)| (i / 6) % 3 == 2).map(|x| x.1.clone()).collect();
    let train = l.iter().map(|x| x.0.clone()).filter(|f| !test.contains(f) && !f.contains("/dev/live_")).collect();
    let live = l.iter().map(|x| x.0.clone()).filter(|f| f.contains("/dev/live_")).collect();
    (train, test, live)
}

fn norm(label: &Option<String>) -> Option<(u32, u32, u32, u32)> {
    let s = label.as_ref()?;
    let p: Vec<&str> = s.split('|').collect();
    let (u, c) = p[2].split_once('/').unwrap();
    Some((p[0].parse().unwrap(), p[1].parse().unwrap(), u.parse().unwrap(), c.parse().unwrap()))
}

fn score(r: &Reader, live: &Path, l: &[(String, Option<String>)], files: &[String], verbose: bool, name: &str) {
    let (mut ok, mut wrong, mut none) = (0, 0, 0);
    for f in files {
        let img = load_png(&live.join(f));
        let text = r.ocr_text(&img).unwrap();
        let got = parse_panel(&text).map(|p| (p.minerals, p.gas, p.used, p.cap));
        let label = &l.iter().find(|x| &x.0 == f).unwrap().1;
        if got == norm(label) {
            ok += 1;
            continue;
        }
        if got.is_none() {
            none += 1
        } else {
            wrong += 1
        }
        if verbose {
            let tag = if got.is_some() { "WRONG" } else { "none " };
            println!("  {name} {tag} {f}: read {text:?}, want {label:?}");
        }
    }
    let n = files.len();
    println!(
        "{name:10} exact {ok}/{n} ({:.0}%)  wrong-but-parsed {wrong}  unreadable {none}",
        100.0 * ok as f64 / n as f64
    );
}

fn panel_json(p: &Option<Panel>) -> serde_json::Value {
    match p {
        Some(p) => {
            serde_json::json!({"minerals": p.minerals, "gas": p.gas, "used": p.used, "cap": p.cap, "raw": p.raw})
        }
        None => serde_json::Value::Null,
    }
}

fn pct(sorted: &[f64], q: f64) -> f64 {
    sorted[((sorted.len() as f64 - 1.0) * q).round() as usize]
}

fn bench(r: &Reader, live: &Path, l: &[(String, Option<String>)], rounds: usize) {
    let imgs: Vec<Rgb> = l.iter().map(|(f, _)| load_png(&live.join(f))).collect();
    for img in &imgs {
        let _ = r.read(img); // warm-up
    }
    let mut t = Vec::with_capacity(imgs.len() * rounds);
    let all = Instant::now();
    for _ in 0..rounds {
        for img in &imgs {
            let s = Instant::now();
            std::hint::black_box(r.read(std::hint::black_box(img)).unwrap());
            t.push(s.elapsed().as_secs_f64() * 1e6);
        }
    }
    let total = all.elapsed().as_secs_f64();
    t.sort_by(|a, b| a.partial_cmp(b).unwrap());
    println!(
        "rust in-process: {} reads ({} crops x {rounds})  p50 {:.1} us  p95 {:.1} us  max {:.1} us  mean {:.1} us",
        t.len(),
        imgs.len(),
        pct(&t, 0.5),
        pct(&t, 0.95),
        t[t.len() - 1],
        total * 1e6 / t.len() as f64
    );
}

fn query(url: &str, key: &str) -> Option<usize> {
    let q = url.split_once('?')?.1;
    q.split('&').find_map(|kv| kv.split_once('=').filter(|(k, _)| *k == key).and_then(|(_, v)| v.parse().ok()))
}

/// POST /read: PNG body, or raw RGB (?w=W&h=H; h defaults to 49, w to len/(3h)). -> panel JSON or null.
fn handle(r: &Reader, url: &str, body: &[u8]) -> Result<serde_json::Value, String> {
    let img = if body.starts_with(b"\x89PNG") {
        decode_png(body)?
    } else {
        let h = query(url, "h").unwrap_or(panel_reader::SIZE_H);
        let w = query(url, "w").unwrap_or(body.len() / (3 * h.max(1)));
        if w * h * 3 != body.len() || w == 0 {
            return Err(format!("raw RGB body of {} bytes does not match w={w} h={h}", body.len()));
        }
        Rgb { w, h, px: body.to_vec() }
    };
    Ok(panel_json(&r.read(&img)?))
}

fn serve(r: &Reader, port: u16) {
    let server = tiny_http::Server::http(("127.0.0.1", port)).expect("bind");
    eprintln!("panel-reader listening on http://127.0.0.1:{port}  (POST /read)");
    let json = tiny_http::Header::from_bytes("Content-Type", "application/json").unwrap();
    for mut req in server.incoming_requests() {
        let url = req.url().to_string();
        let path = url.split('?').next().unwrap_or("");
        let (code, out) = match (req.method(), path) {
            (tiny_http::Method::Post, "/read") => {
                let mut body = Vec::with_capacity(req.body_length().unwrap_or(0));
                match std::io::Read::read_to_end(req.as_reader(), &mut body) {
                    Err(e) => (400, serde_json::json!({"error": e.to_string()})),
                    Ok(_) => match handle(r, &url, &body) {
                        Ok(v) => (200, v),
                        Err(e) => (400, serde_json::json!({"error": e})),
                    },
                }
            }
            (tiny_http::Method::Get, "/health") => (200, serde_json::json!({"ok": true})),
            _ => (404, serde_json::json!({"error": "POST /read"})),
        };
        let resp = tiny_http::Response::from_string(out.to_string()).with_status_code(code).with_header(json.clone());
        let _ = req.respond(resp);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let cmd = args.get(1).map(String::as_str).unwrap_or("");
    let live = find_live(&args);
    let r = Reader::load(&live.join("glyphs2.json")).unwrap();
    match cmd {
        "eval" => {
            let l = labels(&live);
            let (train, test, live_set) = split(&l);
            let v = args.iter().any(|a| a == "-v");
            println!("train {}  held-out {}", train.len(), test.len());
            score(&r, &live, &l, &test, v, "rust");
            if !live_set.is_empty() {
                println!("live crops {}", live_set.len());
                score(&r, &live, &l, &live_set, v, "rust");
            }
            if args.iter().any(|a| a == "--train") {
                score(&r, &live, &l, &train, v, "rust/train");
            }
        }
        "dump" => {
            // {file: raw ocr_text} for every labeled crop, for the parity check against Python
            let mut m = serde_json::Map::new();
            for (f, _) in labels(&live) {
                m.insert(f.clone(), r.ocr_text(&load_png(&live.join(&f))).unwrap().into());
            }
            println!("{}", serde_json::Value::Object(m));
        }
        "bench" => bench(&r, &live, &labels(&live), opt(&args, "--rounds", 50)),
        "read" => {
            for p in args[2..].iter().filter(|a| a.ends_with(".png")) {
                let text = r.ocr_text(&load_png(Path::new(p))).unwrap();
                println!("{p} {text:?} {}", panel_json(&parse_panel(&text)));
            }
        }
        "serve" => serve(&r, opt(&args, "--port", 8766)),
        _ => eprintln!("usage: panel-reader eval [-v] [--train] | bench [--rounds N] | dump | read FILE.png... | serve [--port 8766]  [--live DIR]"),
    }
}
