//! Call rules: the live helper's calls (supply blocked, bank, behind) and the booth brain's (depot, CC, stuck,
//! bank, gas, lost depot / CC, orbital, timed reminders), ported 1:1 from `live/timing.py` and `live/brain.py`.
//! Pure state machines, no I/O; `RulesHub` keeps one state per game for the `/rules/tick` route.
//!
//! The two callers stay separate, as in Python: the helper posts every OCR tick with `helper` (its plan, race and
//! wall clock) and gets the helper call; the brain runner posts every poll with `brain` (matchup + what others said)
//! and gets the brain call. Each uses its own game key; a new key, `new_game: true`, or the clock going back more
//! than 5 s starts a fresh game.

pub mod brain;
pub mod helper;

pub use brain::{describe, describe_as, BrainCall, BrainGame, CallKind, Facts, RulesBrain};
pub use helper::{judge, plan_status, HelperCall, HelperKind, HelperRules};

use crate::Plan;
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// One resource panel read (minerals, gas, used / cap supply).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Panel {
    pub minerals: i64,
    pub gas: i64,
    pub used: i64,
    pub cap: i64,
}

/// A panel read at game time t (the brain's history).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sample {
    pub t: f64,
    pub minerals: i64,
    pub gas: i64,
    pub used: i64,
    pub cap: i64,
}

/// The player's race, for wording ("pylon" / "Probes"). "P" / "Z"; anything else is Terran (brain.py `race_of`).
/// The helper words only Protoss differently (`race == "P"`): Zerg reads as Terran there, as in timing.py.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Race {
    #[default]
    Terran,
    Protoss,
    Zerg,
}

impl Race {
    pub const ALL: [Race; 3] = [Race::Terran, Race::Protoss, Race::Zerg];

    pub fn parse(s: &str) -> Race {
        match s {
            "P" => Race::Protoss,
            "Z" => Race::Zerg,
            _ => Race::Terran,
        }
    }
}

/// The helper's part of a tick.
#[derive(Clone, Debug, PartialEq)]
pub struct HelperInput {
    /// Plan id (A-F, M, T/P/Z, PX...): its steps set "behind" and the CC-saving bank bar.
    pub plan: String,
    pub race: Race,
    /// Clock for the say gaps (the helper sends unix seconds); None = the game clock.
    pub wall: Option<f64>,
}

/// The brain runner's part of a tick.
#[derive(Clone, Debug, PartialEq)]
pub struct BrainInput {
    /// Enemy key (TP, Z, ...); empty = "T" for the timed reminders.
    pub matchup: String,
    /// What others said since the last poll, in order: Some(kind) for a brain kind (a helper "bank"), else None.
    pub external: Vec<Option<CallKind>>,
    /// The followed player's race (wording, Terran-only kinds); absent (an older client) = Terran.
    pub race: Race,
}

/// POST /rules/tick body.
#[derive(Clone, Debug, PartialEq)]
pub struct TickRequest {
    pub game: String,
    pub new_game: bool,
    pub t: f64,
    pub sample: Option<Panel>,
    pub helper: Option<HelperInput>,
    pub brain: Option<BrainInput>,
}

fn int(v: &Value, k: &str) -> Result<i64, String> {
    v.get(k).and_then(Value::as_f64).map(|x| x.round() as i64).ok_or_else(|| format!("sample.{k} must be a number"))
}

impl TickRequest {
    /// Parses and validates a request body (strings become enums here; unknown external kinds become None,
    /// exactly like `kind in KINDS` failing in Python).
    pub fn from_json(v: &Value) -> Result<TickRequest, String> {
        let game = match v.get("game") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            Some(Value::Number(n)) => n.to_string(),
            _ => return Err("\"game\" (string or number) is required".into()),
        };
        let t = v.get("t").and_then(Value::as_f64).ok_or("\"t\" (game seconds) is required")?;
        if !t.is_finite() {
            return Err("\"t\" must be finite".into());
        }
        let sample = match v.get("sample") {
            None | Some(Value::Null) => None,
            Some(s) => Some(Panel {
                minerals: int(s, "minerals")?,
                gas: int(s, "gas")?,
                used: int(s, "used")?,
                cap: int(s, "cap")?,
            }),
        };
        let helper = match v.get("helper") {
            None | Some(Value::Null) => None,
            Some(h) => Some(HelperInput {
                plan: h.get("plan").and_then(Value::as_str).ok_or("helper.plan is required")?.to_string(),
                race: Race::parse(h.get("race").and_then(Value::as_str).unwrap_or("T")),
                wall: h.get("wall").and_then(Value::as_f64),
            }),
        };
        let brain = match v.get("brain") {
            None | Some(Value::Null) => None,
            Some(b) => Some(BrainInput {
                matchup: b.get("matchup").and_then(Value::as_str).unwrap_or("").to_string(),
                external: b
                    .get("external")
                    .and_then(Value::as_array)
                    .map(|a| a.iter().map(|k| k.as_str().and_then(CallKind::parse)).collect())
                    .unwrap_or_default(),
                race: Race::parse(b.get("race").and_then(Value::as_str).unwrap_or("T")),
            }),
        };
        Ok(TickRequest {
            game,
            new_game: v.get("new_game").and_then(Value::as_bool).unwrap_or(false),
            t,
            sample,
            helper,
            brain,
        })
    }
}

