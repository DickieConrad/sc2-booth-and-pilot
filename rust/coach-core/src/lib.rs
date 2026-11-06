//! coach-core: decides which spoken step cues fire, from the game clock and the player's used supply.
//!
//! The owner's "hybrid" rules:
//! 1. Target cue: each group of steps sharing a `sec` fires once when `t >= sec - 5 + shift(sec)`.
//!    A group first seen more than 3 s after its time (`sec + shift`) is skipped, never fired late
//!    (late join, clock jump, game already in progress).
//! 2. Catch-up "now" cue (steps with `sec < 180` and a supply): if `used < supply` at `sec + 3` the step is
//!    pending; it fires once when `used >= supply`. Pendings resolving on the same tick form one event.
//!    Null reads never decide anything by themselves (the last known supply is used); pendings expire at `sec + 60`.
//! 3. Lateness: from `t >= 60`, `t - (plan time the supply curve first reached used)`, clamped >= 0, EMA alpha 0.3.
//!    Updated only while `t < 180` or the plan still has supply steps ahead. `shift(sec >= 180) = clamp(round(lateness/2), 0, 15)`.
//! 4. Reset on start / plan change / clock going back more than 5 s.
//!
//! The call rules (helper: blocked / bank / behind; booth brain) live in `rules`; the HTTP routes in `service`.

pub mod rules;
pub mod service;

use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

/// Target cues fire this many seconds before the step's (shifted) time.
pub const LEAD: f64 = 5.0;
/// A target group first seen more than this many seconds after its time is skipped.
pub const STALE: f64 = 3.0;
/// A catch-up step is judged (on time / late) this many seconds after its time.
pub const LATE_AFTER: f64 = 3.0;
/// Pending catch-up steps expire this many seconds after their time.
pub const EXPIRE_AFTER: f64 = 60.0;
/// Steps at or after this second are shifted by lateness; steps before it get catch-up cues.
pub const SHIFT_FROM: i64 = 180;
/// Lateness is measured from this game second.
pub const LATENESS_FROM: f64 = 60.0;
/// EMA weight of each new lateness measurement.
pub const ALPHA: f64 = 0.3;
pub const MAX_SHIFT: i64 = 15;
/// A clock going back further than this means a new game.
pub const REWIND: f64 = 5.0;
/// Plan files under live/, loaded in this order (a later file wins on a duplicate plan id).
pub const PLAN_FILES: [&str; 3] = ["plans.json", "plans_custom.json", "plans_1v1.json"];
/// The announced sparring bot's build as plan `BOT`, written by the timing helper at the start of a bot game
/// (`bot/run.py --coach-test`). Generated, not in git: loaded last, and a bad file is skipped, never fatal.
pub const BOT_PLAN_FILE: &str = "out/plans_bot.json";

// ---------- cue text (must match the page's cueText exactly) ----------

/// JavaScript's `\s` (WhiteSpace + LineTerminator).
fn js_space(c: char) -> bool {
    matches!(
        c,
        '\t' | '\n' | '\u{b}' | '\u{c}' | '\r' | ' ' | '\u{a0}' | '\u{1680}' | '\u{2000}'
            ..='\u{200a}' | '\u{2028}' | '\u{2029}' | '\u{202f}' | '\u{205f}' | '\u{3000}' | '\u{feff}'
    )
}

/// Characters JavaScript's `.` does not match.
fn js_line_term(c: char) -> bool {
    matches!(c, '\n' | '\r' | '\u{2028}' | '\u{2029}')
}

