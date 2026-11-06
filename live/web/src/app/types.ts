// Shapes served by the helper (live/timing.py). Keep in step with State.data, load_plans() and REC.recent_notes().

export interface Step {
  sec: number;
  t: string;
  supply?: number | null;
  action: string;
  note?: string;
}

export interface Plan {
  name: string;
  title: string;
  hit: string;
  steps: Step[];
  [k: string]: unknown;
}

export interface Plans {
  patch?: string;
  matchups: Record<string, string[]>;
  plans: Record<string, Plan>;
  focus?: string[];
}

export interface Panel {
  minerals: number;
  gas: number;
  used: number;
  cap: number;
}

export interface Say {
  id: number;
  t: string;
  text: string;
  kind: string;
  from: 'helper' | 'coach' | string;
  wall?: number;
  audio?: string;
}

export interface CueEvent {
  id: number;
  type: 'target' | 'now' | string;
  keys: string[];
  sec?: number;
}

export interface State {
  in_game: boolean;
  clock: number;
  clock_wall: number;
  now: number;
  plan: string | null;
  plan_name: string;
  coach_plan?: string | null;
  cue_engine?: boolean;
  cues?: CueEvent[];
  lateness?: number;
  shift?: number;
  vs: string;
  key: string;
  player?: string; // whose game the helper follows: Dickie, or a sparring bot (bot/run.py --coach-test)
  race?: string; // that player's race, "T" | "P" | "Z" (the booth brain's wording)
  panel: Panel | null;
  plan_supply: number | null;
  says: Say[];
  trace?: boolean; // the helper runs with SC2_TIMING_TRACE=1: report delays (trace.ts)
  [k: string]: unknown;
}

export interface Note {
  id: number;
  text: string;
  created_at?: number;
  game_id?: number | null;
  result?: string | null;
  enemies?: string | null;
  length_sec?: number | null;
}

export interface Manifest {
  calls: Record<string, string>;
  cues: Record<string, string>;
  now?: Record<string, string>;
  [k: string]: unknown;
}
