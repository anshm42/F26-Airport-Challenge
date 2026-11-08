"""Greedy gate assignment with a waitlist.

A port of the challenge's scoring-aware reference (`solution_scored.py`) to the
stream's webhook contract. Each update first repairs plans that it made invalid,
then places waitlisted ground visits on the cheapest gate that is free for the
whole remaining stay: priority diversions first, then the most constrained
aircraft. A visit that fits nowhere stays on the waitlist and is retried after
every later update. Nothing reserves a gate that only frees up partway through
a stay, which is what makes this baseline naive.
"""

import json
from collections import defaultdict
from datetime import timedelta
from functools import cache

from smartgate_stream.gates import LEG_FIELDS, GateState, utc_string
from smartgate_stream.inventory import DATA, catalog, gate_fits
from smartgate_stream.validation import flight_key, parse_utc


WIDTHS = "ABCDEF"
BOARD_RETENTION = timedelta(hours=1)


@cache
def airline_names():
    airport = json.loads((DATA / "pearson.json").read_text())
    profiles = json.loads((DATA / "stream_profiles.json").read_text())
    names = {record["carrier"]: record["name"] for record in airport["airlineTerminals"]["records"]}
    names.update({carrier["carrier"]: carrier["name"] for carrier in profiles["carriers"]})
    return names


@cache
def compatible_gates(aircraft_type):
    return sum(gate_fits(gate, {"aircraftType": aircraft_type}) for gate in catalog()["gates"])


def gate_cost(visit, gate):
    """Lower is better; solution_scored's cost terms mapped onto Pearson data."""
    terminal = catalog()["gates"][gate]
    cost = 0.0
    if terminal is None:
        cost += 0 if visit["remote_ok"] else 1000  # keep ordinary flights off remote stands
    elif visit["remote_ok"]:
        cost += 100  # don't spend a terminal gate on an emergency that can use a stand
    preferred = catalog()["airlines"].get(visit["aircraft"]["carrier"])
    if terminal is not None and preferred is not None and terminal != preferred:
        cost += 50  # walking distance: stay in the carrier's own terminal
    size = catalog()["aircraft_widths"][visit["aircraft"]["aircraftType"]]
    cost += 10 * (WIDTHS.index(catalog()["gate_widths"][gate]) - WIDTHS.index(size))  # tight fit
    return cost


def overlaps(start, end, other_start, other_end):
    return (other_end is None or start < other_end) and (end is None or other_start < end)


def flight_id(key):
    return "|".join(part or "" for part in key)


