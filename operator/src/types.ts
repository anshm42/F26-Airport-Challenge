// Wire format for the operator view API. See ../SPEC.md.
// A superset of the passenger view's message: every passenger field keeps its meaning.
// Only dynamic data: static references (gates, size classes) are imported from ../data, see inventory.ts.

export const LOCAL_AIRPORT = "YYZ";
export const LOCAL_TIMEZONE = "America/Toronto";

export interface Gate {
  terminal: string;
  gate: string;
}

export type WidthClass = "A" | "B" | "C" | "D" | "E" | "F";
export type EmergencyState = "Turnback" | "Emergency";

/** A GateOutage from the stream. */
export interface Outage {
  /** The IFATC stand name it was announced on ("Gate 132"); inventory.closedBy gives the resources it closes. */
  stand: string;
  start: string;
  /** null: until further notice. */
  end: string | null;
  misc: Record<string, unknown>;
}

export interface Interval {
  start: string;
  end: string;
}

export interface Flight {
  flightId: string;
  /** Primary (operating) code first, then codeshare alternates. */
  flightCodes: string[];
  airline: string;
  from: string;
  to: string;
  timeArrival: string | null;
  timeDeparture: string | null;
  /** null: on the waitlist (unless cancelled). */
  gate: Gate | null;
  cancelled: boolean;
  previous: Partial<Omit<Flight, "flightId" | "previous">>;
  misc: Record<string, unknown>;

  // Operator-only fields.
  registration: string | null;
  aircraftType: string | null;
  emergencyState: EmergencyState | null;
  /** The window this flight holds its gate; null when it has no gate. */
  occupancy: Interval | null;
  /** The other half of this aircraft's turn at YYZ (inbound <-> outbound), if known. */
  linkedFlightId: string | null;
  /** When the current gate was assigned; null when it has no gate. */
  assignedAt: string | null;
  /** "Domestic", "Transborder" or "International", from the stream. */
  trafficType: string | null;
}

/** An aircraft holding a position without a flight: idle aircraft and spares on the ground since the start of the day. */
export interface ParkedAircraft {
  carrier: string;
  registration: string;
  aircraftType: string | null;
  /** null: on the ground without a position. */
  gate: Gate | null;
  occupancy: Interval | null;
  assignedAt: string | null;
}

export interface AssignmentsMessage {
  type: "assignments";
  generatedAt: string;
  outages: Outage[];
  flights: Flight[];
  parkedAircraft: ParkedAircraft[];
}

/** Sent on a tick with no changes: only the clock moves. */
export interface HeartbeatMessage {
  type: "heartbeat";
  generatedAt: string;
}

export type ServerMessage = AssignmentsMessage | HeartbeatMessage;

/** The WebSocket's state: "closed" means it isn't connected and is retrying. */
export type Connection = "connecting" | "open" | "closed";

/** A source of snapshots: the real WebSocket, or the mock. Returns a function that stops it. */
export type Source = (onMessage: (msg: ServerMessage) => void, onConnection?: (state: Connection) => void) => () => void;

/** The matching algorithm's view feed, on its default port on the host this page came from. */
export const defaultSource = (path: string) => `ws://${location.hostname}:9000${path}`;
