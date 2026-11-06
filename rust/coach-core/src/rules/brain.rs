//! The booth brain (port of `live/brain.py` `describe` + `RulesBrain`, and `live/brain_run.py` `Runner.feed`).
//!
//! Panel samples become a few derived facts (`describe`), then ONE call is picked from a fixed set, or none:
//! timed reminders first, then the rules in order; the first that holds and is off cooldown wins.
//! - Cooldowns: 20 s between any two brain calls (lost CC / lost depot skip it), a per-kind gap otherwise.
//! - Spend families (bank + bank_big, gas + gas_early) share one cooldown keyed to the pile at the last call:
//!   again after 90 s, or from 30 s once the pile is 1.5x what it was. Held while losing a fight (supply -10 in 20 s).
//! - Wording rotates: the n-th call of a kind uses clip n and page text n. Page text has the numbers; clip text none.
//! - Race: the followed player's. Protoss / Zerg get their own wording ("Pylon now", "Overlord now"); kinds with no
//!   sensible translation (CC, orbital, the turret / bunker / stall reminders; lost CC for Zerg) are never offered.

use super::{Panel, Race, Sample};
use std::collections::{BTreeMap, BTreeSet};

/// Seconds of history the windowed facts look at.
pub const WINDOW: f64 = 60.0;
/// Seconds between any two brain calls (urgent kinds skip it).
pub const GLOBAL_GAP: f64 = 20.0;
/// Default per-kind gap.
pub const COOLDOWN: f64 = 60.0;
/// A spend pile is called again only after this many seconds ...
pub const REPEAT_GAP: f64 = 90.0;
/// ... or sooner once it is this many times what it was at the last call ...
pub const ESCALATE: f64 = 1.5;
/// ... but never within this many seconds.
pub const ESCALATE_GAP: f64 = 30.0;
/// Supply down FIGHT_DROP+ inside FIGHT_WINDOW seconds = losing a fight.
pub const FIGHT_WINDOW: f64 = 20.0;
pub const FIGHT_DROP: i64 = 10;
/// Runner history cap (~25 min at 0.5 s) and how much is dropped when it is hit.
pub const HISTORY_MAX: usize = 3000;
pub const HISTORY_TRIM: usize = 600;
/// A clock going back further than this means a new game.
pub const REWIND: f64 = 5.0;

/// The brain's fixed call set. Declaration order = `KINDS` order in brain.py.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum CallKind {
    LostCc,
    LostDepot,
    Depot,
    Depots,
    Cc,
    Stuck,
    Orbital,
    GasIdle,
    BankBig,
    Bank,
    GasEarly,
    Gas,
    Turret,
    Bunker,
    Stall,
}

/// What a spend family is about.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Pile {
    Minerals,
    Gas,
}

/// Static data of one kind: cooldown (0 = timed, fire-once), clip phrasings (no numbers), page-text templates.
/// Clip i and text i say the same thing; a kind with one template uses it for every clip.
pub struct KindSpec {
    pub cooldown: f64,
    pub clips: &'static [&'static str],
    pub texts: &'static [&'static str],
}

