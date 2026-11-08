import { isArrival } from "./flights";
import { formatDuration, formatTime, ms } from "./time";
import { LABEL_WIDTH, gateKey, gateMove, splitPartner } from "./timeline";
import type { Flight } from "./types";

// When the selected flight is one half of a turn split across gates, a pink arrow runs under the bars from the
// arrival's end to the departure's start. If the other half isn't drawn, the connector points to a label in this row.

const GAP = 10;

interface Rect {
  left: number;
  right: number;
  mid: number;
}

/** A bar's position in the timeline's scrollable content. */
function rectOf(el: Element, timeline: HTMLElement): Rect {
  const r = el.getBoundingClientRect();
  const t = timeline.getBoundingClientRect();
  const left = r.left - t.left + timeline.scrollLeft;
  const top = r.top - t.top + timeline.scrollTop;
  return { left, right: left + r.width, mid: top + r.height / 2 };
}

/** Why the other half has no bar on screen. */
function hiddenReason(other: Flight): string {
  if (!other.gate) return "waitlisted";
  const time = ms(other.occupancy?.start);
  const when = time === null ? "" : ` ${formatTime(time)}`;
  if (document.querySelector(`#sheet [data-gate="${CSS.escape(gateKey(other.gate))}"]`)) return `outside the window${when}`;
  return "filtered out";
}

export function drawLink(timeline: HTMLElement, selectedId: string | null, byId: Map<string, Flight>) {
  const svg = timeline.querySelector<SVGSVGElement>("#links")!;
  const label = timeline.querySelector<HTMLElement>("#link-label")!;
  const time = timeline.querySelector<HTMLElement>("#link-time")!;
  const sheet = timeline.querySelector<HTMLElement>("#sheet")!;
  svg.innerHTML = "";
  label.hidden = true;
  time.hidden = true;

  const f = selectedId ? byId.get(selectedId) : undefined;
  const other = f ? splitPartner(f, byId) : undefined;
  const bar = f && sheet.querySelector(`.bar[data-id="${CSS.escape(f.flightId)}"]`);
  if (!f || !other || !bar) return;

  svg.setAttribute("width", String(sheet.scrollWidth));
  svg.setAttribute("height", String(sheet.scrollHeight));
  const here = rectOf(bar, timeline);
  const arriving = isArrival(f);
  const otherBar = sheet.querySelector(`.bar[data-id="${CSS.escape(other.flightId)}"]`);
  const move = gateMove(f, byId);
  const moveTime = move ? (move.minutes >= 0 ? formatDuration(move.minutes) : `overlap ${formatDuration(move.minutes)}`) : "";

  let path: string;
  if (otherBar) {
    const there = rectOf(otherBar, timeline);
    const [a, d] = arriving ? [here, there] : [there, here];
    // Straight from the arrival's end to the departure's start, arrowhead at the departure; time between gates halfway.
    path = `M${a.right},${a.mid} L${d.left},${d.mid}`;
    if (moveTime) {
      time.textContent = moveTime;
      time.hidden = false;
      time.style.left = `${(a.right + d.left) / 2}px`;
      time.style.top = `${(a.mid + d.mid) / 2}px`;
    }
  } else {
    // The label goes on the side the aircraft goes to (arrival) or comes from (departure), unless that side would
    // be under the sticky gate column; then it goes on the right with a back arrow.
    const gate = other.gate ? other.gate.gate : "Waitlist";
    const reason = [moveTime, hiddenReason(other)].filter(Boolean).join(" · ");
    label.hidden = false;
    label.style.top = `${here.mid}px`;
    label.textContent = `${gate} · ${reason} →`;
    const leftOfBar = here.left - 3 * GAP - label.offsetWidth;
    if (!arriving && leftOfBar >= LABEL_WIDTH) {
      label.style.left = `${leftOfBar}px`;
      path = `M${here.left - 3 * GAP},${here.mid} H${here.left}`;
    } else {
      label.textContent = arriving ? `→ ${gate} · ${reason}` : `← from ${gate} · ${reason}`;
      label.style.left = `${here.right + 3 * GAP}px`;
      // The arrow points the way the aircraft moves: out to the label, or in from it.
      path = arriving ? `M${here.right},${here.mid} H${here.right + 3 * GAP}` : `M${here.right + 3 * GAP},${here.mid} H${here.right}`;
    }
  }
  svg.innerHTML =
    `<defs><marker id="link-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">` +
    `<path d="M0,0 L10,5 L0,10 z" /></marker></defs><path class="link" d="${path}" marker-end="url(#link-arrow)" />`;
}
