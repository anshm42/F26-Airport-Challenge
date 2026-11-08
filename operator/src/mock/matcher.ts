// MOCK, POTENTIALLY INCORRECT: a minimal greedy matcher standing in for the real one, so the view has gates to show.
// Only gate choices, their timing, assignment times and the waitlist come from here. Don't treat them as the real
// algorithm's behaviour or as operationally valid.
//
// It keeps the stream's physical rules (GateStateSpecification.md):
// - An aircraft holds one position for its whole ground visit: from arrival (or the start of the day, if parked at
//   initialization) until its next departure, or nightly reset if it has none. No buffers.
// - Positions sharing a conflictGroup can't be occupied at once, and an outage closes the whole group.
// - The aircraft's size class must be known and no larger than the position's.
// Its own choices: the carrier's terminal gates before remote stands (as the stream's generator reserves them), remote
// stands first for emergencies, turnbacks planned before ordinary flights, the smallest fitting size class first, and
// within it the position with the most free time either side.
// A waiting aircraft gets the first position that frees up, ahead of later plans. An aircraft already at a gate is only
// towed when an outage starts there.

import { RANK, type Resource, closedBy, widthOf } from "../inventory";
import { terminalOf } from "./catalog";
import type { Aircraft, FlightRec, Schedule } from "./schedule";

/** A stretch of a ground visit at one position. */
export interface Segment {
  res: number;
  start: number;
  end: number;
  /** When the matcher chose this position. */
  assignedAt: number;
}

/** One aircraft's ground visit at YYZ. */
export interface Stay {
  /** The inbound flight's key, or "parked:<registration>" for an aircraft on the ground at initialization. */
  key: string;
  aircraft: Aircraft;
  inbound: FlightRec | null;
  outbound: FlightRec | null;
  start: number;
  end: number;
  /** Where the aircraft is or will be, in order; empty while it waits for a position. */
  segments: Segment[];
  /** A started visit without a position has been tried at every chance up to here. */
  triedUntil: number;
}

interface Booking {
  start: number;
  end: number;
  stay: Stay;
  seg: Segment;
}

const NO_GATE = "No compatible gate free";
const overlaps = (a0: number, a1: number, b0: number, b1: number) => a0 < b1 && b0 < a1;

export class Matcher {
  stays: Stay[] = [];
  private byKey = new Map<string, Stay>();
  private readonly footprint: string[];
  /** Resource ids by preference list and size tier, smallest class first. */
  private readonly tiers: Record<"gate1" | "gate3" | "gateAny" | "remote", number[][]>;
  /** Rotates the starting position within a tier, to break ties between equally free positions. */
  private turn = 0;
  private bookings = new Map<string, Booking[]>();
  private closed = new Map<string, [number, number][]>();

  constructor(
    readonly resources: Resource[],
    private readonly dayStart: number,
    private readonly dayEnd: number,
  ) {
    this.footprint = resources.map((r) => r.conflictGroup);
    const tiersOf = (match: (r: Resource) => boolean) => {
      const ids = resources.flatMap((r, i) => (match(r) && r.widthClass ? [i] : []));
      const ranks = [...new Set(ids.map((i) => RANK[resources[i].widthClass!]))].sort((a, b) => a - b);
      return ranks.map((k) => ids.filter((i) => RANK[resources[i].widthClass!] === k));
    };
    this.tiers = {
      gate1: tiersOf((r) => r.kind === "gate" && r.terminal === "1"),
      gate3: tiersOf((r) => r.kind === "gate" && r.terminal === "3"),
      gateAny: tiersOf((r) => r.kind === "gate"),
      remote: tiersOf((r) => r.kind === "remote"),
    };
  }

