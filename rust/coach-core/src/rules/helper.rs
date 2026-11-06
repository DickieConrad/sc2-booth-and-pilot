//! The live helper's call rules (port of `live/timing.py`: `plan_status`, `judge`, the CONFIRM count in the main
//! loop and `Pusher.say`'s gaps). Same thresholds, same text.
//!
//! Each tick with a panel read is judged into at most one problem (blocked > bank > behind). A problem is called
//! once it held for CONFIRM reads in a row and the say gate is open: 15 s since any helper say, 45 s since the
//! same kind (a bank call: 25 s while the bank is 600+). An unread / rejected panel (None) breaks the streak.

use super::{Panel, Race};
use crate::Step;

/// Seconds between any two helper says (`SAY_GAP`).
pub const SAY_GAP: f64 = 15.0;
/// Seconds before the same kind may be said again (`REPEAT_GAP`).
pub const REPEAT_GAP: f64 = 45.0;
/// A bank call repeats after this many seconds instead while the bank is still `BANK_REPEAT_FROM`+ minerals.
pub const BANK_REPEAT_GAP: f64 = 25.0;
pub const BANK_REPEAT_FROM: i64 = 600;
/// Consecutive reads (1 s at the 0.5 s tick) before a problem counts.
pub const CONFIRM: u32 = 2;
/// Supply below the plan before "behind".
pub const BEHIND_BY: i64 = 3;
/// Seconds after a step's time before it counts as late.
pub const GRACE: f64 = 10.0;
/// Before this game second 15/15 is normal (an on-time 0:17 depot finishes ~0:38).
pub const BLOCKED_FROM: f64 = 45.0;
/// Bank calls start at 1:00.
pub const BANK_FROM: f64 = 60.0;
/// Minerals that count as a bank before / from 4:00.
pub const BANK_EARLY: i64 = 400;
pub const BANK_LATE: i64 = 600;
pub const BANK_LATE_FROM: f64 = 240.0;
/// Saving for a planned CC / Nexus (step within -15..=20 s of now) is not banking: the bar rises by this much.
pub const CC_SAVING: i64 = 400;
/// "Behind" is only judged until 30 s after the plan's last step.
pub const BEHIND_UNTIL_AFTER_LAST: f64 = 30.0;
/// A clock going back further than this means a new game.
pub const REWIND: f64 = 5.0;

/// The helper's call kinds (the strings are the `kind` the page, the DB and the clips use).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum HelperKind {
    Blocked,
    /// Protoss player: "pylon" instead of "depot" (own clip).
    BlockedP,
    Bank,
    Behind,
    /// Protoss player: "Probes" instead of "SCVs".
    BehindP,
}

impl HelperKind {
    pub const ALL: [HelperKind; 5] =
        [HelperKind::Blocked, HelperKind::BlockedP, HelperKind::Bank, HelperKind::Behind, HelperKind::BehindP];

    pub fn as_str(self) -> &'static str {
        match self {
            HelperKind::Blocked => "blocked",
            HelperKind::BlockedP => "blocked-p",
            HelperKind::Bank => "bank",
            HelperKind::Behind => "behind",
            HelperKind::BehindP => "behind-p",
        }
    }

    pub fn parse(s: &str) -> Option<HelperKind> {
        HelperKind::ALL.into_iter().find(|k| k.as_str() == s)
    }

    fn index(self) -> usize {
        self as usize
    }
}

/// One helper call: what the page shows (and speaks) and the DB records.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HelperCall {
    pub kind: HelperKind,
    pub text: String,
}

/// Where the player should be on the plan at time t.
#[derive(Clone, Copy, Debug)]
pub struct PlanStatus<'a> {
    /// Supply of the last step at least GRACE s old (None before the first one, or if that step has no supply).
    pub expected: Option<i64>,
    /// The step that set `expected`.
    pub step: Option<&'a Step>,
    /// The first step not yet GRACE s old.
    pub next: Option<&'a Step>,
}

