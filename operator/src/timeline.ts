import { hasMoved, isArrival, matchesSearch, timeShift, turnMinutes, waitedMinutes } from "./flights";
import { branchOf, gateLabel, matchesResource, numberOf, parseSelectors } from "./gates";
import { HOUR, MINUTE, formatDay, formatDuration, formatTime, ms } from "./time";
import { RESOURCES, type Resource, closedBy, widthOf } from "./inventory";
import type { AssignmentsMessage, Flight, Gate, Outage, ParkedAircraft } from "./types";

// The gate timeline: one row per resource, time left to right. The sheet is rebuilt only when its HTML changes;
// the scroll container is kept, so the operator's scroll position survives updates.

export interface Filters {
  /** Gate size classes to hide ("?" for unknown); everything else is shown, so new classes appear by default. */
  hiddenSizes: Set<string>;
  gates: string;
  show: "all" | "arrivals" | "departures";
  hoursBefore: number;
  hoursAfter: number;
  pxPerHour: number;
  search: string;
  /** null: the window follows now. Otherwise the window stays put around this time and only the now line moves. */
  anchor: number | null;
  /** A legend key the operator clicked to narrow the timeline to. */
  focus: Focus;
}

export interface Selection {
  flightId: string | null;
  /** A gateKey. */
  gate: string | null;
}

export const LABEL_WIDTH = 110;
const LANE_HEIGHT = 22;
const ROW_PADDING = 4;

/** One bar: a single flight, both halves of a turn on the same gate, or a parked aircraft with no flight. */
interface Block {
  start: number;
  end: number;
  flights: Flight[];
  parked?: ParkedAircraft;
  lane: number;
}

export const esc = (s: string) => s.replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
export const gateKey = (g: Gate) => `${g.terminal}:${g.gate}`;

function push<T>(map: Map<string, T[]>, k: string, value: T) {
  const list = map.get(k);
  if (list) list.push(value);
  else map.set(k, [value]);
}

/** "Remote" sorts after the numbered terminals. */
const terminalOrder = (t: string) => (/^\d+$/.test(t) ? Number(t) : Infinity);

export function compareResources(a: Resource, b: Resource): number {
  return (
    terminalOrder(a.terminal) - terminalOrder(b.terminal) ||
    a.terminal.localeCompare(b.terminal) ||
    branchOf(gateLabel(a.gate)).localeCompare(branchOf(gateLabel(b.gate))) ||
    (numberOf(gateLabel(a.gate)) || 0) - (numberOf(gateLabel(b.gate)) || 0) ||
    a.gate.localeCompare(b.gate)
  );
}

export const terminalName = (t: string) => (/^\d+$/.test(t) ? `Terminal ${t}` : t);

/** Both halves of a turn are on the same gate. */
export const sharesGate = (a: Flight, b: Flight | undefined) => !!a.gate && !!b?.gate && gateKey(a.gate) === gateKey(b.gate);

function blocksByGate(msg: AssignmentsMessage, byId: Map<string, Flight>): Map<string, Block[]> {
  const { flights } = msg;
  const blocks = new Map<string, Block[]>();
  const blockOf = new Map<string, Block>();
  for (const f of flights) {
    if (!f.gate || !f.occupancy) continue;
    const start = ms(f.occupancy.start)!;
    const end = ms(f.occupancy.end)!;
    const linked = f.linkedFlightId ? blockOf.get(f.linkedFlightId) : undefined;
    if (linked && sharesGate(f, byId.get(f.linkedFlightId!))) {
      linked.flights.push(f);
      linked.start = Math.min(linked.start, start);
      linked.end = Math.max(linked.end, end);
      blockOf.set(f.flightId, linked);
      continue;
    }
    const block: Block = { start, end, flights: [f], lane: 0 };
    blockOf.set(f.flightId, block);
    push(blocks, gateKey(f.gate), block);
  }
  for (const list of blocks.values()) for (const b of list) b.flights.sort((x, y) => Number(isArrival(y)) - Number(isArrival(x)));
  for (const p of msg.parkedAircraft) {
    if (p.gate && p.occupancy) push(blocks, gateKey(p.gate), { start: ms(p.occupancy.start)!, end: ms(p.occupancy.end)!, flights: [], parked: p, lane: 0 });
  }
  return blocks;
}

/** Stacks overlapping blocks into lanes; returns the number of lanes used. */
function assignLanes(blocks: Block[]): number {
  const laneEnds: number[] = [];
  for (const b of blocks.sort((x, y) => x.start - y.start)) {
    const lane = laneEnds.findIndex((end) => end <= b.start);
    b.lane = lane === -1 ? laneEnds.length : lane;
    laneEnds[b.lane] = b.end;
  }
  return Math.max(1, laneEnds.length);
}

