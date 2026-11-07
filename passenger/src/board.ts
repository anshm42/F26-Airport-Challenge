import "./style.css";
import { describeSelectors, matchesCurrentGate, parseSelectors } from "./gates";
import { mockSource } from "./mock";
import { STRINGS, title, type Lang, type Show } from "./strings";
import { LOCAL_AIRPORT, LOCAL_TIMEZONE, type Flight, type Gate, type Source } from "./types";
import { websocketSource } from "./websocket";

const params = new URLSearchParams(location.search);
const gates = params.get("gates");
const selectors = parseSelectors(gates);
const lang: Lang = params.get("lang") === "fr" ? "fr" : "en";
const show: Show = (["departures", "arrivals"] as const).find((s) => s === params.get("show")) ?? "all";
const t = STRINGS[lang];
document.documentElement.lang = lang;
const sourceParam = params.get("source") ?? "mock";
const source: Source = sourceParam === "mock" ? mockSource : websocketSource(sourceParam);

// Pearson-style city names; unknown codes fall back to the IATA code.
const CITIES: Record<string, string> = {
  YVR: "Vancouver", YYC: "Calgary", YUL: "Montréal", YOW: "Ottawa", YHZ: "Halifax", YQB: "Québec City",
  YWG: "Winnipeg", LHR: "London-Heathrow", FRA: "Frankfurt", MUC: "Munich", LIS: "Lisbon", DXB: "Dubai",
  ORD: "Chicago-O'Hare", DTW: "Detroit", DFW: "Dallas/Ft.W", MCO: "Orlando", LAS: "Las Vegas", BOS: "Boston",
  EWR: "Newark", ATL: "Atlanta", CUN: "Cancún", PUJ: "Punta Cana",
};

