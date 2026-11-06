//! Replays recorded games from live/coach.db (exported to tests/fixtures by scripts/export_fixtures.py)
//! against the real plans in live/, and checks the outcomes are sane. Each game runs twice: at the recorded
//! ~5 s cadence and upsampled to the live helper's 0.5 s tick.

use coach_core::{
    load_plans, parse_samples, replay, Kind, Plan, Replay, Sample, TraceTick, EXPIRE_AFTER, LATE_AFTER, LEAD,
    MAX_SHIFT, SHIFT_FROM, STALE,
};
use std::collections::{BTreeMap, HashSet};
use std::path::PathBuf;

const GAMES: [u32; 10] = [3, 5, 6, 9, 10, 11, 12, 13, 14, 15];

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn plans() -> BTreeMap<String, Plan> {
    load_plans(&root().join("../../live")).expect("live/ plans")
}

fn fixture(g: u32) -> (String, Vec<Sample>) {
    let p = root().join(format!("tests/fixtures/game_{g}.json"));
    let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
    let (plan, samples) = parse_samples(&v).unwrap();
    (plan.expect("fixture has a plan"), samples)
}

/// Last known supply at or before trace index i.
fn known_at(trace: &[TraceTick], i: usize) -> Option<i64> {
    trace[..=i].iter().rev().find_map(|x| x.used)
}

fn check(g: u32, plan: &Plan, samples: &[Sample], r: &Replay, label: &str) {
    let tr = &r.trace;
    let t0 = samples[0].t;
    assert_eq!(r.engine.rewinds(), 0, "game {g} {label}: recorded clocks are monotonic");

    // bounded lateness and shift
    for x in tr {
        assert!(x.lateness.is_finite() && x.lateness >= 0.0, "game {g} {label}: lateness {}", x.lateness);
        assert!((0..=MAX_SHIFT).contains(&x.shift));
    }
    // lateness is frozen after 3:00 once the plan has no supply steps ahead
    let last_supply = plan.curve().last().map(|c| c.0 as f64).unwrap_or(0.0);
    let freeze = (SHIFT_FROM as f64).max(last_supply);
    let after: Vec<f64> = tr.iter().filter(|x| x.t >= freeze).map(|x| x.lateness).collect();
    assert!(after.windows(2).all(|w| w[0] == w[1]), "game {g} {label}: lateness moved after {freeze}");

    // targets: each group at most once, inside its window, never for a group already past at the join
    // (window bound: [sec - 5, sec + max shift + 3], max shift 0 before 3:00)
    let mut seen = HashSet::new();
    for (t, e, _, _) in r.events.iter() {
        if e.kind != Kind::Target {
            continue;
        }
        assert!(seen.insert(e.sec), "game {g} {label}: group {} fired twice", e.sec);
        let sec = e.sec as f64;
        let max_shift = if e.sec < SHIFT_FROM { 0.0 } else { MAX_SHIFT as f64 };
        assert!(*t >= sec - LEAD && *t <= sec + max_shift + STALE, "game {g} {label}: group {} at {t}", e.sec);
        assert!(sec + STALE >= t0, "game {g} {label}: old group {} fired after joining at {t0}", e.sec);
    }
    // every group is either fired or (late join / gap) skipped by the end; none left behind that was due
    let end = tr.last().unwrap().t;
    for (gr, st) in r.engine.groups().iter().zip(r.engine.group_states()) {
        if (gr.sec as f64) + (MAX_SHIFT as f64) - LEAD <= end {
            assert_ne!(*st, coach_core::GroupState::Waiting, "game {g} {label}: group {} never decided", gr.sec);
        }
    }

    // now cues: only for steps that were genuinely late, fired at the first read that reached them, once
    let steps: Vec<(i64, i64, String)> = plan
        .steps
        .iter()
        .filter(|s| s.cued() && s.sec < SHIFT_FROM)
        .filter_map(|s| s.supply.map(|u| (s.sec, u, s.key.clone())))
        .collect();
    let mut fired_now: Vec<(i64, String)> = Vec::new();
    for (t, e, _, _) in r.events.iter().filter(|x| x.1.kind == Kind::Now) {
        for k in &e.keys {
            // the step this key belongs to: the earliest not-yet-claimed late step with this key whose window holds t
            let (sec, supply, _) = steps
                .iter()
                .find(|(s, _, key)| {
                    key == k
                        && !fired_now.contains(&(*s, key.clone()))
                        && (*s as f64) + LATE_AFTER <= *t
                        && *t < (*s as f64) + EXPIRE_AFTER
                })
                .unwrap_or_else(|| panic!("game {g} {label}: now cue {k:?} at {t} matches no step"));
            fired_now.push((*sec, k.clone()));
            let judge = tr.iter().position(|x| x.t >= *sec as f64 + LATE_AFTER).unwrap();
            let at_judge = known_at(tr, judge).expect("a supply read before judging");
            assert!(at_judge < *supply, "game {g} {label}: {k:?} (sec {sec}) was on time ({at_judge} >= {supply})");
            let fire = tr.iter().position(|x| x.t == *t).unwrap();
            assert!(tr[fire].used.unwrap() >= *supply);
            assert!(
                tr[judge..fire].iter().all(|x| x.used.is_none_or(|u| u < *supply)),
                "game {g} {label}: {k:?} reached earlier than its now cue"
            );
        }
    }
    // ... and every genuinely late step that was reached in time got one
    for (sec, supply, key) in &steps {
        let Some(judge) = tr.iter().position(|x| x.t >= *sec as f64 + LATE_AFTER) else { continue };
        if tr[judge].t >= *sec as f64 + EXPIRE_AFTER {
            continue;
        }
        let Some(at_judge) = known_at(tr, judge) else { continue };
        if at_judge >= *supply {
            assert!(!fired_now.contains(&(*sec, key.clone())), "game {g} {label}: on-time {key:?} got a now cue");
            continue;
        }
        let reached = tr[judge..].iter().find(|x| x.used.is_some_and(|u| u >= *supply)).map(|x| x.t);
        let expect = reached.is_some_and(|t| t < *sec as f64 + EXPIRE_AFTER);
        assert_eq!(
            fired_now.contains(&(*sec, key.clone())),
            expect,
            "game {g} {label}: late step {key:?} (sec {sec}, supply {supply}) reached at {reached:?}"
        );
    }
}