  /** Rebuilds ground visits from the schedule, keeping the plan of visits that still exist. */
  rebuild(schedule: Schedule) {
    // Per aircraft: its non-cancelled movements at YYZ, departures before arrivals at the same time (as the stream).
    const moves = new Map<string, { t: number; arriving: boolean; rec: FlightRec }[]>();
    const add = (tail: string, m: { t: number; arriving: boolean; rec: FlightRec }) => (moves.get(tail) ?? moves.set(tail, []).get(tail)!).push(m);
    for (const rec of schedule.flights.values()) {
      if (rec.cancelled) continue;
      if (rec.leg.departureStation === "YYZ") add(rec.aircraft.registration, { t: rec.dep, arriving: false, rec });
      if (rec.leg.arrivalStation === "YYZ") add(rec.aircraft.registration, { t: rec.arr, arriving: true, rec });
    }
    const parked = new Map(schedule.parked.map((p) => [p.registration, p]));
    const old = this.byKey;
    this.byKey = new Map();
    this.stays = [];

    const open = (key: string, aircraft: Aircraft, start: number, inbound: FlightRec | null): Stay => {
      const st = old.get(key) ?? { key, aircraft, inbound, outbound: null, start, end: this.dayEnd, segments: [], triedUntil: -Infinity };
      Object.assign(st, { aircraft, inbound, outbound: null, start, end: this.dayEnd });
      return st;
    };
    const close = (st: Stay, end: number, outbound: FlightRec | null) => {
      st.end = end;
      st.outbound = outbound;
      if (st.end > st.start) {
        this.stays.push(st);
        this.byKey.set(st.key, st);
      }
    };

    for (const tail of new Set([...moves.keys(), ...parked.keys()])) {
      const p = parked.get(tail);
      let cur: Stay | null = null;
      if (p) {
        cur = open(`parked:${tail}`, { carrier: p.carrier, registration: tail, aircraftType: p.aircraftType }, this.dayStart, null);
        const res = this.resources.findIndex((r) => r.gate === p.gate);
        if (!old.has(cur.key) && res >= 0) cur.segments = [{ res, start: this.dayStart, end: this.dayEnd, assignedAt: this.dayStart }];
      }
      for (const m of (moves.get(tail) ?? []).sort((a, b) => a.t - b.t || Number(a.arriving) - Number(b.arriving))) {
        if (!m.arriving) {
          // A departure ends the visit; one with no aircraft on the ground (a turnback's original leg) has nothing to end.
          if (cur) close(cur, m.t, m.rec);
          cur = null;
        } else {
          if (cur) close(cur, m.t, null);
          cur = open(m.rec.key, m.rec.aircraft, m.t, m.rec);
        }
      }
      if (cur) close(cur, this.dayEnd, null);
    }
  }