/// `plan_status(steps, t)`: steps in plan order.
pub fn plan_status(steps: &[Step], t: f64) -> PlanStatus<'_> {
    let step = steps.iter().rev().find(|s| s.sec as f64 <= t - GRACE);
    PlanStatus { expected: step.and_then(|s| s.supply), step, next: steps.iter().find(|s| s.sec as f64 > t - GRACE) }
}

/// `judge(panel, t, steps, race)`: the most important problem right now, or None.
pub fn judge(panel: &Panel, t: f64, steps: &[Step], race: Race) -> Option<HelperCall> {
    let (m, used, cap) = (panel.minerals, panel.used, panel.cap);
    let p = race == Race::Protoss;
    if t >= BLOCKED_FROM && cap < 200 && used >= cap {
        return Some(if p {
            HelperCall { kind: HelperKind::BlockedP, text: format!("Supply blocked {used}/{cap}: pylon now") }
        } else {
            HelperCall { kind: HelperKind::Blocked, text: format!("Supply blocked {used}/{cap}: depot now") }
        });
    }
    let st = plan_status(steps, t);
    let behind = match (st.expected, steps.last()) {
        (Some(exp), Some(last)) => t <= last.sec as f64 + BEHIND_UNTIL_AFTER_LAST && used < exp - BEHIND_BY,
        _ => false,
    };
    let mut bank = if t < BANK_LATE_FROM { BANK_EARLY } else { BANK_LATE };
    let saving = steps.iter().any(|s| {
        let d = s.sec as f64 - t;
        (s.action.contains("Command Center") || s.action.contains("Nexus")) && (-15.0..=20.0).contains(&d)
    });
    if saving {
        bank += CC_SAVING;
    }
    if t >= BANK_FROM && m >= bank {
        // behind with money in the bank: spending is the fix, so say that
        let mut text = format!("Bank {}, spend", m.div_euclid(50) * 50);
        if let (true, Some(exp)) = (behind, st.expected) {
            text.push_str(&format!(": supply {used} vs plan {exp}"));
        }
        return Some(HelperCall { kind: HelperKind::Bank, text });
    }
    if behind {
        let (exp, step) = (st.expected?, st.step?);
        let (kind, workers) = if p { (HelperKind::BehindP, "Probes") } else { (HelperKind::Behind, "SCVs") };
        return Some(HelperCall {
            kind,
            text: format!("Behind: {used} supply, plan {exp}: {workers} + {}", step.action),
        });
    }
    None
}

/// `repeat_gap(kind, minerals)`: seconds before `kind` may be said again. REPEAT_GAP, or BANK_REPEAT_GAP for a bank
/// call while the bank is still BANK_REPEAT_FROM+ (sitting on 800-1600 for minutes got one call every 45 s).
pub fn repeat_gap(kind: HelperKind, minerals: i64) -> f64 {
    if kind == HelperKind::Bank && minerals >= BANK_REPEAT_FROM {
        BANK_REPEAT_GAP
    } else {
        REPEAT_GAP
    }
}

/// `Pusher.say`'s gaps, on whatever clock the caller passes (the live helper: wall seconds).
/// Both marks start at 0, like the Python (`last_say = 0.0`, `recent.get(kind, 0)`).
#[derive(Clone, Debug, Default)]
pub struct SayGate {
    last_say: f64,
    recent: [f64; 5],
}

impl SayGate {
    /// `repeat`: seconds since the same kind (`repeat_gap`).
    pub fn is_open(&self, kind: HelperKind, wall: f64, repeat: f64) -> bool {
        !(wall - self.last_say < SAY_GAP || wall - self.recent[kind.index()] < repeat)
    }

    /// Says (and marks) if open; false when a gap is still closed.
    pub fn try_say(&mut self, kind: HelperKind, wall: f64, repeat: f64) -> bool {
        if !self.is_open(kind, wall, repeat) {
            return false;
        }
        self.last_say = wall;
        self.recent[kind.index()] = wall;
        true
    }
}

