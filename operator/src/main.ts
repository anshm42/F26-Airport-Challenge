import "./style.css";
import { DEFAULT_HOURS_AFTER, DEFAULT_HOURS_BEFORE, MAX_HOURS, type Side, attachEdgeScroll } from "./edges";
import { drawLink } from "./links";
import { mockSource } from "./mock";
import { positionNowMarks, renderFlight, renderGate, renderWaitlist } from "./sidebar";
import { formatClock, ms } from "./time";
import { type Filters, type Selection, type TimelineResult, esc, gateKey, renderTimeline } from "./timeline";
import { RESOURCES, closedBy } from "./inventory";
import { type AssignmentsMessage, type Connection, type Flight, defaultSource } from "./types";
import { RECONNECT_MS, websocketSource } from "./websocket";

// Operator view: a read-only gate timeline plus a sidebar. See ../SPEC.md.

const STALE_AFTER_MS = 5000;

const params = new URLSearchParams(location.search);
const num = (name: string, fallback: number) => {
  const v = Number(params.get(name));
  return params.has(name) && Number.isFinite(v) && v > 0 ? v : fallback;
};
// Default: the matching algorithm. "mock" replays a scenario with a stand-in matcher instead.
const sourceParam = params.get("source") || defaultSource("/operator/ws");
const isMock = sourceParam === "mock";
const source = isMock
  ? mockSource({ scenario: params.get("scenario") ?? "1200", rate: num("rate", 60), start: params.get("start") })
  : websocketSource(sourceParam);

const $ = <T extends Element>(selector: string) => document.querySelector<T>(selector)!;

document.querySelector("#app")!.innerHTML = `
  <header>
    <h1>Gate Operations</h1>
    <span class="muted">${esc(isMock ? `Mock data (stream scenario ${params.get("scenario") ?? "1200"})` : sourceParam)}</span>
    <span id="stats" class="muted"></span>
    <span id="clock"></span>
    <span id="fresh" class="muted"></span>
  </header>
  <form id="controls" autocomplete="off">
    <span id="size-filter" class="toggles" title="Gate size class: the largest aircraft the gate takes. Swipe across to flip.">Size <span id="sizes" class="toggles"></span></span>
    <label title="Terminals (1, 3, R for remote), branches (1:D), gates (3:B24) or ranges (1:D20-30), comma-separated">Gates <input name="gates" size="16" placeholder="1, 3:B20-30, R" /></label>
    <span class="toggles">
      <button type="button" class="toggle" data-toggle="arrivals" aria-pressed="true">Arrivals</button>
      <button type="button" class="toggle" data-toggle="departures" aria-pressed="true">Departures</button>
    </span>
    <label>Zoom <input name="pxPerHour" type="range" min="40" max="600" step="10" value="160" /></label>
    <label><input name="search" type="search" size="16" placeholder="Flight or registration" /> <span id="matches" class="muted"></span></label>
    <button type="button" class="toggle" data-toggle="paused" aria-pressed="false" title="Freeze the timeline; only the now line moves">Pause scrolling</button>
    <button type="button" class="toggle" data-action="reset-range" title="Back to the default window, 1 h back to 12 h ahead" hidden>Reset range</button>
  </form>
  <div id="legend" title="Click a key to show only the gates with it">
    <button type="button" class="key arr" data-focus="arr">Arrival</button>
    <button type="button" class="key dep" data-focus="dep">Departure</button>
    <button type="button" class="key turn" data-focus="turn" title="One aircraft arriving and then departing from the same gate">Turn (arr → dep)</button>
    <button type="button" class="key delayed" data-focus="delayed">Delayed</button>
    <button type="button" class="key early" data-focus="early">Early</button>
    <button type="button" class="key moved" data-focus="moved">Gate changed</button>
    <button type="button" class="key split" data-focus="split" title="The same aircraft arrives at one gate and departs from another (or is waitlisted)"><span class="swatch split-arr"></span><span class="swatch split-dep"></span>Changing gates</button>
    <button type="button" class="key emergency" data-focus="emergency">Emergency</button>
    <button type="button" class="key turnback" data-focus="turnback">Turnback</button>
    <button type="button" class="key waited" data-focus="waited" title="Arrived before a gate was free, and waited on the ground">From waitlist</button>
    <button type="button" class="key parked" data-focus="parked" title="An aircraft holding a position with no flight: idle aircraft and spares">Parked</button>
    <button type="button" class="key outage" data-focus="outage">Outage</button>
  </div>
  <main>
    <div id="timeline-wrap">
      <div id="notice" hidden></div>
      <div id="timeline"><div id="now"></div><svg id="links"></svg><div id="sheet"></div><div id="link-label" hidden></div><div id="link-time" hidden></div></div>
      <div id="edge-hint" hidden></div>
      <button type="button" class="edge-arrow" data-extend="before" title="Show 1 h earlier">‹</button>
      <button type="button" class="edge-arrow" data-extend="after" title="Show 1 h later">›</button>
    </div>
    <aside>
      <section id="alarms">
        <h2>Alarms</h2>
        <p class="muted">Not specified yet.</p>
      </section>
      <section id="waitlist">
        <h2>Waitlist <span class="count muted"></span></h2>
        <ol></ol>
      </section>
      <section id="gate">
        <h2>Gate</h2>
        <div class="body"></div>
      </section>
      <section id="flight">
        <h2>Flight</h2>
        <div class="body"></div>
      </section>
    </aside>
  </main>
`;

