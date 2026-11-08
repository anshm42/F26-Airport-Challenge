import type { Resource } from "./inventory";

// Gate selectors, as in the passenger API: "1" (terminal), "1:D" (branch), "1:D24" (gate), "1:D20-D30" or "1:D20-30" (range).
// The operator API has no server-side selectors, so here they're only a view filter. A selector matches a resource's label or any alias,
// and a named terminal can be abbreviated to its first letter ("R" for "Remote", "R:H01").

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
export const numberOf = (gate: string) => Number(gate.match(/\d+/)?.[0] ?? NaN);

function matchesTerminal(selected: string, terminal: string): boolean {
  const s = selected.toUpperCase();
  const t = terminal.toUpperCase();
  return s === t || (/^[A-Z]$/.test(s) && !/^\d+$/.test(t) && t.startsWith(s));
}

function matchesLabel(sel: Selector, terminal: string, gate: string): boolean {
  if (!matchesTerminal(sel.terminal, terminal)) return false;
  if ("gate" in sel) return sel.gate === gate.toUpperCase();
  if ("from" in sel) return sel.branch === branchOf(gate) && numberOf(gate) >= sel.from && numberOf(gate) <= sel.to;
  if ("branch" in sel) return sel.branch === branchOf(gate);
  return true;
}

export function matchesResource(selectors: Selector[] | null, r: Resource): boolean {
  if (!selectors) return true;
  const labels = [gateLabel(r.gate), ...r.aliases];
  return selectors.some((sel) => labels.some((label) => matchesLabel(sel, r.terminal, label)));
}