function showsBlock(b: Block, filters: Filters): boolean {
  // A parked aircraft is neither an arrival nor a departure, but it holds the gate either way.
  if (filters.show === "all" || b.parked) return true;
  return b.flights.some((f) => isArrival(f) === (filters.show === "arrivals"));
}

/** The other half of a turn, when it's live but not on this flight's gate: the aircraft is changing gates (or waiting for one). */
export function splitPartner(f: Flight, byId: Map<string, Flight>): Flight | undefined {
  const other = f.linkedFlightId ? byId.get(f.linkedFlightId) : undefined;
  return other && !other.cancelled && !f.cancelled && !sharesGate(f, other) ? other : undefined;
}

export interface GateMove {
  /** When the aircraft leaves the arrival gate. */
  from: number;
  /** When it reaches the departure gate. */
  to: number;
  minutes: number;
}

/** A split turn's time between gates: from the end of the inbound's stay to the start of the outbound's. */
export function gateMove(f: Flight, byId: Map<string, Flight>): GateMove | null {
  const other = splitPartner(f, byId);
  if (!other?.occupancy || !f.occupancy) return null;
  const [inbound, outbound] = isArrival(f) ? [f, other] : [other, f];
  const from = ms(inbound.occupancy!.end)!;
  const to = ms(outbound.occupancy!.start)!;
  return { from, to, minutes: Math.round((to - from) / MINUTE) };
}

/** An outage's reason, or the IFATC stand it was announced on. */
export function outageText(o: Outage): string {
  return typeof o.misc.reason === "string" ? o.misc.reason : `Outage (${o.stand})`;
}

export const moveText = (m: GateMove) =>
  m.minutes >= 0
    ? `${formatDuration(m.minutes)} between gates (${formatTime(m.from)}–${formatTime(m.to)})`
    : `gate stays overlap by ${formatDuration(m.minutes)}`;

/** Where the other half of an aircraft's turn is, when it isn't in the same bar. */
export function partnerNote(f: Flight, byId: Map<string, Flight>): string {
  const other = f.linkedFlightId ? byId.get(f.linkedFlightId) : undefined;
  if (!other || sharesGate(f, other)) return "";
  const move = gateMove(f, byId);
  return `${isArrival(f) ? "Outbound" : "Inbound"} ${other.flightCodes[0]} at ${other.gate ? gateLabel(other.gate.gate) : "waitlist"}` + (move ? ` · ${moveText(move)}` : "");
}

/** "Gate assigned 12:00, after 1 h on the waitlist", for an arrival that waited on the ground. */
export function waitedText(f: Flight): string {
  const minutes = waitedMinutes(f);
  return minutes === null ? "" : `From waitlist: gate assigned ${formatTime(ms(f.assignedAt)!)}, after ${formatDuration(minutes)} on the ground`;
}

function blockTitle(b: Block, byId: Map<string, Flight>): string {
  if (b.parked) {
    const p = b.parked;
    return `${p.registration} parked, no flight\n${p.aircraftType ?? "?"} (${widthOf(p.aircraftType) ?? "?"}) · ${p.carrier}`;
  }
  const lines = b.flights.map((f) => {
    const time = ms(isArrival(f) ? f.timeArrival : f.timeDeparture);
    const shift = timeShift(f);
    return [
      `${f.flightCodes.join(" / ")}  ${f.from} → ${f.to}`,
      `${isArrival(f) ? "Arr" : "Dep"} ${time === null ? "—" : formatTime(time)}${shift ? ` (${shift > 0 ? "+" : ""}${shift} min)` : ""}`,
      `${f.aircraftType ?? "?"} (${widthOf(f.aircraftType) ?? "?"}) ${f.registration ?? ""}`,
      f.cancelled ? "CANCELLED" : "",
      f.emergencyState ?? "",
      hasMoved(f) ? `Moved from ${gateLabel(f.previous.gate!.gate)}` : "",
      waitedText(f),
      partnerNote(f, byId),
    ]
      .filter(Boolean)
      .join("\n");
  });
  // A turn on one gate shows its ground time; a split turn's time between gates is in partnerNote.
  const turn = b.flights.length > 1 ? turnMinutes(b.flights[0], byId) : null;
  if (turn !== null) lines.push(`Turn: ${formatDuration(turn)}`);
  return lines.join("\n\n");
}