fn summary(g: u32, r: &Replay) -> String {
    let n = |k| r.events.iter().filter(|x| x.1.kind == k).count();
    format!(
        "game {g:2}: {:2} targets, {} now cues, skipped {}, final lateness {:.1} s, final shift {} s, max shift {}",
        n(Kind::Target),
        n(Kind::Now),
        r.engine.state_json()["skipped"],
        r.engine.lateness(),
        r.engine.shift(),
        r.max_shift()
    )
}

#[test]
fn replay_recorded_games() {
    let plans = plans();
    for g in GAMES {
        let (id, samples) = fixture(g);
        let plan = &plans[&id];
        for (label, step) in [("5s", None), ("0.5s", Some(0.5))] {
            let r = replay(plan, &samples, step);
            check(g, plan, &samples, &r, label);
            println!("[{label:4}] plan {id} {}", summary(g, &r));
        }
    }
}

#[test]
fn late_joins_have_no_burst() {
    // games 3, 5, 6 and 9 were recorded from 56 s, 72 s, 17 s and 57 s in
    let plans = plans();
    for g in [3, 5, 6, 9] {
        let (id, samples) = fixture(g);
        let r = replay(&plans[&id], &samples, Some(0.5));
        let t0 = samples[0].t;
        let first: Vec<_> = r.events.iter().filter(|x| x.0 == t0).collect();
        assert!(first.len() <= 1, "game {g}: {} events on the join tick: {first:?}", first.len());
        if t0 > 20.0 {
            assert!(!r.engine.state_json()["skipped"].as_array().unwrap().is_empty(), "game {g}: nothing skipped");
        }
    }
}

#[test]
fn second_game_after_rewind_matches_a_fresh_run() {
    // two recorded games back to back through one engine: the clock rewind resets everything
    let plans = plans();
    let (id, a) = fixture(15);
    let (_, b) = fixture(13);
    let plan = &plans[&id];
    let both: Vec<Sample> = a.iter().chain(b.iter()).copied().collect();
    let joined = replay(plan, &both, None);
    assert_eq!(joined.engine.rewinds(), 1);
    let fresh = replay(plan, &b, None);
    let after: Vec<_> =
        joined.events[joined.events.len() - fresh.events.len()..].iter().map(|x| (x.0, x.1.clone())).collect();
    let expect: Vec<_> = fresh.events.iter().map(|x| (x.0, x.1.clone())).collect();
    assert_eq!(after, expect);
    assert_eq!(joined.engine.lateness(), fresh.engine.lateness());
}