fn js_word(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// Port of the page's
/// `a.replace(/\s*\(.*?\)/g, "").replace(/\bx(\d)/g, "$1").replace(/#/g, "").trim()`.
pub fn cue_text(action: &str) -> String {
    // 1. /\s*\(.*?\)/g: leftmost match = whitespace run + "(" + shortest run up to ")" on the same line.
    let c: Vec<char> = action.chars().collect();
    let mut a = Vec::with_capacity(c.len());
    let mut i = 0;
    while i < c.len() {
        let mut j = i;
        while j < c.len() && js_space(c[j]) {
            j += 1;
        }
        if j < c.len() && c[j] == '(' {
            if let Some(k) = (j + 1..c.len()).take_while(|&k| !js_line_term(c[k])).find(|&k| c[k] == ')') {
                i = k + 1;
                continue;
            }
        }
        a.push(c[i]);
        i += 1;
    }
    // 2. /\bx(\d)/g -> "$1" (\b: previous char is not an ASCII word char; \d: ASCII digit)
    let mut b = Vec::with_capacity(a.len());
    let mut i = 0;
    while i < a.len() {
        if a[i] == 'x' && i + 1 < a.len() && a[i + 1].is_ascii_digit() && (i == 0 || !js_word(a[i - 1])) {
            b.push(a[i + 1]);
            i += 2;
        } else {
            b.push(a[i]);
            i += 1;
        }
    }
    // 3. drop '#', 4. trim
    let s: String = b.into_iter().filter(|&ch| ch != '#').collect();
    s.trim_matches(js_space).to_string()
}

// ---------- plans ----------

#[derive(Clone, Debug, PartialEq)]
pub struct Step {
    pub sec: i64,
    pub supply: Option<i64>,
    pub action: String,
    /// cue_text(action)
    pub key: String,
}

impl Step {
    pub fn new(sec: i64, supply: Option<i64>, action: &str) -> Step {
        Step { sec, supply, action: action.to_string(), key: cue_text(action) }
    }

    /// "Check..." steps are checkpoints, not actions: never cued.
    pub fn cued(&self) -> bool {
        !self.action.starts_with("Check") && !self.key.is_empty()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Group {
    pub sec: i64,
    pub keys: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct Plan {
    pub id: String,
    /// Sorted by sec (stable, so plan order is kept within a sec).
    pub steps: Vec<Step>,
}

impl Plan {
    pub fn new(id: &str, mut steps: Vec<Step>) -> Plan {
        steps.sort_by_key(|s| s.sec);
        Plan { id: id.to_string(), steps }
    }

    pub fn from_json(id: &str, v: &Value) -> Result<Plan, String> {
        let arr = v.get("steps").and_then(Value::as_array).ok_or_else(|| format!("plan {id}: no steps array"))?;
        let mut steps = Vec::with_capacity(arr.len());
        for s in arr {
            let sec = s.get("sec").and_then(Value::as_f64).ok_or_else(|| format!("plan {id}: step without sec"))?;
            let supply = s.get("supply").and_then(Value::as_f64).map(|x| x.round() as i64);
            let action = s.get("action").and_then(Value::as_str).unwrap_or("");
            steps.push(Step::new(sec.round() as i64, supply, action));
        }
        Ok(Plan::new(id, steps))
    }

    /// Cued steps grouped by sec, keys in plan order.
    pub fn groups(&self) -> Vec<Group> {
        let mut out: Vec<Group> = Vec::new();
        for s in self.steps.iter().filter(|s| s.cued()) {
            match out.last_mut() {
                Some(g) if g.sec == s.sec => g.keys.push(s.key.clone()),
                _ => out.push(Group { sec: s.sec, keys: vec![s.key.clone()] }),
            }
        }
        out
    }

    /// The plan's supply curve: (sec, supply) of every step with a supply, in sec order.
    pub fn curve(&self) -> Vec<(i64, i64)> {
        self.steps.iter().filter_map(|s| s.supply.map(|u| (s.sec, u))).collect()
    }
}

/// Loads live/plans.json, plans_custom.json and plans_1v1.json (missing files are skipped), then the bot plan
/// (`BOT_PLAN_FILE`) if there is a good one.
pub fn load_plans(live: &Path) -> Result<BTreeMap<String, Plan>, String> {
    let mut out = BTreeMap::new();
    for f in PLAN_FILES {
        out.extend(load_plan_file(&live.join(f))?);
    }
    if out.is_empty() {
        return Err(format!("no plans found in {}", live.display()));
    }
    match load_plan_file(&live.join(BOT_PLAN_FILE)) {
        Ok(bot) => out.extend(bot),
        Err(e) => eprintln!("coach-core: skipping the bot plan: {e}"),
    }
    Ok(out)
}

/// One `{"plans": {id: {steps: [...]}}}` file; a missing file is an empty map.
fn load_plan_file(p: &Path) -> Result<BTreeMap<String, Plan>, String> {
    let text = match std::fs::read_to_string(p) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(e) => return Err(format!("{}: {e}", p.display())),
    };
    let v: Value = serde_json::from_str(&text).map_err(|e| format!("{}: {e}", p.display()))?;
    let plans = v.get("plans").and_then(Value::as_object).ok_or_else(|| format!("{}: no \"plans\"", p.display()))?;
    plans.iter().map(|(id, pv)| Ok((id.clone(), Plan::from_json(id, pv)?))).collect()
}

// ---------- engine ----------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Target,
    Now,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Event {
    pub kind: Kind,
    pub keys: Vec<String>,
    /// The group's sec (for a now event: the earliest sec among the resolved steps).
    pub sec: i64,
}

impl Event {
    pub fn to_json(&self) -> Value {
        let kind = match self.kind {
            Kind::Target => "target",
            Kind::Now => "now",
        };
        json!({"type": kind, "keys": self.keys, "sec": self.sec})
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GroupState {
    Waiting,
    Fired,
    Skipped,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Catch {
    /// Not judged yet (before sec + 3, or no supply read so far).
    Waiting,
    /// Was below the step's supply at sec + 3; a now cue fires when supply reaches it.
    Pending,
    OnTime,
    Fired,
    Expired,
}

#[derive(Clone, Debug)]
pub struct Watch {
    pub sec: i64,
    pub supply: i64,
    pub key: String,
    pub state: Catch,
}

pub struct Engine {
    plan: Plan,
    groups: Vec<Group>,
    gstate: Vec<GroupState>,
    watch: Vec<Watch>,
    curve: Vec<(i64, i64)>,
    max_t: Option<f64>,
    last_t: Option<f64>,
    last_used: Option<i64>,
    lateness: f64,
    log: Vec<(f64, Event)>,
    resets: u32,
}

impl Engine {
    pub fn new(plan: Plan) -> Engine {
        let groups = plan.groups();
        let curve = plan.curve();
        let mut e = Engine {
            plan,
            gstate: vec![GroupState::Waiting; groups.len()],
            groups,
            watch: Vec::new(),
            curve,
            max_t: None,
            last_t: None,
            last_used: None,
            lateness: 0.0,
            log: Vec::new(),
            resets: 0,
        };
        e.reset();
        e
    }

    /// Forget the game: nothing fired, nothing pending, lateness 0. Keeps the plan.
    pub fn reset(&mut self) {
        self.gstate = vec![GroupState::Waiting; self.groups.len()];
        self.watch = self
            .plan
            .steps
            .iter()
            .filter(|s| s.cued() && s.sec < SHIFT_FROM)
            .filter_map(|s| {
                s.supply.map(|u| Watch { sec: s.sec, supply: u, key: s.key.clone(), state: Catch::Waiting })
            })
            .collect();
        self.max_t = None;
        self.last_t = None;
        self.last_used = None;
        self.lateness = 0.0;
        self.log.clear();
    }

    pub fn plan(&self) -> &Plan {
        &self.plan
    }
    pub fn groups(&self) -> &[Group] {
        &self.groups
    }
    pub fn group_states(&self) -> &[GroupState] {
        &self.gstate
    }
    pub fn watches(&self) -> &[Watch] {
        &self.watch
    }
    pub fn lateness(&self) -> f64 {
        self.lateness
    }
    /// Events fired since the last reset, with the tick time that fired them.
    pub fn log(&self) -> &[(f64, Event)] {
        &self.log
    }
    /// Resets caused by the clock going backwards.
    pub fn rewinds(&self) -> u32 {
        self.resets
    }

    /// Current shift for steps at or after 3:00.
    pub fn shift(&self) -> i64 {
        ((self.lateness / 2.0).round() as i64).clamp(0, MAX_SHIFT)
    }

    pub fn shift_for(&self, sec: i64) -> i64 {
        if sec < SHIFT_FROM {
            0
        } else {
            self.shift()
        }
    }

    /// One helper tick: game clock `t` (s) and used supply (None = unreadable). Returns the cues to play now.
    pub fn tick(&mut self, t: f64, used: Option<i64>) -> Vec<Event> {
        if matches!(self.max_t, Some(m) if t < m - REWIND) {
            self.reset();
            self.resets += 1;
        }
        self.max_t = Some(self.max_t.map_or(t, |m| m.max(t)));
        self.last_t = Some(t);
        if used.is_some() {
            self.last_used = used;
        }
        self.update_lateness(t, used);
        let mut out = self.catch_up(t, used);
        out.extend(self.targets(t));
        self.log.extend(out.iter().map(|e| (t, e.clone())));
        out
    }

    fn update_lateness(&mut self, t: f64, used: Option<i64>) {
        let Some(u) = used else { return };
        if t < LATENESS_FROM {
            return;
        }
        let ahead = self.curve.iter().any(|&(s, _)| s as f64 > t);
        if !(t < SHIFT_FROM as f64 || ahead) {
            return; // frozen: past 3:00 and the plan has no supply targets left
        }
        // step function: the curve first reaches `u` at the first step whose supply is >= u
        let Some(&(reached, _)) = self.curve.iter().find(|&&(_, sup)| sup >= u) else {
            return; // above everything the plan has
        };
        let raw = (t - reached as f64).max(0.0);
        self.lateness = ALPHA * raw + (1.0 - ALPHA) * self.lateness;
    }

    fn catch_up(&mut self, t: f64, used: Option<i64>) -> Vec<Event> {
        let known = self.last_used;
        let (mut keys, mut first) = (Vec::new(), None);
        for w in &mut self.watch {
            let sec = w.sec as f64;
            match w.state {
                Catch::Waiting if t >= sec + LATE_AFTER => {
                    if t >= sec + EXPIRE_AFTER {
                        w.state = Catch::Expired;
                    } else if let Some(u) = known {
                        w.state = if u >= w.supply { Catch::OnTime } else { Catch::Pending };
                    }
                }
                Catch::Pending => {
                    if t >= sec + EXPIRE_AFTER {
                        w.state = Catch::Expired;
                    } else if matches!(used, Some(u) if u >= w.supply) {
                        w.state = Catch::Fired;
                        keys.push(w.key.clone());
                        first.get_or_insert(w.sec);
                    }
                }
                _ => {}
            }
        }
        match first {
            Some(sec) => vec![Event { kind: Kind::Now, keys, sec }],
            None => vec![],
        }
    }

    fn targets(&mut self, t: f64) -> Vec<Event> {
        let mut out = Vec::new();
        for i in 0..self.groups.len() {
            if self.gstate[i] != GroupState::Waiting {
                continue;
            }
            let g = &self.groups[i];
            let due = (g.sec + self.shift_for(g.sec)) as f64;
            if t < due - LEAD {
                continue;
            }
            if t > due + STALE {
                self.gstate[i] = GroupState::Skipped; // already in the past when first seen
            } else {
                self.gstate[i] = GroupState::Fired;
                out.push(Event { kind: Kind::Target, keys: g.keys.clone(), sec: g.sec });
            }
        }
        out
    }

    /// The /tick response body.
    pub fn tick_json(&self, events: &[Event]) -> Value {
        json!({
            "events": events.iter().map(Event::to_json).collect::<Vec<_>>(),
            "lateness": round1(self.lateness),
            "shift": self.shift(),
        })
    }

    /// The /state body.
    pub fn state_json(&self) -> Value {
        let logged = |k: Kind| -> Vec<Value> {
            self.log
                .iter()
                .filter(|(_, e)| e.kind == k)
                .map(|(t, e)| json!({"t": t, "sec": e.sec, "keys": e.keys}))
                .collect()
        };
        let skipped: Vec<i64> = self
            .groups
            .iter()
            .zip(&self.gstate)
            .filter(|(_, s)| **s == GroupState::Skipped)
            .map(|(g, _)| g.sec)
            .collect();
        let pending: Vec<&str> =
            self.watch.iter().filter(|w| w.state == Catch::Pending).map(|w| w.key.as_str()).collect();
        json!({
            "plan": self.plan.id,
            "t": self.last_t,
            "used": self.last_used,
            "lateness": round1(self.lateness),
            "shift": self.shift(),
            "pending": pending,
            "fired": logged(Kind::Target),
            "now_fired": logged(Kind::Now),
            "skipped": skipped,
            "groups": self.groups.len(),
        })
    }
}

pub fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

// ---------- replay ----------

#[derive(Clone, Copy, Debug)]
pub struct Sample {
    pub t: f64,
    pub used: Option<i64>,
}

/// A fixture `{"plan": "T", "samples": [{"t", "used"}, ...]}` or a bare array of samples. -> (plan, samples)
pub fn parse_samples(v: &Value) -> Result<(Option<String>, Vec<Sample>), String> {
    let (plan, arr) = match v {
        Value::Array(a) => (None, a),
        Value::Object(o) => (
            o.get("plan").and_then(Value::as_str).map(String::from),
            o.get("samples").and_then(Value::as_array).ok_or("no \"samples\" array")?,
        ),
        _ => return Err("expected an object or an array".into()),
    };
    let mut out = Vec::with_capacity(arr.len());
    for s in arr {
        let t = s.get("t").and_then(Value::as_f64).ok_or("sample without t")?;
        out.push(Sample { t, used: s.get("used").and_then(Value::as_f64).map(|u| u.round() as i64) });
    }
    Ok((plan, out))
}

/// When upsampling, a recorded read is held this long; later ticks in a gap carry no read (null).
pub const REPLAY_HOLD: f64 = 6.0;

#[derive(Clone, Copy, Debug)]
pub struct TraceTick {
    pub t: f64,
    pub used: Option<i64>,
    /// Lateness and shift after this tick.
    pub lateness: f64,
    pub shift: i64,
}

pub struct Replay {
    pub engine: Engine,
    /// (tick time, event, lateness after the tick, shift after the tick)
    pub events: Vec<(f64, Event, f64, i64)>,
    /// Every tick fed to the engine.
    pub trace: Vec<TraceTick>,
}

impl Replay {
    pub fn max_shift(&self) -> i64 {
        self.trace.iter().map(|x| x.shift).max().unwrap_or(0)
    }
    pub fn max_lateness(&self) -> f64 {
        self.trace.iter().map(|x| x.lateness).fold(0.0, f64::max)
    }
}

/// Feeds recorded samples through a fresh engine. With `step`, also ticks every `step` seconds between samples
/// (the live helper ticks every 0.5 s; coach.db keeps one sample per ~5 s), holding each read for up to
/// REPLAY_HOLD seconds and sending null after that.
pub fn replay(plan: &Plan, samples: &[Sample], step: Option<f64>) -> Replay {
    let mut r = Replay { engine: Engine::new(plan.clone()), events: Vec::new(), trace: Vec::new() };
    let run = |r: &mut Replay, t: f64, used: Option<i64>| {
        let evs = r.engine.tick(t, used);
        let (lateness, shift) = (r.engine.lateness(), r.engine.shift());
        r.trace.push(TraceTick { t, used, lateness, shift });
        r.events.extend(evs.into_iter().map(|e| (t, e, lateness, shift)));
    };
    for (i, s) in samples.iter().enumerate() {
        run(&mut r, s.t, s.used);
        if let (Some(dt), Some(next)) = (step.filter(|d| *d > 0.0), samples.get(i + 1)) {
            let mut k = 1.0;
            while s.t + k * dt < next.t - 1e-9 {
                let held = if k * dt <= REPLAY_HOLD { s.used } else { None };
                run(&mut r, s.t + k * dt, held);
                k += 1.0;
            }
        }
    }
    r
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan(steps: &[(i64, Option<i64>, &str)]) -> Plan {
        Plan::new("X", steps.iter().map(|&(s, u, a)| Step::new(s, u, a)).collect())
    }

    /// Ticks every 0.5 s over [from, to] with a supply function; returns (t, event) pairs.
    fn run(e: &mut Engine, from: f64, to: f64, used: impl Fn(f64) -> Option<i64>) -> Vec<(f64, Event)> {
        let mut out = Vec::new();
        let mut t = from;
        while t <= to + 1e-9 {
            out.extend(e.tick(t, used(t)).into_iter().map(|ev| (t, ev)));
            t += 0.5;
        }
        out
    }

    fn targets(evs: &[(f64, Event)]) -> Vec<(f64, i64)> {
        evs.iter().filter(|(_, e)| e.kind == Kind::Target).map(|(t, e)| (*t, e.sec)).collect()
    }
    fn nows(evs: &[(f64, Event)]) -> Vec<(f64, Event)> {
        evs.iter().filter(|(_, e)| e.kind == Kind::Now).cloned().collect()
    }

    #[test]
    fn bot_plan_file_is_loaded_last_and_never_fatal() {
        let dir = std::env::temp_dir().join(format!("coach-core-botplan-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("out")).unwrap();
        let a = r#"{"plans": {"A": {"steps": [{"sec": 18, "supply": 14, "action": "Supply Depot"}]}}}"#;
        std::fs::write(dir.join("plans.json"), a).unwrap();
        // no bot file: just the real plans
        assert_eq!(load_plans(&dir).unwrap().keys().collect::<Vec<_>>(), ["A"]);
        let bot = r#"{"plans": {"BOT": {"name": "SixRax", "steps": [
            {"sec": 30, "t": "0:30", "supply": 15, "action": "Barracks"},
            {"sec": 18, "t": "0:18", "supply": 14, "action": "Supply Depot"}]}}}"#;
        std::fs::write(dir.join(BOT_PLAN_FILE), bot).unwrap();
        let plans = load_plans(&dir).unwrap();
        let b = &plans["BOT"];
        assert_eq!(b.steps.iter().map(|s| (s.sec, s.supply)).collect::<Vec<_>>(), [(18, Some(14)), (30, Some(15))]);
        assert_eq!(b.groups()[1].keys, ["Barracks"]);
        // a half-written bot file is skipped; the real plans still load
        std::fs::write(dir.join(BOT_PLAN_FILE), "{\"plans\": {").unwrap();
        assert_eq!(load_plans(&dir).unwrap().keys().collect::<Vec<_>>(), ["A"]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn cue_text_matches_page() {
        assert_eq!(cue_text("Command Center (natural)"), "Command Center");
        assert_eq!(cue_text("Refinery #2"), "Refinery 2");
        assert_eq!(cue_text("Marines x2 continuous"), "Marines 2 continuous");
        assert_eq!(cue_text("Widow Mine x2 (or 1 Siege Tank vs Zerg)"), "Widow Mine 2");
        assert_eq!(cue_text("Stimpack (Rax 2) + Combat Shield (Rax 3)"), "Stimpack + Combat Shield");
        assert_eq!(cue_text("Barracks #4 and #5, Armory"), "Barracks 4 and 5, Armory");
        assert_eq!(cue_text("Barracks \u{d7}2"), "Barracks \u{d7}2"); // only an ASCII x is rewritten
        assert_eq!(cue_text("Box2 x3"), "Box2 3"); // \b: "ox2" has no boundary before x
        assert_eq!(cue_text("a (b (c) d)"), "a d)"); // lazy .*? stops at the first ")"
        assert_eq!(cue_text("Depot (unclosed"), "Depot (unclosed");
        assert_eq!(cue_text("  (all)  "), "");
        assert_eq!(cue_text("x2"), "2");
        // checked against node running the page's regex
        assert_eq!(cue_text("Tank\u{a0}(sieged)\u{3000}"), "Tank");
        assert_eq!(cue_text("_x2 -x3"), "_x2 -3");
        assert_eq!(cue_text("a (b\n) c (d)"), "a (b\n) c");
        assert_eq!(cue_text("\u{feff}x1#"), "1");
    }

    #[test]
    fn target_fires_five_seconds_early_once() {
        let mut e = Engine::new(plan(&[(100, None, "Factory"), (200, None, "Armory")]));
        let evs = run(&mut e, 0.0, 300.0, |_| None);
        assert_eq!(targets(&evs), vec![(95.0, 100), (195.0, 200)]);
    }

    #[test]
    fn grouping_and_check_steps() {
        let mut e = Engine::new(plan(&[
            (40, Some(16), "Barracks"),
            (40, None, "Check: 16 SCVs"),
            (40, Some(16), "Refinery #1"),
            (60, None, "Check: only a checkpoint"),
        ]));
        assert_eq!(e.groups().len(), 1);
        let evs = run(&mut e, 0.0, 100.0, |_| Some(20));
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].1, Event { kind: Kind::Target, keys: vec!["Barracks".into(), "Refinery 1".into()], sec: 40 });
    }

    #[test]
    fn late_join_no_burst() {
        let steps: Vec<(i64, Option<i64>, &str)> = (0..15).map(|i| (i * 10, None, "Step")).collect();
        let mut e = Engine::new(plan(&steps));
        // join at 2:24: 140 is 4 s past (skipped), nothing older fires either
        let evs = run(&mut e, 144.0, 160.0, |_| None);
        assert!(targets(&evs).is_empty(), "{evs:?}");
        // join at 2:22: 140 is only 2 s past, so it still plays
        let mut e = Engine::new(plan(&steps));
        let evs = run(&mut e, 142.0, 160.0, |_| None);
        assert_eq!(targets(&evs), vec![(142.0, 140)]);
    }

    #[test]
    fn forward_clock_jump_skips_the_gap() {
        let steps: Vec<(i64, Option<i64>, &str)> = (1..20).map(|i| (i * 10, None, "Step")).collect();
        let mut e = Engine::new(plan(&steps));
        let mut evs = run(&mut e, 0.0, 20.0, |_| None);
        evs.extend(run(&mut e, 120.0, 130.0, |_| None));
        assert_eq!(targets(&evs).iter().map(|x| x.1).collect::<Vec<_>>(), vec![10, 20, 120, 130]);
    }

    #[test]
    fn now_cue_fires_once_when_late_step_is_reached() {
        let mut e = Engine::new(plan(&[(40, Some(16), "Barracks")]));
        let evs = run(&mut e, 0.0, 120.0, |t| Some(if t < 50.0 { 15 } else { 16 }));
        let n = nows(&evs);
        assert_eq!(n.len(), 1);
        assert_eq!(n[0], (50.0, Event { kind: Kind::Now, keys: vec!["Barracks".into()], sec: 40 }));
    }

    #[test]
    fn on_time_step_gets_no_now_cue() {
        let mut e = Engine::new(plan(&[(40, Some(16), "Barracks")]));
        // reaches 16 exactly at sec + 3
        let evs = run(&mut e, 0.0, 120.0, |t| Some(if t < 43.0 { 15 } else { 16 }));
        assert!(nows(&evs).is_empty());
        assert_eq!(e.watches()[0].state, Catch::OnTime);
    }

    #[test]
    fn steps_from_3_00_or_without_supply_get_no_now_cue() {
        let mut e = Engine::new(plan(&[(170, None, "Factory"), (180, Some(30), "Starport")]));
        assert!(e.watches().is_empty());
        let evs = run(&mut e, 0.0, 300.0, |t| Some(if t < 200.0 { 10 } else { 40 }));
        assert!(nows(&evs).is_empty());
    }

    #[test]
    fn null_supply_uses_last_known_and_never_decides_alone() {
        // last known 15 judges the step late even though the read at sec + 3 failed
        let mut e = Engine::new(plan(&[(40, Some(16), "Barracks")]));
        let mut evs = run(&mut e, 0.0, 42.0, |_| Some(15));
        evs.extend(run(&mut e, 42.5, 50.0, |_| None));
        assert_eq!(e.watches()[0].state, Catch::Pending);
        evs.extend(run(&mut e, 50.5, 55.0, |_| Some(16)));
        let n = nows(&evs);
        assert_eq!(n.len(), 1);
        assert_eq!(n[0].0, 50.5);

        // no read at all: nothing becomes pending; the first read decides (here: on time)
        let mut e = Engine::new(plan(&[(40, Some(16), "Barracks")]));
        let mut evs = run(&mut e, 0.0, 50.0, |_| None);
        assert_eq!(e.watches()[0].state, Catch::Waiting);
        evs.extend(run(&mut e, 50.5, 60.0, |_| Some(17)));
        assert!(nows(&evs).is_empty());
        assert_eq!(e.watches()[0].state, Catch::OnTime);
    }

    #[test]
    fn pending_expires_sixty_seconds_after_sec() {
        let mut e = Engine::new(plan(&[(40, Some(16), "Barracks")]));
        let mut evs = run(&mut e, 0.0, 99.5, |_| Some(15));
        assert_eq!(e.watches()[0].state, Catch::Pending);
        evs.extend(run(&mut e, 100.0, 120.0, |_| Some(16)));
        assert!(nows(&evs).is_empty());
        assert_eq!(e.watches()[0].state, Catch::Expired);
        // a pending that only sees null reads also expires
        let mut e = Engine::new(plan(&[(40, Some(16), "Barracks")]));
        run(&mut e, 0.0, 43.0, |_| Some(15));
        run(&mut e, 43.5, 100.0, |_| None);
        assert_eq!(e.watches()[0].state, Catch::Expired);
    }

    #[test]
    fn now_cues_resolving_together_are_grouped() {
        let mut e =
            Engine::new(plan(&[(40, Some(16), "Barracks"), (45, Some(16), "Refinery"), (60, Some(19), "Orbital")]));
        let evs = run(&mut e, 0.0, 80.0, |t| Some(if t < 55.0 { 15 } else { 16 }));
        let n = nows(&evs);
        assert_eq!(n.len(), 1);
        assert_eq!(n[0].1, Event { kind: Kind::Now, keys: vec!["Barracks".into(), "Refinery".into()], sec: 40 });
    }

    #[test]
    fn lateness_ema() {
        // curve: 12 @ 0, 20 @ 100, 30 @ 300
        let mut e = Engine::new(plan(&[(0, Some(12), "SCVs"), (100, Some(20), "CC"), (300, Some(30), "Third")]));
        e.tick(59.5, Some(5));
        assert_eq!(e.lateness(), 0.0, "no lateness before 1:00");
        e.tick(60.0, Some(12)); // reached at 0 -> raw 60
        assert!((e.lateness() - 18.0).abs() < 1e-9);
        e.tick(61.0, Some(15)); // first >= 15 is 20 @ 100 -> raw max(0, -39) = 0
        assert!((e.lateness() - 12.6).abs() < 1e-9);
        e.tick(62.0, None); // unknown: no update
        assert!((e.lateness() - 12.6).abs() < 1e-9);
        e.tick(63.0, Some(31)); // above the whole curve: no update
        assert!((e.lateness() - 12.6).abs() < 1e-9);
        e.tick(200.0, Some(25)); // first >= 25 is 30 @ 300 -> raw 0; still updating (supply ahead)
        assert!((e.lateness() - 8.82).abs() < 1e-9);
        e.tick(400.0, Some(20)); // nothing ahead after 3:00 -> frozen
        assert!((e.lateness() - 8.82).abs() < 1e-9);
    }

    #[test]
    fn lateness_freezes_after_3_00_on_1v1_plans() {
        let mut e =
            Engine::new(plan(&[(0, Some(12), "SCVs"), (178, Some(26), "Starport"), (240, None, "Barracks x2")]));
        e.tick(179.5, Some(20)); // first >= 20 is 178 -> raw 1.5
        let l = e.lateness();
        assert!((l - 0.45).abs() < 1e-9);
        e.tick(185.0, Some(12)); // would be raw 185, but frozen
        assert_eq!(e.lateness(), l);
    }

    #[test]
    fn shift_rounds_half_lateness_and_clamps() {
        let mut e = Engine::new(plan(&[(0, Some(12), "SCVs"), (900, Some(100), "Max")]));
        assert_eq!(e.shift(), 0);
        e.lateness = 3.0;
        assert_eq!(e.shift(), 2); // round(1.5)
        e.lateness = 0.9;
        assert_eq!(e.shift(), 0);
        e.lateness = 100.0;
        assert_eq!(e.shift(), 15);
        assert_eq!(e.shift_for(179), 0);
        assert_eq!(e.shift_for(180), 15);
    }

    #[test]
    fn shift_delays_targets_from_3_00_only() {
        // the player is stuck at 12 supply: lateness climbs, later targets move back
        let mut e = Engine::new(plan(&[
            (0, Some(12), "SCVs"),
            (170, None, "Factory"),
            (240, None, "Barracks"),
            (600, Some(60), "Max"),
        ]));
        let evs = run(&mut e, 0.0, 300.0, |_| Some(12));
        let tg = targets(&evs);
        assert!(tg.contains(&(165.0, 170)));
        let (t240, _) = tg.iter().find(|x| x.1 == 240).unwrap();
        assert_eq!(e.shift(), 15);
        assert_eq!(*t240, 240.0 - 5.0 + 15.0);
    }

    #[test]
    fn rewind_resets_small_jitter_does_not() {
        let mut e = Engine::new(plan(&[(10, Some(13), "Depot"), (100, None, "Factory")]));
        let mut evs = run(&mut e, 0.0, 120.0, |_| Some(20));
        evs.extend(run(&mut e, 116.0, 120.0, |_| Some(20))); // back 4 s: same game, nothing refires
        assert_eq!(targets(&evs).len(), 2);
        assert_eq!(e.rewinds(), 0);
        let evs = run(&mut e, 0.0, 20.0, |_| Some(12)); // back 120 s: new game
        assert_eq!(e.rewinds(), 1);
        assert_eq!(targets(&evs), vec![(5.0, 10)]);
        assert!(e.state_json()["fired"].as_array().unwrap().len() == 1);
    }

    #[test]
    fn reset_clears_everything() {
        let mut e = Engine::new(plan(&[(0, Some(12), "SCVs"), (40, Some(16), "Barracks"), (600, Some(60), "Max")]));
        run(&mut e, 0.0, 90.0, |_| Some(12));
        assert!(e.lateness() > 0.0);
        assert_eq!(e.watches()[1].state, Catch::Pending);
        e.reset();
        assert_eq!(e.lateness(), 0.0);
        assert!(e.log().is_empty());
        assert!(e.watches().iter().all(|w| w.state == Catch::Waiting));
        assert!(e.group_states().iter().all(|s| *s == GroupState::Waiting));
    }

    #[test]
    fn tick_json_shape() {
        let mut e = Engine::new(plan(&[(0, Some(12), "SCVs nonstop")]));
        let evs = e.tick(0.0, Some(12));
        assert_eq!(
            e.tick_json(&evs),
            json!({"events": [{"type": "target", "keys": ["SCVs nonstop"], "sec": 0}], "lateness": 0.0, "shift": 0})
        );
    }
}