export function flightClasses(fs: Flight[], byId: Map<string, Flight>): string[] {
  const classes = [fs.length > 1 ? "turn" : isArrival(fs[0]) ? "arr" : "dep"];
  if (fs.every((f) => f.cancelled)) classes.push("cancelled");
  if (fs.some((f) => timeShift(f) > 0)) classes.push("delayed");
  else if (fs.some((f) => timeShift(f) < 0)) classes.push("early");
  if (fs.some(hasMoved)) classes.push("moved");
  if (fs.some((f) => f.emergencyState === "Emergency")) classes.push("emergency");
  if (fs.some((f) => f.emergencyState === "Turnback")) classes.push("turnback");
  if (fs.length === 1 && splitPartner(fs[0], byId)) classes.push("split");
  if (fs.some((f) => waitedMinutes(f) !== null)) classes.push("waited");
  return classes;
}

const blockClasses = (b: Block, byId: Map<string, Flight>) => ["bar", ...(b.parked ? ["parked"] : flightClasses(b.flights, byId))];

export interface TimelineResult {
  html: string;
  rows: number;
  /** Flights drawn (a turn's bar counts both). */
  flights: number;
  /** The now line's offset from the sheet's left edge; null when now is outside the window. */
  nowX: number | null;
  /** While searching or focusing: matching bars in the window, and how many more are outside it. */
  matches: { inWindow: number; outside: number } | null;
}

/** Legend keys that can focus the timeline: a bar class, or "outage" for gates with an outage. */
export type Focus = string | null;

