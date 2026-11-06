//! Parity with the Python call rules on every recorded game: tests/fixtures/rules_parity.json holds each game's
//! ticks (from live/coach.db) and the calls the Python helper (timing.judge + CONFIRM + Pusher gaps) and brain
//! (brain_run.Runner) made on them, written by `tools/rules_parity.py --export`. The Rust hub must make exactly
//! the same calls (tick, kind, text, clip text). Also: `coach-core serve --port 0` answers POST /rules/tick.

use coach_core::load_plans;
use coach_core::rules::{RulesHub, TickRequest};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// Replays a fixture's games through one hub; asserts every game's calls equal Python's.
/// -> (games, helper calls, brain calls).
fn replay_fixture(name: &str) -> (usize, usize, usize) {
    let plans = load_plans(&root().join("../../live")).expect("live/ plans");
    let text = std::fs::read_to_string(root().join("tests/fixtures").join(name)).unwrap();
    let v: Value = serde_json::from_str(&text).unwrap();
    let games = v["games"].as_array().unwrap();
    let mut hub = RulesHub::new();
    let (mut n_helper, mut n_brain) = (0, 0);
    for g in games {
        let id = &g["id"];
        let mut helper: Vec<Value> = Vec::new();
        let mut brain: Vec<Value> = Vec::new();
        for (i, tick) in g["ticks"].as_array().unwrap().iter().enumerate() {
            let a = tick.as_array().unwrap();
            let sample = if a[1].is_null() {
                Value::Null
            } else {
                json!({"minerals": a[1], "gas": a[2], "used": a[3], "cap": a[4]})
            };
            let mut req = json!({"game": format!("g{id}"), "t": a[0], "sample": sample,
                                 "brain": {"matchup": g["matchup"], "external": a[5], "race": g["race"]}});
            if !g["plan"].is_null() {
                req["helper"] = json!({"plan": g["plan"], "race": g["race"], "wall": a[0]});
            }
            let r = hub.tick(&TickRequest::from_json(&req).unwrap(), &plans).unwrap();
            if let Some(c) = r.helper {
                helper.push(json!([i, a[0], c.kind.as_str(), c.text]));
            }
            if let Some(c) = r.brain {
                brain.push(json!([i, a[0], c.kind.as_str(), c.text, c.clip_text]));
            }
        }
        assert_eq!(&Value::Array(helper.clone()), &g["helper"], "game {id}: helper calls differ from Python");
        assert_eq!(&Value::Array(brain.clone()), &g["brain"], "game {id}: brain calls differ from Python");
        n_helper += helper.len();
        n_brain += brain.len();
    }
    println!("{name}: {} games, {n_helper} helper calls, {n_brain} brain calls, identical to Python", games.len());
    (games.len(), n_helper, n_brain)
}

#[test]
fn same_calls_as_python_on_every_recorded_game() {
    let (games, n_helper, n_brain) = replay_fixture("rules_parity.json");
    assert!(games >= 20, "fixture has {games} games");
    assert!(n_helper > 100 && n_brain > 100);
}

/// Made-up Protoss / Zerg / Terran games (`tools/rules_parity.py --synthetic --export`): race wording, Terran-only
/// kinds left out, the same as Python.
#[test]
fn same_calls_as_python_on_synthetic_race_games() {
    let (games, n_helper, n_brain) = replay_fixture("rules_parity_synth.json");
    assert!(games >= 4 && n_helper > 0 && n_brain >= 20, "{games} games, {n_helper} helper, {n_brain} brain calls");
}

/// Starts the real binary on a free port (never 8767), posts two ticks over one connection, stops it.
#[test]
fn serve_answers_rules_tick_on_a_free_port() {
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_coach-core"))
        .args(["serve", "--port", "0", "--live"])
        .arg(root().join("../../live"))
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let mut line = String::new();
    BufReader::new(child.stderr.take().unwrap()).read_line(&mut line).unwrap();
    let port: u16 = line.split("127.0.0.1:").nth(1).and_then(|s| s.split_whitespace().next()).unwrap().parse().unwrap();
    assert!(![8765, 8766, 8767].contains(&port), "{line}");
    let post = |body: Value| -> Value {
        let body = body.to_string();
        let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
        write!(
            s,
            "POST /rules/tick HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        let mut resp = String::new();
        s.read_to_string(&mut resp).unwrap();
        assert!(resp.starts_with("HTTP/1.1 200"), "{resp}");
        serde_json::from_str(resp.split("\r\n\r\n").nth(1).unwrap()).unwrap()
    };
    let tick = |t: f64| {
        json!({"game": "test", "t": t, "sample": {"minerals": 50, "gas": 0, "used": 23, "cap": 23},
               "helper": {"plan": "A", "race": "T", "wall": t}})
    };
    let first = post(tick(100.0));
    let second = post(tick(100.5));
    let _ = child.kill(); // the child this test started
    let _ = child.wait();
    assert_eq!(first["helper"], Value::Null);
    assert_eq!(second["helper"], json!({"kind": "blocked", "text": "Supply blocked 23/23: depot now"}));
}
