//! The HTTP routes of `coach-core serve`, without the socket: `Service::handle(method, path, body)`.
//!
//! POST /start {plan}            start the cue engine on a plan
//! POST /tick {t, used}          cue engine tick -> {events, lateness, shift}
//! GET  /state, GET /health
//! POST /rules/tick {game, t, sample, helper?, brain?, new_game?}
//!                               call rules (see `rules`) -> {game, fresh, helper, brain}

use crate::rules::{RulesHub, TickError, TickRequest};
use crate::{load_plans, Engine, Plan};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Method {
    Get,
    Post,
    Other,
}

pub struct Service {
    live: PathBuf,
    plans: BTreeMap<String, Plan>,
    engine: Option<Engine>,
    rules: RulesHub,
}

fn err(code: u16, msg: impl Into<String>) -> (u16, Value) {
    (code, json!({"error": msg.into()}))
}

fn body_json(body: &str) -> Result<Value, String> {
    serde_json::from_str(if body.trim().is_empty() { "{}" } else { body }).map_err(|e| format!("bad JSON: {e}"))
}

impl Service {
    pub fn new(live: PathBuf, plans: BTreeMap<String, Plan>) -> Service {
        Service { live, plans, engine: None, rules: RulesHub::new() }
    }

    pub fn plans(&self) -> &BTreeMap<String, Plan> {
        &self.plans
    }

    pub fn rules(&self) -> &RulesHub {
        &self.rules
    }

    /// Pick up plan edits without a restart; keep the last good set if a file is mid-write.
    fn reload_plans(&mut self) {
        match load_plans(&self.live) {
            Ok(p) => self.plans = p,
            Err(e) => eprintln!("coach-core: plan reload failed, keeping the previous plans: {e}"),
        }
    }

    pub fn handle(&mut self, method: Method, path: &str, body: &str) -> (u16, Value) {
        let path = path.split('?').next().unwrap_or("");
        match (method, path) {
            (Method::Post, "/start") => {
                let v = match body_json(body) {
                    Ok(v) => v,
                    Err(e) => return err(400, e),
                };
                let Some(id) = v.get("plan").and_then(Value::as_str) else {
                    return err(400, "body must be {\"plan\": ID}");
                };
                self.reload_plans();
                match self.plans.get(id) {
                    Some(p) => {
                        let n = p.steps.len();
                        self.engine = Some(Engine::new(p.clone()));
                        eprintln!("coach-core: start plan {id} ({n} steps)");
                        (200, json!({"ok": true, "steps": n}))
                    }
                    None => (
                        404,
                        json!({"error": format!("unknown plan {id:?}"), "plans": self.plans.keys().collect::<Vec<_>>()}),
                    ),
                }
            }
            (Method::Post, "/tick") => {
                let v = match body_json(body) {
                    Ok(v) => v,
                    Err(e) => return err(400, e),
                };
                let Some(t) = v.get("t").and_then(Value::as_f64) else {
                    return err(400, "body must be {\"t\": seconds, \"used\": n|null}");
                };
                let used = v.get("used").and_then(Value::as_f64).map(|u| u.round() as i64);
                match self.engine.as_mut() {
                    Some(e) => {
                        let events = e.tick(t, used);
                        (200, e.tick_json(&events))
                    }
                    None => err(409, "no plan: POST /start first"),
                }
            }
            (Method::Post, "/rules/tick") => {
                let req = match body_json(body).and_then(|v| TickRequest::from_json(&v)) {
                    Ok(r) => r,
                    Err(e) => return err(400, e),
                };
                if req.helper.as_ref().is_some_and(|h| !self.plans.contains_key(&h.plan)) {
                    self.reload_plans(); // a plan added since the start
                }
                match self.rules.tick(&req, &self.plans) {
                    Ok(r) => (200, r.to_json()),
                    Err(TickError::UnknownPlan(id)) => err(404, format!("unknown plan {id:?}")),
                }
            }
            (Method::Get, "/state") => match &self.engine {
                Some(e) => (200, e.state_json()),
                None => (200, json!({"plan": null})),
            },
            (Method::Get, "/health") => (
                200,
                json!({"ok": true, "plan": self.engine.as_ref().map(|e| e.plan().id.clone()), "plans": self.plans.len(),
                       "rules_games": self.rules.games().count()}),
            ),
            _ => err(404, "POST /start | POST /tick | POST /rules/tick | GET /state | GET /health"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Step;

    fn service() -> Service {
        let a = Plan::new("A", vec![Step::new(0, Some(12), "SCVs"), Step::new(40, Some(16), "Barracks")]);
        // a live dir with no plan files: reloads fail and keep this set
        Service::new(PathBuf::from("/nonexistent-coach-core-test"), BTreeMap::from([("A".to_string(), a)]))
    }

    fn tick(s: &mut Service, t: f64, used: i64) -> (u16, Value) {
        let body = json!({"game": "h1", "t": t, "sample": {"minerals": 50, "gas": 0, "used": used, "cap": used},
                          "helper": {"plan": "A", "race": "T", "wall": t}});
        s.handle(Method::Post, "/rules/tick", &body.to_string())
    }

    #[test]
    fn rules_tick_route() {
        let mut s = service();
        let (code, v) = tick(&mut s, 60.0, 19);
        assert_eq!(code, 200);
        assert_eq!(v, json!({"game": "h1", "fresh": true, "helper": null, "brain": null}));
        let (_, v) = tick(&mut s, 60.5, 19);
        assert_eq!(v["helper"], json!({"kind": "blocked", "text": "Supply blocked 19/19: depot now"}));
        assert_eq!(s.handle(Method::Get, "/health", "").1["rules_games"], 1);
    }

    #[test]
    fn rules_tick_errors() {
        let mut s = service();
        assert_eq!(s.handle(Method::Post, "/rules/tick", "{").0, 400);
        assert_eq!(s.handle(Method::Post, "/rules/tick", "{}").0, 400);
        let body = json!({"game": "g", "t": 1, "helper": {"plan": "Q"}}).to_string();
        let (code, v) = s.handle(Method::Post, "/rules/tick", &body);
        assert_eq!((code, v["error"].as_str().unwrap()), (404, "unknown plan \"Q\""));
        assert_eq!(s.handle(Method::Get, "/rules/tick", "").0, 404);
    }

    #[test]
    fn cue_routes_unchanged() {
        let mut s = service();
        assert_eq!(s.handle(Method::Post, "/tick", r#"{"t": 1}"#).0, 409);
        assert_eq!(s.handle(Method::Get, "/state", "").1, json!({"plan": null}));
        assert_eq!(s.handle(Method::Post, "/start", r#"{"plan": "A"}"#), (200, json!({"ok": true, "steps": 2})));
        let (code, v) = s.handle(Method::Post, "/tick?x=1", r#"{"t": 0, "used": 12}"#);
        assert_eq!(code, 200);
        assert_eq!(v["events"][0]["keys"], json!(["SCVs"]));
        assert_eq!(s.handle(Method::Post, "/start", r#"{"plan": "Z"}"#).0, 404);
    }
}