/// One game's helper rules: the CONFIRM streak and the say gate.
#[derive(Clone, Debug, Default)]
pub struct HelperRules {
    /// The problem judged on the last read and how many reads in a row it held.
    hits: Option<(HelperKind, u32)>,
    gate: SayGate,
    last_t: Option<f64>,
}

impl HelperRules {
    pub fn new() -> HelperRules {
        HelperRules::default()
    }

    /// New game: the streak goes; the say gate stays (it is on the wall clock, like the Python Pusher).
    pub fn new_game(&mut self) {
        self.hits = None;
        self.last_t = None;
    }

    pub fn streak(&self) -> Option<(HelperKind, u32)> {
        self.hits
    }

    /// One OCR tick. `panel` None = unread or rejected read. `wall` drives the say gaps.
    /// Returns the call to say now, if any. A clock rewind of more than 5 s starts a new game first.
    pub fn tick(&mut self, t: f64, panel: Option<&Panel>, steps: &[Step], race: Race, wall: f64) -> Option<HelperCall> {
        if matches!(self.last_t, Some(l) if t < l - REWIND) {
            self.new_game();
        }
        self.last_t = Some(t);
        let Some(panel) = panel else {
            self.hits = None;
            return None;
        };
        let Some(call) = judge(panel, t, steps, race) else {
            self.hits = None;
            return None;
        };
        let n = match self.hits {
            Some((k, n)) if k == call.kind => n + 1,
            _ => 1,
        };
        self.hits = Some((call.kind, n));
        if n >= CONFIRM && self.gate.try_say(call.kind, wall, repeat_gap(call.kind, panel.minerals)) {
            self.hits = None;
            return Some(call);
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn steps(s: &[(i64, Option<i64>, &str)]) -> Vec<Step> {
        s.iter().map(|&(sec, sup, a)| Step::new(sec, sup, a)).collect()
    }
    fn panel(minerals: i64, gas: i64, used: i64, cap: i64) -> Panel {
        Panel { minerals, gas, used, cap }
    }
    fn plan() -> Vec<Step> {
        steps(&[
            (0, Some(12), "SCVs nonstop"),
            (17, Some(14), "Supply Depot"),
            (40, Some(16), "Barracks"),
            (100, Some(20), "Command Center (natural)"),
            (180, Some(30), "Factory"),
            (300, Some(44), "Starport"),
        ])
    }

    #[test]
    fn plan_status_uses_grace() {
        let p = plan();
        let st = plan_status(&p, 49.9);
        assert_eq!(st.expected, Some(14));
        assert_eq!(st.next.unwrap().sec, 40);
        let st = plan_status(&p, 50.0);
        assert_eq!((st.expected, st.step.unwrap().sec), (Some(16), 40));
        assert!(plan_status(&p, 5.0).expected.is_none());
        // a step without supply: expected is None (behind can't be judged), the step is still the last done
        let p = steps(&[(0, Some(12), "SCVs"), (30, None, "Scout")]);
        let st = plan_status(&p, 45.0);
        assert_eq!((st.expected, st.step.unwrap().sec), (None, 30));
    }

    #[test]
    fn blocked_only_from_45_and_under_200() {
        let p = plan();
        assert_eq!(judge(&panel(50, 0, 15, 15), 44.0, &p, Race::Terran), None);
        let c = judge(&panel(50, 0, 15, 15), 45.0, &p, Race::Terran).unwrap();
        assert_eq!((c.kind, c.text.as_str()), (HelperKind::Blocked, "Supply blocked 15/15: depot now"));
        let c = judge(&panel(50, 0, 23, 23), 90.0, &p, Race::Protoss).unwrap();
        assert_eq!((c.kind, c.text.as_str()), (HelperKind::BlockedP, "Supply blocked 23/23: pylon now"));
        assert_ne!(judge(&panel(50, 0, 200, 200), 900.0, &p, Race::Terran).map(|c| c.kind), Some(HelperKind::Blocked));
    }

    #[test]
    fn bank_bar_and_cc_saving() {
        let p = plan();
        // 1:20: a CC step at 1:40 is 20 s away -> bar 800
        assert_eq!(judge(&panel(790, 0, 18, 23), 80.0, &p, Race::Terran), None);
        let c = judge(&panel(810, 0, 18, 23), 80.0, &p, Race::Terran).unwrap();
        assert_eq!(c.text, "Bank 800, spend");
        // 1:19: 21 s before the CC -> bar 400
        assert_eq!(judge(&panel(420, 0, 18, 23), 79.0, &p, Race::Terran).unwrap().text, "Bank 400, spend");
        // before 1:00 never
        assert_eq!(judge(&panel(900, 0, 14, 15), 59.0, &p, Race::Terran), None);
        // from 4:00 the bar is 600
        assert_eq!(judge(&panel(590, 0, 40, 46), 240.0, &p, Race::Terran), None);
        assert_eq!(judge(&panel(649, 0, 40, 46), 240.0, &p, Race::Terran).unwrap().text, "Bank 600, spend");
        // "Nexus" counts too
        let n = steps(&[(0, Some(12), "Probes"), (100, Some(20), "Nexus")]);
        assert_eq!(judge(&panel(500, 0, 19, 23), 90.0, &n, Race::Protoss), None);
    }

    #[test]
    fn behind_and_bank_while_behind() {
        let p = plan();
        // 3:10: plan 30 (Factory at 3:00, 10 s grace), 26 < 27 -> behind
        let c = judge(&panel(100, 0, 26, 31), 190.0, &p, Race::Terran).unwrap();
        assert_eq!((c.kind, c.text.as_str()), (HelperKind::Behind, "Behind: 26 supply, plan 30: SCVs + Factory"));
        assert_eq!(judge(&panel(100, 0, 27, 31), 190.0, &p, Race::Terran), None);
        let c = judge(&panel(100, 0, 26, 31), 190.0, &p, Race::Protoss).unwrap();
        assert_eq!(c.text, "Behind: 26 supply, plan 30: Probes + Factory");
        assert_eq!(c.kind, HelperKind::BehindP);
        let c = judge(&panel(480, 0, 26, 31), 190.0, &p, Race::Terran).unwrap();
        assert_eq!(c.text, "Bank 450, spend: supply 26 vs plan 30");
        // only until 30 s after the last step
        assert!(judge(&panel(100, 0, 26, 50), 330.0, &p, Race::Terran).is_some());
        assert_eq!(judge(&panel(100, 0, 26, 50), 331.0, &p, Race::Terran), None);
    }

    #[test]
    fn confirm_then_gaps() {
        let p = plan();
        let mut h = HelperRules::new();
        let blocked = panel(50, 0, 23, 23);
        // first read: streak 1; second: said (wall = game clock here)
        assert_eq!(h.tick(100.0, Some(&blocked), &p, Race::Terran, 100.0), None);
        assert_eq!(h.tick(100.5, Some(&blocked), &p, Race::Terran, 100.5).unwrap().kind, HelperKind::Blocked);
        assert_eq!(h.streak(), None);
        // same kind again within 45 s: confirmed but gated; the streak keeps growing
        for i in 1..=10 {
            let t = 100.5 + i as f64;
            assert_eq!(h.tick(t, Some(&blocked), &p, Race::Terran, t), None);
        }
        assert_eq!(h.streak(), Some((HelperKind::Blocked, 10)));
        // the gap opens at 145.5: said at once (streak already long)
        assert_eq!(h.tick(145.0, Some(&blocked), &p, Race::Terran, 145.0), None);
        assert!(h.tick(145.5, Some(&blocked), &p, Race::Terran, 145.5).is_some());
        // a different kind waits for the 15 s SAY_GAP only
        let rich = panel(700, 0, 20, 31);
        assert_eq!(h.tick(150.0, Some(&rich), &p, Race::Terran, 150.0), None);
        assert_eq!(h.tick(155.0, Some(&rich), &p, Race::Terran, 155.0), None);
        assert_eq!(h.tick(160.5, Some(&rich), &p, Race::Terran, 160.5).unwrap().kind, HelperKind::Bank);
    }

    #[test]
    fn bank_repeats_every_25_s_while_600_plus() {
        assert_eq!(repeat_gap(HelperKind::Bank, 600), BANK_REPEAT_GAP);
        assert_eq!(repeat_gap(HelperKind::Bank, 599), REPEAT_GAP);
        assert_eq!(repeat_gap(HelperKind::Blocked, 2000), REPEAT_GAP);
        let p = plan();
        // 4:00+: an 800-1600 bank held for a minute -> a call every 25 s (wall = game clock), same wording
        let mut h = HelperRules::new();
        let mut said = Vec::new();
        for i in 0..=140 {
            let t = 250.0 + i as f64 * 0.5;
            if let Some(c) = h.tick(t, Some(&panel(800 + 5 * i, 0, 60, 70)), &p, Race::Terran, t) {
                assert!(c.text.starts_with("Bank ") && c.text.ends_with(", spend"), "{}", c.text);
                said.push(t);
            }
        }
        assert_eq!(said, [250.5, 275.5, 300.5]);
        // under 600 (a 400-599 bank before 4:00) the 45 s gap still holds
        let mut h = HelperRules::new();
        let mut said = Vec::new();
        for i in 0..=120 {
            let t = 150.0 + i as f64 * 0.5;
            if h.tick(t, Some(&panel(500, 0, 30, 39)), &p, Race::Terran, t).is_some() {
                said.push(t);
            }
        }
        assert_eq!(said, [150.5, 195.5]);
        // the bank fell under 600 since the last call: the 45 s gap again; back at 600+: 25 s
        let mut h = HelperRules::new();
        let mut tick = |t: f64, m: i64| h.tick(t, Some(&panel(m, 0, 30, 39)), &p, Race::Terran, t).is_some();
        assert!(!tick(150.0, 900) && tick(150.5, 900));
        assert!(!tick(170.0, 500) && !tick(170.5, 500));
        assert!(!tick(175.0, 650) && tick(175.5, 650));
        assert!(!tick(200.0, 500) && !tick(220.0, 500) && tick(220.5, 500));
    }

    #[test]
    fn unread_or_other_kind_breaks_the_streak() {
        let p = plan();
        let mut h = HelperRules::new();
        let blocked = panel(50, 0, 23, 23);
        assert_eq!(h.tick(100.0, Some(&blocked), &p, Race::Terran, 100.0), None);
        assert_eq!(h.tick(100.5, None, &p, Race::Terran, 100.5), None);
        assert_eq!(h.tick(101.0, Some(&blocked), &p, Race::Terran, 101.0), None);
        assert_eq!(h.tick(101.5, Some(&panel(700, 0, 20, 31)), &p, Race::Terran, 101.5), None);
        assert_eq!(h.tick(102.0, Some(&blocked), &p, Race::Terran, 102.0), None);
        assert!(h.tick(102.5, Some(&blocked), &p, Race::Terran, 102.5).is_some());
    }

    #[test]
    fn rewind_resets_the_streak_not_the_gate() {
        let p = plan();
        let mut h = HelperRules::new();
        let blocked = panel(50, 0, 23, 23);
        assert!(h.tick(100.0, Some(&blocked), &p, Race::Terran, 1000.0).is_none());
        // new game (clock back to 0:50): the old streak of 1 does not count
        assert!(h.tick(50.0, Some(&blocked), &p, Race::Terran, 1001.0).is_none());
        assert!(h.tick(50.5, Some(&blocked), &p, Race::Terran, 1001.5).is_some());
        // the gate is on the wall clock: same kind 10 s later in a newer game is still held
        assert!(h.tick(50.0, Some(&blocked), &p, Race::Terran, 1010.0).is_none());
        assert!(h.tick(50.5, Some(&blocked), &p, Race::Terran, 1010.5).is_none());
    }

    #[test]
    fn kinds_round_trip() {
        for k in HelperKind::ALL {
            assert_eq!(HelperKind::parse(k.as_str()), Some(k));
        }
        assert_eq!(HelperKind::parse("plan"), None);
    }
}