const form = $<HTMLFormElement>("#controls");
const filters: Filters = {
  hiddenSizes: new Set(),
  gates: "",
  show: "all",
  hoursBefore: DEFAULT_HOURS_BEFORE,
  hoursAfter: DEFAULT_HOURS_AFTER,
  pxPerHour: 160,
  search: "",
  anchor: null,
  focus: null,
};
const selection: Selection = { flightId: null, gate: null };

let latest: AssignmentsMessage | null = null;
let byId = new Map<string, Flight>();
const resources = new Map(RESOURCES.map((r) => [gateKey(r), r]));
let receivedAt = 0;
let connection: Connection = isMock ? "open" : "connecting";
let centerOn: string | null = null;
/** Scroll adjustments to apply once the next render has laid out the sheet. */
let scrollTo: number | null = null;
let scrollBy = 0;
let scrolledToGate: string | null = null;

/** Replaces an element's contents only when they changed, so hover, scroll and text selection survive idle ticks. */
const rendered = new WeakMap<Element, string>();
function setHtml(el: Element, html: string) {
  if (rendered.get(el) === html) return;
  el.innerHTML = html;
  rendered.set(el, html);
}

const pressed = (el: Element) => el.getAttribute("aria-pressed") === "true";

function readControls() {
  const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement;
  filters.gates = field("gates").value;
  const arrivals = pressed($("[data-toggle=arrivals]"));
  const departures = pressed($("[data-toggle=departures]"));
  filters.show = arrivals && departures ? "all" : arrivals ? "arrivals" : "departures";
  filters.pxPerHour = Number(field("pxPerHour").value);
  filters.search = field("search").value;
  filters.hiddenSizes = new Set([...form.querySelectorAll<HTMLElement>("[data-size]")].filter((b) => !pressed(b)).map((b) => b.dataset.size!));
}

/** Size toggles, one per class the inventory contains ("?" for unknown). */
function renderSizeToggles() {
  const sizes = [...new Set(RESOURCES.map((r) => r.widthClass ?? "?"))].sort();
  $("#sizes").innerHTML = sizes
    .map((c) => `<button type="button" class="toggle" data-size="${esc(c)}" aria-pressed="${!filters.hiddenSizes.has(c)}">${esc(c)}</button>`)
    .join("");
}

function renderHeader(msg: AssignmentsMessage, stats: string) {
  const flights = msg.flights;
  const cancelled = flights.filter((f) => f.cancelled).length;
  const now = ms(msg.generatedAt)!;
  // Gates & stands with an outage right now (counted once even with overlapping outages).
  const outages = new Set(msg.outages.filter((o) => ms(o.start)! <= now && (ms(o.end) ?? Infinity) > now).flatMap((o) => closedBy(o.stand).map(gateKey))).size;
  $("#clock").textContent = formatClock(now);
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  $("#stats").textContent =
    `${plural(flights.length, "flight")} · ${cancelled} cancelled · ${msg.parkedAircraft.length} parked · ` +
    `${RESOURCES.length - outages}/${RESOURCES.length} gates & stands in service · ${stats}`;
}