impl CallKind {
    pub const ALL: [CallKind; 15] = [
        CallKind::LostCc,
        CallKind::LostDepot,
        CallKind::Depot,
        CallKind::Depots,
        CallKind::Cc,
        CallKind::Stuck,
        CallKind::Orbital,
        CallKind::GasIdle,
        CallKind::BankBig,
        CallKind::Bank,
        CallKind::GasEarly,
        CallKind::Gas,
        CallKind::Turret,
        CallKind::Bunker,
        CallKind::Stall,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            CallKind::LostCc => "lost_cc",
            CallKind::LostDepot => "lost_depot",
            CallKind::Depot => "depot",
            CallKind::Depots => "depots",
            CallKind::Cc => "cc",
            CallKind::Stuck => "stuck",
            CallKind::Orbital => "orbital",
            CallKind::GasIdle => "gas_idle",
            CallKind::BankBig => "bank_big",
            CallKind::Bank => "bank",
            CallKind::GasEarly => "gas_early",
            CallKind::Gas => "gas",
            CallKind::Turret => "turret",
            CallKind::Bunker => "bunker",
            CallKind::Stall => "stall",
        }
    }

    /// `kind in KINDS`: anything else (helper "blocked", "plan", the coach) is not a brain kind.
    pub fn parse(s: &str) -> Option<CallKind> {
        CallKind::ALL.into_iter().find(|k| k.as_str() == s)
    }

    /// Same coach habit, different wording: depots -> depot, bank_big -> bank, gas_early -> gas.
    pub fn family(self) -> CallKind {
        match self {
            CallKind::Depots => CallKind::Depot,
            CallKind::BankBig => CallKind::Bank,
            CallKind::GasEarly => CallKind::Gas,
            k => k,
        }
    }

    /// The pile a spend family is about (None: not a spend kind).
    pub fn pile(self) -> Option<Pile> {
        match self.family() {
            CallKind::Bank => Some(Pile::Minerals),
            CallKind::Gas => Some(Pile::Gas),
            _ => None,
        }
    }

    /// Skips the global gap.
    pub fn is_urgent(self) -> bool {
        matches!(self, CallKind::LostCc | CallKind::LostDepot)
    }

    /// Fire-once reminders: (matchup letters any of, from s, to s), inclusive.
    pub fn timed(self) -> &'static [(&'static str, i64, i64)] {
        match self {
            CallKind::Turret => &[("TP", 300, 330), ("Z", 360, 390)],
            CallKind::Bunker => &[("Z", 165, 195)],
            CallKind::Stall => &[("TPZR", 150, 165)],
            _ => &[],
        }
    }

    pub fn is_timed(self) -> bool {
        !self.timed().is_empty()
    }

    pub fn spec(self) -> KindSpec {
        let (cooldown, clips, texts): (f64, &'static [&'static str], &'static [&'static str]) = match self {
            CallKind::LostCc => (
                60.0,
                &["You lost a Command Center. Army home, now."],
                &["Lost a Command Center ({cap_dropped} cap): army home"],
            ),
            CallKind::LostDepot => (
                60.0,
                &["You lost a depot. Check your main.", "Supply depot down. Look at your main."],
                &["Lost a depot ({cap_dropped} cap), check your main"],
            ),
            CallKind::Depot => {
                (60.0, &["Depot now.", "Depot now, you're close to the cap."], &["Depot now, {used} of {cap}"])
            }
            CallKind::Depots => {
                (60.0, &["Two depots now.", "Two depots, you're near the cap."], &["2 depots now, {used} of {cap}"])
            }
            CallKind::Cc => (
                30.0,
                &["Command Center now.", "Command Center at the natural, now."],
                &["Command Center now, {minerals} minerals"],
            ),
            CallKind::Stuck => (
                60.0,
                &["Supply is stuck. Queue SCVs.", "Supply hasn't moved. SCVs and Marines, now."],
                &["Supply stuck at {used}: queue SCVs"],
            ),
            CallKind::Orbital => (
                120.0,
                &["Command Center's done. Orbital Command, SCVs from both."],
                &["CC done: Orbital Command, SCVs from both"],
            ),
            CallKind::GasIdle => {
                (90.0, &["Gas stopped rising. Check your refinery."], &["Gas stuck at {gas}: check your refinery"])
            }
            CallKind::BankBig => (
                REPEAT_GAP,
                &[
                    "Way too much money. Four Barracks, Marines from every one.",
                    "Huge bank. Spend it: Barracks and depots, now.",
                    "Big bank. Second Factory and two Barracks.",
                ],
                &[
                    "{minerals} banked! 4 Barracks, Marines from every one",
                    "{minerals} banked! Barracks and depots, now",
                    "Big bank, {minerals}: 2nd Factory, 2 Barracks",
                ],
            ),
            CallKind::Bank => (
                REPEAT_GAP,
                &[
                    "Money's piling up. Two Barracks, now.",
                    "Spend it. Two more Barracks.",
                    "Bank's piling. Starport, Barracks, Marines.",
                ],
                &[
                    "{minerals} banked: 2 Barracks",
                    "Spend {minerals}: 2 more Barracks",
                    "{minerals} banked: Starport, Barracks, Marines",
                ],
            ),
            CallKind::GasEarly => (
                REPEAT_GAP,
                &[
                    "Gas is up. Stim and a Tank.",
                    "Spend the gas. Stim, then a Tank.",
                    "Gas is high. Combat Shield with that gas.",
                ],
                &["Gas {gas}: Stim and a Tank", "Gas {gas}: Stim, then a Tank", "Gas {gas}: Combat Shield"],
            ),
            CallKind::Gas => (
                REPEAT_GAP,
                &[
                    "Gas is piling up. Tanks and Medivacs.",
                    "Spend the gas. Medivacs, Tanks, an upgrade.",
                    "Too much gas. Three SCVs to minerals, Tanks and upgrades.",
                ],
                &[
                    "Gas {gas}: Tanks and Medivacs",
                    "Gas {gas}: Medivacs, Tanks, upgrade",
                    "Gas {gas}: 3 SCVs to minerals, Tanks, upgrades",
                ],
            ),
            CallKind::Turret => (0.0, &["Turret in each mineral line."], &["Turret in each mineral line"]),
            CallKind::Bunker => {
                (0.0, &["Bunker at the natural. Watch for lings."], &["Bunker at the natural, watch for lings"])
            }
            CallKind::Stall => (
                0.0,
                &["Stall minute. Marines nonstop, SCVs from every CC."],
                &["Stall minute: Marines nonstop, SCVs from every CC"],
            ),
        };
        KindSpec { cooldown, clips, texts }
    }

    /// (clip phrasings, page-text templates) for a race (`brain.py` `wording` / `RACE_KINDS`); `spec()` is the
    /// Terran wording. None: the race never hears this kind.
    pub fn wording(self, race: Race) -> Option<Wording> {
        const GAS_EARLY_PZ: Wording = (
            &["Gas is up. Spend it on tech.", "Spend the gas. Tech and upgrades."],
            &["Gas {gas}: spend it on tech", "Gas {gas}: tech and upgrades"],
        );
        let w: Wording = match (race, self) {
            (Race::Terran, k) => {
                let s = k.spec();
                (s.clips, s.texts)
            }
            (_, CallKind::Cc | CallKind::Orbital | CallKind::Turret | CallKind::Bunker | CallKind::Stall) => {
                return None
            }
            (_, CallKind::GasEarly) => GAS_EARLY_PZ,
            (Race::Protoss, CallKind::LostCc) => {
                (&["You lost a Nexus. Army home, now."], &["Lost a Nexus ({cap_dropped} cap): army home"])
            }
            (Race::Protoss, CallKind::LostDepot) => (
                &["You lost a pylon. Check your main.", "Pylon down. Look at your main."],
                &["Lost a pylon ({cap_dropped} cap), check your main"],
            ),
            (Race::Protoss, CallKind::Depot) => {
                (&["Pylon now.", "Pylon now, you're close to the cap."], &["Pylon now, {used} of {cap}"])
            }
            (Race::Protoss, CallKind::Depots) => {
                (&["Two pylons now.", "Two pylons, you're near the cap."], &["2 pylons now, {used} of {cap}"])
            }
            (Race::Protoss, CallKind::Stuck) => (
                &["Supply is stuck. Queue Probes.", "Supply hasn't moved. Probes and units, now."],
                &["Supply stuck at {used}: queue Probes"],
            ),
            (Race::Protoss, CallKind::GasIdle) => {
                (&["Gas stopped rising. Check your assimilator."], &["Gas stuck at {gas}: check your assimilator"])
            }
            (Race::Protoss, CallKind::BankBig) => (
                &[
                    "Way too much money. Four Gateways, warp in nonstop.",
                    "Huge bank. Spend it: Gateways and pylons, now.",
                ],
                &["{minerals} banked! 4 Gateways, warp in nonstop", "{minerals} banked! Gateways and pylons, now"],
            ),
            (Race::Protoss, CallKind::Bank) => (
                &["Money's piling up. Two Gateways, now.", "Spend it. Two more Gateways."],
                &["{minerals} banked: 2 Gateways", "Spend {minerals}: 2 more Gateways"],
            ),
            (Race::Protoss, CallKind::Gas) => (
                &["Gas is piling up. Gas units and upgrades.", "Too much gas. Three Probes to minerals."],
                &["Gas {gas}: gas units and upgrades", "Gas {gas}: 3 Probes to minerals"],
            ),
            // a Hatchery is 6 supply: a 14+ cap drop is not one
            (Race::Zerg, CallKind::LostCc) => return None,
            (Race::Zerg, CallKind::LostDepot) => (
                &["Overlord down. Make more, now.", "You lost an overlord. Replace it."],
                &["Lost overlords ({cap_dropped} cap): make more"],
            ),
            (Race::Zerg, CallKind::Depot) => {
                (&["Overlord now.", "Overlord now, you're close to the cap."], &["Overlord now, {used} of {cap}"])
            }
            (Race::Zerg, CallKind::Depots) => {
                (&["Two overlords now.", "Two overlords, you're near the cap."], &["2 overlords now, {used} of {cap}"])
            }
            (Race::Zerg, CallKind::Stuck) => (
                &["Supply is stuck. Make Drones.", "Supply hasn't moved. Drones and units, now."],
                &["Supply stuck at {used}: make Drones"],
            ),
            (Race::Zerg, CallKind::GasIdle) => {
                (&["Gas stopped rising. Check your extractor."], &["Gas stuck at {gas}: check your extractor"])
            }
            (Race::Zerg, CallKind::BankBig) => (
                &["Way too much money. Two Hatcheries, now.", "Huge bank. Spend it: Hatcheries and Queens, now."],
                &["{minerals} banked! 2 Hatcheries", "{minerals} banked! Hatcheries and Queens, now"],
            ),
            (Race::Zerg, CallKind::Bank) => (
                &["Money's piling up. Another Hatchery, now.", "Spend it. Drones and units, now."],
                &["{minerals} banked: another Hatchery", "Spend {minerals}: Drones and units"],
            ),
            (Race::Zerg, CallKind::Gas) => (
                &["Gas is piling up. Gas units and upgrades.", "Too much gas. Three Drones to minerals."],
                &["Gas {gas}: gas units and upgrades", "Gas {gas}: 3 Drones to minerals"],
            ),
        };
        Some(w)
    }

    /// Does this race ever hear this kind?
    pub fn speaks(self, race: Race) -> bool {
        self.wording(race).is_some()
    }
}

/// (clip phrasings, page-text templates) of a kind for a race.
pub type Wording = (&'static [&'static str], &'static [&'static str]);

