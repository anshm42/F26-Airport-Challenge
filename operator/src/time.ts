import { LOCAL_TIMEZONE } from "./types";

// Always format in Toronto time, regardless of the device's timezone.

export const HOUR = 3_600_000;
export const MINUTE = 60_000;

const hm = new Intl.DateTimeFormat("en-CA", { timeZone: LOCAL_TIMEZONE, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const clock = new Intl.DateTimeFormat("en-CA", {
  timeZone: LOCAL_TIMEZONE,
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const day = new Intl.DateTimeFormat("en-CA", { timeZone: LOCAL_TIMEZONE, weekday: "short", month: "short", day: "numeric" });

export const formatTime = (ms: number) => hm.format(ms);
export const formatDay = (ms: number) => day.format(ms);
export const formatClock = (ms: number) => clock.format(ms);

/** Parses an ISO time from the API; null stays null. */
export const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : null);

/** "25 min", "1 h 25 min". */
export function formatDuration(minutes: number): string {
  const m = Math.abs(minutes);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`;
}