class NaiveMatcher:
    def __init__(self):
        self.state = None
        self.epoch = None
        self.parked = set()
        self.plan = {}              # visit id -> gate, or None while waitlisted
        self.waitlisted_since = {}  # visit id -> simulated time it joined the waitlist
        self.assigned_at = {}       # visit id -> simulated time its current gate was chosen
        self.sent = {}              # flight key -> gate last returned to the stream
        self.leg_visits = {}        # flight key -> visit id, kept after the visit ends for the board
        self.leg_gates = {}         # flight key -> gate, kept after the visit ends for the board
        self.policies = {}          # diverted flight key -> (emergencyState, reason)
        self.cancelled_at = {}
        self.board_fields = {}
        self.board_previous = {}

    @property
    def waitlist(self):
        return [vid for vid, gate in self.plan.items() if gate is None]

    def handle(self, message, simulated_time):
        """Apply one webhook body and return the stream decision object."""
        now = parse_utc(simulated_time)
        message = {key: value for key, value in message.items() if key != "unassignedAircraft"}
        gate_state = message["gateState"]
        if message["action"] == "InitializeAirport":
            self.__init__()
            self.state, self.epoch = GateState(), now
            self.state.apply_message(message, now)
            self.parked = {plane["registration"] for plane in message["parkedAircraft"]}
        else:
            if self.state is None:
                raise ValueError("InitializeAirport must be the first message")
            del message["gateState"]
            self.state.apply_message(message, now)
            if message["action"] == "DivertFlight":
                reason = str(message.get("reason") or "").strip().lower()
                for leg in message["legs"]:
                    self.policies[flight_key(message["flight"]["carrier"], leg)] = (message.get("emergencyState"), reason)
        # The stream owns physical occupancy; trust its copy over our own replay of movements.
        self.state.gates = gate_state
        for key, record in self.state.flights.items():
            if record["cancelled"]:
                self.cancelled_at.setdefault(key, now)
            else:
                self.cancelled_at.pop(key, None)
        return self.decide(now)

    def _visits(self, now):
        """Unfinished ground stays at YYZ: arrival (or initial parking) to the next departure."""
        movements = defaultdict(list)
        for key, record in self.state.flights.items():
            if record["cancelled"]:
                continue
            tail = record["aircraft"]["registration"]
            if record["leg"]["departureStation"] == "YYZ":
                movements[tail].append((record["departure"], 0, key))
            if record["leg"]["arrivalStation"] == "YYZ":
                movements[tail].append((record["arrival"], 1, key))
        visits = {}
        for tail in movements.keys() | self.parked:
            opened = (self.epoch, ("PARKED", tail), []) if tail in self.parked else None
            for when, kind, key in sorted(movements.get(tail, []), key=lambda move: (move[0], move[1], repr(move[2]))):
                if kind == 1 and opened is None:
                    opened = (when, key, [key])
                elif kind == 1:
                    # Back-to-back arrivals happen while a cancelled departure awaits reinstatement.
                    # The stream pairs both with the next departure, so they share this visit's gate.
                    opened[2].append(key)
                elif opened is not None:
                    self._add_visit(visits, tail, opened, when, key, now)
                    opened = None
            if opened is not None:
                self._add_visit(visits, tail, opened, self.state.day_end, None, now)
        return visits

    def _add_visit(self, visits, tail, opened, end, outbound, now):
        start, vid, inbound = opened
        if end <= now:
            return
        if inbound and inbound[0] in self.policies:
            emergency, reason = self.policies[inbound[0]]
            remote_ok = emergency == "Emergency" or (emergency is None and reason in ("medical", "diversion"))
            priority = not remote_ok
        else:
            remote_ok = priority = False
        visits[vid] = {
            "tail": tail, "aircraft": self.state.aircraft[tail], "start": start, "end": end,
            "inbound": inbound, "outbound": outbound, "priority": priority, "remote_ok": remote_ok,
            "current": tail in self.state.grounded and start <= now,
        }

    def _out_of_service(self, gate, start, end):
        footprint = catalog()["footprints"][gate]
        return any(catalog()["footprints"][other] == footprint and overlaps(start, end, begins, ends)
                   for other, begins, ends in self.state.outages)

    def _blocked(self, gate, start, end, bookings, vid):
        return (self._out_of_service(gate, start, end)
                or any(other != vid and overlaps(start, end, begins, ends)
                       for begins, ends, other in bookings[catalog()["footprints"][gate]]))

    def _best_gate(self, vid, visit, now, bookings, skip=None):
        start = max(visit["start"], now)
        options = [gate for gate in catalog()["gates"]
                   if gate != skip and gate_fits(gate, visit["aircraft"])
                   and not self._blocked(gate, start, visit["end"], bookings, vid)]
        return min(options, key=lambda gate: gate_cost(visit, gate), default=None)

    def _assign(self, vid, visit, gate, now, bookings):
        self.plan[vid] = gate
        self.assigned_at[vid] = now
        self.waitlisted_since.pop(vid, None)
        bookings[catalog()["footprints"][gate]].append((max(visit["start"], now), visit["end"], vid))

    def decide(self, now):
        visits = self._visits(now)
        position = {plane["registration"]: gate for gate, plane in self.state.gates.items() if plane is not None}
        for vid in self.plan.keys() - visits.keys():
            del self.plan[vid]
            self.waitlisted_since.pop(vid, None)
        bookings = defaultdict(list)  # footprint -> [(start, end, visit id)]
        repairs = []

        # Aircraft on the ground hold their physical position for the rest of their stay.
        for vid, visit in visits.items():
            if not visit["current"]:
                continue
            gate = position.get(visit["tail"])
            if gate is None:
                self.plan[vid] = None
                self.assigned_at.pop(vid, None)
                self.waitlisted_since.setdefault(vid, visit["start"])
                continue
            if self.plan.get(vid) != gate:
                self.plan[vid] = gate
                self.assigned_at.setdefault(vid, now)
                self.waitlisted_since.pop(vid, None)
            bookings[catalog()["footprints"][gate]].append((now, visit["end"], vid))
            if self._out_of_service(gate, now, visit["end"]):
                repairs.append(vid)
        grounded = {visit["tail"] for visit in visits.values() if visit["current"]}
        for tail, gate in position.items():
            if tail not in grounded:
                bookings[catalog()["footprints"][gate]].append((now, None, None))

        # 1. Repair future plans an update made invalid (timing, equipment, or outage).
        future = sorted((vid for vid, visit in visits.items() if not visit["current"] and self.plan.get(vid)),
                        key=lambda vid: (visits[vid]["start"], repr(vid)))
        for vid in future:
            visit, gate = visits[vid], self.plan[vid]
            if gate_fits(gate, visit["aircraft"]) and not self._blocked(gate, visit["start"], visit["end"], bookings, vid):
                bookings[catalog()["footprints"][gate]].append((visit["start"], visit["end"], vid))
            else:
                repairs.append(vid)
        for vid in repairs:
            visit, old = visits[vid], self.plan[vid]
            gate = self._best_gate(vid, visit, now, bookings, skip=old)
            if gate is not None:
                if visit["current"]:
                    footprint = catalog()["footprints"][old]
                    bookings[footprint] = [booking for booking in bookings[footprint] if booking[2] != vid]
                self._assign(vid, visit, gate, now, bookings)
            elif not visit["current"]:
                self.plan[vid] = None
                self.assigned_at.pop(vid, None)
                self.waitlisted_since[vid] = now

        # 2. Place waitlisted visits: emergencies first, then most-constrained, then longest waiting.
        waiting = [vid for vid in visits if self.plan.get(vid) is None]
        for vid in waiting:
            self.plan[vid] = None
            self.waitlisted_since.setdefault(vid, now)
        waiting.sort(key=lambda vid: (not visits[vid]["priority"], compatible_gates(visits[vid]["aircraft"]["aircraftType"]),
                                      self.waitlisted_since[vid], visits[vid]["start"], repr(vid)))
        for vid in waiting:
            gate = self._best_gate(vid, visits[vid], now, bookings)
            if gate is not None:
                self._assign(vid, visits[vid], gate, now, bookings)
        return self._decisions(visits, position, now)

    def _decisions(self, visits, position, now):
        desired, moves = {}, []
        # Inbound last: a YYZ-YYZ turnback leg belongs to the visit it starts.
        for vid, visit in visits.items():
            if visit["outbound"] is not None:
                desired[visit["outbound"]] = self.plan[vid]
                self.leg_visits[visit["outbound"]] = vid
        for vid, visit in visits.items():
            for key in visit["inbound"]:
                desired[key] = self.plan[vid]
                self.leg_visits[key] = vid
        for vid, visit in visits.items():
            gate = self.plan[vid]
            if not visit["current"] or gate is None:
                continue
            if position.get(visit["tail"]) != gate:
                moves.append({"registration": visit["tail"], "gate": gate})
            elif any(catalog()["footprints"][other] == catalog()["footprints"][gate] and begins <= now
                     and (ends is None or now < ends) for other, begins, ends in self.state.outages):
                # Re-sending a closed position would be rejected; the aircraft simply stays put.
                for key in (*visit["inbound"], visit["outbound"]):
                    desired.pop(key, None)
        assignments = []
        for key, gate in desired.items():
            if self.sent.get(key) == gate:
                continue
            leg = self.state.flights[key]["leg"]
            assignments.append({"carrier": key[0], "leg": {field: leg.get(field) for field in LEG_FIELDS}, "gate": gate})
            self.sent[key] = gate
        self.leg_gates.update(desired)
        return {"assignments": assignments, "moves": moves}

    def board(self):
        """Passenger WebSocket payload, in the matcher's documented full-list shape."""
        zone = catalog()["zones"]["YYZ"]
        now = self.state.now
        flights = []
        for key, record in self.state.flights.items():
            leg = record["leg"]
            arrival = record["arrival"] if leg["arrivalStation"] == "YYZ" else None
            departure = record["departure"] if leg["departureStation"] == "YYZ" else None
            if arrival is None and departure is None:
                continue
            cancelled = record["cancelled"]
            if cancelled:
                if now - self.cancelled_at[key] > BOARD_RETENTION:
                    continue
            elif now - max(when for when in (arrival, departure) if when is not None) > BOARD_RETENTION:
                continue
            gate = None if cancelled else self.leg_gates.get(key)
            terminal = catalog()["gates"][gate] if gate else None
            codes = [f"{key[0]}{leg['flightNumber']}{leg.get('flightSuffix') or ''}"]
            codes += [f"{share['carrier']}{share['flightNumber']}" for share in leg.get("codeshare", [])
                      if f"{share['carrier']}{share['flightNumber']}" not in codes]
            fields = {
                "flightCodes": codes,
                "airline": airline_names().get(key[0], key[0]),
                "from": leg["departureStation"],
                "to": leg["arrivalStation"],
                "timeArrival": arrival.astimezone(zone).isoformat() if arrival else None,
                "timeDeparture": departure.astimezone(zone).isoformat() if departure else None,
                "gate": {"terminal": terminal.removeprefix("T") if terminal else "Remote", "gate": gate} if gate else None,
                "cancelled": cancelled,
                "misc": {"aircraftType": record["aircraft"]["aircraftType"],
                         "registration": record["aircraft"]["registration"],
                         "trafficType": leg.get("trafficType")},
            }
            fid = flight_id(key)
            previous = self.board_previous.setdefault(fid, {})
            old = self.board_fields.get(fid)
            if old is not None:
                previous.update({name: old[name] for name, value in fields.items() if old[name] != value})
            self.board_fields[fid] = fields
            vid = self.leg_visits.get(key)
            since = self.waitlisted_since.get(vid) if gate is None and not cancelled else None
            assigned = self.assigned_at.get(vid) if gate else None
            flights.append((departure or arrival, {
                "flightId": fid, **fields, "previous": dict(previous),
                "assignedAt": utc_string(assigned) if assigned else None,
                "waitlistedSince": utc_string(since) if since else None,
            }))
        flights.sort(key=lambda flight: flight[0])
        return {"type": "assignments", "generatedAt": now.astimezone(zone).isoformat(),
                "flights": [flight for _, flight in flights]}