/// Every clip phrasing once: Terran (kind order), then Protoss, then Zerg (what `render_brain.py` pre-renders).
pub fn all_clip_texts() -> Vec<&'static str> {
    let mut out: Vec<&'static str> = Vec::new();
    for race in Race::ALL {
        for k in CallKind::ALL {
            for c in k.wording(race).map_or(&[][..], |w| w.0) {
                if !out.contains(c) {
                    out.push(c);
                }
            }
        }
    }
    out
}

/// The rules, checked in this order after the timed reminders.
pub const RULE_ORDER: [CallKind; 12] = [
    CallKind::LostCc,
    CallKind::LostDepot,
    CallKind::Depot,
    CallKind::Depots,
    CallKind::Cc,
    CallKind::Stuck,
    CallKind::Orbital,
    CallKind::GasIdle,
    CallKind::BankBig,
    CallKind::Bank,
    CallKind::GasEarly,
    CallKind::Gas,
];
/// Timed reminders, in `TIMED` order.
pub const TIMED_ORDER: [CallKind; 3] = [CallKind::Turret, CallKind::Bunker, CallKind::Stall];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Opening,
    Stall,
    Mid,
}

/// Derived facts at the latest sample (`describe`). Windowed facts use the last 60 s / 30 s / 20 s;
/// `cc_started` and `gas_seen` look at the whole history (one-way flags).
#[derive(Clone, Debug, PartialEq)]
pub struct Facts {
    /// round(t), ties to even (Python's round).
    pub clock_s: i64,
    pub phase: Phase,
    pub matchup: String,
    /// The followed player's race: wording, and which kinds are said.
    pub race: Race,
    pub minerals: i64,
    pub gas: i64,
    pub used: i64,
    pub cap: i64,
    pub cap_headroom: i64,
    /// Headroom that warrants a depot: 4 while cap <= 31, else 4 + used / 6.
    pub depot_room: i64,
    /// Minerals that count as a bank: 400, 500 from 5:00.
    pub bank_min: i64,
    pub supply_delta_30s: i64,
    /// Informational (no rule uses it); rounded to 0.1.
    pub growth_per_min: f64,
    pub gas_delta_30s: i64,
    pub gas_flat_30s: bool,
    pub bank_trend: i64,
    pub supply_drop_30s: i64,
    pub losing_fight: bool,
    /// `losing_fight` held at some read of the last 30 s: supply traded away and rebuilt reads flat, not stuck.
    pub fight_30s: bool,
    pub cap_dropped: i64,
    pub cap_jump: i64,
    pub cc_started: bool,
    pub gas_seen: bool,
    pub history_s: i64,
}

impl Facts {
    pub fn pile(&self, p: Pile) -> i64 {
        match p {
            Pile::Minerals => self.minerals,
            Pile::Gas => self.gas,
        }
    }
}

/// Python's `round(x)` for game clocks: half to even.
fn py_round(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// `_at`: the latest sample at or before t (else the earliest); stops at the first later sample.
fn at(hist: &[Sample], t: f64) -> &Sample {
    let mut best = &hist[0];
    for s in hist {
        if s.t <= t {
            best = s;
        } else {
            break;
        }
    }
    best
}

/// `describe(history, matchup)` for a Terran player; None for an empty history. History in time order.
pub fn describe(history: &[Sample], matchup: &str) -> Option<Facts> {
    describe_as(history, matchup, Race::Terran)
}

/// `describe(history, matchup, race)`.
pub fn describe_as(history: &[Sample], matchup: &str, race: Race) -> Option<Facts> {
    let now = history.last()?;
    let t = now.t;
    let win_first = history.iter().find(|s| s.t >= t - WINDOW).unwrap_or(now);
    let ago30 = at(history, t - 30.0);
    let last30 = || history.iter().filter(move |s| s.t >= t - 30.0);
    let last30_first = last30().next().unwrap_or(now);
    let span = t - win_first.t;
    // a single 300+ mineral drop between reads after 1:15 is the natural CC; a 14+ cap jump means a CC finished
    let cc_started = history.windows(2).any(|w| {
        let (a, b) = (&w[0], &w[1]);
        (b.t >= 75.0 && a.minerals - b.minerals >= 300 && b.t - a.t <= 10.0) || b.cap - a.cap >= 14
    });
    let max_used30 = last30().map(|s| s.used).max().unwrap_or(now.used);
    let max_cap30 = last30().map(|s| s.cap).max().unwrap_or(now.cap);
    let min_cap30 = last30().map(|s| s.cap).min().unwrap_or(now.cap);
    let fight_max = history.iter().filter(|s| s.t >= t - FIGHT_WINDOW).map(|s| s.used).max().unwrap_or(now.used);
    let gas_flat = t - last30_first.t >= 25.0 && last30().all(|s| s.gas == last30_first.gas);
    // losing_fight at each read of the last 30 s (its 20 s window reaches back at most 50 s)
    let lo = history.iter().position(|s| s.t >= t - 30.0 - FIGHT_WINDOW).unwrap_or(history.len() - 1);
    let first30 = history.iter().position(|s| s.t >= t - 30.0).unwrap_or(history.len() - 1);
    let fight_30s = (first30..history.len()).any(|j| {
        let tj = history[j].t;
        let peak = history[lo..=j].iter().filter(|s| s.t >= tj - FIGHT_WINDOW).map(|s| s.used).max();
        peak.unwrap_or(history[j].used) - history[j].used >= FIGHT_DROP
    });
    Some(Facts {
        clock_s: py_round(t),
        phase: if t < 120.0 {
            Phase::Opening
        } else if t < 180.0 {
            Phase::Stall
        } else {
            Phase::Mid
        },
        matchup: matchup.to_string(),
        race,
        minerals: now.minerals,
        gas: now.gas,
        used: now.used,
        cap: now.cap,
        cap_headroom: now.cap - now.used,
        depot_room: if now.cap <= 31 { 4 } else { 4 + now.used.div_euclid(6) },
        bank_min: if t < 300.0 { 400 } else { 500 },
        supply_delta_30s: now.used - ago30.used,
        growth_per_min: if span >= 10.0 {
            ((now.used - win_first.used) as f64 * 60.0 / span * 10.0).round_ties_even() / 10.0
        } else {
            0.0
        },
        gas_delta_30s: now.gas - ago30.gas,
        gas_flat_30s: gas_flat,
        bank_trend: now.minerals - ago30.minerals,
        supply_drop_30s: max_used30 - now.used,
        losing_fight: fight_max - now.used >= FIGHT_DROP,
        fight_30s,
        cap_dropped: (max_cap30 - now.cap).max(0),
        cap_jump: (now.cap - min_cap30).max(0),
        cc_started,
        gas_seen: history.iter().any(|s| s.gas > 0),
        history_s: py_round(t - history[0].t),
    })
}

/// Does `kind`'s rule hold for these facts? (Timed kinds have no rule: always false.)
pub fn rule_holds(kind: CallKind, f: &Facts) -> bool {
    let c = f.clock_s;
    match kind {
        // cap fell in the last 30 s past 1:00: by 14+ and not a multiple of 8 -> a CC died; by 8+ -> depot(s)
        CallKind::LostCc => c >= 60 && f.cap_dropped >= 14 && f.cap_dropped % 8 != 0,
        CallKind::LostDepot => c >= 60 && f.cap_dropped >= 8 && f.cap_dropped % 8 == 0,
        // 1:00+, cap < 200, headroom <= depot_room, supply grew >= 1 in 30 s
        CallKind::Depot => {
            c >= 60 && f.cap < 200 && f.used < 36 && f.cap_headroom <= f.depot_room && f.supply_delta_30s >= 1
        }
        CallKind::Depots => {
            c >= 60 && f.cap < 200 && f.used >= 36 && f.cap_headroom <= f.depot_room && f.supply_delta_30s >= 1
        }
        // 1:20-3:15, natural CC not started, minerals >= 350
        CallKind::Cc => (80..=195).contains(&c) && !f.cc_started && f.minerals >= 350,
        // 2:30+, supply not up over 30 s and not down 3+, not blocked, under 190
        CallKind::Stuck => {
            c >= 150
                && f.supply_delta_30s <= 0
                && f.supply_drop_30s <= 2
                && f.cap_headroom > 0
                && f.used < 190
                && f.history_s >= 30
        }
        // cap jumped 14+ in 30 s, not a multiple of 8, 2:00-7:00 -> a CC finished
        CallKind::Orbital => (120..420).contains(&c) && f.cap_jump >= 14 && f.cap_jump % 8 != 0,
        // gas mined before, the exact same gas for 30 s, under 300, from 1:40
        CallKind::GasIdle => c >= 100 && f.gas_seen && f.gas_flat_30s && f.gas < 300,
        CallKind::BankBig => c >= 150 && f.minerals >= 1000,
        CallKind::Bank => c >= 150 && f.minerals >= f.bank_min && f.bank_trend > -100 && (f.cc_started || c > 195),
        CallKind::GasEarly => (180..300).contains(&c) && f.gas >= 350 && f.gas_delta_30s >= 20,
        CallKind::Gas => c >= 300 && f.gas >= 350 && f.gas_delta_30s >= 20,
        CallKind::Turret | CallKind::Bunker | CallKind::Stall => false,
    }
}

/// The matchup letters for the timed reminders: unknown ("" or the helper's "?" for opponents it couldn't read, e.g.
/// a bot test game) counts as "T". Same as brain.py.
pub fn known_matchup(matchup: &str) -> &str {
    match matchup {
        "" | "?" => "T",
        m => m,
    }
}

/// Timed reminders whose window holds now for this matchup (unknown counts as "T"), in TIMED order.
pub fn timed_now(f: &Facts) -> impl Iterator<Item = CallKind> + '_ {
    let m = known_matchup(&f.matchup);
    TIMED_ORDER.into_iter().flat_map(move |k| {
        k.timed()
            .iter()
            .filter(move |(letters, a, b)| *a <= f.clock_s && f.clock_s <= *b && m.chars().any(|c| letters.contains(c)))
            .map(move |_| k)
    })
}