/// What a tick decided.
#[derive(Clone, Debug, PartialEq)]
pub struct TickResult {
    pub game: String,
    /// This tick started a fresh game state (new key or `new_game`).
    pub fresh: bool,
    pub helper: Option<HelperCall>,
    pub brain: Option<BrainCall>,
}

impl TickResult {
    pub fn to_json(&self) -> Value {
        json!({
            "game": self.game,
            "fresh": self.fresh,
            "helper": self.helper.as_ref().map(|c| json!({"kind": c.kind.as_str(), "text": c.text})),
            "brain": self.brain.as_ref().map(|c| json!({
                "kind": c.kind.as_str(), "text": c.text, "clip_text": c.clip_text, "confidence": c.confidence,
            })),
        })
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum TickError {
    UnknownPlan(String),
}

/// One game's rule state.
#[derive(Clone, Debug, Default)]
pub struct GameRules {
    pub helper: HelperRules,
    pub brain: BrainGame,
    touched: u64,
}

/// Rule state per game key. Old games are dropped beyond MAX_GAMES (least recently used first).
#[derive(Debug, Default)]
pub struct RulesHub {
    games: BTreeMap<String, GameRules>,
    ticks: u64,
}

impl RulesHub {
    pub const MAX_GAMES: usize = 8;

    pub fn new() -> RulesHub {
        RulesHub::default()
    }

    pub fn games(&self) -> impl Iterator<Item = &String> {
        self.games.keys()
    }

    pub fn game(&self, key: &str) -> Option<&GameRules> {
        self.games.get(key)
    }

    pub fn tick(&mut self, req: &TickRequest, plans: &BTreeMap<String, Plan>) -> Result<TickResult, TickError> {
        let steps = match &req.helper {
            Some(h) => Some(&plans.get(&h.plan).ok_or_else(|| TickError::UnknownPlan(h.plan.clone()))?.steps),
            None => None,
        };
        self.ticks += 1;
        let fresh = req.new_game || !self.games.contains_key(&req.game);
        if fresh {
            self.games.insert(req.game.clone(), GameRules { touched: self.ticks, ..GameRules::default() });
            while self.games.len() > Self::MAX_GAMES {
                let oldest = self.games.iter().min_by_key(|(_, g)| g.touched).map(|(k, _)| k.clone());
                match oldest {
                    Some(k) if k != req.game => self.games.remove(&k),
                    _ => break,
                };
            }
        }
        let g = self.games.get_mut(&req.game).expect("inserted above");
        g.touched = self.ticks;
        let helper = match (&req.helper, steps) {
            (Some(h), Some(steps)) => g.helper.tick(req.t, req.sample.as_ref(), steps, h.race, h.wall.unwrap_or(req.t)),
            _ => None,
        };
        let brain =
            req.brain.as_ref().and_then(|b| g.brain.feed(req.t, req.sample.as_ref(), &b.matchup, b.race, &b.external));
        Ok(TickResult { game: req.game.clone(), fresh, helper, brain })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Step;

    fn plans() -> BTreeMap<String, Plan> {
        let steps = vec![
            Step::new(0, Some(12), "SCVs"),
            Step::new(17, Some(14), "Supply Depot"),
            Step::new(600, Some(80), "Max"),
        ];
        BTreeMap::from([("A".to_string(), Plan::new("A", steps))])
    }

    fn req(game: &str, t: f64, used: i64, cap: i64) -> TickRequest {
        TickRequest::from_json(&json!({
            "game": game, "t": t, "sample": {"minerals": 50, "gas": 0, "used": used, "cap": cap},
            "helper": {"plan": "A", "race": "T", "wall": t}, "brain": {"matchup": "TP", "external": []},
        }))
        .unwrap()
    }

    #[test]
    fn parse_request() {
        let r = TickRequest::from_json(&json!({
            "game": 7, "t": 61.5, "new_game": true, "sample": null,
            "helper": {"plan": "M", "race": "P"}, "brain": {"matchup": "Z", "external": ["bank", null, "blocked", "coach"]},
        }))
        .unwrap();
        assert_eq!(r.game, "7");
        assert!(r.new_game && r.sample.is_none());
        assert_eq!(r.helper, Some(HelperInput { plan: "M".into(), race: Race::Protoss, wall: None }));
        let b = r.brain.unwrap();
        assert_eq!(b.external, vec![Some(CallKind::Bank), None, None, None]);
        assert_eq!(b.race, Race::Terran); // an older client sends no race: Terran
        let race = |r: Value| {
            let v = json!({"game": "g", "t": 1, "brain": {"matchup": "T", "external": [], "race": r}});
            TickRequest::from_json(&v).unwrap().brain.unwrap().race
        };
        assert_eq!((race(json!("P")), race(json!("Z")), race(json!("T"))), (Race::Protoss, Race::Zerg, Race::Terran));
        assert_eq!((race(json!("random")), race(Value::Null)), (Race::Terran, Race::Terran));
        assert!(TickRequest::from_json(&json!({"t": 1})).is_err());
        assert!(TickRequest::from_json(&json!({"game": "g"})).is_err());
        assert!(TickRequest::from_json(&json!({"game": "g", "t": 1, "sample": {"minerals": 1}})).is_err());
        assert!(TickRequest::from_json(&json!({"game": "g", "t": 1, "helper": {}})).is_err());
    }

    #[test]
    fn hub_runs_both_and_keeps_games_apart() {
        let plans = plans();
        let mut hub = RulesHub::new();
        // blocked at 23/23 from 1:40: confirmed on the second read
        let r = hub.tick(&req("g1", 100.0, 23, 23), &plans).unwrap();
        assert!(r.fresh && r.helper.is_none());
        let r = hub.tick(&req("g1", 100.5, 23, 23), &plans).unwrap();
        assert!(!r.fresh);
        assert_eq!(r.helper.unwrap().text, "Supply blocked 23/23: depot now");
        // another game key has its own streak
        assert!(hub.tick(&req("g2", 100.0, 23, 23), &plans).unwrap().helper.is_none());
        assert_eq!(hub.games().count(), 2);
        // new_game resets the key
        let mut again = req("g1", 101.0, 23, 23);
        again.new_game = true;
        assert!(hub.tick(&again, &plans).unwrap().fresh);
        assert_eq!(hub.game("g1").unwrap().brain.history().len(), 1);
    }

    #[test]
    fn unknown_plan_is_an_error_and_changes_nothing() {
        let mut hub = RulesHub::new();
        let mut r = req("g", 10.0, 12, 15);
        r.helper.as_mut().unwrap().plan = "nope".into();
        assert_eq!(hub.tick(&r, &plans()), Err(TickError::UnknownPlan("nope".into())));
        assert_eq!(hub.games().count(), 0);
    }

    #[test]
    fn old_games_are_dropped() {
        let plans = plans();
        let mut hub = RulesHub::new();
        for i in 0..20 {
            hub.tick(&req(&format!("g{i}"), 10.0, 12, 15), &plans).unwrap();
        }
        assert_eq!(hub.games().count(), RulesHub::MAX_GAMES);
        assert!(hub.game("g19").is_some() && hub.game("g0").is_none());
    }

    #[test]
    fn brain_race_from_the_request() {
        let plans = plans();
        let mut hub = RulesHub::new();
        let mut said = |game: &str, race: Option<&str>| {
            let mut last = None;
            for (t, m, u) in [(70.0, 200, 17), (85.0, 250, 18), (100.0, 280, 19)] {
                let mut v = json!({"game": game, "t": t, "sample": {"minerals": m, "gas": 40, "used": u, "cap": 23},
                                   "brain": {"matchup": "?", "external": []}});
                if let Some(r) = race {
                    v["brain"]["race"] = json!(r);
                }
                last = hub.tick(&TickRequest::from_json(&v).unwrap(), &plans).unwrap().brain;
            }
            last.map(|c| c.text)
        };
        assert_eq!(said("p", Some("P")).as_deref(), Some("Pylon now, 19 of 23"));
        assert_eq!(said("z", Some("Z")).as_deref(), Some("Overlord now, 19 of 23"));
        assert_eq!(said("old", None).as_deref(), Some("Depot now, 19 of 23"));
    }

    #[test]
    fn result_json_shape() {
        let plans = plans();
        let mut hub = RulesHub::new();
        let r = hub.tick(&req("g", 10.0, 12, 15), &plans).unwrap();
        assert_eq!(r.to_json(), json!({"game": "g", "fresh": true, "helper": null, "brain": null}));
    }
}