  /** Brings the plan up to date at `now`. */
  plan(now: number, schedule: Schedule) {
    this.closed = new Map();
    for (const o of schedule.outages) {
      const fp = closedBy(o.stand)[0]?.conflictGroup;
      if (fp) (this.closed.get(fp) ?? this.closed.set(fp, []).get(fp)!).push([o.start, o.end ?? Infinity]);
    }

    // Visits still to come follow their times: the first stretch starts at arrival, the last ends at departure.
    for (const st of this.stays) {
      if (st.end <= now || !st.segments.length) continue;
      if (st.start > now) st.segments[0].start = st.start;
      st.segments = st.segments.filter((g) => g.start < st.end);
      if (st.segments.length) st.segments.at(-1)!.end = st.end;
    }

    // 1. Where aircraft are (or were): fixed. Past visits stay as history.
    this.bookings = new Map();
    const isFuture = (g: Segment) => g.start > now;
    for (const st of this.stays) for (const g of st.segments) if (!isFuture(g)) this.book(g, st);

    // An aircraft at a position where an outage starts is towed to another one, if there is one.
    for (const st of this.stays) {
      const g = st.segments.at(-1);
      if (!g || isFuture(g) || g.end <= now) continue;
      const hit = (this.closed.get(this.footprint[g.res]) ?? []).find(([s, e]) => overlaps(s, e, Math.max(g.start, now), g.end));
      if (!hit) continue;
      const at = Math.max(hit[0], now);
      const r = this.find(st, at, g.end, this.footprint[g.res]);
      if (r < 0) continue;
      this.unbook(g);
      const tow = { res: r, start: at, end: g.end, assignedAt: now };
      g.end = at;
      if (g.end > g.start) this.book(g, st);
      else st.segments.pop();
      st.segments.push(tow);
      this.book(tow, st);
    }

    // 2. Plans for visits still to come are kept while they fit, turnbacks first. The rest are placed around them below,
    // so one broken plan doesn't take another's gate and set off a cascade of changes.
    const active = this.stays.filter((st) => st.end > now);
    const turnback = (st: Stay) => st.inbound?.emergencyState === "Turnback";
    const startOf = (st: Stay) => st.segments.at(-1)?.start ?? st.start;
    const order = (a: Stay, b: Stay) => Number(turnback(b)) - Number(turnback(a)) || startOf(a) - startOf(b);
    const queue: Stay[] = [];
    for (const st of active.filter((s) => s.segments.length && isFuture(s.segments.at(-1)!)).sort(order)) {
      const g = st.segments.at(-1)!;
      if (this.fits(g.res, st, g.start, g.end)) this.book(g, st);
      else {
        st.segments.pop();
        queue.push(st);
      }
    }

    // Later plans overlapping [from, to) on r that `which` accepts give way, and are placed again below.
    const evict = (r: number, from: number, to: number, which: (b: Booking) => boolean) => {
      const list = this.bookings.get(this.footprint[r]) ?? [];
      for (const b of list.filter((b) => b.seg.start > now && which(b) && overlaps(b.start, b.end, from, to))) {
        list.splice(list.indexOf(b), 1);
        b.stay.segments.pop();
        queue.push(b.stay);
      }
    };
    // Places a visit from `from`; with `bump`, it may take a later ordinary plan's position when nothing else is free.
    const bumpable = (b: Booking) => b.seg.start > now && !turnback(b.stay);
    const place = (st: Stay, from: number, assignedAt: number, bump: boolean): boolean => {
      let r = this.find(st, from, st.end);
      if (r < 0 && bump) {
        r = this.find(st, from, st.end, undefined, bumpable);
        if (r >= 0) evict(r, from, st.end, bumpable);
      }
      if (r < 0) return false;
      const g = { res: r, start: from, end: st.end, assignedAt };
      st.segments.push(g);
      this.book(g, st);
      return true;
    };

    // 3. Aircraft on the ground waiting for a position get the first one that frees up, else a later plan's.
    for (const st of active.filter((s) => !s.segments.length && s.start <= now).sort((a, b) => a.start - b.start)) {
      const times = new Set([now]);
      if (st.triedUntil < st.start) times.add(st.start);
      for (const list of [...this.bookings.values(), ...this.closed.values()])
        for (const b of list) {
          const t = Array.isArray(b) ? b[1] : b.end;
          if (t > st.triedUntil && t >= st.start && t < now) times.add(t);
        }
      if (![...times].sort((a, b) => a - b).some((t) => place(st, t, t, false))) place(st, now, now, true);
      st.triedUntil = now;
    }

    // 4. New and broken plans, turnbacks first; a turnback can take an ordinary flight's planned gate.
    queue.push(...active.filter((st) => !st.segments.length && st.start > now));
    queue.sort(order);
    for (let i = 0; i < queue.length; i++) {
      const st = queue[i];
      const from = st.segments.at(-1)?.end ?? st.start;
      if (place(st, from, now, turnback(st)) || !st.segments.length) continue;
      // A planned tow that no longer works: the aircraft stays where it is, and later plans there give way.
      const last = st.segments.at(-1)!;
      this.unbook(last);
      last.end = st.end;
      evict(last.res, last.start, last.end, (b) => b.stay !== st);
      this.book(last, st);
    }
  }

  private lastSignature = "";
  /** Whether any position changed since the last call. */
  changed(): boolean {
    const s = this.signature();
    const changed = s !== this.lastSignature;
    this.lastSignature = s;
    return changed;
  }

