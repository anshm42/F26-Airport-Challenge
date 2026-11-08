import { hasMoved, isArrival, localTime, timeShift, turnMinutes, waitedMinutes } from "./flights";
import { gateLabel } from "./gates";
import { MINUTE, formatDuration, formatTime, ms } from "./time";
import { esc, flightClasses, gateKey, gateMove, moveText, outageText, partnerNote, sharesGate, terminalName, waitedText } from "./timeline";
import { type Resource, WIDTH_CLASSES, closedBy, conflictGroupOf, widthOf } from "./inventory";
import type { AssignmentsMessage, Flight, Gate } from "./types";

// Sidebar panels. Each returns HTML; the caller only replaces a panel when its HTML changes,
// so text selection survives ticks where nothing in that panel moved.


const timeOf = (f: Flight) => {
  const t = localTime(f);
  return t === null ? "—" : formatTime(t);
};
const shiftText = (f: Flight) => {
  const shift = timeShift(f);
  return shift ? ` (${shift > 0 ? "+" : ""}${shift} min)` : "";
};
const was = (value: string | null | undefined) => (value ? ` <s class="muted">${esc(value)}</s>` : "");
const gateLink = (g: Gate | null) => (g ? `<a href="#" data-gate="${esc(gateKey(g))}">${esc(`${terminalName(g.terminal)} ${gateLabel(g.gate)}`)}</a>` : "Waitlist");
/** A misc key as a label: "zone" -> "Zone", "waitlistReason" -> "Waitlist reason". */
const labelOf = (key: string) => {
  const words = key.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
};
const valueOf = (v: unknown) => (typeof v === "string" ? v : Array.isArray(v) ? v.join(", ") : JSON.stringify(v));
const miscRows = (misc: Record<string, unknown>): [string, string][] => Object.entries(misc).map(([k, v]) => [esc(labelOf(k)), esc(valueOf(v))]);
const dl = (rows: [string, string][]) => `<dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;

/** "Waiting 25 min" for an arrival already on the ground, "Overdue 10 min" for a departure past its time. */
function overdueText(f: Flight, now: number): string {
  const t = localTime(f);
  if (t === null || t > now) return "";
  const d = formatDuration(Math.round((now - t) / MINUTE));
  return isArrival(f) ? `On the ground, waiting ${d}` : `Departure time passed ${d} ago`;
}

/** Every waitlisted flight the server sends, earliest first: the oldest ones are the most urgent, so none are hidden. */
export function renderWaitlist(msg: AssignmentsMessage, selectedId: string | null): { html: string; count: number } {
  const now = ms(msg.generatedAt)!;
  const waiting = msg.flights.filter((f) => !f.gate && !f.cancelled).sort((a, b) => (localTime(a) ?? 0) - (localTime(b) ?? 0));
  const html = waiting
    .map((f) => {
      const reason = typeof f.misc.waitlistReason === "string" ? f.misc.waitlistReason : "";
      const overdue = overdueText(f, now);
      const cls = ["item", f.flightId === selectedId ? "selected" : "", f.emergencyState ? "emergency" : "", overdue ? "overdue" : ""].join(" ");
      return (
        `<li class="${cls}" data-id="${esc(f.flightId)}">` +
        `<b>${esc(f.flightCodes[0])}</b> ${isArrival(f) ? "Arr" : "Dep"} ${timeOf(f)} · ${esc(f.aircraftType ?? "?")} (${widthOf(f.aircraftType) ?? "?"})` +
        (f.emergencyState ? ` · ${esc(f.emergencyState)}` : "") +
        (overdue ? `<div class="overdue-note">${esc(overdue)}</div>` : "") +
        (reason ? `<div class="muted">${esc(reason)}</div>` : "") +
        `</li>`
      );
    })
    .join("");
  return { html, count: waiting.length };
}

export function renderFlight(f: Flight | undefined, byId: Map<string, Flight>): string {
  if (!f) return `<p class="muted">Select a flight.</p>`;
  const prevTime = isArrival(f) ? f.previous.timeArrival : f.previous.timeDeparture;
  const linked = f.linkedFlightId ? byId.get(f.linkedFlightId) : undefined;
  const turn = turnMinutes(f, byId);
  const waited = waitedMinutes(f);
  const rows: [string, string][] = [
    ["Flight", esc(f.flightCodes.join(" / "))],
    ["Airline", esc(f.airline)],
    ["Route", `${esc(f.from)} → ${esc(f.to)}`],
    [isArrival(f) ? "Arrival" : "Departure", timeOf(f) + shiftText(f) + was(prevTime ? formatTime(ms(prevTime)!) : null)],
    ["Gate", gateLink(f.gate) + (hasMoved(f) ? was(gateLabel(f.previous.gate!.gate)) : "")],
    ["Gate assigned", f.assignedAt ? formatTime(ms(f.assignedAt)!) + (waited === null ? "" : ` <span class="waited-note">⟵ from waitlist, after ${formatDuration(waited)} on the ground</span>`) : "—"],
    ["Occupancy", f.occupancy ? `${formatTime(ms(f.occupancy.start)!)}–${formatTime(ms(f.occupancy.end)!)}` : "—"],
    ["Aircraft", `${esc(f.aircraftType ?? "?")} (${widthOf(f.aircraftType) ?? "?"})` + was(f.previous.aircraftType)],
    ["Registration", esc(f.registration ?? "—")],
    ["Traffic", esc(f.trafficType ?? "—")],
  ];
  if (f.cancelled) rows.push(["Status", "Cancelled"]);
  if (f.emergencyState) rows.push(["Emergency", esc(f.emergencyState)]);
  if (linked) {
    // Same gate: ground time from arrival to departure. Different gates: the time between the two gate stays.
    const move = gateMove(f, byId);
    const detail = sharesGate(f, linked)
      ? `same gate${turn === null ? "" : ` · turn ${formatDuration(turn)}`}`
      : `${linked.gate ? `at ${esc(gateLabel(linked.gate.gate))}` : "waitlisted"}${move ? ` · ${moveText(move)}` : ""}`;
    rows.push([isArrival(f) ? "Outbound" : "Inbound", `<a href="#" data-id="${esc(linked.flightId)}">${esc(linked.flightCodes[0])}</a> · ${detail}`]);
  }
  rows.push(...miscRows(f.misc));
  return dl(rows);
}

interface Entry {
  start: number;
  /** null: open-ended outage. */
  end: number | null;
  /** Holds the gate; flights that moved away or were cancelled don't. */
  occupies: boolean;
  /** A free stretch between stays. */
  gap?: boolean;
  /** Contains NOW_MARK where the now line goes if the entry is in progress. */
  html: string;
}

const NOW_MARK = "<!--now-->";

/** Gate details and its whole schedule as a vertical timeline, with a divider at now. */
export function renderGate(msg: AssignmentsMessage, r: Resource | undefined, byId: Map<string, Flight>, selectedId: string | null): string {
  if (!r) return `<p class="muted">Select a gate on the timeline.</p>`;
  const k = gateKey(r);
  const now = ms(msg.generatedAt)!;
  const outages = msg.outages.filter((o) => closedBy(o.stand).includes(r));

  const shared = conflictGroupOf(r).filter((x) => x !== r).map((x) => gateLabel(x.gate));
  const info = dl([
    ["Gate", esc(`${terminalName(r.terminal)} ${gateLabel(r.gate)}`) + (r.aliases.length ? ` <span class="muted">also ${esc(r.aliases.join(", "))}</span>` : "")],
    ["Type", r.kind === "remote" ? "Remote stand" : "Terminal gate"],
    ["Size", r.widthClass ? `Class ${r.widthClass} <span class="muted">up to ${esc(WIDTH_CLASSES[r.widthClass] ?? "?")}</span>` : "Unknown"],
    ...(shared.length ? [["Shares space", `${esc(shared.join(", "))} <span class="muted">only one can be occupied</span>`] as [string, string]] : []),
    ["IFATC stands", esc(r.ifatcStandNames.join(", "))],
  ]);

  const entries: Entry[] = [];
  for (const o of outages) {
    const start = ms(o.start)!;
    const end = ms(o.end);
    entries.push({
      start,
      end,
      occupies: true,
      html:
        `<li class="entry outage">${NOW_MARK}<span class="when">${formatTime(start)}–${end === null ? "…" : formatTime(end)}</span>` +
        `<div class="what"><b>${esc(outageText(o))}</b></div></li>`,
    });
  }

  // Aircraft parked here with no flight.
  for (const p of msg.parkedAircraft) {
    if (!p.gate || gateKey(p.gate) !== k || !p.occupancy) continue;
    const start = ms(p.occupancy.start)!;
    const end = ms(p.occupancy.end)!;
    entries.push({
      start,
      end,
      occupies: true,
      html:
        `<li class="entry parked">${NOW_MARK}<span class="when">${formatTime(start)}–${formatTime(end)}</span>` +
        `<div class="what"><b>${esc(p.registration)}</b> parked, no flight<div class="muted">${esc(p.aircraftType ?? "?")} (${widthOf(p.aircraftType) ?? "?"}) · ${esc(p.carrier)}</div></div></li>`,
    });
  }

  // Flights here, with a turn's halves together; plus flights that have moved away, so the operator sees where they went.
  const done = new Set<string>();
  for (const f of msg.flights) {
    if (done.has(f.flightId)) continue;
    const here = !!f.gate && gateKey(f.gate) === k;
    const movedAway = !here && !!f.previous.gate && gateKey(f.previous.gate) === k;
    if (!here && !movedAway) continue;
    const partner = f.linkedFlightId ? byId.get(f.linkedFlightId) : undefined;
    const flights = here && partner && sharesGate(f, partner) ? [f, partner].sort((a, b) => Number(isArrival(b)) - Number(isArrival(a))) : [f];
    for (const x of flights) done.add(x.flightId);

    const starts = flights.map((x) => ms(x.occupancy?.start) ?? localTime(x) ?? 0);
    const ends = flights.map((x) => ms(x.occupancy?.end) ?? localTime(x) ?? 0);
    const start = Math.min(...starts);
    const classes = ["entry", ...flightClasses(flights, byId), movedAway ? "away" : ""];
    if (flights.some((x) => x.flightId === selectedId)) classes.push("selected");
    const lines = flights
      .map((x) => {
        const place = isArrival(x) ? `from ${esc(x.from)}` : `to ${esc(x.to)}`;
        return `<div data-id="${esc(x.flightId)}"><b>${esc(x.flightCodes[0])}</b> ${isArrival(x) ? "Arr" : "Dep"} ${timeOf(x)}${shiftText(x)} ${place}</div>`;
      })
      .join("");
    const notes = [
      `${esc(f.aircraftType ?? "?")} (${widthOf(f.aircraftType) ?? "?"}) ${esc(f.registration ?? "")}`,
      movedAway && !f.cancelled ? `Moved to ${f.gate ? esc(gateLabel(f.gate.gate)) : "waitlist"}` : "",
      ...flights.map((x) => esc(waitedText(x))),
      flights.length > 1 ? `Turn ${formatDuration(turnMinutes(f, byId) ?? 0)}` : "",
      ...flights.map((x) => esc(partnerNote(x, byId))),
      flights.some((x) => x.emergencyState) ? esc(flights.find((x) => x.emergencyState)!.emergencyState!) : "",
      flights.every((x) => x.cancelled) ? "Cancelled" : "",
    ].filter(Boolean);
    entries.push({
      start,
      end: Math.max(...ends),
      occupies: here && !flights.every((x) => x.cancelled),
      html:
        `<li class="${classes.join(" ")}" data-id="${esc(f.flightId)}">${NOW_MARK}` +
        `<span class="when">${formatTime(start)}–${formatTime(Math.max(...ends))}</span>` +
        `<div class="what">${lines}<div class="muted">${notes.join(" · ")}</div></div></li>`,
    });
  }

  // Now runs through whatever is at the gate; only between entries when nothing is. The mark's position isn't in the HTML
  // (positionNowMarks sets it), so the panel only re-renders when an entry starts or ends.
  entries.sort((a, b) => a.start - b.start);
  entries.push(...gaps(entries));
  entries.sort((a, b) => a.start - b.start);
  // Only the gate's actual state carries the now line: what's holding it, or a free stretch. Moved-away and cancelled
  // flights are reminders, not occupants.
  const live = (e: Entry) => (e.occupies || !!e.gap) && e.end !== null && e.start <= now && now < e.end;
  const items = entries.map((e) =>
    e.html.replace(NOW_MARK, live(e) ? `<span class="now-mark" data-start="${e.start}" data-end="${e.end}"></span>` : ""),
  );
  if (!entries.some(live)) {
    const next = entries.findIndex((e) => e.start > now);
    items.splice(next === -1 ? items.length : next, 0, `<li class="now-divider"><span>Now</span></li>`);
  }
  return `${info}<ol class="schedule">${items.join("")}</ol>`;
}

// Free time between stays, drawn taller the longer it is so empty stretches stand out.
const GAP_PX_PER_MIN = 0.6;
const GAP_MIN_PX = 18;
const GAP_MAX_PX = 120;

function gaps(entries: Entry[]): Entry[] {
  const out: Entry[] = [];
  let free = -Infinity;
  for (const e of entries) {
    if (!e.occupies) continue;
    if (free !== -Infinity && e.start > free) {
      const minutes = Math.round((e.start - free) / 60_000);
      const height = Math.min(GAP_MAX_PX, Math.max(GAP_MIN_PX, minutes * GAP_PX_PER_MIN));
      out.push({
        start: free,
        end: e.start,
        occupies: false,
        gap: true,
        html:
          `<li class="gap" style="height:${height}px">${NOW_MARK}` +
          `<span class="when">${formatTime(free)}–${formatTime(e.start)}</span><span>Free · ${formatDuration(minutes)}</span></li>`,
      });
    }
    free = Math.max(free, e.end ?? Infinity);
  }
  return out;
}

export function positionNowMarks(root: Element, now: number) {
  for (const mark of root.querySelectorAll<HTMLElement>(".now-mark")) {
    const start = Number(mark.dataset.start);
    const end = Number(mark.dataset.end);
    mark.style.top = `${((now - start) / (end - start)) * 100}%`;
  }
}
