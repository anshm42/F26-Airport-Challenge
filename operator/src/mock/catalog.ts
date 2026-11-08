// Static references only the mock needs, read the way the stream reads them (smartgate_stream/inventory.py): airline
// terminals and station time zones. The gate inventory and size classes are shared with the view, in ../inventory.ts.

import pearsonJson from "../../../data/pearson.json";
import profilesJson from "../../../data/stream_profiles.json";

interface Pearson {
  airlineTerminals: { records: { carrier: string; name: string; terminal: string }[] };
}
interface Profiles {
  airports: Record<string, { timeZone: string }>;
}
const pearson = pearsonJson as unknown as Pearson;
const profiles = profilesJson as unknown as Profiles;

/** Time zone of a station; the stream rejects stations it has no zone for. */
export function zoneOf(station: string): string {
  const zone = profiles.airports[station]?.timeZone;
  if (!zone) throw new Error(`No time zone configured for station ${station}`);
  return zone;
}

const AIRLINES = new Map(pearson.airlineTerminals.records.map((r) => [r.carrier, r]));
export const airlineName = (carrier: string) => AIRLINES.get(carrier)?.name ?? carrier;
/** "1" or "3"; null for carriers outside Pearson's directory. */
export const terminalOf = (carrier: string) => AIRLINES.get(carrier)?.terminal.replace(/^T/, "") ?? null;
