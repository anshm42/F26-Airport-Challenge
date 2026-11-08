import { LOCAL_AIRPORT, LOCAL_TIMEZONE } from "./types";
import type { AssignmentsMessage, Flight, Gate, ParkedAircraft, ServerMessage, Source } from "./types";
import { RESOURCES } from "./inventory";
import { airlineName } from "./mock/catalog";
import { Matcher, type Segment, type Stay } from "./mock/matcher";
import { type FlightRec, Schedule, type ScenarioEvent } from "./mock/schedule";
import { HOUR, iso, localDate, localMidnight, zonedTime } from "./mock/util";

// Mock data source standing in for the matching algorithm's /operator/ws. It replays a scenario made by the stream
// (smartgate_stream generate, see ../../docs/stream.md) against a simulated clock, the way the stream delivers it.
// Flight data is passed through from the stream unchanged, as the real matcher must. Gates, occupancy, assignment
// times, links and the waitlist come from a stand-in greedy matcher (mock/matcher.ts) and are potentially incorrect.
// One message per second: the full state when something changed, otherwise a heartbeat.

export interface MockOptions {
  /** A built-in scenario name (see SCENARIOS) or the URL of a scenario file. */
  scenario: string;
  /** Simulated seconds per real second. */
  rate: number;
  /** Toronto time ("HH:MM") on the scenario's day to start at; default: the current Toronto time of day. */
  start: string | null;
}

export const SCENARIOS: Record<string, string> = {
  "1200": new URL("../../fixtures/pearson-2026-11-07-seed42.json", import.meta.url).href,
  "400": new URL("../../fixtures/pearson-400-flights-seed42.json", import.meta.url).href,
  scripted: new URL("../../fixtures/scripted.json", import.meta.url).href,
};

/** Fields whose last value goes into `previous` when they change; times as instants, so tracking is cheap. */
interface Tracked {
  gate: Gate | null;
  timeArrival: number;
  timeDeparture: number;
  registration: string;
  aircraftType: string;
}
const TRACKED = ["gate", "timeArrival", "timeDeparture", "registration", "aircraftType"] as const;

