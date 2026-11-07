// Wire format for the passenger view API. See ../SPEC.md.

export const LOCAL_AIRPORT = "YYZ";
export const LOCAL_TIMEZONE = "America/Toronto";

export interface Gate {
  terminal: string;
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

export type ServerMessage = AssignmentsMessage;

/** A source of assignment lists: the real WebSocket, or the mock. Returns a function that stops it. */
export type Source = (gates: string | null, onMessage: (msg: ServerMessage) => void) => () => void;
