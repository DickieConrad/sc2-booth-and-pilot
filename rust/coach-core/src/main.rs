//! coach-core serve [--port 8767] | replay [--plan ID] [--tick 0.5] SAMPLES.json | rules-replay REQUESTS.jsonl | plans | keys
//! Options: --live DIR (the project's live/ folder; default: found by walking up from the cwd, then from the binary)

use coach_core::rules::{RulesHub, TickRequest};
use coach_core::service::{Method, Service};
use coach_core::{load_plans, parse_samples, replay, round1};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::Instant;

const FLAGS_WITH_VALUE: [&str; 4] = ["--live", "--port", "--plan", "--tick"];

fn find_live(args: &[String]) -> PathBuf {
    if let Some(v) = flag(args, "--live") {
        return PathBuf::from(v);
    }
    let starts = [std::env::current_dir().ok(), std::env::current_exe().ok()];
    for start in starts.into_iter().flatten() {
        let mut d = start;
        loop {
            if d.join("live/plans.json").exists() {
                return d.join("live");
            }
            if !d.pop() {
                break;
            }
        }
    }
    eprintln!("live/plans.json not found above the cwd or the binary; pass --live DIR");
    std::process::exit(2);
}

fn flag<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).map(String::as_str)
}

/// Arguments after the subcommand that are neither flags nor flag values.
fn positional(args: &[String]) -> Vec<&str> {
    let mut out = Vec::new();
    let mut i = 2;
    while i < args.len() {
        if FLAGS_WITH_VALUE.contains(&args[i].as_str()) {
            i += 2;
            continue;
        }
        if !args[i].starts_with("--") {
            out.push(args[i].as_str());
        }
        i += 1;
    }
    out
}

fn method(m: &tiny_http::Method) -> Method {
    match m {
        tiny_http::Method::Get => Method::Get,
        tiny_http::Method::Post => Method::Post,
        _ => Method::Other,
    }
}

fn serve(live: &Path, port: u16) {
    let plans = load_plans(live).unwrap_or_else(|e| {
        eprintln!("coach-core: {e}");
        std::process::exit(2);
    });
    let n = plans.len();
    let mut service = Service::new(live.to_path_buf(), plans);
    let server = tiny_http::Server::http(("127.0.0.1", port)).unwrap_or_else(|e| {
        eprintln!("coach-core: cannot bind 127.0.0.1:{port}: {e}");
        std::process::exit(1);
    });
    // --port 0 binds a free port (tests, the parity tool): print the real one
    let port = server.server_addr().to_ip().map_or(port, |a| a.port());
    eprintln!(
        "coach-core listening on http://127.0.0.1:{port}  ({n} plans from {})  POST /start /tick /rules/tick, GET /state /health",
        live.display()
    );
    let ctype = tiny_http::Header::from_bytes("Content-Type", "application/json").unwrap();
    for mut req in server.incoming_requests() {
        let mut body = String::new();
        let (code, out) = match std::io::Read::read_to_string(req.as_reader(), &mut body) {
            Ok(_) => service.handle(method(req.method()), req.url(), &body),
            Err(e) => (400, json!({"error": e.to_string()})),
        };
        let resp = tiny_http::Response::from_string(out.to_string()).with_status_code(code).with_header(ctype.clone());
        let _ = req.respond(resp);
    }
}