export function createWorld(events: ScenarioEvent[], startAt: number) {
  const dayStart = localMidnight(Date.parse(events[0].timestamp));
  const dayEnd = localMidnight(dayStart + 30 * HOUR);
  const schedule = new Schedule();
  const matcher = new Matcher(RESOURCES, dayStart, dayEnd);
  const gates: Gate[] = RESOURCES.map((r) => ({ terminal: r.terminal, gate: r.gate }));
  const last = new Map<string, Tracked>();
  const previous = new Map<string, Flight["previous"]>();
  let next = 0;
  let clock = startAt;

  const atYyz = (rec: FlightRec) => rec.leg.departureStation === LOCAL_AIRPORT || rec.leg.arrivalStation === LOCAL_AIRPORT;

  /** The stretch of a visit a flight is shown with: an inbound where it arrives, anything else where it is (or departs). */
  function segmentOf(rec: FlightRec, st: Stay | undefined): Segment | null {
    if (!st?.segments.length) return null;
    return st.inbound === rec && st.outbound ? st.segments[0] : st.segments.at(-1)!;
  }

  function tracked(rec: FlightRec, st: Stay | undefined): Tracked {
    const g = rec.cancelled ? null : segmentOf(rec, st);
    return {
      gate: g ? gates[g.res] : null,
      timeArrival: rec.arr,
      timeDeparture: rec.dep,
      registration: rec.aircraft.registration,
      aircraftType: rec.aircraft.aircraftType,
    };
  }

  /** Records the last value of every field that changed, as the real matcher must for `previous`. */
  function track() {
    const stays = matcher.stayOf();
    for (const rec of schedule.flights.values()) {
      if (!atYyz(rec)) continue;
      const now = tracked(rec, stays.get(rec.key));
      const before = last.get(rec.key);
      last.set(rec.key, now);
      if (!before) continue;
      const prev = previous.get(rec.key) ?? {};
      for (const k of TRACKED) {
        const changed = k === "gate" ? before.gate?.gate !== now.gate?.gate : before[k] !== now[k];
        if (changed) Object.assign(prev, { [k]: typeof before[k] === "number" ? iso(before[k]) : before[k] });
      }
      previous.set(rec.key, prev);
    }
  }

  /** Applies the events up to `t` and brings the plan up to date; returns whether anything changed. */
  function advance(t: number): boolean {
    let changed = false;
    while (next < events.length && Date.parse(events[next].timestamp) <= t) {
      const e = events[next++];
      const at = Date.parse(e.timestamp);
      schedule.apply(e.message, at);
      matcher.rebuild(schedule);
      matcher.plan(at, schedule);
      track();
      changed = true;
    }
    matcher.plan(t, schedule);
    if (matcher.changed()) {
      track();
      changed = true;
    }
    clock = t;
    return changed;
  }

  function flight(rec: FlightRec, st: Stay | undefined): Flight {
    const g = rec.cancelled ? null : segmentOf(rec, st);
    const other = st && (st.inbound === rec ? st.outbound : st.inbound);
    const misc: Record<string, unknown> = { ...rec.extra };
    if (!g && !rec.cancelled) misc.waitlistReason = st ? matcher.reasonFor(st) : "Aircraft not on the ground at YYZ";
    const { leg } = rec;
    return {
      flightId: rec.key,
      flightCodes: [`${rec.carrier}${leg.flightNumber}${leg.flightSuffix ?? ""}`, ...(leg.codeshare ?? []).map((c) => c.carrier + c.flightNumber)],
      airline: airlineName(rec.carrier),
      from: leg.departureStation,
      to: leg.arrivalStation,
      timeArrival: iso(rec.arr),
      timeDeparture: iso(rec.dep),
      gate: g ? gates[g.res] : null,
      cancelled: rec.cancelled,
      previous: { ...previous.get(rec.key) },
      misc,
      registration: rec.aircraft.registration,
      aircraftType: rec.aircraft.aircraftType,
      emergencyState: rec.emergencyState,
      occupancy: g ? { start: iso(g.start), end: iso(g.end) } : null,
      linkedFlightId: !rec.cancelled && other ? other.key : null,
      assignedAt: g ? iso(g.assignedAt) : null,
      trafficType: leg.trafficType ?? null,
    };
  }

  function parked(st: Stay): ParkedAircraft {
    const g = st.segments.at(-1);
    return {
      ...st.aircraft,
      gate: g ? gates[g.res] : null,
      occupancy: g ? { start: iso(g.start), end: iso(g.end) } : null,
      assignedAt: g ? iso(g.assignedAt) : null,
    };
  }

  function snapshot(): AssignmentsMessage {
    const stays = matcher.stayOf();
    return {
      type: "assignments",
      generatedAt: iso(clock),
      outages: schedule.outages.map((o) => ({ stand: o.stand, start: iso(o.start), end: o.end === null ? null : iso(o.end), misc: {} })),
      flights: [...schedule.flights.values()].filter(atYyz).map((rec) => flight(rec, stays.get(rec.key))),
      parkedAircraft: matcher.stays.filter((st) => !st.inbound && !st.outbound).map(parked),
    };
  }

  return { advance, snapshot, heartbeat: (): ServerMessage => ({ type: "heartbeat", generatedAt: iso(clock) }), matcher };
}

/** The scenario day at the given Toronto time, or at the current Toronto time of day. */
function startTime(events: ScenarioEvent[], start: string | null): number {
  const day = localDate(Date.parse(events[0].timestamp));
  const hhmm = start ?? new Date().toLocaleTimeString("en-GB", { timeZone: LOCAL_TIMEZONE, hour: "2-digit", minute: "2-digit" });
  return zonedTime(day, hhmm, LOCAL_TIMEZONE);
}

export function mockSource(options: MockOptions): Source {
  return (onMessage) => {
    let timer = 0;
    let stopped = false;
    fetch(SCENARIOS[options.scenario] ?? options.scenario)
      .then((r) => r.json())
      .then(({ events }: { events: ScenarioEvent[] }) => {
        if (stopped) return;
        const realStart = Date.now();
        const simStart = startTime(events, options.start);
        const world = createWorld(events, simStart);
        world.advance(simStart);
        onMessage(world.snapshot());
        timer = window.setInterval(() => {
          const sim = simStart + ((Date.now() - realStart) * options.rate);
          onMessage(world.advance(sim) ? world.snapshot() : world.heartbeat());
        }, 1000);
      })
      .catch((e) => console.error("Mock scenario failed to load", e));
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  };
}