/// Page text of the n-th variant (0-based, wraps), in the facts' race wording (Terran for a kind it never hears).
pub fn render_text(kind: CallKind, f: &Facts, n: usize) -> String {
    let tpl = kind.wording(f.race).map_or(kind.spec().texts, |w| w.1);
    tpl[n % tpl.len()]
        .replace("{minerals}", &f.minerals.to_string())
        .replace("{gas}", &f.gas.to_string())
        .replace("{used}", &f.used.to_string())
        .replace("{cap}", &f.cap.to_string())
        .replace("{cap_dropped}", &f.cap_dropped.to_string())
}

/// One brain call: page text (with numbers) and the clip phrasing (no numbers) that plays.
#[derive(Clone, Debug, PartialEq)]
pub struct BrainCall {
    pub kind: CallKind,
    pub text: String,
    pub clip_text: &'static str,
    pub confidence: f64,
}

/// The last call of a spend family: when, and the pile then (None: unknown, e.g. a helper call with no reading).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SpendMark {
    pub t: f64,
    pub pile: Option<i64>,
}

/// Cooldowns and fire-once reminders (`RulesBrain`). `reset()` at a new game.
#[derive(Clone, Debug)]
pub struct RulesBrain {
    pub global_gap: f64,
    last: BTreeMap<CallKind, f64>,
    /// keyed by family (Bank / Gas)
    last_spend: BTreeMap<CallKind, SpendMark>,
    last_any: f64,
    fired_once: BTreeSet<CallKind>,
    count: BTreeMap<CallKind, usize>,
}

impl Default for RulesBrain {
    fn default() -> Self {
        RulesBrain::new(GLOBAL_GAP)
    }
}

impl RulesBrain {
    pub fn new(global_gap: f64) -> RulesBrain {
        RulesBrain {
            global_gap,
            last: BTreeMap::new(),
            last_spend: BTreeMap::new(),
            last_any: -1e9,
            fired_once: BTreeSet::new(),
            count: BTreeMap::new(),
        }
    }

    pub fn reset(&mut self) {
        *self = RulesBrain::new(self.global_gap);
    }

    pub fn last_spend(&self, family: CallKind) -> Option<SpendMark> {
        self.last_spend.get(&family.family()).copied()
    }

    pub fn fired_once(&self, kind: CallKind) -> bool {
        self.fired_once.contains(&kind)
    }

    /// A spend family may speak: never called, REPEAT_GAP passed, or the pile is ESCALATE x worse (from ESCALATE_GAP).
    pub fn spend_open(&self, fam: CallKind, t: f64, f: Option<&Facts>) -> bool {
        let Some(mark) = self.last_spend.get(&fam) else { return true };
        if t - mark.t >= REPEAT_GAP {
            return true;
        }
        let (Some(f), Some(pile0), Some(pile)) = (f, mark.pile, fam.pile()) else { return false };
        if t - mark.t < ESCALATE_GAP {
            return false;
        }
        f.pile(pile) as f64 >= ESCALATE * pile0.max(1) as f64
    }

    /// Kinds off cooldown at t, in kind order. With facts, spend kinds escalate early and hold while losing a fight
    /// ("stuck" also while one was lost in the last 30 s), and kinds the facts' race never hears are left out.
    pub fn available(&self, t: f64, f: Option<&Facts>) -> Vec<CallKind> {
        let busy = f.is_some_and(|f| f.losing_fight);
        // "stuck" over a window with a lost fight in it is trading, not a stall
        let fought = f.is_some_and(|f| f.fight_30s);
        CallKind::ALL
            .into_iter()
            .filter(|&kind| {
                let fam = kind.family();
                if f.is_some_and(|f| !kind.speaks(f.race)) || (kind == CallKind::Stuck && (busy || fought)) {
                    false
                } else if kind.is_timed() {
                    !self.fired_once.contains(&kind)
                } else if fam.pile().is_some() {
                    !busy && self.spend_open(fam, t, f)
                } else {
                    let cd = kind.spec().cooldown;
                    let cd = if cd == 0.0 { COOLDOWN } else { cd };
                    t - self.last.get(&kind).copied().unwrap_or(-1e9) >= cd
                }
            })
            .collect()
    }

    pub fn gap_open(&self, t: f64) -> bool {
        t - self.last_any >= self.global_gap
    }

    /// Something else spoke (the coach: kind None; a helper call of the same habit: its kind): respect the gaps.
    /// `sample` (the latest reading) lets a helper bank call set the pile the brain escalates from.
    pub fn note_external(&mut self, t: f64, kind: Option<CallKind>, sample: Option<&Sample>) {
        self.last_any = self.last_any.max(t);
        if let Some(kind) = kind {
            self.last.insert(kind, t);
            let fam = kind.family();
            if let Some(pile) = fam.pile() {
                let pile = sample.map(|s| match pile {
                    Pile::Minerals => s.minerals,
                    Pile::Gas => s.gas,
                });
                self.last_spend.insert(fam, SpendMark { t, pile });
            }
        }
    }