// Square airline marks, keyed by the IATA airline code (the flight code's first two characters).
const LOGOS: Record<string, string> = Object.fromEntries(
  Object.entries(import.meta.glob<string>("./airlines/*.{png,svg}", { eager: true, query: "?url", import: "default" })).map(
    ([path, url]) => [path.match(/([^/]+)\.\w+$/)![1].toUpperCase(), url],
  ),
);
const PLANE_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg>`;

const MINUTE = 60_000;
const BOARDING_BEFORE_DEPARTURE = 30 * MINUTE; // no boarding times in the feed yet (SPEC.md)
const GATE_CHANGE_WINDOW = 60 * MINUTE;
const PANEL_MIN_WIDTH = 620;

// Times must carry an offset (SPEC.md); never fall back to the device's timezone.
const HAS_OFFSET = /(Z|[+-]\d{2}:\d{2})$/;
const timeFormat = new Intl.DateTimeFormat("en-CA", { timeZone: LOCAL_TIMEZONE, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const clockFormat = new Intl.DateTimeFormat("en-CA", { timeZone: LOCAL_TIMEZONE, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

function parseTime(iso: string | null | undefined): number | null {
  if (!iso) return null;
  if (!HAS_OFFSET.test(iso)) {
    console.error(`Rejected time without UTC offset: ${iso}`);
    return null;
  }
  return Date.parse(iso);
}

const isDeparture = (f: Flight) => f.from === LOCAL_AIRPORT;
const localTimeKey = (f: Flight) => (isDeparture(f) ? "timeDeparture" : "timeArrival");
const localTime = (f: Flight) => parseTime(f[localTimeKey(f)]);
const previousLocalTime = (f: Flight) => parseTime(f.previous[localTimeKey(f)]);
/** Like a real board, the time column keeps the earlier time and the status column carries the new one. */
const boardTime = (f: Flight) => previousLocalTime(f) ?? localTime(f);
const sameGate = (a: Gate | null | undefined, b: Gate | null | undefined) => a?.terminal === b?.terminal && a?.gate === b?.gate;

const escape = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const formatTime = (t: number | null) => (t === null ? "--:--" : timeFormat.format(t));

/** Gate changes are only flagged when they matter: the flight is within the hour, or it has left this board's gates. */
function showGateChange(f: Flight): boolean {
  if (!f.previous.gate || sameGate(f.previous.gate, f.gate)) return false;
  if (!matchesCurrentGate(selectors, f)) return true;
  const t = localTime(f);
  return t !== null && t - Date.now() < GATE_CHANGE_WINDOW;
}

/** Status column: one line, most important first. Colours: red cancelled, yellow delayed, blue gate change, green boarding. */
function status(f: Flight): [cls: string, text: string] {
  if (f.cancelled) return ["bad", t.cancelled];
  const now = Date.now();
  const time = localTime(f);
  const before = previousLocalTime(f);
  if (time !== null && before !== null && time > before) return ["delayed", t.delayed(formatTime(time))];
  if (time !== null && before !== null && time < before) return ["delayed", t.revised(formatTime(time))];
  if (showGateChange(f)) return ["moved", t.gateChange(f.previous.gate!.gate)];
  // A real status from the feed beats the estimated boarding time.
  if (typeof f.misc.status === "string") {
    const text = t.feedStatus[f.misc.status.toLowerCase()] ?? f.misc.status;
    return [/board|final/i.test(f.misc.status) ? "go" : "", text];
  }
  if (time === null) return ["", ""];
  if (!isDeparture(f)) return ["", now >= time ? t.arrived : t.onTime];
  const boarding = time - BOARDING_BEFORE_DEPARTURE;
  if (now >= time) return ["", t.departed];
  if (now >= boarding) return ["go", t.boarding];
  const minutes = Math.ceil((boarding - now) / MINUTE);
  return ["", minutes <= 60 ? t.boardsIn(minutes) : t.boardsAt(formatTime(boarding))];
}

function logo(code: string, airline: string): string {
  const url = LOGOS[code.slice(0, 2).toUpperCase()];
  return url
    ? `<img class="logo" src="${url}" alt="${escape(airline)}" title="${escape(airline)}">`
    : `<span class="logo plane" title="${escape(airline)}">${PLANE_ICON}</span>`;
}

/** Primary code first, then codeshare alternates; scrolls sideways when they don't fit. */
function flightCell(f: Flight): string {
  const items = f.flightCodes.map((code, i) => `<span class="code">${logo(code, i === 0 ? f.airline : code)}${escape(code)}</span>`);
  return `<span class="flight" data-id="${escape(f.flightId)}">${items.join("")}</span>`;
}

// Codeshare lists that don't fit scroll on their own in a seamless loop: the codes are appended a second time,
// and the scroll offset wraps by exactly one copy's width.
const MARQUEE_SPEED = 28; // px per second
const MARQUEE_PAUSE = 2000; // ms held on the primary code each time round
const marqueePos = new Map<string, number>(); // flightId → offset, kept across re-renders
const marqueeHold = new Map<string, number>(); // flightId → time the pause on the primary code ends
let marquees: { el: HTMLElement; id: string; loop: number }[] = [];

function setUpMarquees() {
  marquees = [];
  boardEl.querySelectorAll<HTMLElement>(".flight").forEach((el) => {
    if (el.scrollWidth <= el.clientWidth + 1) return;
    const first = el.firstElementChild as HTMLElement;
    const copies = [...el.children].map((child) => {
      const copy = child.cloneNode(true) as HTMLElement;
      copy.setAttribute("aria-hidden", "true");
      return copy;
    });
    el.append(...copies);
    const id = el.dataset.id!;
    const loop = copies[0].offsetLeft - first.offsetLeft;
    el.scrollLeft = (marqueePos.get(id) ?? 0) % loop;
    marquees.push({ el, id, loop });
  });
}

let lastFrame = performance.now();
function scrollMarquees(now: number) {
  const dt = Math.min(now - lastFrame, 100) / 1000;
  lastFrame = now;
  for (const m of marquees) {
    if (!marqueeHold.has(m.id)) marqueeHold.set(m.id, now + MARQUEE_PAUSE);
    if (now < marqueeHold.get(m.id)!) continue;
    let pos = (marqueePos.get(m.id) ?? 0) + MARQUEE_SPEED * dt;
    // Back round to the primary code: the copy lines up exactly, so snap to the start and pause there.
    if (pos >= m.loop) {
      pos = 0;
      marqueeHold.set(m.id, now + MARQUEE_PAUSE);
    }
    marqueePos.set(m.id, pos);
    m.el.scrollLeft = pos;
  }
  requestAnimationFrame(scrollMarquees);
}
if (!matchMedia("(prefers-reduced-motion: reduce)").matches) requestAnimationFrame(scrollMarquees);

// Flash colours match the status colours; neutral for anything else.
type Flash = "cancelled" | "gate" | "delayed" | "other";

function flashKind(before: Flight, after: Flight): Flash {
  if (after.cancelled && !before.cancelled) return "cancelled";
  if (!sameGate(before.gate, after.gate) && showGateChange(after)) return "gate";
  if ((localTime(after) ?? 0) > (localTime(before) ?? 0)) return "delayed";
  return "other";
}

function row(f: Flight, flash: Flash | undefined): string {
  const code = isDeparture(f) ? f.to : f.from;
  // On a mixed board, arrivals are marked "From"; on an arrivals-only board the column header says it.
  const fromLabel = !isDeparture(f) && show === "all" ? `<span class="from">${t.from}</span>` : "";
  const city = `${fromLabel}${escape(CITIES[code] ?? code)}`;
  const gate = f.gate ? escape(f.gate.gate) : "";
  const gateCls = f.cancelled ? "gate cancelled" : showGateChange(f) ? "gate moved" : "gate";
  const [statusCls, statusText] = status(f);
  const classes = ["row", f.cancelled ? "cancelled" : "", flash ? `flash flash-${flash}` : ""].join(" ");
  return `
    <div class="${classes}">
      <span class="time">${formatTime(boardTime(f))}</span>
      <span class="city">${city}</span>
      ${flightCell(f)}
      <span class="${gateCls}">${gate}</span>
      <span class="status ${statusCls}">${escape(statusText)}</span>
    </div>`;
}

const PANEL_HEAD = `
  <div class="row head">
    <span>${t.time}</span><span>${show === "arrivals" ? t.origin : t.destination}</span>
    <span>${t.flight}</span><span>${t.gate}</span><span>${t.status}</span>
  </div>`;

// --- state & rendering ---

/** The round sign in the title bar: the branch letter or terminal number this board covers, if there's just one. */
function sign(): string {
  const all = selectors ?? [];
  const branches = new Set(all.map((s) => ("branch" in s ? s.branch : "gate" in s ? s.gate.match(/^[A-Z]*/)![0] : null)));
  if (all.length && branches.size === 1 && !branches.has(null)) return [...branches][0]!;
  const terminals = new Set(all.map((s) => s.terminal));
  return terminals.size === 1 ? [...terminals][0] : "✈";
}

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
  <header>
    <span class="sign">${escape(sign())}</span>
    <h1>${title(lang, show)[0]} <span class="fr">| ${title(lang, show)[1]}</span></h1>
    <span class="scope">${selectors ? escape(describeSelectors(gates, lang)) : ""}</span>
    <span id="clock"></span>
  </header>
  <main id="board"></main>
  <footer>
    <span id="updated"></span>
    <span id="pager">
      <button id="prev" aria-label="Previous page">‹</button>
      <span id="page"></span>
      <button id="next" aria-label="Next page">›</button>
    </span>
    <span>${t.footnote}</span>
  </footer>`;