/// `rules-replay REQUESTS.jsonl`: each line a /rules/tick body, through one hub (as the server would);
/// prints one response per line with `ns`, the time the rules took for that tick.
fn run_rules_replay(args: &[String], live: &Path) {
    let Some(file) = positional(args).first().copied() else {
        eprintln!("usage: coach-core rules-replay REQUESTS.jsonl   (one /rules/tick body per line)");
        std::process::exit(2);
    };
    let text = std::fs::read_to_string(file).unwrap_or_else(|e| panic!("{file}: {e}"));
    let plans = load_plans(live).unwrap_or_else(|e| panic!("{e}"));
    let mut hub = RulesHub::new();
    let mut out = String::new();
    for (i, line) in text.lines().enumerate().filter(|(_, l)| !l.trim().is_empty()) {
        let v: Value = serde_json::from_str(line).unwrap_or_else(|e| panic!("{file}:{}: {e}", i + 1));
        let req = TickRequest::from_json(&v).unwrap_or_else(|e| panic!("{file}:{}: {e}", i + 1));
        let t0 = Instant::now();
        let res = hub.tick(&req, &plans);
        let ns = t0.elapsed().as_nanos() as u64;
        let mut j = match res {
            Ok(r) => r.to_json(),
            Err(e) => json!({"error": format!("{e:?}")}),
        };
        j["ns"] = json!(ns);
        out.push_str(&j.to_string());
        out.push('\n');
    }
    print!("{out}");
}

fn run_replay(args: &[String], live: &Path) {
    let Some(file) = positional(args).first().copied() else {
        eprintln!("usage: coach-core replay [--plan ID] [--tick 0.5] SAMPLES.json");
        std::process::exit(2);
    };
    let text = std::fs::read_to_string(file).unwrap_or_else(|e| panic!("{file}: {e}"));
    let v: Value = serde_json::from_str(&text).unwrap_or_else(|e| panic!("{file}: {e}"));
    let (fixture_plan, samples) = parse_samples(&v).unwrap_or_else(|e| panic!("{file}: {e}"));
    let id = flag(args, "--plan").map(String::from).or(fixture_plan).unwrap_or_else(|| {
        eprintln!("no plan in {file}; pass --plan ID");
        std::process::exit(2);
    });
    let plans = load_plans(live).unwrap_or_else(|e| panic!("{e}"));
    let plan = plans.get(&id).unwrap_or_else(|| panic!("unknown plan {id:?}"));
    let step = flag(args, "--tick").and_then(|s| s.parse::<f64>().ok());
    let r = replay(plan, &samples, step);
    for (t, e, lat, sh) in &r.events {
        let mut j = e.to_json();
        j["t"] = json!(t);
        j["lateness"] = json!(round1(*lat));
        j["shift"] = json!(sh);
        println!("{j}");
    }
    let count = |k| r.events.iter().filter(|x| x.1.kind == k).count();
    let s = r.engine.state_json();
    println!(
        "{}",
        json!({"summary": {
            "plan": id, "samples": samples.len(), "ticks": r.trace.len(),
            "targets": count(coach_core::Kind::Target), "now": count(coach_core::Kind::Now),
            "skipped": s["skipped"], "pending_at_end": s["pending"],
            "final_lateness": round1(r.engine.lateness()), "final_shift": r.engine.shift(),
            "max_lateness": round1(r.max_lateness()), "max_shift": r.max_shift(),
        }})
    );
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str).unwrap_or("") {
        "serve" => {
            let live = find_live(&args);
            serve(&live, flag(&args, "--port").and_then(|p| p.parse().ok()).unwrap_or(8767))
        }
        "replay" => run_replay(&args, &find_live(&args)),
        "rules-replay" => run_rules_replay(&args, &find_live(&args)),
        "keys" => {
            // every step's cue key, as the page computes it: {plan: [[action, key, cued], ...]}
            let all = load_plans(&find_live(&args)).unwrap_or_else(|e| panic!("{e}"));
            let m: serde_json::Map<String, Value> = all
                .iter()
                .map(|(id, p)| (id.clone(), p.steps.iter().map(|s| json!([s.action, s.key, s.cued()])).collect()))
                .collect();
            println!("{}", Value::Object(m));
        }
        "plans" => {
            for (id, p) in load_plans(&find_live(&args)).unwrap_or_else(|e| panic!("{e}")) {
                println!("{id}: {} steps, {} cue groups, {} supply steps", p.steps.len(), p.groups().len(), p.curve().len());
            }
        }
        _ => eprintln!(
            "usage: coach-core serve [--port 8767] | replay [--plan ID] [--tick 0.5] SAMPLES.json | rules-replay REQUESTS.jsonl | plans | keys  [--live DIR]"
        ),
    }
}