    /// The rule decision among available kinds; no state change.
    pub fn pick(&self, f: &Facts, avail: &[CallKind]) -> Option<CallKind> {
        if let Some(k) = timed_now(f).find(|k| avail.contains(k)) {
            return Some(k);
        }
        RULE_ORDER.into_iter().find(|&k| avail.contains(&k) && rule_holds(k, f))
    }

    pub fn commit(&mut self, kind: CallKind, f: &Facts, confidence: f64) -> BrainCall {
        let t = f.clock_s as f64;
        self.last.insert(kind, t);
        self.last_any = t;
        if kind.is_timed() {
            self.fired_once.insert(kind);
        }
        let fam = kind.family();
        if let Some(pile) = fam.pile() {
            self.last_spend.insert(fam, SpendMark { t, pile: Some(f.pile(pile)) });
        }
        let n = self.count.entry(kind).or_insert(0);
        *n += 1;
        // available() never offers a kind the race doesn't hear; Terran wording if a caller forces one
        let clips = kind.wording(f.race).map_or(kind.spec().clips, |w| w.0);
        BrainCall {
            kind,
            text: render_text(kind, f, *n - 1),
            clip_text: clips[(*n - 1) % clips.len()],
            confidence: (confidence * 1000.0).round() / 1000.0,
        }
    }

    pub fn decide_facts(&mut self, f: &Facts) -> Option<BrainCall> {
        let t = f.clock_s as f64;
        let mut avail = self.available(t, Some(f));
        if !self.gap_open(t) {
            avail.retain(|k| k.is_urgent());
        }
        let kind = self.pick(f, &avail)?;
        Some(self.commit(kind, f, 1.0))
    }

    /// `decide(history, matchup)` for a Terran player.
    pub fn decide(&mut self, history: &[Sample], matchup: &str) -> Option<BrainCall> {
        self.decide_as(history, matchup, Race::Terran)
    }

    /// `decide(history, matchup, race=race)`.
    pub fn decide_as(&mut self, history: &[Sample], matchup: &str, race: Race) -> Option<BrainCall> {
        let f = describe_as(history, matchup, race)?;
        self.decide_facts(&f)
    }
}

/// One game's history + the brain (`brain_run.Runner`, minus the /state parsing).
#[derive(Clone, Debug)]
pub struct BrainGame {
    pub brain: RulesBrain,
    hist: Vec<Sample>,
    last_t: f64,
}

impl Default for BrainGame {
    fn default() -> Self {
        BrainGame { brain: RulesBrain::default(), hist: Vec::new(), last_t: -1.0 }
    }
}

impl BrainGame {
    pub fn new() -> BrainGame {
        BrainGame::default()
    }

    pub fn reset(&mut self) {
        self.brain.reset();
        self.hist.clear();
        self.last_t = -1.0;
    }

    pub fn history(&self) -> &[Sample] {
        &self.hist
    }

    /// One poll of the helper's state: clock `t`, the panel (None: unread), the matchup key, the followed player's
    /// race, and what others said since the last poll (`external`: Some(kind) for a brain kind said by the helper,
    /// None for anything else).
    pub fn feed(
        &mut self,
        t: f64,
        panel: Option<&Panel>,
        matchup: &str,
        race: Race,
        external: &[Option<CallKind>],
    ) -> Option<BrainCall> {
        if t < self.last_t - REWIND {
            self.reset();
        }
        if let Some(&last) = self.hist.last() {
            for kind in external {
                self.brain.note_external(t, *kind, Some(&last));
            }
        }
        let p = panel?;
        if t <= self.last_t {
            return None;
        }
        self.last_t = t;
        self.hist.push(Sample { t, minerals: p.minerals, gas: p.gas, used: p.used, cap: p.cap });
        if self.hist.len() > HISTORY_MAX {
            self.hist.drain(..HISTORY_TRIM);
        }
        self.brain.decide_as(&self.hist, matchup, race)
    }
}

#[cfg(test)]
mod tests {
    //! The cases of live/test_brain.py, one for one.
    use super::*;

    fn hist(points: &[(f64, i64, i64, i64, i64)]) -> Vec<Sample> {
        points.iter().map(|&(t, minerals, gas, used, cap)| Sample { t, minerals, gas, used, cap }).collect()
    }
    fn flat(t0: i64, t1: i64, m: i64, g: i64, used: i64, cap: i64) -> Vec<Sample> {
        (t0..=t1).step_by(5).map(|t| Sample { t: t as f64, minerals: m, gas: g, used, cap }).collect()
    }
    fn cat(a: &[Sample], b: &[Sample]) -> Vec<Sample> {
        a.iter().chain(b).copied().collect()
    }
    fn first(h: &[Sample], matchup: &str) -> Option<CallKind> {
        RulesBrain::default().decide(h, matchup).map(|c| c.kind)
    }
    fn bank_hist() -> Vec<Sample> {
        hist(&[(230.0, 450, 100, 30, 46), (240.0, 480, 110, 31, 46), (250.0, 520, 120, 32, 46)])
    }

    #[test]
    fn describe_facts() {
        let h = hist(&[(100.0, 300, 50, 18, 23), (110.0, 350, 60, 19, 23), (130.0, 420, 90, 20, 23)]);
        let f = describe(&h, "T").unwrap();
        assert!(f.supply_delta_30s == 2 && f.cap_headroom == 3 && f.gas_delta_30s == 40);
        assert!(f.phase == Phase::Stall && f.matchup == "T" && !f.cc_started);
        assert!(f.depot_room == 4 && f.bank_min == 400);
        assert!(describe(&[], "T").is_none());
    }

    #[test]
    fn clock_rounds_half_to_even_like_python() {
        let f = |t| describe(&hist(&[(t, 0, 0, 12, 15)]), "").unwrap().clock_s;
        assert_eq!((f(122.5), f(123.5), f(59.5), f(60.5)), (122, 124, 60, 60));
    }

    #[test]
    fn depot() {
        let h = hist(&[(70.0, 200, 10, 17, 23), (85.0, 250, 30, 18, 23), (100.0, 280, 40, 19, 23)]);
        assert_eq!(first(&h, "T"), Some(CallKind::Depot));
        let c = RulesBrain::default().decide(&h, "T").unwrap();
        assert_eq!(c.text, "Depot now, 19 of 23");
        assert!(!c.clip_text.chars().any(|ch| ch.is_ascii_digit()));
        assert_eq!(first(&hist(&[(70.0, 100, 10, 14, 23), (100.0, 120, 40, 15, 23)]), "T"), None);
    }

    #[test]
    fn cc_then_not_after_spend() {
        let h = hist(&[(80.0, 300, 30, 19, 23), (85.0, 330, 40, 19, 23), (90.0, 370, 50, 19, 23)]);
        assert_eq!(first(&h, "T"), Some(CallKind::Cc));
        let spent = cat(&h, &hist(&[(95.0, 20, 60, 19, 23), (100.0, 380, 70, 19, 23)]));
        assert!(describe(&spent, "").unwrap().cc_started);
        assert_ne!(first(&spent, "T"), Some(CallKind::Cc));
    }

