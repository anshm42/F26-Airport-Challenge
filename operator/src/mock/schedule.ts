// The stream's flight bookkeeping (GateState in smartgate_stream/gates.py), without occupancy: applies scenario messages
// to flight legs. Everything the view shows about a flight except its gate comes from here, passed through unchanged.

import type { EmergencyState } from "../types";
import { closedBy } from "../inventory";
import { zoneOf } from "./catalog";
import { addDays, localDate, zonedTime } from "./util";
import { LOCAL_TIMEZONE } from "../types";

export interface Aircraft {
  carrier: string;
  registration: string;
  aircraftType: string;
}

export interface Leg {
  flightNumber: string;
  flightSuffix?: string | null;
  originatingDate: string;
  departureStation: string;
  arrivalStation: string;
  trafficType?: string;
  scheduledDepartureTime?: string;
  scheduledArrivalTime?: string;
  codeshare?: { carrier: string; flightNumber: string }[];
}

/** A flight/resource message from JsonFlightMessageSpecification.md. */
export interface Message {
  action: string;
  flight?: Aircraft;
  legs?: Leg[];
  emergencyState?: EmergencyState;
  cancelScope?: { type: string };
  equipmentUpdate?: { scope: string };
  gate?: string;
  scheduledStartTime?: string;
  scheduledEndTime?: string;
  flights?: Message[];
  parkedAircraft?: (Aircraft & { gate: string })[];
  gateOutages?: Message[];
  [field: string]: unknown;
}

export interface ScenarioEvent {
  timestamp: string;
  message: Message;
}

export interface FlightRec {
  /** The stream's flight_key: carrier, number, suffix, date and stations. */
  key: string;
  route: string;
  carrier: string;
  leg: Leg;
  aircraft: Aircraft;
  dep: number;
  arr: number;
  cancelled: boolean;
  emergencyState: EmergencyState | null;
  /** Message fields the operator API has no slot for (e.g. a diversion's reason); passed through in misc. */
  extra: Record<string, unknown>;
}

export interface OutageRec {
  /** IFATC stand name, as sent. */
  stand: string;
  start: number;
  end: number | null;
}

/** Top-level fields with a place in the operator API; anything else is passed through in misc. */
const MAPPED = new Set(["action", "flight", "legs", "emergencyState", "cancelScope", "equipmentUpdate", "gateState", "unassignedAircraft"]);

const legKey = (carrier: string, l: Leg) => `${carrier}${l.flightNumber}${l.flightSuffix ?? ""}/${l.originatingDate}/${l.departureStation}-${l.arrivalStation}`;
const routeKey = (carrier: string, l: Leg) => `${carrier}${l.flightNumber}${l.flightSuffix ?? ""}/${l.originatingDate}`;

/**
 * Departure and arrival instants: each time is local to its station, and an arrival before the departure is the next day.
 * POTENTIALLY INCORRECT for western Canada: this uses the browser's tzdata, which may predate 2026f's permanent
 * daylight time in Vancouver, Edmonton and Winnipeg; the stream uses the system's. YYZ-side times are unaffected.
 */
export function legTimes(leg: Leg): [number, number] {
  const dep = zonedTime(leg.originatingDate, leg.scheduledDepartureTime!, zoneOf(leg.departureStation));
  const zone = zoneOf(leg.arrivalStation);
  const day = localDate(dep, zone);
  let arr = zonedTime(day, leg.scheduledArrivalTime!, zone);
  if (arr < dep) arr = zonedTime(addDays(day, 1), leg.scheduledArrivalTime!, zone);
  return [dep, arr];
}

export class Schedule {
  flights = new Map<string, FlightRec>();
  /** Aircraft on the ground at initialization, with the position the stream gave them. */
  parked: (Aircraft & { gate: string })[] = [];
  outages: OutageRec[] = [];

  apply(m: Message, at: number) {
    switch (m.action) {
      case "InitializeAirport":
        for (const f of m.flights ?? []) this.addRoute(f, false);
        this.parked = (m.parkedAircraft ?? []).map((p) => ({ ...p }));
        for (const o of m.gateOutages ?? []) this.outage(o, at);
        return;
      case "CreateFlight":
      case "DivertFlight":
        return this.addRoute(m, false);
      case "ReplaceFlight":
        return this.addRoute(m, true);
      case "GateOutage":
        return this.outage(m, at);
    }
    const carrier = m.flight!.carrier;
    const recs = m.legs!.map((l) => this.flights.get(legKey(carrier, l)));
    if (recs.some((r) => !r)) return console.warn(`${m.action} references an unknown flight`, m);
    let affected = recs as FlightRec[];
    if (m.action === "UpdateTiming") {
      m.legs!.forEach((leg, i) => {
        affected[i].leg = structuredClone(leg);
        [affected[i].dep, affected[i].arr] = legTimes(leg);
      });
    } else {
      const allLegs = m.action === "ReinstateFlight" || m.cancelScope?.type === "AllLegs" || m.equipmentUpdate?.scope === "AllLegs";
      if (allLegs) affected = [...this.flights.values()].filter((r) => r.route === affected[0].route);
      for (const r of affected) {
        if (m.action === "UpdateEquipment") r.aircraft = { ...m.flight! };
        else if (m.action === "CancelFlight" || m.action === "ReinstateFlight") r.cancelled = m.action === "CancelFlight";
      }
    }
    for (const r of affected) Object.assign(r.extra, extraFields(m));
    if (m.emergencyState) for (const r of affected) r.emergencyState = m.emergencyState;
  }

  private addRoute(m: Message, replace: boolean) {
    const carrier = m.flight!.carrier;
    const route = routeKey(carrier, m.legs![0]);
    const old = new Map([...this.flights].filter(([, r]) => r.route === route));
    if (replace) for (const k of old.keys()) this.flights.delete(k);
    for (const leg of m.legs!) {
      const key = legKey(carrier, leg);
      const before = old.get(key);
      const [dep, arr] = legTimes(leg);
      this.flights.set(key, {
        key,
        route,
        carrier,
        leg: structuredClone(leg),
        aircraft: { ...m.flight! },
        dep,
        arr,
        cancelled: false,
        // A message without the field keeps the flight's existing emergency state.
        emergencyState: m.emergencyState ?? before?.emergencyState ?? null,
        extra: { ...before?.extra, ...extraFields(m) },
      });
    }
  }

  /** Undated outage windows use the announcement's Toronto date; an end before the start is the next day. */
  private outage(m: Message, at: number) {
    if (!closedBy(m.gate!).length) return console.warn(`GateOutage for an unmapped stand: ${m.gate}`);
    const day = localDate(at);
    const start = zonedTime(day, m.scheduledStartTime!, LOCAL_TIMEZONE);
    let end = m.scheduledEndTime ? zonedTime(day, m.scheduledEndTime, LOCAL_TIMEZONE) : null;
    if (end !== null && end < start) end = zonedTime(addDays(day, 1), m.scheduledEndTime!, LOCAL_TIMEZONE);
    this.outages.push({ stand: m.gate!, start, end });
  }
}

const extraFields = (m: Message) => Object.fromEntries(Object.entries(m).filter(([k]) => !MAPPED.has(k)));
