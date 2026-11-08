// Static references, imported from ../data the way the stream reads them (smartgate_stream/inventory.py) rather than
// sent over the wire: the gate-state keys with their size classes and conflict groups, and aircraft size classes.

import aircraftJson from "../../data/aircraft.json";
import pearsonJson from "../../data/pearson.json";
import type { Gate, WidthClass } from "./types";

interface Pearson {
  passengerGateGroups: { terminals: Record<string, { groups: string[][] }> };
  passengerStandMapping: { records: { gate: string; ifatcStandNames: string[]; widthClass: WidthClass | null; conflictGroup: string }[] };
  ifatcStands: { records: { name: string }[] };
}
interface Aircraft {
  aircraftTypes: { records: { aircraftType: string; widthClass: WidthClass | null }[] };
  gateWidthClasses: { classes: Record<string, string> };
}
const pearson = pearsonJson as unknown as Pearson;
const aircraft = aircraftJson as unknown as Aircraft;

/** A parking resource: a terminal gate or a remote stand, one per key of the stream's gateState. */
export interface Resource extends Gate {
  /** Other passenger labels for the same resource, e.g. a swing gate's "F32" for "D32". */
  aliases: string[];
  kind: "gate" | "remote";
  /** Largest aircraft class the resource takes; null if unknown. */
  widthClass: WidthClass | null;
  /** Resources sharing a conflict group can't be occupied at the same time (e.g. H01, H01A, H01B). */
  conflictGroup: string;
  /** IFATC stand names this resource covers; a GateOutage on any of them closes it. */
  ifatcStandNames: string[];
}

export const RANK: Record<WidthClass, number> = { A: 0, B: 1, C: 2, D: 3, E: 4, F: 5 };

const WIDTH = new Map(aircraft.aircraftTypes.records.map((r) => [r.aircraftType, r.widthClass]));
/** An aircraft type's size class; null if unknown or unclassified. */
export const widthOf = (type: string | null | undefined): WidthClass | null => (type ? (WIDTH.get(type) ?? null) : null);

/** IFATC's description of each size class, e.g. "A340, 747-400, 777, 787". */
export const WIDTH_CLASSES: Record<string, string> = aircraft.gateWidthClasses.classes;

/** Every gate-state key, terminal groups first, then H remote positions. */
export const RESOURCES: Resource[] = (() => {
  const mapping = new Map(pearson.passengerStandMapping.records.map((r) => [r.gate, r]));
  const out: Resource[] = [];
  const add = (terminal: string, gate: string, aliases: string[], kind: Resource["kind"]) => {
    const m = mapping.get(gate)!;
    out.push({ terminal, gate, aliases, kind, widthClass: m.widthClass, conflictGroup: m.conflictGroup, ifatcStandNames: m.ifatcStandNames });
  };
  for (const [key, { groups }] of Object.entries(pearson.passengerGateGroups.terminals)) {
    // Groups without an unambiguous IFATC match are outside the stream's inventory.
    for (const [gate, ...aliases] of groups) if (mapping.has(gate)) add(key.replace(/^T/, ""), gate, aliases, "gate");
  }
  for (const { name } of pearson.ifatcStands.records) if (name.startsWith("Remote Stand H")) add("Remote", name, [], "remote");
  return out;
})();

const BY_GATE = new Map(RESOURCES.map((r) => [r.gate, r]));
export const resourceOf = (gate: string) => BY_GATE.get(gate);

/** Resources sharing r's conflict group, r included. */
export const conflictGroupOf = (r: Resource) => RESOURCES.filter((x) => x.conflictGroup === r.conflictGroup);

const BY_STAND = new Map(RESOURCES.flatMap((r) => r.ifatcStandNames.map((n) => [n, r] as const)));
/** The resources an outage on an IFATC stand ("Gate 132") closes: its gate, and the rest of its conflict group. */
export const closedBy = (stand: string): Resource[] => {
  const r = BY_STAND.get(stand);
  return r ? conflictGroupOf(r) : [];
};