    #[test]
    fn stuck_and_fight() {
        assert_eq!(first(&flat(150, 200, 200, 100, 28, 46), "T"), Some(CallKind::Stuck));
        let fight = hist(&[(170.0, 200, 100, 40, 62), (180.0, 200, 120, 34, 62), (200.0, 200, 150, 34, 62)]);
        assert_eq!(first(&fight, "T"), None);
    }

    /// Bot game 39, 3:49-4:47: 48 -> 33 in one fight at ~4:17, rebuilt to 42 (test_brain.py TRADE).
    fn trade() -> Vec<Sample> {
        hist(&[
            (229.0, 40, 0, 40, 55),
            (234.0, 15, 0, 42, 55),
            (239.0, 40, 0, 43, 55),
            (244.0, 6, 0, 45, 55),
            (249.0, 27, 0, 46, 55),
            (253.0, 0, 0, 48, 63),
            (256.5, 5, 0, 44, 63),
            (257.5, 10, 0, 38, 63),
            (258.0, 21, 0, 33, 63),
            (263.0, 45, 0, 34, 63),
            (268.0, 18, 0, 36, 63),
            (273.0, 45, 0, 37, 63),
            (278.0, 29, 0, 39, 63),
            (283.0, 8, 0, 41, 63),
            (287.0, 35, 0, 42, 63),
        ])
    }

    #[test]
    fn stuck_holds_after_a_lost_fight() {
        let h = trade();
        let f = describe(&h, "T").unwrap();
        assert!(rule_holds(CallKind::Stuck, &f));
        assert!(!f.losing_fight && f.fight_30s);
        let mut b = RulesBrain::default();
        let avail = b.available(f.clock_s as f64, Some(&f));
        assert!(!avail.contains(&CallKind::Stuck));
        assert!(b.decide(&h, "T").is_none());
        for k in [CallKind::Depot, CallKind::Depots, CallKind::LostDepot, CallKind::LostCc] {
            assert!(avail.contains(&k));
        }
        let calm: Vec<Sample> = h.iter().map(|s| Sample { used: 42, ..*s }).collect();
        assert!(!describe(&calm, "").unwrap().fight_30s);
        assert_eq!(first(&calm, "T"), Some(CallKind::Stuck));
        let later = cat(&h, &flat(292, 320, 50, 0, 42, 63));
        assert!(!describe(&later, "").unwrap().fight_30s);
        assert_eq!(first(&later, "Z"), Some(CallKind::Stuck));
        let mid = describe(&h[..9], "").unwrap();
        assert!(mid.losing_fight && !b.available(258.0, Some(&mid)).contains(&CallKind::Stuck));
    }

    #[test]
    fn bank_and_big_bank() {
        assert_eq!(first(&bank_hist(), "T"), Some(CallKind::Bank));
        let big = hist(&[(400.0, 1100, 100, 60, 78), (410.0, 1200, 110, 61, 78), (420.0, 1300, 120, 62, 78)]);
        assert_eq!(first(&big, "T"), Some(CallKind::BankBig));
    }

    #[test]
    fn lost_depot_beats_gap_and_cc_loss() {
        let mut b = RulesBrain::default();
        let h = hist(&[(390.0, 800, 100, 51, 78), (395.0, 900, 100, 51, 78)]);
        assert_eq!(b.decide(&h, "T").unwrap().kind, CallKind::Bank);
        let lost = cat(&h, &hist(&[(400.0, 950, 100, 51, 70)]));
        assert_eq!(b.decide(&lost, "T").unwrap().kind, CallKind::LostDepot);
        let cc = hist(&[(250.0, 600, 280, 34, 46), (260.0, 620, 300, 31, 46), (270.0, 840, 360, 26, 31)]);
        assert_eq!(first(&cc, "Z"), Some(CallKind::LostCc));
        let c = RulesBrain::default().decide(&cc, "Z").unwrap();
        assert_eq!(c.text, "Lost a Command Center (15 cap): army home");
    }

    #[test]
    fn gas() {
        let h = hist(&[(330.0, 200, 330, 50, 62), (345.0, 210, 360, 51, 70), (360.0, 220, 380, 52, 70)]);
        assert_eq!(first(&h, "T"), Some(CallKind::Gas));
        let early = hist(&[(250.0, 200, 330, 30, 46), (265.0, 210, 360, 31, 46), (280.0, 220, 380, 32, 46)]);
        assert_eq!(first(&early, "T"), Some(CallKind::GasEarly));
    }

    #[test]
    fn timed_once_and_matchup() {
        let mut b = RulesBrain::default();
        let h: Vec<Sample> = flat(280, 305, 100, 100, 40, 62)
            .into_iter()
            .enumerate()
            .map(|(i, s)| Sample { used: s.used + i as i64, ..s })
            .collect();
        assert_eq!(b.decide(&h, "T").unwrap().kind, CallKind::Turret);
        let last = *h.last().unwrap();
        let again = b.decide(&cat(&h, &[Sample { t: 330.0, used: last.used + 1, ..last }]), "T");
        assert!(again.is_none_or(|c| c.kind != CallKind::Turret));
        assert!(b.fired_once(CallKind::Turret) && !b.available(400.0, None).contains(&CallKind::Turret));
        assert_ne!(first(&h, "Z"), Some(CallKind::Turret));
    }

    #[test]
    fn unknown_matchup_counts_as_terran() {
        // the helper sends key "?" when the opponents are unknown (bot test game)
        assert_eq!(known_matchup(""), "T");
        assert_eq!(known_matchup("?"), "T");
        assert_eq!(known_matchup("T?"), "T?");
        let h: Vec<Sample> = flat(280, 305, 100, 100, 40, 62)
            .into_iter()
            .enumerate()
            .map(|(i, s)| Sample { used: s.used + i as i64, ..s })
            .collect();
        assert_eq!(first(&h, "?"), Some(CallKind::Turret));
    }

    #[test]
    fn cooldowns() {
        let mut b = RulesBrain::default();
        let h = bank_hist();
        assert_eq!(b.decide(&h, "T").unwrap().kind, CallKind::Bank);
        assert!(b.decide(&cat(&h, &hist(&[(260.0, 560, 130, 33, 46)])), "T").is_none());
        assert!(b.decide(&cat(&h, &hist(&[(275.0, 600, 140, 34, 46)])), "T").is_none());
        assert!(b.decide(&cat(&h, &hist(&[(285.0, 600, 140, 34, 46)])), "T").is_none());
        assert_eq!(b.decide(&cat(&h, &hist(&[(340.0, 600, 140, 35, 46)])), "T").unwrap().kind, CallKind::Bank);
        assert!(all_clip_texts().iter().all(|p| !p.chars().any(|c| c.is_ascii_digit())));
    }

