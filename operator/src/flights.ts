import { MINUTE, ms } from "./time";
import { LOCAL_AIRPORT, type Flight } from "./types";

// Display rules derived from a flight. None of these are alarms.

/** Inbound to YYZ, including turnbacks (YYZ -> YYZ). */
export const isArrival = (f: Flight) => f.to === LOCAL_AIRPORT;

/** The time that matters here: arrival for inbound, departure for outbound. */
export const localTime = (f: Flight) => ms(isArrival(f) ? f.timeArrival : f.timeDeparture);

const previousTime = (f: Flight) => ms(isArrival(f) ? f.previous.timeArrival : f.previous.timeDeparture);

/** Minutes later (+) or earlier (-) than the previous time; 0 if unchanged. */
export function timeShift(f: Flight): number {
  const now = localTime(f);
  const before = previousTime(f);
  return now === null || before === null ? 0 : Math.round((now - before) / MINUTE);
}

export const hasMoved = (f: Flight) =>
  !!f.previous.gate && (f.previous.gate.gate !== f.gate?.gate || f.previous.gate.terminal !== f.gate?.terminal);

/** Ground time between an inbound and its linked outbound, in minutes; null if not a complete turn. */
export function turnMinutes(f: Flight, byId: Map<string, Flight>): number | null {
  const other = f.linkedFlightId ? byId.get(f.linkedFlightId) : undefined;
  if (!other || f.cancelled || other.cancelled) return null;
  const [inbound, outbound] = isArrival(f) ? [f, other] : [other, f];
  const arrival = ms(inbound.timeArrival);
  const departure = ms(outbound.timeDeparture);
  return arrival === null || departure === null ? null : Math.round((departure - arrival) / MINUTE);
}

/**
 * Minutes an arrival spent on the ground waiting for its gate: from arrival to assignment, when it had no gate before.
 * null if it didn't wait.
 */
export function waitedMinutes(f: Flight): number | null {
  const assigned = ms(f.assignedAt);
  const arrival = ms(f.timeArrival);
  if (!isArrival(f) || assigned === null || arrival === null || f.previous.gate) return null;
  // Whole clock minutes, so the duration agrees with the HH:MM times shown next to it.
  const minutes = Math.floor(assigned / MINUTE) - Math.floor(arrival / MINUTE);
  return minutes > 0 ? minutes : null;
}

export function matchesSearch(f: Flight, term: string): boolean {
  const t = term.trim().toUpperCase();
  if (!t) return true;
  return f.flightCodes.some((c) => c.toUpperCase().includes(t)) || !!f.registration?.toUpperCase().includes(t);
}
