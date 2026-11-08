// The timeline's time window grows from its edges. Scrolling sideways into an edge stops there and shows a hint;
// a fresh scroll gesture at that edge extends the window by an hour on that side (up to a limit).

export type Side = "before" | "after";

export const DEFAULT_HOURS_BEFORE = 1;
export const DEFAULT_HOURS_AFTER = 12;
export const MAX_HOURS = 24;

/** Wheel events closer together than this belong to one gesture (including trackpad momentum). */
const GESTURE_GAP_MS = 200;
const HINT_MS = 1800;

interface Options {
  /** Whether that side can still grow. */
  canExtend: (side: Side) => boolean;
  /** Grow the window on that side; `delta` is the scroll that asked for it, to carry on into the new hour. */
  extend: (side: Side, delta: number) => void;
}

export function attachEdgeScroll(timeline: HTMLElement, hint: HTMLElement, { canExtend, extend }: Options) {
  let lastWheel = 0;
  let heldAt: Side | null = null;
  let hideTimer = 0;

  const showHint = (side: Side, text: string) => {
    hint.textContent = text;
    hint.dataset.side = side;
    hint.hidden = false;
    clearTimeout(hideTimer);
    hideTimer = window.setTimeout(() => (hint.hidden = true), HINT_MS);
  };

  timeline.addEventListener(
    "wheel",
    (e) => {
      const now = performance.now();
      const newGesture = now - lastWheel > GESTURE_GAP_MS;
      lastWheel = now;
      // Trackpads scroll sideways directly; a mouse wheel does with Shift.
      const dx = e.deltaX || (e.shiftKey ? e.deltaY : 0);
      if (!dx) return;

      const atStart = timeline.scrollLeft <= 0;
      const atEnd = timeline.scrollLeft + timeline.clientWidth >= timeline.scrollWidth - 1;
      const side: Side | null = dx < 0 && atStart ? "before" : dx > 0 && atEnd ? "after" : null;
      if (!side) {
        heldAt = null;
        return;
      }

      const word = side === "before" ? "earlier" : "later";
      if (!canExtend(side)) {
        showHint(side, `Showing the full ${MAX_HOURS} h ${side === "before" ? "back" : "ahead"}`);
      } else if (heldAt === side && newGesture) {
        extend(side, dx);
        showHint(side, `+1 h ${word}`);
      } else {
        // The gesture that reached the edge stops here; the next one extends.
        heldAt = side;
        showHint(side, `Scroll again for 1 h ${word}`);
      }
    },
    { passive: true },
  );
}