    #[test]
    fn bank_escalates_only_when_worse() {
        let mut b = RulesBrain::default();
        let bank = bank_hist();
        assert_eq!(b.decide(&bank, "T").unwrap().kind, CallKind::Bank);
        assert_eq!(b.last_spend(CallKind::Bank), Some(SpendMark { t: 250.0, pile: Some(520) }));
        assert!(b.decide(&cat(&bank, &hist(&[(275.0, 800, 130, 33, 46)])), "T").is_none());
        assert!(b.decide(&cat(&bank, &hist(&[(285.0, 770, 130, 33, 46)])), "T").is_none());
        let c = b.decide(&cat(&bank, &hist(&[(285.0, 790, 130, 33, 46)])), "T").unwrap();
        assert_eq!(c.kind, CallKind::Bank);
        assert_eq!(b.last_spend(CallKind::Bank), Some(SpendMark { t: 285.0, pile: Some(790) }));
        let mut b2 = RulesBrain::default();
        b2.decide(&hist(&[(390.0, 700, 100, 51, 78), (400.0, 720, 100, 52, 78)]), "T");
        assert!(b2.decide(&hist(&[(390.0, 700, 100, 51, 78), (435.0, 1050, 100, 53, 78)]), "T").is_none());
        let big = b2.decide(&hist(&[(390.0, 700, 100, 51, 78), (435.0, 1100, 100, 53, 78)]), "T").unwrap();
        assert_eq!(big.kind, CallKind::BankBig);
    }

    #[test]
    fn gas_family_shares_cooldown() {
        let mut b = RulesBrain::default();
        let early = hist(&[(250.0, 200, 330, 30, 46), (265.0, 210, 360, 31, 46), (280.0, 220, 380, 32, 46)]);
        assert_eq!(b.decide(&early, "Z").unwrap().kind, CallKind::GasEarly);
        let later = cat(&early, &hist(&[(305.0, 220, 420, 33, 46), (330.0, 220, 480, 34, 46)]));
        assert!(b.decide(&later, "Z").is_none());
        let worse = cat(&early, &hist(&[(305.0, 220, 420, 33, 46), (330.0, 220, 600, 34, 46)]));
        assert_eq!(b.decide(&worse, "Z").unwrap().kind, CallKind::Gas);
    }

    #[test]
    fn wording_rotates_with_matching_text() {
        for kind in [CallKind::Bank, CallKind::BankBig, CallKind::Gas, CallKind::GasEarly] {
            let s = kind.spec();
            assert!((2..=4).contains(&s.clips.len()) && s.texts.len() == s.clips.len(), "{kind:?}");
        }
        for kind in CallKind::ALL {
            let s = kind.spec();
            assert!(s.texts.len() == 1 || s.texts.len() == s.clips.len(), "{kind:?}");
        }
        let mut b = RulesBrain::default();
        let calls: Vec<BrainCall> = (0..4)
            .map(|i| {
                let t = 250.0 + 90.0 * i as f64;
                b.decide(&hist(&[(t - 20.0, 520, 100, 30 + i, 62), (t, 520, 100, 31 + i, 62)]), "T").unwrap()
            })
            .collect();
        assert!(calls.iter().all(|c| c.kind == CallKind::Bank));
        let clips: Vec<&str> = calls.iter().map(|c| c.clip_text).collect();
        assert!(clips[0] != clips[1] && clips[1] != clips[2] && clips[0] != clips[2] && clips[3] == clips[0]);
        let texts: Vec<&str> = calls[..3].iter().map(|c| c.text.as_str()).collect();
        assert_eq!(
            texts,
            ["520 banked: 2 Barracks", "Spend 520: 2 more Barracks", "520 banked: Starport, Barracks, Marines"]
        );
        assert!(clips.iter().all(|c| all_clip_texts().contains(c)));
    }

    #[test]
    fn spend_calls_hold_while_losing_a_fight() {
        let fight = hist(&[(390.0, 900, 500, 70, 94), (400.0, 950, 520, 66, 94), (410.0, 1000, 540, 58, 94)]);
        let f = describe(&fight, "T").unwrap();
        assert!(f.losing_fight);
        let mut b = RulesBrain::default();
        let avail = b.available(f.clock_s as f64, Some(&f));
        for k in [CallKind::Bank, CallKind::BankBig, CallKind::Gas, CallKind::GasEarly] {
            assert!(!avail.contains(&k));
        }
        for k in [CallKind::Depot, CallKind::Depots, CallKind::LostDepot, CallKind::LostCc] {
            assert!(avail.contains(&k));
        }
        assert!(b.decide(&fight, "T").is_none());
        let lost = cat(&fight, &hist(&[(415.0, 1000, 540, 57, 86)]));
        assert_eq!(b.decide(&lost, "T").unwrap().kind, CallKind::LostDepot);
        let after = cat(&lost, &hist(&[(440.0, 1100, 560, 59, 94)]));
        assert!(!describe(&after, "").unwrap().losing_fight);
        assert_eq!(b.decide(&after, "T").unwrap().kind, CallKind::BankBig);
        assert!(!describe(&hist(&[(390.0, 900, 100, 70, 94), (410.0, 900, 100, 61, 94)]), "").unwrap().losing_fight);
    }

    #[test]
    fn helper_bank_call_sets_the_pile() {
        let mut b = RulesBrain::default();
        let s = Sample { t: 250.0, minerals: 600, gas: 100, used: 0, cap: 0 };
        b.note_external(250.0, Some(CallKind::Bank), Some(&s));
        assert!(b.decide(&cat(&bank_hist(), &hist(&[(285.0, 650, 120, 33, 46)])), "T").is_none());
        assert_eq!(
            b.decide(&cat(&bank_hist(), &hist(&[(285.0, 900, 120, 33, 46)])), "T").unwrap().kind,
            CallKind::Bank
        );
        b.reset();
        b.note_external(250.0, Some(CallKind::Bank), None);
        assert!(b.decide(&cat(&bank_hist(), &hist(&[(285.0, 2000, 120, 33, 46)])), "T").is_none());
        assert_eq!(
            b.decide(&cat(&bank_hist(), &hist(&[(340.0, 600, 120, 33, 46)])), "T").unwrap().kind,
            CallKind::Bank
        );
    }

    #[test]
    fn game_feed_like_the_runner() {
        // brain_run's test: three polls, one depot call; same clock again adds nothing; a rewind resets
        let mut g = BrainGame::new();
        let p = |m, u| Panel { minerals: m, gas: 40, used: u, cap: 23 };
        assert!(g.feed(70.0, Some(&p(200, 17)), "T", Race::Terran, &[]).is_none());
        assert!(g.feed(85.0, Some(&p(250, 18)), "T", Race::Terran, &[]).is_none());
        let c = g.feed(100.0, Some(&p(280, 19)), "T", Race::Terran, &[]).unwrap();
        assert_eq!((c.kind, c.text.as_str()), (CallKind::Depot, "Depot now, 19 of 23"));
        assert!(g.feed(100.0, Some(&p(280, 19)), "T", Race::Terran, &[]).is_none());
        assert_eq!(g.history().len(), 3);
        assert!(g.feed(101.0, None, "T", Race::Terran, &[]).is_none());
        assert!(g.feed(10.0, Some(&p(50, 12)), "T", Race::Terran, &[]).is_none());
        assert_eq!(g.history().len(), 1);
    }

    #[test]
    fn external_says_hold_the_gap() {
        let mut g = BrainGame::new();
        let p = |t: f64, m, u| (t, Panel { minerals: m, gas: 40, used: u, cap: 23 });
        for (t, panel) in [p(70.0, 200, 17), p(85.0, 250, 18)] {
            assert!(g.feed(t, Some(&panel), "T", Race::Terran, &[]).is_none());
        }
        // the coach spoke just now: the depot call waits for the 20 s global gap
        let (t, panel) = p(100.0, 280, 19);
        assert!(g.feed(t, Some(&panel), "T", Race::Terran, &[None]).is_none());
        // no history yet: an external is ignored (Runner: `if self.hist`)
        let mut fresh = BrainGame::new();
        fresh.feed(5.0, None, "T", Race::Terran, &[Some(CallKind::Bank)]);
        assert!(fresh.brain.last_spend(CallKind::Bank).is_none());
    }