let frame = 0;
function render() {
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(() => {
    if (!latest) return;
    const started = performance.now();
    const timeline = renderTimeline(latest, byId, filters, selection);
    const sheet = $<HTMLElement>("#sheet");
    setHtml(sheet, timeline.html);
    const nowLine = $<HTMLElement>("#now");
    nowLine.hidden = timeline.nowX === null;
    nowLine.style.left = `${timeline.nowX ?? 0}px`;
    nowLine.style.height = `${sheet.offsetHeight}px`;
    // Keep the view steady when the window grows (or jump, for "Now").
    const tl = $<HTMLElement>("#timeline");
    if (scrollTo !== null) tl.scrollLeft = scrollTo;
    tl.scrollLeft += scrollBy;
    scrollTo = null;
    scrollBy = 0;
    for (const arrow of document.querySelectorAll<HTMLButtonElement>("[data-extend]")) arrow.disabled = !canExtend(arrow.dataset.extend as Side);
    $<HTMLElement>("[data-action=reset-range]").hidden =
      filters.hoursBefore === DEFAULT_HOURS_BEFORE && filters.hoursAfter === DEFAULT_HOURS_AFTER;
    if (centerOn) centerBar(centerOn);
    centerOn = null;
    drawLink($("#timeline"), selection.flightId, byId);

    const waitlist = renderWaitlist(latest, selection.flightId);
    $("#waitlist .count").textContent = String(waitlist.count);
    setHtml($("#waitlist ol"), waitlist.html);

    const gate = selection.gate ? resources.get(selection.gate) : undefined;
    $<HTMLElement>("#gate").classList.toggle("empty", !gate);
    // The schedule scrolls and is rebuilt with the panel, so carry its position across.
    const scrollTop = document.querySelector("#gate .schedule")?.scrollTop ?? 0;
    setHtml($("#gate .body"), renderGate(latest, gate, byId, selection.flightId));
    const schedule = document.querySelector("#gate .schedule");
    if (schedule) schedule.scrollTop = scrollTop;
    positionNowMarks($("#gate"), ms(latest.generatedAt)!);
    if (gate && scrolledToGate !== selection.gate) {
      $("#gate .now-divider, #gate .now-mark")?.scrollIntoView({ block: "center" });
      scrolledToGate = selection.gate;
    }
    setHtml($("#flight .body"), renderFlight(selection.flightId ? byId.get(selection.flightId) : undefined, byId));

    renderMatches(timeline.matches);

    const took = Math.round(performance.now() - started);
    renderHeader(latest, `showing ${timeline.rows} gates, ${timeline.flights} flights · render ${took} ms`);
  });
}

/** Match count while searching or focusing; on a new query, scroll to the first match. */
let lastQuery = "";
function renderMatches(matches: TimelineResult["matches"]) {
  const el = $("#matches");
  el.textContent = !matches
    ? ""
    : `${matches.inWindow || "No"} match${matches.inWindow === 1 ? "" : "es"}` + (matches.outside ? ` (+${matches.outside} outside the window)` : "");
  const query = `${filters.search.trim()}|${filters.focus ?? ""}`;
  if (query === lastQuery) return;
  lastQuery = query;
  if (!matches) return;
  $("#timeline").scrollTop = 0;
  document.querySelector(filters.focus === "outage" ? "#sheet .outage" : "#sheet .bar.match")?.scrollIntoView({ block: "nearest", inline: "center" });
}

/** Clicking the selected flight or gate again deselects it. */
/** Scrolls the timeline so the flight's bar (or its turn's bar) is in the middle, if it's drawn. */
function centerBar(id: string) {
  const f = byId.get(id);
  const ids = [id, f?.linkedFlightId].filter((x): x is string => !!x);
  const bar = ids.map((x) => document.querySelector(`#sheet .bar[data-id="${CSS.escape(x)}"]`)).find(Boolean);
  bar?.scrollIntoView({ block: "center", inline: "center" });
}

function toggle(kind: keyof Selection, id: string | null) {
  selection[kind] = selection[kind] === id ? null : id;
  if (kind === "gate" && !selection.gate) scrolledToGate = null;
  render();
}

function renderFreshness() {
  const age = receivedAt ? Date.now() - receivedAt : Infinity;
  const fresh = $("#fresh");
  const updated = receivedAt ? `updated ${Math.floor(age / 1000)} s ago` : "";
  const stale = connection === "closed" || age > STALE_AFTER_MS;
  // The age only matters once updates stop arriving.
  fresh.textContent =
    connection === "closed" ? (receivedAt ? `disconnected · ${updated}` : "no data source")
    : connection === "connecting" ? "connecting…"
    : !receivedAt ? "waiting for data…"
    : stale ? updated
    : "Live information";
  fresh.classList.toggle("stale", stale);
  renderNotice();
}

