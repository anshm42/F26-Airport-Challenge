// Time-zone helpers for the mock. Stream flight times are local to each station (smartgate_stream/gates.py leg_times).

import { LOCAL_TIMEZONE } from "../types";

export const MIN = 60_000;
export const HOUR = 3_600_000;
const DAY = 24 * HOUR;

const formatters = new Map<string, Intl.DateTimeFormat>();
const formatter = (zone: string) => {
  let f = formatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-CA", {
      timeZone: zone,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
    });
    formatters.set(zone, f);
  }
  return f;
};

// Offsets only change on a whole UTC hour, so cache them per zone and hour.
const offsets = new Map<string, number>();

/** The zone's UTC offset in minutes at the given instant (DST-aware). */
export function offsetMinutes(ms: number, zone = LOCAL_TIMEZONE): number {
  const h = Math.floor(ms / HOUR);
  const k = `${zone}@${h}`;
  let o = offsets.get(k);
  if (o === undefined) {
    const p: Record<string, number> = {};
    for (const x of formatter(zone).formatToParts(h * HOUR)) if (x.type !== "literal") p[x.type] = +x.value;
    o = (Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - h * HOUR) / MIN;
    offsets.set(k, o);
  }
  return o;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** ISO 8601 with the zone's UTC offset, e.g. 2026-11-07T13:12:44-05:00. */
export function iso(ms: number, zone = LOCAL_TIMEZONE): string {
  const o = offsetMinutes(ms, zone);
  const s = new Date(ms + o * MIN).toISOString().slice(0, 19);
  const a = Math.abs(o);
  return `${s}${o < 0 ? "-" : "+"}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
}

/** The local calendar date ("2026-11-07") of an instant in the zone. */
export const localDate = (ms: number, zone = LOCAL_TIMEZONE) => iso(ms, zone).slice(0, 10);

export const addDays = (date: string, days: number) => new Date(Date.parse(`${date}T00:00:00Z`) + days * DAY).toISOString().slice(0, 10);

/** The instant a local date and "HH:MM" occur in the zone. */
export function zonedTime(date: string, hhmm: string, zone: string): number {
  const wall = Date.parse(`${date}T${hhmm}:00Z`);
  const guess = wall - offsetMinutes(wall, zone) * MIN;
  // A second pass settles instants near a DST change.
  return wall - offsetMinutes(guess, zone) * MIN;
}

/** Toronto midnight starting the local day that contains ms. */
export const localMidnight = (ms: number) => zonedTime(localDate(ms), "00:00", LOCAL_TIMEZONE);