  /** The visit holding a flight, as inbound or outbound. */
  stayOf(): Map<string, Stay> {
    const out = new Map<string, Stay>();
    for (const st of this.stays) if (st.outbound) out.set(st.outbound.key, st);
    // A turnback leg (YYZ -> YYZ) is the arrival of its visit.
    for (const st of this.stays) if (st.inbound) out.set(st.inbound.key, st);
    return out;
  }

  /** Why a visit has no position. */
  reasonFor(st: Stay): string {
    const w = widthOf(st.aircraft.aircraftType);
    if (w === null) return `Unknown size for aircraft type ${st.aircraft.aircraftType}`;
    if (!this.resources.some((r) => r.widthClass && RANK[r.widthClass] >= RANK[w])) return `No position takes class ${w}`;
    return NO_GATE;
  }

  /** A position for the visit over [from, to), or -1. Bookings `ignore` accepts don't count as taken. */
  private find(st: Stay, from: number, to: number, avoid?: string, ignore?: (b: Booking) => boolean): number {
    const w = widthOf(st.aircraft.aircraftType);
    if (w === null) return -1;
    const term = terminalOf(st.aircraft.carrier);
    const gates = term === "1" ? this.tiers.gate1 : term === "3" ? this.tiers.gate3 : this.tiers.gateAny;
    const emergency = (st.inbound ?? st.outbound)?.emergencyState === "Emergency";
    for (const list of emergency ? [this.tiers.remote, gates] : [gates, this.tiers.remote]) {
      for (const ids of list) {
        if (RANK[this.resources[ids[0]].widthClass!] < RANK[w]) continue;
        // The fitting position with the most slack either side, so a small delay doesn't force a change.
        const o = this.turn++;
        let best = -1;
        let bestSlack = -1;
        for (let k = 0; k < ids.length; k++) {
          const r = ids[(o + k) % ids.length];
          if (this.footprint[r] === avoid || !this.fits(r, st, from, to, ignore)) continue;
          const slack = this.slack(r, st, from, to);
          if (slack > bestSlack) [best, bestSlack] = [r, slack];
        }
        if (best >= 0) return best;
      }
    }
    return -1;
  }

  /** Free time next to [from, to) on the position, the smaller side, capped at two hours. */
  private slack(r: number, st: Stay, from: number, to: number): number {
    let before = 2 * 3_600_000;
    let after = before;
    for (const b of this.bookings.get(this.footprint[r]) ?? []) {
      if (b.stay === st) continue;
      if (b.end <= from) before = Math.min(before, from - b.end);
      if (b.start >= to) after = Math.min(after, b.start - to);
    }
    return Math.min(before, after);
  }

  private fits(r: number, st: Stay, from: number, to: number, ignore?: (b: Booking) => boolean): boolean {
    const cap = this.resources[r].widthClass;
    const w = widthOf(st.aircraft.aircraftType);
    if (!cap || !w || RANK[w] > RANK[cap]) return false;
    const fp = this.footprint[r];
    if ((this.closed.get(fp) ?? []).some(([s, e]) => overlaps(s, e, from, to))) return false;
    return !(this.bookings.get(fp) ?? []).some((b) => b.stay !== st && !ignore?.(b) && overlaps(b.start, b.end, from, to));
  }

  private book(g: Segment, stay: Stay) {
    const fp = this.footprint[g.res];
    (this.bookings.get(fp) ?? this.bookings.set(fp, []).get(fp)!).push({ start: g.start, end: g.end, stay, seg: g });
  }

  private unbook(g: Segment) {
    const list = this.bookings.get(this.footprint[g.res]) ?? [];
    const i = list.findIndex((b) => b.seg === g);
    if (i >= 0) list.splice(i, 1);
  }

  private signature(): string {
    return this.stays.map((st) => `${st.key}:${st.segments.map((g) => `${g.res}@${g.start}-${g.end}`).join(",")}`).join(";");
  }
}