/** What's wrong with the data source, over the timeline: unreachable, lost, or connected with nothing to show yet. */
function renderNotice() {
  const retry = `Retrying every ${RECONNECT_MS / 1000} s.`;
  const where = `<code>${esc(sourceParam)}</code>`;
  const help = `Start the matcher (<code>python run_local.py</code>), or open <a href="?source=mock">?source=mock</a> for simulated data.`;
  let html = "";
  if (connection === "closed" && !latest) {
    html = `<h2>No data source</h2><p>The matching algorithm isn't reachable at ${where}. ${retry}</p><p class="muted">${help}</p>`;
  } else if (connection === "closed") {
    html = `<h2>Connection lost</h2><p>Showing the last state received from ${where}. ${retry}</p>`;
  } else if (connection === "connecting" && !latest) {
    html = `<p>Connecting to the matching algorithm at ${where}…</p>`;
  } else if (latest && !latest.flights.length && !latest.parkedAircraft.length) {
    html = `<h2>No flights yet</h2><p>Connected to ${where}, but the matcher has no airport state: waiting for the stream to send <code>InitializeAirport</code>.</p>`;
  }
  const notice = $<HTMLElement>("#notice");
  notice.hidden = !html;
  notice.classList.toggle("bad", connection === "closed");
  setHtml(notice, html);
}

form.addEventListener("input", () => {
  readControls();
  render();
});
// Size toggles: one swipe surface (the "Size" title included). Every button the pointer moves onto flips, so swiping
// back and forth keeps toggling. The hit area is forgiving: buttons split the row between them with no dead gaps, and
// the pointer can drift above or below the row by SWIPE_SLOP before it counts as having left.
const SWIPE_SLOP = 16;
let swiping = false;
let underPointer: HTMLElement | null = null;

function sizeButtonAt(x: number, y: number): HTMLElement | null {
  const buttons = [...document.querySelectorAll<HTMLElement>("#sizes [data-size]")];
  const row = $("#size-filter").getBoundingClientRect();
  if (!buttons.length || y < row.top - SWIPE_SLOP || y > row.bottom + SWIPE_SLOP) return null;
  // Left of the first button is the title: a place to start a swipe without flipping anything.
  if (x < buttons[0].getBoundingClientRect().left - 2 || x > buttons.at(-1)!.getBoundingClientRect().right + SWIPE_SLOP) return null;
  const distance = (b: HTMLElement) => {
    const r = b.getBoundingClientRect();
    return x < r.left ? r.left - x : x > r.right ? x - r.right : 0;
  };
  return buttons.reduce((best, b) => (distance(b) < distance(best) ? b : best));
}

// A flip gives a subtle shrink; flipping the same button again quickly (a fast back-and-forth) gets the big squish.
const DOUBLE_FLIP_MS = 400;
const lastFlip = new WeakMap<HTMLElement, number>();

function flipSize(button: HTMLElement) {
  button.setAttribute("aria-pressed", String(!pressed(button)));
  const now = performance.now();
  const animation = now - (lastFlip.get(button) ?? -Infinity) < DOUBLE_FLIP_MS ? "goosh" : "nudge";
  lastFlip.set(button, now);
  // Restart the animation even if one is mid-way.
  button.classList.remove("goosh", "nudge");
  void button.offsetWidth;
  button.classList.add(animation);
  readControls();
  render();
}

const sizeFilter = $<HTMLElement>("#size-filter");
sizeFilter.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  sizeFilter.setPointerCapture(e.pointerId);
  swiping = true;
  underPointer = sizeButtonAt(e.clientX, e.clientY);
  if (underPointer) flipSize(underPointer);
});
sizeFilter.addEventListener("pointermove", (e) => {
  if (!swiping) return;
  const button = sizeButtonAt(e.clientX, e.clientY);
  if (button === underPointer) return;
  underPointer = button;
  if (button) flipSize(button);
});
const endSwipe = () => {
  swiping = false;
  underPointer = null;
};
sizeFilter.addEventListener("pointerup", endSwipe);
sizeFilter.addEventListener("pointercancel", endSwipe);
sizeFilter.addEventListener("animationend", (e) => (e.target as Element).classList.remove("goosh", "nudge"));

