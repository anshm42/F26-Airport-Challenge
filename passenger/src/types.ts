// Wire format for the passenger view API. See ../SPEC.md.

export const LOCAL_AIRPORT = "YYZ";
export const LOCAL_TIMEZONE = "America/Toronto";

export interface Gate {
  /** "1", "3", or "Remote" for remote stands. */
  terminal: string;
  /** The gate's key: "D24", or the full IFATC name for remote stands ("Remote Stand H01A"); see gateLabel. */
  gate: string;
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
  gate: Gate | null;
  cancelled: boolean;
  previous: Partial<Omit<Flight, "flightId" | "previous">>;
  misc: Record<string, unknown>;
}

export interface AssignmentsMessage {
  type: "assignments";
  generatedAt: string;
  flights: Flight[];
}

/** Sent on a tick with no changes: only the clock moves. */
export interface HeartbeatMessage {
  type: "heartbeat";
  generatedAt: string;
}

export type ServerMessage = AssignmentsMessage | HeartbeatMessage;

/** The WebSocket's state: "closed" means it isn't connected and is retrying. */
export type Connection = "connecting" | "open" | "closed";

/** A source of assignment lists: the real WebSocket, or the mock. The WebSocket ignores `gates` (the board filters). Returns a function that stops it. */
export type Source = (
  gates: string | null,
  onMessage: (msg: ServerMessage) => void,
  onConnection?: (state: Connection) => void,
) => () => void;

/** The matching algorithm's view feed, on its default port on the host this page came from. */
export const defaultSource = (path: string) => `ws://${location.hostname}:9000${path}`;