    #[test]
    fn history_is_trimmed_like_the_runner() {
        let mut g = BrainGame::new();
        let p = Panel { minerals: 50, gas: 0, used: 12, cap: 15 };
        for i in 0..=HISTORY_MAX {
            g.feed(i as f64 * 0.5, Some(&p), "T", Race::Terran, &[]);
        }
        assert_eq!(g.history().len(), HISTORY_MAX + 1 - HISTORY_TRIM);
        assert_eq!(g.history()[0].t, HISTORY_TRIM as f64 * 0.5);
    }

    #[test]
    fn kinds_round_trip() {
        for k in CallKind::ALL {
            assert_eq!(CallKind::parse(k.as_str()), Some(k));
        }
        assert_eq!(CallKind::parse("blocked"), None);
        assert_eq!(CallKind::parse("brain-bank"), None);
    }

    // ---------- race (test_brain.py's race tests, one for one) ----------

    fn call_as(h: &[Sample], matchup: &str, race: Race) -> Option<BrainCall> {
        RulesBrain::default().decide_as(h, matchup, race)
    }
    fn growing(t0: i64, t1: i64, m: i64, g: i64, used: i64, cap: i64) -> Vec<Sample> {
        flat(t0, t1, m, g, used, cap)
            .into_iter()
            .enumerate()
            .map(|(i, s)| Sample { used: s.used + i as i64, ..s })
            .collect()
    }

    #[test]
    fn supply_and_worker_words_follow_the_race() {
        let h = hist(&[(70.0, 200, 10, 17, 23), (85.0, 250, 30, 18, 23), (100.0, 280, 40, 19, 23)]);
        let said = |r| call_as(&h, "T", r).map(|c| (c.kind, c.text, c.clip_text));
        assert_eq!(said(Race::Protoss), Some((CallKind::Depot, "Pylon now, 19 of 23".into(), "Pylon now.")));
        assert_eq!(said(Race::Zerg), Some((CallKind::Depot, "Overlord now, 19 of 23".into(), "Overlord now.")));
        assert_eq!(said(Race::Terran), Some((CallKind::Depot, "Depot now, 19 of 23".into(), "Depot now.")));
        let stuck = flat(150, 200, 200, 100, 28, 46);
        assert_eq!(call_as(&stuck, "T", Race::Protoss).unwrap().text, "Supply stuck at 28: queue Probes");
        assert_eq!(call_as(&stuck, "T", Race::Zerg).unwrap().text, "Supply stuck at 28: make Drones");
        let mut b = RulesBrain::default();
        let h = hist(&[(390.0, 800, 100, 51, 78), (395.0, 900, 100, 51, 78)]);
        assert_eq!(b.decide_as(&h, "T", Race::Protoss).unwrap().text, "900 banked: 2 Gateways");
        let lost = b.decide_as(&cat(&h, &hist(&[(400.0, 950, 100, 51, 70)])), "T", Race::Protoss).unwrap();
        assert_eq!((lost.kind, lost.text.as_str()), (CallKind::LostDepot, "Lost a pylon (8 cap), check your main"));
        let gas = hist(&[(330.0, 200, 330, 50, 62), (345.0, 210, 360, 51, 70), (360.0, 220, 380, 52, 70)]);
        let c = call_as(&gas, "T", Race::Zerg).unwrap();
        assert_eq!((c.kind, c.clip_text), (CallKind::Gas, "Gas is piling up. Gas units and upgrades."));
    }

    #[test]
    fn terran_only_kinds_are_never_said_for_protoss_or_zerg() {
        let cc = hist(&[(80.0, 300, 30, 19, 23), (85.0, 330, 40, 19, 23), (90.0, 370, 50, 19, 23)]);
        let orbital = hist(&[(190.0, 300, 100, 30, 31), (200.0, 320, 110, 31, 46)]);
        let stall = flat(150, 160, 100, 100, 30, 46);
        let turret = growing(280, 305, 100, 100, 40, 62);
        let bunker = growing(165, 190, 100, 100, 20, 46);
        let kind = |h: &[Sample], m: &str, r| call_as(h, m, r).map(|c| c.kind);
        assert_eq!(kind(&cc, "T", Race::Terran), Some(CallKind::Cc));
        assert_eq!(kind(&orbital, "T", Race::Terran), Some(CallKind::Orbital));
        assert_eq!(kind(&stall, "?", Race::Terran), Some(CallKind::Stall));
        assert_eq!(kind(&turret, "T", Race::Terran), Some(CallKind::Turret));
        assert_eq!(kind(&bunker, "Z", Race::Terran), Some(CallKind::Bunker));
        for r in [Race::Protoss, Race::Zerg] {
            for (h, m, k) in [
                (&cc, "T", CallKind::Cc),
                (&orbital, "T", CallKind::Orbital),
                (&stall, "?", CallKind::Stall),
                (&turret, "T", CallKind::Turret),
                (&bunker, "Z", CallKind::Bunker),
            ] {
                assert_ne!(kind(h, m, r), Some(k), "{r:?} {k:?}");
            }
        }
        // a lost Nexus is called (15 cap); Zerg has no lost-CC call (a Hatchery is 6 supply)
        let lost = hist(&[(250.0, 600, 280, 34, 46), (260.0, 620, 300, 31, 46), (270.0, 840, 360, 26, 31)]);
        let c = call_as(&lost, "Z", Race::Protoss).unwrap();
        assert_eq!((c.kind, c.text.as_str()), (CallKind::LostCc, "Lost a Nexus (15 cap): army home"));
        assert_ne!(kind(&lost, "Z", Race::Zerg), Some(CallKind::LostCc));
        let avail = RulesBrain::default().available(100.0, Some(&describe_as(&cc, "T", Race::Zerg).unwrap()));
        assert!(![CallKind::Cc, CallKind::Orbital, CallKind::LostCc, CallKind::Stall]
            .iter()
            .any(|k| avail.contains(k)));
    }

    #[test]
    fn race_wording_has_no_terran_words_or_digits() {
        const TERRAN: [&str; 14] = [
            "SCV", "Marine", "Barracks", "depot", "Depot", "Command", "CC", "Orbital", "Bunker", "Turret", "refinery",
            "Tank", "Medivac", "Stim",
        ];
        for r in [Race::Protoss, Race::Zerg] {
            for k in CallKind::ALL {
                let Some((clips, texts)) = k.wording(r) else { continue };
                assert!(texts.len() == 1 || texts.len() == clips.len(), "{r:?} {k:?}");
                for s in clips.iter().chain(texts) {
                    assert!(!TERRAN.iter().any(|w| s.contains(w)), "{r:?} {k:?}: {s}");
                }
                assert!(clips.iter().all(|c| !c.chars().any(|ch| ch.is_ascii_digit()) && all_clip_texts().contains(c)));
            }
        }
        // Terran wording is unchanged: the spec
        for k in CallKind::ALL {
            let (clips, texts) = k.wording(Race::Terran).unwrap();
            assert!(clips == k.spec().clips && texts == k.spec().texts);
        }
        assert_eq!(all_clip_texts().len(), 60); // same count as test_brain.py
    }
}
