import type { Lang } from "./strings";
import type { Flight, Gate } from "./types";

// Gate selectors: "1" (terminal), "1:D" (branch), "1:D24" (gate), "1:D20-D30" or "1:D20-30" (range). See ../SPEC.md.
// The server doesn't filter, so this is the board's gate filter (and the mock's). Selectors match a gate's label, and a named
// terminal can be abbreviated to its first letter ("R" for "Remote", "R:H01A"), as in the operator view.

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

/** A gate's label for display: remote stands are keyed by their full IFATC name ("Remote Stand H01A"), shown as "H01A". */
export const gateLabel = (gate: string) => gate.replace(/^Remote Stand /, "");

export const branchOf = (gate: string) => gate.match(/^[A-Za-z]*/)![0].toUpperCase();
const numberOf = (gate: string) => Number(gate.match(/\d+/)?.[0] ?? NaN);
const isRemote = (terminal: string) => /^R(EMOTE)?$/i.test(terminal);

function matchesTerminal(selected: string, terminal: string): boolean {
  const s = selected.toUpperCase();
  const t = terminal.toUpperCase();
  return s === t || (/^[A-Z]$/.test(s) && !/^\d+$/.test(t) && t.startsWith(s));
}

function matchesGate(sel: Selector, g: Gate): boolean {
  if (!matchesTerminal(sel.terminal, g.terminal)) return false;
  const label = gateLabel(g.gate);
  if ("gate" in sel) return sel.gate === label.toUpperCase();
  if ("from" in sel) return sel.branch === branchOf(label) && numberOf(label) >= sel.from && numberOf(label) <= sel.to;
  if ("branch" in sel) return sel.branch === branchOf(label);
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
      if (isRemote(s.terminal)) {
        if ("gate" in s) return `${fr ? "Poste" : "Stand"} ${s.gate}`;
        // Stand numbers are two digits (H01), so a range reads the same way.
        if ("from" in s) return `${fr ? "Postes" : "Stands"} ${s.branch}${String(s.from).padStart(2, "0")}–${s.branch}${String(s.to).padStart(2, "0")}`;
        if ("branch" in s) return fr ? `Postes ${s.branch}` : `${s.branch} stands`;
        return fr ? "Postes éloignés" : "Remote stands";
      }
      if ("gate" in s) return `${fr ? "Porte" : "Gate"} ${s.gate}`;
      if ("from" in s) return `${fr ? "Portes" : "Gates"} ${s.branch}${s.from}–${s.branch}${s.to}`;
      if ("branch" in s) return fr ? `Portes ${s.branch}` : `${s.branch} gates`;
      return `Terminal ${s.terminal}`;
    })
    .join(" · ");
}