form.addEventListener("click", (e) => {
  const button = (e.target as Element).closest<HTMLButtonElement>("button.toggle");
  if (!button) return;
  // Size buttons flip on pointer swipes; a click with no pointer (keyboard Enter/Space) still flips them here.
  if (button.dataset.size) {
    if (e.detail === 0) flipSize(button);
    return;
  }
  const on = !pressed(button);
  button.setAttribute("aria-pressed", String(on));
  // Arrivals/departures: turning off the only one shown switches to the other, since showing neither is pointless.
  const other = { arrivals: "departures", departures: "arrivals" }[button.dataset.toggle ?? ""];
  if (other && !on) {
    const otherButton = $(`[data-toggle=${other}]`);
    if (!pressed(otherButton)) otherButton.setAttribute("aria-pressed", "true");
  }
  // Paused: the window stays around the time it was paused at. The label names what clicking does next.
  if (button.dataset.toggle === "paused") {
    button.textContent = on ? "Follow now" : "Pause scrolling";
    button.title = on ? "Scroll the timeline with the clock again" : "Freeze the timeline; only the now line moves";
    filters.anchor = on && latest ? ms(latest.generatedAt) : null;
  }
  readControls();
  render();
});
form.addEventListener("submit", (e) => e.preventDefault());

const canExtend = (side: Side) => (side === "before" ? filters.hoursBefore : filters.hoursAfter) < MAX_HOURS;

attachEdgeScroll($("#timeline"), $("#edge-hint"), {
  canExtend,
  extend: (side, delta) => {
    // Growing the start pushes everything right by an hour; compensate, then carry on with the scroll that asked.
    if (side === "before") {
      filters.hoursBefore++;
      scrollBy += filters.pxPerHour + delta;
    } else {
      filters.hoursAfter++;
      scrollBy += delta;
    }
    render();
  },
});

// Arrow buttons at the ends of the time axis, for a mouse: add an hour on that side and scroll to show it.
for (const arrow of document.querySelectorAll<HTMLButtonElement>("[data-extend]")) {
  arrow.addEventListener("click", () => {
    const side = arrow.dataset.extend as Side;
    if (!canExtend(side)) return;
    if (side === "before") filters.hoursBefore++;
    else filters.hoursAfter++;
    scrollTo = side === "before" ? 0 : Number.MAX_SAFE_INTEGER;
    render();
  });
}

form.querySelector("[data-action=reset-range]")!.addEventListener("click", () => {
  filters.hoursBefore = DEFAULT_HOURS_BEFORE;
  filters.hoursAfter = DEFAULT_HOURS_AFTER;
  scrollTo = 0;
  render();
});
$("#legend").addEventListener("click", (e) => {
  const key = (e.target as Element).closest<HTMLElement>("[data-focus]");
  if (!key) return;
  filters.focus = filters.focus === key.dataset.focus ? null : key.dataset.focus!;
  for (const k of document.querySelectorAll("#legend [data-focus]")) k.setAttribute("aria-pressed", String(k === key && !!filters.focus));
  render();
});

document.addEventListener("click", (e) => {
  const target = (e.target as Element).closest<HTMLElement>("[data-id], [data-gate]");
  // A drag that selects text also fires a click; don't treat it as one.
  if (!target || !document.getSelection()?.isCollapsed) return;
  e.preventDefault();
  // A flight picked outside the timeline (gate panel, details link, waitlist), or through a shared gate it blocks,
  // is brought into view there.
  const remote = !target.closest("#timeline") || target.classList.contains("blocked");
  if (target.dataset.id && remote && selection.flightId !== target.dataset.id) centerOn = target.dataset.id;
  if (target.dataset.id) toggle("flightId", target.dataset.id);
  else toggle("gate", target.dataset.gate!);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  selection.flightId = null;
  selection.gate = null;
  scrolledToGate = null;
  render();
});

const onConnection = (state: Connection) => {
  connection = state;
  renderFreshness();
};
source((msg) => {
  receivedAt = Date.now();
  // A heartbeat only moves the clock; the last full state stands.
  if (msg.type === "heartbeat") {
    if (!latest) return;
    latest = { ...latest, generatedAt: msg.generatedAt };
    renderFreshness();
    render();
    return;
  }
  latest = msg;
  byId = new Map(msg.flights.map((f) => [f.flightId, f]));
  readControls();
  renderFreshness();
  render();
}, onConnection);
renderSizeToggles();
renderFreshness();
setInterval(renderFreshness, 1000);
