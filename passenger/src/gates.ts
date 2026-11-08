import type { Lang } from "./strings";
import type { Flight, Gate } from "./types";

// Gate selectors: "1" (terminal), "1:D" (branch), "1:D24" (gate), "1:D20-D30" or "1:D20-30" (range). See ../SPEC.md.
// Filtering is the server's job; this is used by the mock source and for the board title.

type Selector =
  | { terminal: string }
  | { terminal: string; branch: string }
  | { terminal: string; gate: string }
  | { terminal: string; branch: string; from: number; to: number };

const RANGE = /^([A-Z]+)(\d+)-(?:([A-Z]+))?(\d+)$/;

export function parseSelectors(gates: string | null): Selector[] | null {
  if (!gates?.trim()) return null;
  return gates
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .flatMap((s): Selector[] => {
      const [terminal, raw] = s.split(":");
      const rest = raw?.toUpperCase();
      if (!rest) return [{ terminal }];
      const range = rest.match(RANGE);
      if (range) {
        const [, branch, a, endBranch, b] = range;
        if (endBranch && endBranch !== branch) {
          console.error(`Ignoring gate range across branches: ${s}`);
          return [];
        }
        return [{ terminal, branch, from: Math.min(+a, +b), to: Math.max(+a, +b) }];
      }
      return [/^[A-Z]+$/.test(rest) ? { terminal, branch: rest } : { terminal, gate: rest }];
    });
}

export const branchOf = (gate: string) => gate.match(/^[A-Za-z]*/)![0].toUpperCase();
const numberOf = (gate: string) => Number(gate.match(/\d+/)?.[0] ?? NaN);

function matchesGate(sel: Selector, g: Gate): boolean {
  if (sel.terminal !== g.terminal) return false;
  if ("gate" in sel) return sel.gate === g.gate.toUpperCase();
  if ("from" in sel) return sel.branch === branchOf(g.gate) && numberOf(g.gate) >= sel.from && numberOf(g.gate) <= sel.to;
  if ("branch" in sel) return sel.branch === branchOf(g.gate);
  return true;
}

const matchesAny = (selectors: Selector[], g: Gate | null | undefined) => !!g && selectors.some((sel) => matchesGate(sel, g));

/** Matches on the current gate or the previous one, so the old gate's board shows the move. */
export function matchesFlight(selectors: Selector[] | null, f: Flight): boolean {
  return !selectors || matchesAny(selectors, f.gate) || matchesAny(selectors, f.previous.gate);
}

/** False when the flight is only on this board because its previous gate matched, i.e. it has moved away. */
export function matchesCurrentGate(selectors: Selector[] | null, f: Flight): boolean {
  return !selectors || matchesAny(selectors, f.gate);
}

export function describeSelectors(gates: string | null, lang: Lang = "en"): string {
  const selectors = parseSelectors(gates);
  const fr = lang === "fr";
  if (!selectors) return fr ? "toutes les portes" : "All gates";
  return selectors
    .map((s) => {
      if ("gate" in s) return `${fr ? "Porte" : "Gate"} ${s.gate}`;
      if ("from" in s) return `${fr ? "Portes" : "Gates"} ${s.branch}${s.from}–${s.branch}${s.to}`;
      if ("branch" in s) return fr ? `Portes ${s.branch}` : `${s.branch} gates`;
      return `Terminal ${s.terminal}`;
    })
    .join(" · ");
}
