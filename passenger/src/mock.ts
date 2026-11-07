import { matchesFlight, parseSelectors } from "./gates";
import type { Flight, Gate, Source } from "./types";

// Temporary hard-coded data standing in for the matching algorithm.
// Times are minutes relative to page load so the board always looks live.

const CHANGE_EVERY_MS = 6000;

type Row = [code: string, airline: string, from: string, to: string, minutes: number, terminal: string, gate: string | null];

const ROWS: Row[] = [
  ["AC101", "Air Canada", "YYZ", "YVR", -40, "1", "D24"],
  ["WS651", "WestJet", "YYZ", "YYC", -25, "3", "B22"],
  ["AC845", "Air Canada", "LHR", "YYZ", -15, "1", "E73"],
  ["PD271", "Porter", "YYZ", "YOW", -5, "3", "C32"],
  ["UA3421", "United", "YYZ", "ORD", 5, "1", "F60"],
  ["AC403", "Air Canada", "YYZ", "YUL", 10, "1", "D26"],
  ["LH471", "Lufthansa", "FRA", "YYZ", 15, "1", "E75"],
  ["DL4875", "Delta", "YYZ", "DTW", 20, "3", "A12"],
  ["AC1251", "Air Canada", "YYZ", "MCO", 25, "1", "D28"],
  ["BA99", "British Airways", "YYZ", "LHR", 35, "3", "C36"],
  ["WS1214", "WestJet", "YYZ", "CUN", 40, "3", "B26"],
  ["AC8605", "Air Canada", "YYZ", "YHZ", 45, "1", "D30"],
  ["EK241", "Emirates", "DXB", "YYZ", 50, "1", "E77"],
  ["AA1360", "American", "YYZ", "DFW", 55, "3", "A8"],
  ["AC417", "Air Canada", "YYZ", "YUL", 60, "1", "D32"],
  ["TS710", "Air Transat", "YYZ", "LIS", 70, "3", "B30"],
  ["AC856", "Air Canada", "YYZ", "LHR", 75, "1", "E69"],
  ["F8501", "Flair", "YYZ", "YWG", 80, "3", "A14"],
  ["AC1609", "Air Canada", "YYZ", "LAS", 90, "1", "F57"],
  ["UA518", "United", "EWR", "YYZ", 95, "1", "F62"],
  ["AC118", "Air Canada", "YYZ", "YVR", 100, "1", "D34"],
  ["WS3402", "WestJet", "YYZ", "YQB", 110, "3", "B24"],
  ["PD155", "Porter", "YYZ", "YHZ", 120, "3", "C30"],
  ["AC7524", "Air Canada", "YYZ", "BOS", 130, "1", "F64"],
  ["LH6789", "Lufthansa", "YYZ", "MUC", 140, "1", "E71"],
  ["AC1802", "Air Canada", "YYZ", "PUJ", 150, "1", null],
  ["DL5512", "Delta", "ATL", "YYZ", 160, "3", "A10"],
  ["AC127", "Air Canada", "YYZ", "YYC", 175, "1", null],
];

const BRANCH_GATES: Record<string, string[]> = {
  "1:D": ["D20", "D22", "D24", "D26", "D28", "D30", "D32", "D34", "D36", "D38"],
  "1:E": ["E67", "E69", "E71", "E73", "E75", "E77", "E79"],
  "1:F": ["F55", "F57", "F60", "F62", "F64", "F66"],
  "3:A": ["A5", "A8", "A10", "A12", "A14", "A16"],
  "3:B": ["B20", "B22", "B24", "B26", "B28", "B30"],
  "3:C": ["C30", "C32", "C34", "C36", "C38"],
};

// Marketing codes sold on some operating flights.
const CODESHARES: Record<string, string[]> = {
  AC101: ["UA8001", "NH6812"],
  AC845: ["LH6833"],
  LH471: ["AC9101", "UA9050", "OS7961"],
  AC856: ["LH6591", "UA8045", "SN9216", "LX4112"],
  UA518: ["AC3518"],
  AC1251: ["UA8270"],
  DL4875: ["KL6025", "AF4811"],
};

const STATUSES = ["Boarding", "Final call", "Gate closed", "Departed"];

const pick = <T>(xs: T[]): T => xs[Math.floor(Math.random() * xs.length)];
const addMinutes = (iso: string, m: number) => new Date(Date.parse(iso) + m * 60_000).toISOString();

function initialFlights(): Flight[] {
  const now = Date.now();
  return ROWS.map(([code, airline, from, to, minutes, terminal, gate]) => {
    const time = new Date(now + minutes * 60_000).toISOString();
    const departing = from === "YYZ";
    return {
      flightId: `${code}-${time.slice(0, 10)}`,
      flightCodes: [code, ...(CODESHARES[code] ?? [])],
      airline,
      from,
      to,
      timeArrival: departing ? null : time,
      timeDeparture: departing ? time : null,
      gate: gate ? { terminal, gate } : null,
      cancelled: false,
      previous: {},
      misc: departing && minutes < 0 ? { status: "Departed" } : departing && minutes < 15 ? { status: "Boarding" } : {},
    };
  });
}

/** Applies one random change, the way an algorithm update would. */
function mutate(flights: Flight[]): void {
  const live = flights.filter((f) => !f.cancelled && f.misc.status !== "Departed");
  const f = pick(live);
  const roll = Math.random();

  if (roll < 0.4) {
    const terminal = f.gate?.terminal ?? "1";
    const branches = Object.keys(BRANCH_GATES).filter((b) => b.startsWith(`${terminal}:`));
    const gate: Gate = { terminal, gate: pick(BRANCH_GATES[pick(branches)]) };
    if (gate.gate === f.gate?.gate) return;
    if (f.gate) f.previous.gate = f.gate;
    f.gate = gate;
  } else if (roll < 0.75) {
    const key = f.from === "YYZ" ? "timeDeparture" : "timeArrival";
    f.previous[key] = f[key];
    f[key] = addMinutes(f[key]!, 15 + 5 * Math.floor(Math.random() * 7));
  } else if (roll < 0.9) {
    // Only flights departing within the next half hour progress through statuses.
    if (f.from !== "YYZ" || Date.parse(f.timeDeparture!) - Date.now() > 30 * 60_000) return;
    const next = STATUSES[STATUSES.indexOf(f.misc.status as string) + 1] ?? STATUSES[0];
    f.misc = { ...f.misc, status: next };
  } else {
    f.previous.cancelled = false;
    f.cancelled = true;
  }
}

export const mockSource: Source = (gates, onMessage) => {
  const selectors = parseSelectors(gates);
  const flights = initialFlights();

  const send = () =>
    onMessage({
      type: "assignments",
      generatedAt: new Date().toISOString(),
      flights: structuredClone(flights.filter((f) => matchesFlight(selectors, f))),
    });

  send();
  const timer = setInterval(() => {
    mutate(flights);
    send();
  }, CHANGE_EVERY_MS);
  return () => clearInterval(timer);
};