export function renderTimeline(msg: AssignmentsMessage, byId: Map<string, Flight>, filters: Filters, selection: Selection): TimelineResult {
  const now = ms(msg.generatedAt)!;
  const center = filters.anchor ?? now;
  const from = center - filters.hoursBefore * HOUR;
  const to = center + filters.hoursAfter * HOUR;
  const px = filters.pxPerHour;
  const x = (t: number) => ((Math.max(from, Math.min(to, t)) - from) / HOUR) * px;
  const width = x(to);

  // Searching or focusing narrows the rows to those with a match, and dims the other bars in them.
  const term = filters.search.trim();
  const focusBar = filters.focus && filters.focus !== "outage" ? filters.focus : null;
  const narrowing = !!term || !!filters.focus;
  const searchMatches = (b: Block) =>
    b.parked ? !!b.parked.registration.toUpperCase().includes(term.toUpperCase()) : b.flights.some((f) => matchesSearch(f, term));
  const barMatches = (b: Block, classes: string[]) => (!term || searchMatches(b)) && (!focusBar || classes.includes(focusBar));
  const matches = { inWindow: 0, outside: 0 };

  const selectors = parseSelectors(filters.gates);
  const blocks = blocksByGate(msg, byId);
  const outages = new Map<string, Outage[]>();
  for (const o of msg.outages) for (const r of closedBy(o.stand)) push(outages, gateKey(r), o);
  // Positions sharing a conflict group: what's on one blocks the others.
  const groups = new Map<string, Resource[]>();
  for (const r of RESOURCES) push(groups, r.conflictGroup, r);

  // Hour ticks and gridlines, aligned to the hour.
  const firstHour = Math.ceil(from / HOUR) * HOUR;
  let ticks = "";
  // Midnight shows the day instead, so a window stretched past it stays readable.
  for (let t = firstHour; t <= to; t += HOUR) {
    const label = formatTime(t);
    ticks += label === "00:00" ? `<span class="tick day" style="left:${x(t)}px">${formatDay(t)}</span>` : `<span class="tick" style="left:${x(t)}px">${label}</span>`;
  }
  const gridOffset = x(firstHour);

  let rows = "";
  let rowCount = 0;
  let flightCount = 0;
  let group = "";
  const resources = RESOURCES
    .filter((r) => !filters.hiddenSizes.has(r.widthClass ?? "?") && matchesResource(selectors, r))
    .sort(compareResources);

  for (const r of resources) {
    const k = gateKey(r);
    const all = (blocks.get(k) ?? []).filter((b) => showsBlock(b, filters));
    const visible = all.filter((b) => b.end > from && b.start < to);
    const down = (outages.get(k) ?? []).filter((o) => (ms(o.end) ?? Infinity) > from && ms(o.start)! < to);
    const classesOf = new Map(visible.map((b) => [b, blockClasses(b, byId)]));

    let rowMatches = 0;
    if (narrowing) {
      if (filters.focus === "outage") rowMatches = down.length && (!term || visible.some((b) => barMatches(b, []))) ? 1 : 0;
      else {
        rowMatches = visible.filter((b) => barMatches(b, classesOf.get(b)!)).length;
        const outside = all.filter((b) => !(b.end > from && b.start < to) && barMatches(b, blockClasses(b, byId)));
        matches.outside += outside.length;
      }
      matches.inWindow += rowMatches;
      if (!rowMatches) continue;
    }

    const g = `${terminalName(r.terminal)} · ${r.kind === "remote" ? "Remote stands" : branchOf(r.gate)}`;
    if (g !== group) {
      group = g;
      rows += `<div class="group"><div class="label">${esc(g)}</div></div>`;
    }

    const lanes = assignLanes(visible);
    let track = "";
    for (const o of down) {
      const left = x(ms(o.start)!);
      track += `<div class="outage" style="left:${left}px;width:${x(ms(o.end) ?? to) - left}px" title="${esc(outageText(o))}"></div>`;
    }
    for (const s of groups.get(r.conflictGroup) ?? []) {
      if (s === r) continue;
      for (const b of blocks.get(gateKey(s)) ?? []) {
        if (!(b.end > from && b.start < to)) continue;
        const left = x(b.start);
        const who = b.parked ? b.parked.registration : b.flights.map((f) => f.flightCodes[0]).join(" / ");
        // Clicking it selects the flight holding the space, like clicking that flight's own bar.
        const selected = b.flights.some((f) => f.flightId === selection.flightId) ? " selected" : "";
        track +=
          `<div class="blocked${selected}"${b.parked ? "" : ` data-id="${esc(b.flights[0].flightId)}"`}` +
          ` style="left:${left}px;width:${x(b.end) - left}px" title="${esc(`Blocked: ${who} at ${gateLabel(s.gate)} shares this space`)}"></div>`;
      }
    }
    for (const b of visible) {
      const left = x(b.start);
      // Start of the stay on the left, where it goes next right-aligned: a turn's two flights, or for a split turn
      // the other gate on the side the aircraft comes from or goes to.
      const split = b.flights.length === 1 ? splitPartner(b.flights[0], byId) : undefined;
      const other = split ? (split.gate ? gateLabel(split.gate.gate) : "Waitlist") : "";
      const code = b.parked ? b.parked.registration : b.flights[0].flightCodes[0];
      const [start, end] = split
        ? isArrival(b.flights[0]) ? [code, other] : [other, code]
        : [code, b.flights[1]?.flightCodes[0] ?? ""];
      // Leftward badge: the stay started when the aircraft was brought from the waitlist.
      const badge = classesOf.get(b)!.includes("waited") ? `<i class="badge" aria-label="From waitlist">⟵</i>` : "";
      const label = `<span>${badge}${esc(start)}</span>${end ? `<span>${esc(end)}</span>` : ""}`;
      const classes = classesOf.get(b)!;
      if (narrowing && filters.focus !== "outage") classes.push(barMatches(b, classes) ? "match" : "dim");
      if (b.flights.some((f) => f.flightId === selection.flightId)) classes.push("selected");
      track +=
        `<div class="${classes.join(" ")}"${b.parked ? "" : ` data-id="${esc(b.flights[0].flightId)}"`}` +
        ` style="left:${left}px;width:${Math.max(3, x(b.end) - left)}px;top:${ROW_PADDING / 2 + b.lane * LANE_HEIGHT}px"` +
        ` title="${esc(blockTitle(b, byId))}">${label}</div>`;
    }
    flightCount += visible.reduce((n, b) => n + b.flights.length, 0);
    rowCount++;

    const aliases = r.aliases.length ? ` <span class="alias">/${esc(r.aliases.join("/"))}</span>` : "";
    rows +=
      `<div class="row${selection.gate === k ? " selected" : ""}" data-gate="${esc(k)}" style="height:${lanes * LANE_HEIGHT + ROW_PADDING}px">` +
      `<div class="label" data-gate="${esc(k)}">${esc(gateLabel(r.gate))}${aliases}</div>` +
      `<div class="track">${track}</div></div>`;
  }

  const html =
    `<div class="sheet" style="width:${LABEL_WIDTH + width}px;--px-per-hour:${px}px;--grid-offset:${gridOffset}px">` +
    `<div class="axis"><div class="label"></div><div class="ticks">${ticks}</div></div>` +
    rows +
    `</div>`;
  const nowX = now >= from && now <= to ? LABEL_WIDTH + x(now) : null;
  return { html, rows: rowCount, flights: flightCount, nowX, matches: narrowing ? matches : null };
}