const boardEl = document.querySelector<HTMLElement>("#board")!;
const pageEl = document.querySelector<HTMLElement>("#page")!;
const prevEl = document.querySelector<HTMLButtonElement>("#prev")!;
const nextEl = document.querySelector<HTMLButtonElement>("#next")!;

let flights: Flight[] = [];
let seen = new Map<string, { flight: Flight; json: string }>(); // to flash rows that changed since the last message
let flashing = new Map<string, Flash>();
let page = 0;
let pages = 1;

function layout(): { columns: number; rows: number } {
  const css = getComputedStyle(document.documentElement);
  const rowHeight = parseFloat(css.getPropertyValue("--row-height"));
  const headHeight = parseFloat(css.getPropertyValue("--head-height"));
  const columns = Math.max(1, Math.floor(boardEl.clientWidth / PANEL_MIN_WIDTH));
  const rows = Math.max(1, Math.floor((boardEl.clientHeight - headHeight) / rowHeight));
  return { columns, rows };
}

function render() {
  const { columns, rows } = layout();
  const perPage = columns * rows;
  pages = Math.max(1, Math.ceil(flights.length / perPage));
  page = Math.min(page, pages - 1);
  const visible = flights.slice(page * perPage, (page + 1) * perPage);
  boardEl.style.gridTemplateColumns = `repeat(${columns}, 1fr)`;
  boardEl.innerHTML = Array.from({ length: columns }, (_, c) => {
    const panel = visible.slice(c * rows, (c + 1) * rows);
    const body = panel.map((f) => row(f, flashing.get(f.flightId))).join("");
    const empty = c === 0 && !visible.length ? `<p class="empty">${escape(t.noFlights(describeSelectors(gates, lang)))}</p>` : "";
    return `<section class="panel">${PANEL_HEAD}${body}${empty}</section>`;
  }).join("");
  pageEl.textContent = `${page + 1} / ${pages}`;
  prevEl.disabled = page === 0;
  nextEl.disabled = page === pages - 1;
  setUpMarquees();
}

function receive(incoming: Flight[], generatedAt: string) {
  flashing = new Map();
  for (const f of incoming) {
    const old = seen.get(f.flightId);
    if (old && old.json !== JSON.stringify(f)) flashing.set(f.flightId, flashKind(old.flight, f));
  }
  seen = new Map(incoming.map((f) => [f.flightId, { flight: f, json: JSON.stringify(f) }]));
  const shown = show === "all" ? incoming : incoming.filter((f) => isDeparture(f) === (show === "departures"));
  flights = shown.sort((a, b) => (boardTime(a) ?? Infinity) - (boardTime(b) ?? Infinity));
  document.querySelector("#updated")!.textContent = t.updated(clockFormat.format(parseTime(generatedAt) ?? Date.now()));
  render();
}

function turnPage(delta: number) {
  page = Math.max(0, Math.min(pages - 1, page + delta));
  flashing.clear();
  render();
}

function tickClock() {
  document.querySelector("#clock")!.textContent = timeFormat.format(Date.now());
}

source(gates, (msg) => receive(msg.flights, msg.generatedAt));

prevEl.addEventListener("click", () => turnPage(-1));
nextEl.addEventListener("click", () => turnPage(1));
addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft" || e.key === "PageUp") turnPage(-1);
  if (e.key === "ArrowRight" || e.key === "PageDown") turnPage(1);
});

setInterval(tickClock, 1000);
tickClock();
addEventListener("resize", render);

// Countdowns and the gate-change window depend on the current time.
setInterval(() => {
  flashing.clear();
  render();
}, MINUTE);

// Hidden: press "g" to open the setup page with this board's current settings.
addEventListener("keydown", (e) => {
  if (e.key !== "g" || e.ctrlKey || e.metaKey || e.altKey) return;
  location.href = new URL(`./${location.search}`, location.href).toString();
});
